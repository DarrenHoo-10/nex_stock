# -*- coding: utf-8 -*-
"""Market-wide setup pre-scan for screening strategies (``screening.setup_scan``).

DSA-native module (not derived from AlphaSift). The screening pipeline normally
filters today's snapshot and fetches daily K-lines only for a bounded shortlist.
Event-style rules need a cross-sectional look back instead: "which stocks had a
qualifying volume surge in the last N sessions, have not broken the surge-day low,
have not triggered yet, and where is the next session's buy line?".

``surge_pullback`` implements the rules validated in the 2018-2026 backtest:

- surge day S: change >= 7%, volume 2-5x the prior 5-day average, S-day turnover
  <= 10%, prior 20-session change <= 20%, at least one same-industry peer surging
  on S, 20-day average amount >= 30M yuan, listed >= 90 days, not one-price bar;
- a newer surge of the same stock supersedes the older setup;
- a close below the surge-day low cancels the setup;
- the entry is a limit buy at the previous close's MA10 within 10 sessions after
  S, placed only while the market switch is on (CSI 1000 20-session change > 5%);
  setups whose entry already triggered are not listed again.

Full-market bars come from Tushare, one request per trade date, cached on disk,
so a daily run needs only a couple of new requests.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.services.screening.models import SetupScanConfig

logger = logging.getLogger(__name__)

_CN_TZ = timezone(timedelta(hours=8))
_TUSHARE_PAGE_ROWS = 6000
_TUSHARE_MIN_INTERVAL_SEC = 0.2
_STOCK_BASIC_TTL_SEC = 24 * 3600
_TRADE_CAL_TTL_SEC = 6 * 3600
_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount"
_DAILY_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,circ_mv"
_STOCK_BASIC_FIELDS = "ts_code,name,industry,list_date"
REGIME_OFF_FLAG = "市场开关关闭：按规则暂不开新仓"


class SetupScanDataError(RuntimeError):
    """Raised when the market data needed by a setup scan is unavailable."""


class MarketDataProvider(Protocol):
    """Full-market daily data source used by the scan (Tushare in production, fakes in tests)."""

    def open_trade_dates(self, start: str, end: str) -> list[str]:
        """Open SSE trade dates in [start, end], ascending (YYYYMMDD)."""

    def daily(self, trade_date: str) -> pd.DataFrame | None:
        """Bars of all stocks for one date; ``None`` when not published yet."""

    def daily_basic(self, trade_date: str) -> pd.DataFrame | None:
        """``ts_code`` / ``turnover_rate`` for one date; ``None`` when unavailable."""

    def stock_basic(self) -> pd.DataFrame:
        """Listed stocks with ``ts_code``, ``name``, ``industry``, ``list_date``."""

    def index_daily(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        """Index ``trade_date`` / ``close`` rows in [start, end]."""


@dataclass
class SetupScanResult:
    candidates: pd.DataFrame
    as_of: str
    next_session: str
    regime_on: bool
    regime_change_20d_pct: float | None
    regime_as_of: str
    stats: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- data


class TushareMarketData:
    """Full-market daily data from Tushare, cached per trade date on disk.

    Past trade dates never change, so each date is requested once. Empty
    responses (e.g. today before publication) are never cached.
    """

    def __init__(self, cache_dir: Path, *, token: str | None = None, pro: Any = None) -> None:
        self._cache_dir = Path(cache_dir)
        self._token = token
        self._pro = pro
        self._last_call = 0.0

    # -- client
    def _client(self) -> Any:
        if self._pro is None:
            token = self._token or os.getenv("TUSHARE_TOKEN", "").strip() or os.getenv("TUSHARE_API_TOKEN", "").strip()
            if not token:
                raise SetupScanDataError("该策略需要配置 TUSHARE_TOKEN：形态预扫描按交易日读取全市场日线")
            # Reuse DSA's Tushare HTTP client (classic endpoint, TUSHARE_HTTP_URL override);
            # it raises on API errors instead of returning silent empty frames.
            from data_provider.tushare_fetcher import _TushareHttpClient, _resolve_tushare_http_url

            self._pro = _TushareHttpClient(token=token, api_url=_resolve_tushare_http_url() or "http://api.tushare.pro")
        return self._pro

    def _call(self, api_name: str, **params: Any) -> pd.DataFrame:
        wait = _TUSHARE_MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
        try:
            df = getattr(self._client(), api_name)(**params)
        except SetupScanDataError:
            raise
        except Exception as exc:  # network / permission / rate limit
            raise SetupScanDataError(f"Tushare {api_name} 调用失败：{exc}") from exc
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    def _call_paged(self, api_name: str, **params: Any) -> pd.DataFrame:
        df = self._call(api_name, **params)
        if len(df) < _TUSHARE_PAGE_ROWS:
            return df
        parts, offset = [df], len(df)
        while True:
            part = self._call(api_name, offset=offset, limit=_TUSHARE_PAGE_ROWS, **params)
            if part.empty:
                break
            parts.append(part)
            offset += len(part)
            if len(part) < _TUSHARE_PAGE_ROWS:
                break
        return pd.concat(parts, ignore_index=True).drop_duplicates()

    # -- cache helpers
    def _read(self, path: Path) -> pd.DataFrame | None:
        if not path.exists():
            return None
        try:
            return pd.read_csv(path, dtype={"ts_code": str, "trade_date": str, "cal_date": str, "list_date": str},
                               compression="gzip")
        except Exception as exc:  # corrupt cache: refetch
            logger.warning("setup scan cache unreadable, refetching: %s (%s)", path, exc)
            return None

    def _write(self, path: Path, df: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        df.to_csv(tmp, index=False, compression="gzip")
        os.replace(tmp, path)

    def _fresh(self, path: Path, ttl: float) -> bool:
        return path.exists() and time.time() - path.stat().st_mtime < ttl

    # -- provider API
    def open_trade_dates(self, start: str, end: str) -> list[str]:
        path = self._cache_dir / "trade_cal.csv.gz"
        cal = self._read(path) if self._fresh(path, _TRADE_CAL_TTL_SEC) else None
        if cal is None or cal.empty or str(cal["cal_date"].min()) > start or str(cal["cal_date"].max()) < end:
            cal = self._call("trade_cal", exchange="SSE", start_date=start, end_date=end, fields="cal_date,is_open")
            if cal.empty:
                raise SetupScanDataError("Tushare trade_cal 没有返回交易日历")
            self._write(path, cal)
        cal = cal[(cal["cal_date"].astype(str) >= start) & (cal["cal_date"].astype(str) <= end)]
        return sorted(cal.loc[cal["is_open"].astype(int) == 1, "cal_date"].astype(str).tolist())

    def daily(self, trade_date: str) -> pd.DataFrame | None:
        path = self._cache_dir / "daily" / f"{trade_date}.csv.gz"
        cached = self._read(path)
        if cached is not None:
            return cached
        df = self._call_paged("daily", trade_date=trade_date, fields=_DAILY_FIELDS)
        if df.empty:
            return None
        self._write(path, df)
        return df

    def daily_basic(self, trade_date: str) -> pd.DataFrame | None:
        path = self._cache_dir / "daily_basic" / f"{trade_date}.csv.gz"
        cached = self._read(path)
        if cached is not None:
            return cached
        df = self._call_paged("daily_basic", trade_date=trade_date, fields=_DAILY_BASIC_FIELDS)
        if df.empty:
            return None
        self._write(path, df)
        return df

    def stock_basic(self) -> pd.DataFrame:
        path = self._cache_dir / "stock_basic.csv.gz"
        if self._fresh(path, _STOCK_BASIC_TTL_SEC):
            cached = self._read(path)
            if cached is not None and not cached.empty:
                return cached
        df = self._call("stock_basic", exchange="", list_status="L", fields=_STOCK_BASIC_FIELDS)
        if df.empty:
            raise SetupScanDataError("Tushare stock_basic 没有返回股票列表")
        self._write(path, df)
        return df

    def index_daily(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        return self._call("index_daily", ts_code=ts_code, start_date=start, end_date=end,
                          fields="trade_date,close")


def build_market_data_provider(config: Any) -> MarketDataProvider:
    data_dir = Path(getattr(config, "data_dir", Path("data") / "screening"))
    return TushareMarketData(data_dir / "market_daily")


# --------------------------------------------------------------------------- scan


def cn_today() -> date:
    return datetime.now(_CN_TZ).date()


def _required_sessions(params: SetupScanConfig) -> int:
    # S can be K-1 sessions before the as-of day; S needs 21 prior sessions
    # (20-session change and 20-day average amount); the buy line needs MA bars.
    return max(params.lookback_days + 21, params.lookback_days + params.buy_line_ma) + 4


def _pivot(long: pd.DataFrame, column: str, dates: list[str], codes: list[str]) -> pd.DataFrame:
    wide = long.pivot_table(index="trade_date", columns="ts_code", values=column, aggfunc="last")
    return wide.reindex(index=dates, columns=codes).astype(float)


def scan_surge_pullback(
    provider: MarketDataProvider,
    params: SetupScanConfig,
    *,
    today: date | None = None,
) -> SetupScanResult:
    """Return live surge-pullback setups as of the latest completed session."""
    today = today or cn_today()
    need = _required_sessions(params)
    today_s = today.strftime("%Y%m%d")
    cal_start = (today - timedelta(days=int(need * 1.7) + 40)).strftime("%Y%m%d")
    cal_end = (today + timedelta(days=20)).strftime("%Y%m%d")
    open_dates = provider.open_trade_dates(cal_start, cal_end)
    past = [d for d in open_dates if d <= today_s]
    if len(past) < need:
        raise SetupScanDataError(f"交易日历不足：需要 {need} 个交易日，仅有 {len(past)} 个")

    # latest completed session: today's bars may not be published yet
    as_of_pos = len(past) - 1
    frames: dict[str, pd.DataFrame] = {}
    for _ in range(3):
        bars = provider.daily(past[as_of_pos])
        if bars is not None and not bars.empty:
            frames[past[as_of_pos]] = bars
            break
        as_of_pos -= 1
    else:
        raise SetupScanDataError("最近交易日的全市场日线不可用")
    if as_of_pos + 1 < need:
        raise SetupScanDataError("历史交易日不足以计算形态")
    window = past[as_of_pos - need + 1: as_of_pos + 1]
    as_of = window[-1]
    for d in window[:-1]:
        bars = provider.daily(d)
        if bars is None or bars.empty:
            raise SetupScanDataError(f"{d} 的全市场日线不可用")
        frames[d] = bars
    future = [d for d in open_dates if d > as_of]
    next_session = future[0] if future else ""

    long = pd.concat([frames[d].assign(trade_date=d) for d in window], ignore_index=True)
    long["ts_code"] = long["ts_code"].astype(str)
    long = long[~long["ts_code"].str.endswith(".BJ")]
    codes = sorted(long["ts_code"].unique())
    T = len(window)
    O, H, L, C, PC, PCT, VOL, AMT = (
        _pivot(long, col, window, codes)
        for col in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount")
    )
    valid = C.notna() & VOL.gt(0)

    # back-adjust within the window from the pre_close chain (ex-dividend safe)
    prev_close = C.ffill().shift(1)
    ratio = (prev_close / PC).where(C.notna() & prev_close.notna() & PC.gt(0), 1.0)
    ratio = ratio.where((ratio > 0.2) & (ratio < 50) & ((ratio - 1).abs() > 1e-4), 1.0)
    F = ratio.cumprod()
    aL, aC = L * F, C * F
    vadj = VOL / F

    vr5 = vadj / vadj.rolling(5, min_periods=4).mean().shift(1)
    amt20_prev = AMT.rolling(20, min_periods=15).mean().shift(1)  # thousand yuan
    ret20_prev = aC.shift(1) / aC.shift(21) - 1
    ma = aC.rolling(params.buy_line_ma, min_periods=params.buy_line_ma).mean()
    one_price = (H - L).abs() < 1e-9

    basic = provider.stock_basic().drop_duplicates("ts_code").set_index("ts_code")
    names = basic["name"].reindex(codes).fillna("").astype(str)
    industry = basic["industry"].reindex(codes)
    list_dt = pd.to_datetime(basic["list_date"].reindex(codes), format="%Y%m%d", errors="coerce")
    risky_name = names.str.contains("ST|退", regex=True) | names.eq("")
    window_dt = pd.to_datetime(pd.Series(window), format="%Y%m%d")
    age = pd.DataFrame(
        (window_dt.to_numpy()[:, None] - list_dt.to_numpy()[None, :]).astype("timedelta64[D]").astype(float),
        index=window, columns=codes,
    )

    universe = (valid & (age >= params.listed_days_min) & (amt20_prev >= params.amount_20d_avg_min / 1000.0))
    universe.loc[:, risky_name.to_numpy()] = False  # ST / delisting / unknown names
    base = universe & (PCT >= params.surge_change_pct_min) & (vr5 >= params.surge_volume_ratio_min) & ~one_price
    base = base.fillna(False).astype(bool)

    # market switch per session, evaluated on the previous close (known before the open)
    idx = provider.index_daily(params.regime_index, window[0], as_of)
    regime_change = pd.Series(np.nan, index=window)
    regime_as_of = ""
    if not idx.empty:
        idx_close = (idx.assign(trade_date=idx["trade_date"].astype(str))
                     .drop_duplicates("trade_date").set_index("trade_date")["close"].astype(float)
                     .reindex(window).ffill())
        regime_change = (idx_close / idx_close.shift(20) - 1) * 100
        last_idx = [d for d in idx["trade_date"].astype(str) if d <= as_of]
        regime_as_of = max(last_idx) if last_idx else ""
    threshold = params.regime_change_20d_pct_min
    if threshold is None:
        regime_on_series = pd.Series(True, index=window)
    else:
        regime_on_series = regime_change > threshold
        if idx.empty or pd.isna(regime_change.iloc[-1]):
            raise SetupScanDataError(f"市场开关指数 {params.regime_index} 数据不足，无法判断开关状态")
    order_allowed = regime_on_series.shift(1).fillna(False).astype(bool)  # switch as of the prior close

    first_s = T - params.lookback_days  # sessions S..as_of keep the setup open for the next session
    base_np = base.to_numpy()
    last_surge = np.full(len(codes), -1)
    any_surge = base_np.any(axis=0)
    last_surge[any_surge] = T - 1 - np.argmax(base_np[::-1, any_surge], axis=0)
    in_window = last_surge >= first_s

    stats = {"surges_in_window": int(in_window.sum()), "qualified": 0, "broken": 0,
             "triggered": 0, "no_line": 0, "waiting": 0}
    rows: list[dict[str, Any]] = []
    turnover_cache: dict[str, pd.Series] = {}
    peer_cache: dict[int, pd.Series] = {}
    for ci in np.nonzero(in_window)[0]:
        s = int(last_surge[ci])
        code = codes[ci]
        s_date = window[s]
        if s not in peer_cache:
            surged = pd.Series(base_np[s], index=codes)
            peer_cache[s] = industry[surged.to_numpy()].value_counts()
        ind = industry.iloc[ci]
        peers = int(peer_cache[s].get(ind, 1)) - 1 if isinstance(ind, str) and ind else 0
        if s_date not in turnover_cache:
            db = provider.daily_basic(s_date)
            turnover_cache[s_date] = (
                pd.Series(dtype=float) if db is None or db.empty
                else db.drop_duplicates("ts_code").set_index("ts_code")["turnover_rate"].astype(float)
            )
        turnover = turnover_cache[s_date].get(code, np.nan)
        vr = float(vr5.iat[s, ci])
        r20 = float(ret20_prev.iat[s, ci])
        if (
            peers < params.industry_peers_min
            or (params.surge_volume_ratio_max is not None and not vr <= params.surge_volume_ratio_max)
            or (params.surge_turnover_rate_max is not None and not turnover <= params.surge_turnover_rate_max)
            or (params.prior_20d_change_pct_max is not None and not r20 * 100 <= params.prior_20d_change_pct_max)
        ):
            continue
        stats["qualified"] += 1

        surge_low = float(aL.iat[s, ci])
        status = "waiting"
        for d in range(s + 1, T):
            low_d = aL.iat[d, ci]
            line_d = ma.iat[d - 1, ci]
            if (bool(order_allowed.iloc[d]) and pd.notna(low_d) and pd.notna(line_d) and low_d <= line_d):
                status = "triggered"
                break
            close_d = aC.iat[d, ci]
            if pd.notna(close_d) and close_d < surge_low * 0.999:
                status = "broken"
                break
        if status != "waiting":
            stats[status] += 1
            continue
        f_now = float(F.iat[T - 1, ci])
        line_adj = ma.iat[T - 1, ci]
        last_close = C.iat[T - 1, ci]
        if pd.isna(line_adj) or pd.isna(last_close) or f_now <= 0:
            stats["no_line"] += 1
            continue
        buy_line = round(float(line_adj) / f_now, 2)
        session_index = T - s  # the next session is the k-th session after S
        rows.append({
            "code": code.split(".")[0],
            "setup_type": "surge_pullback",
            "setup_surge_date": f"{s_date[:4]}-{s_date[4:6]}-{s_date[6:]}",
            "setup_surge_change_pct": round(float(PCT.iat[s, ci]), 2),
            "setup_surge_volume_ratio": round(vr, 2),
            "setup_surge_turnover_rate": round(float(turnover), 2),
            "setup_prior_20d_change_pct": round(r20 * 100, 2),
            "setup_industry": ind if isinstance(ind, str) else "",
            "setup_industry_peers": peers,
            "setup_session_index": session_index,
            "setup_window_sessions_left": params.lookback_days - session_index + 1,
            "setup_buy_line": buy_line,
            "setup_buy_line_ma": params.buy_line_ma,
            "setup_cancel_line": round(surge_low / f_now, 2),
            "setup_last_close": round(float(last_close), 2),
            "setup_buy_line_gap_pct": round((buy_line / float(last_close) - 1) * 100, 2),
        })
        stats["waiting"] += 1

    regime_now = regime_change.iloc[-1]
    regime_on = True if threshold is None else bool(regime_on_series.iloc[-1])
    candidates = pd.DataFrame(rows)
    if not candidates.empty:
        candidates = candidates.set_index("code")
        candidates["setup_as_of"] = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}"
        candidates["setup_next_session"] = (
            f"{next_session[:4]}-{next_session[4:6]}-{next_session[6:]}" if next_session else ""
        )
        candidates["setup_regime_on"] = regime_on
        candidates["setup_regime_change_20d_pct"] = None if pd.isna(regime_now) else round(float(regime_now), 2)
        candidates["setup_regime_index"] = params.regime_index
        candidates["setup_regime_threshold_pct"] = threshold
    return SetupScanResult(
        candidates=candidates,
        as_of=as_of,
        next_session=next_session,
        regime_on=regime_on,
        regime_change_20d_pct=None if pd.isna(regime_now) else float(regime_now),
        regime_as_of=regime_as_of,
        stats=stats,
    )


def _fmt_date(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:]}" if len(value) == 8 else value


def apply_setup_scan(
    snapshot_df: pd.DataFrame,
    params: SetupScanConfig,
    *,
    config: Any,
    provider: MarketDataProvider | None = None,
    today: date | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Restrict the snapshot to live setups and attach ``setup_*`` columns.

    Returns the restricted frame and human-readable notes for the run warnings.
    """
    if params.type != "surge_pullback":
        raise ValueError(f"Unsupported setup_scan type: {params.type}")
    provider = provider or build_market_data_provider(config)
    result = scan_surge_pullback(provider, params, today=today)

    notes: list[str] = []
    as_of = _fmt_date(result.as_of)
    next_session = _fmt_date(result.next_session) or "下一交易日"
    if params.regime_change_20d_pct_min is not None:
        change = result.regime_change_20d_pct
        change_text = "-" if change is None else f"{change:+.2f}%"
        basis = f"{params.regime_index} 截至 {_fmt_date(result.regime_as_of) or as_of} 近20个交易日 {change_text}"
        threshold = f"阈值 > {params.regime_change_20d_pct_min:+.2f}%"
        if result.regime_on:
            notes.append(f"市场开关开启：{basis}（{threshold}），{next_session} 可按价格线挂单")
        elif params.regime_off_action == "block":
            notes.append(f"市场开关关闭：{basis}（{threshold}），按规则 {next_session} 不开新仓，未返回候选")
        else:
            notes.append(f"市场开关关闭：{basis}（{threshold}），按规则 {next_session} 不开新仓；以下名单仅供观察")
        if result.regime_as_of and result.regime_as_of != result.as_of:
            notes.append(f"市场开关指数行情截至 {_fmt_date(result.regime_as_of)}，晚于个股日线 {as_of} 的数据尚未发布")
    s = result.stats
    notes.append(
        f"形态预扫描（截至 {as_of}）：近 {params.lookback_days} 个交易日放量大涨 {s['surges_in_window']} 只，"
        f"满足选股条件 {s['qualified']} 只，其中已跌破大涨日最低价 {s['broken']}、"
        f"已触发买点 {s['triggered']}、无法计算价格线 {s['no_line']}、等待回踩 {s['waiting']}"
    )

    work = snapshot_df.copy()
    code_col = "code" if "code" in work.columns else None
    if code_col is None or result.candidates.empty:
        empty = work.iloc[0:0].copy()
        empty.attrs.update(snapshot_df.attrs)
        return empty, notes
    work["_setup_code"] = work[code_col].astype(str).str.extract(r"(\d{6})", expand=False)
    merged = work.merge(result.candidates, left_on="_setup_code", right_index=True, how="inner")
    missing = sorted(set(result.candidates.index) - set(merged["_setup_code"]))
    if missing:
        sample = "、".join(missing[:5])
        notes.append(f"形态预扫描有 {len(missing)} 只不在本次快照/板块范围内（如 {sample}），未列入")
    merged = merged.drop(columns="_setup_code")
    if not result.regime_on and params.regime_off_action == "block":
        merged = merged.iloc[0:0]
    merged.attrs.update(snapshot_df.attrs)
    return merged, notes
