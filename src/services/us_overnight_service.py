# -*- coding: utf-8 -*-
"""Latest completed US session for the US leaders: fetch, store in ``stock_daily``, compute moves.

Design: ``docs/design/core-backend-design.md`` 5.1 - 5.3.

* Sources are tried one after the other, each only for the leaders that have not yet delivered the
  expected session: ``eastmoney`` (AKShare; the overnight session is there right after the US close; only
  leaders with an ``em`` code), ``tushare`` (published 13:00 / 09:00 Beijing time; fallback only) and
  ``yfinance`` (Yahoo, rate limited in practice).
* 17:00 board run: ``SOURCES_ALL = ("eastmoney", "tushare", "yfinance")``.
* 08:15 pre-open run: ``SOURCES_PREOPEN = ("eastmoney", "yfinance")`` - Tushare does not have the
  overnight session yet. Rows are upserted by ``(code, date)`` with a priority
  Tushare > Eastmoney > yfinance: a source never overwrites a row of a higher-priority source, so a later
  Tushare fallback replaces Eastmoney values of the same session, and Eastmoney replaces yfinance.
* Tushare: ``us_daily`` is limited to 1 call per minute for this account, so it is asked at most ONCE per
  run, by ``trade_date`` (one call returns every US ticker of that session; the leaders still missing are
  taken from it). ``index_global`` stays one call per index. A frequency-limit error is not retried: Tushare
  is skipped for the rest of the run (``TushareRateLimitedError``).
* Eastmoney: stocks / ETFs via ``stock_us_hist`` (one call per leader, ``<market>.<ticker>``), indices via
  one ``index_global_spot_em`` snapshot per refresh -> one bar whose session date is the New York date of
  ``最新行情时间`` (Beijing time). A snapshot taken during a session, or newer than ``as_of`` (replay of a
  past board day), yields no bar; ``index_global_hist_em`` (daily history of that index, one call, addressed
  by the snapshot's ``名称``) then supplies the completed sessions. Every AKShare call has a timeout
  (``call_with_timeout``); after ``EASTMONEY_MAX_CONSECUTIVE_FAILURES`` failures in a row the rest of the
  run skips Eastmoney.
* Daily bars only, in the existing ``stock_daily`` table (``code`` = leader symbol, e.g. ``NVDA``,
  ``SPX``; ``data_source`` records the origin). No new price table.
* The "latest completed US session" comes from the data: of the bars whose 16:00 New York close is
  not after ``as_of``, the newest date among the leaders (weekends / holidays need no calendar).
  Leaders whose newest bar is older are reported as ``stale`` and produce no move.
* Moves are computed from the stored rows (single read path shared with the pre-open context):
  ``pct_chg`` close vs previous close, ``gap_pct`` open vs previous close, and whether ``|pct_chg|``
  reaches the leader's threshold (``US_MOVE_STOCK_PCT`` / ``US_MOVE_INDEX_PCT``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from data_provider.tushare_market_data import IncompleteMarketDataError, TushareRateLimitedError
from src.services.news_akshare_source import CN_TZ, _akshare, call_with_timeout
from src.services.us_leaders import UsLeader, UsMoveThresholds, get_us_leaders, thresholds_from_config

logger = logging.getLogger(__name__)

_NY_TZ = ZoneInfo("America/New_York")
_SESSION_CLOSE = dtime(16, 0)
WINDOW_DAYS = 20  # calendar days of daily bars fetched per leader (previous close + a few sessions)

SOURCE_TUSHARE_STOCK = "TushareUsDaily"
SOURCE_TUSHARE_INDEX = "TushareIndexGlobal"
SOURCE_EASTMONEY_STOCK = "EastmoneyUsHist"
SOURCE_EASTMONEY_INDEX = "EastmoneyIndexGlobal"
SOURCE_YFINANCE = "YfinanceFetcher"
SOURCES_ALL = ("eastmoney", "tushare", "yfinance")  # 17:00 board run
SOURCES_PREOPEN = ("eastmoney", "yfinance")  # 08:15 pre-open run
SOURCES_YFINANCE_ONLY = ("yfinance",)
KNOWN_SOURCES = ("tushare", "eastmoney", "yfinance")

EASTMONEY_TIMEOUT_SEC = 20.0
EASTMONEY_MAX_CONSECUTIVE_FAILURES = 3
_EM_MIN_CLOSE = dtime(13, 0)  # earliest New York time of a quote that can be a closing quote (early-close days)
_EM_INDEX_SPOT_KEY = "us-em-index-spot"
_EM_INDEX_HIST_KEY = "us-em-index-hist"

_FRAME_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "pre_close"]


def is_tushare_source(source: Optional[str]) -> bool:
    return bool(source) and str(source).startswith("Tushare")


def source_rank(source: Optional[str]) -> int:
    """Overwrite priority of a stored ``data_source``: lower wins (Tushare < Eastmoney < yfinance < other)."""
    text = str(source or "")
    if text.startswith("Tushare"):
        return 0
    if text.startswith("Eastmoney"):
        return 1
    if text == SOURCE_YFINANCE:
        return 2
    return 9


def session_closed(day: date, as_of: datetime) -> bool:
    """Whether the regular US session of ``day`` (16:00 New York) is over at ``as_of``."""
    close = datetime.combine(day, _SESSION_CLOSE, tzinfo=_NY_TZ)
    return close <= _aware(as_of)


def expected_weekday_session(as_of: datetime) -> date:
    """Newest weekday whose US close has passed (ignores holidays: a lower bound for "fresh enough")."""
    local = _aware(as_of).astimezone(_NY_TZ)
    day = local.date()
    if local.time() < _SESSION_CLOSE:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass
class UsMove:
    symbol: str
    name: str
    kind: str
    trade_date: date
    close: Optional[float]
    pre_close: Optional[float]
    pct_chg: Optional[float]
    gap_pct: Optional[float]
    threshold: float
    crossed: bool
    source: Optional[str] = None

    @property
    def direction(self) -> Optional[str]:
        if not self.crossed or self.pct_chg is None:
            return None
        return "up" if self.pct_chg > 0 else "down"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name, "kind": self.kind,
            "trade_date": self.trade_date.isoformat(),
            "close": self.close, "pre_close": self.pre_close,
            "pct_chg": _r(self.pct_chg), "gap_pct": _r(self.gap_pct),
            "threshold": self.threshold, "crossed": self.crossed, "direction": self.direction,
            "source": self.source,
        }


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 2)


@dataclass
class UsOvernightResult:
    session: Optional[date] = None
    moves: List[UsMove] = field(default_factory=list)
    stored_rows: int = 0
    sources: Dict[str, str] = field(default_factory=dict)  # symbol -> data_source of the fetched bars
    failures: Dict[str, str] = field(default_factory=dict)  # symbol -> reason (no usable bar)
    stale: List[str] = field(default_factory=list)  # symbols whose newest bar is older than ``session``
    notes: List[str] = field(default_factory=list)

    @property
    def crossed(self) -> List[UsMove]:
        return [m for m in self.moves if m.crossed]

    @property
    def ok(self) -> bool:
        return self.session is not None and bool(self.moves)

    @property
    def complete(self) -> bool:
        return self.ok and not self.failures and not self.stale

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session": self.session.isoformat() if self.session else None,
            "stored_rows": self.stored_rows,
            "moves": [m.to_dict() for m in self.moves],
            "crossed": [m.symbol for m in self.crossed],
            "sources": dict(self.sources),
            "failures": dict(self.failures),
            "stale": list(self.stale),
            "notes": list(self.notes),
        }


class UsOvernightService:
    def __init__(
        self,
        config: Any = None,
        *,
        leaders: Optional[Sequence[UsLeader]] = None,
        thresholds: Optional[UsMoveThresholds] = None,
        tushare_fetcher: Any = None,
        yfinance_fetcher: Any = None,
        akshare_module: Any = None,
        em_timeout_sec: float = EASTMONEY_TIMEOUT_SEC,
        db: Any = None,
        now_fn: Any = None,
    ) -> None:
        if config is None:
            from src.config import get_config

            config = get_config()
        self._config = config
        self._leaders = list(leaders) if leaders is not None else list(get_us_leaders(config).leaders)
        self._thresholds = thresholds or thresholds_from_config(config)
        self._tushare = tushare_fetcher
        self._yfinance = yfinance_fetcher
        self._akshare = akshare_module
        self._em_timeout = float(em_timeout_sec)
        self._em_spot: Any = None  # per-refresh index snapshot (DataFrame) or the Exception that replaced it
        self._db = db
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # dependencies
    # ------------------------------------------------------------------
    @property
    def leaders(self) -> List[UsLeader]:
        return list(self._leaders)

    @property
    def thresholds(self) -> UsMoveThresholds:
        return self._thresholds

    @property
    def db(self) -> Any:
        if self._db is None:
            from src.storage import get_db

            self._db = get_db()
        return self._db

    def _get_tushare(self) -> Optional[Any]:
        if self._tushare is None:
            try:
                from data_provider.tushare_fetcher import TushareFetcher

                fetcher = TushareFetcher(
                    rate_limit_per_minute=getattr(self._config, "market_snapshot_requests_per_minute", 60)
                )
                self._tushare = fetcher if fetcher.is_available() else False
            except Exception as exc:  # noqa: BLE001 - Tushare is optional here
                logger.warning("[USOvernight] Tushare 不可用: %s", exc)
                self._tushare = False
        return self._tushare or None

    def _get_yfinance(self) -> Any:
        if self._yfinance is None:
            from data_provider.yfinance_fetcher import YfinanceFetcher

            self._yfinance = YfinanceFetcher()
        return self._yfinance

    def _get_akshare(self) -> Any:
        if self._akshare is None:
            self._akshare = _akshare()
        return self._akshare

    # ------------------------------------------------------------------
    # fetch + store
    # ------------------------------------------------------------------
    def refresh(
        self,
        as_of: Optional[datetime] = None,
        *,
        sources: Sequence[str] = SOURCES_ALL,
    ) -> UsOvernightResult:
        """Fetch the newest completed session for every leader, upsert it, and compute the moves."""
        as_of = _aware(as_of) if as_of is not None else self._now_fn()
        result = UsOvernightResult()
        if not self._leaders:
            result.notes.append("未配置美股龙头 (US_LEADERS 为空数组)")
            return result

        end_day = as_of.astimezone(_NY_TZ).date()
        start_day = end_day - timedelta(days=WINDOW_DAYS)
        expected = expected_weekday_session(as_of)
        order = [name for name in dict.fromkeys(sources) if name in KNOWN_SOURCES]
        run = _RunState()
        self._em_spot = None
        candidates: Dict[str, List[Tuple[pd.DataFrame, str]]] = {leader.symbol: [] for leader in self._leaders}
        errors: Dict[str, List[str]] = {leader.symbol: [] for leader in self._leaders}

        def delivered(leader: UsLeader) -> bool:
            frames = candidates[leader.symbol]
            return bool(frames) and max(frame["date"].max() for frame, _ in frames) >= expected

        for name in order:
            # a later source only serves the leaders for which no earlier one delivered the expected session
            pending = [leader for leader in self._leaders if not delivered(leader)]
            if not pending:
                break
            if name == "eastmoney":
                self._eastmoney_pass(pending, start_day, end_day, as_of, run, candidates, errors, result)
            elif name == "tushare":
                self._tushare_pass(pending, expected, start_day, end_day, as_of, run, candidates, errors, result)
            else:
                for leader in pending:
                    try:
                        frame = self._fetch_yfinance(leader, start_day, end_day, as_of)
                        if frame is not None and not frame.empty:
                            candidates[leader.symbol].append((frame, SOURCE_YFINANCE))
                        else:
                            errors[leader.symbol].append("yfinance 无数据")
                    except Exception as exc:  # noqa: BLE001
                        errors[leader.symbol].append(f"yfinance: {str(exc)[:120]}")

        chosen: Dict[str, Tuple[pd.DataFrame, str]] = {}
        for leader in self._leaders:
            found = candidates[leader.symbol]
            if not found:
                result.failures[leader.symbol] = "；".join(errors[leader.symbol]) or "没有可用数据源"
                continue
            # newest bar wins; on a tie the source with the higher overwrite priority (Tushare > Eastmoney > yfinance)
            chosen[leader.symbol] = min(found, key=lambda c: (-c[0]["date"].max().toordinal(), source_rank(c[1])))

        if not chosen:
            result.notes.append("所有龙头都没有取到已收盘的美股日线")
            return result

        result.session = max(frame["date"].max() for frame, _ in chosen.values())
        for leader in self._leaders:
            entry = chosen.get(leader.symbol)
            if entry is None:
                continue
            frame, source = entry
            result.sources[leader.symbol] = source
            if frame["date"].max() < result.session:
                result.stale.append(leader.symbol)
            try:
                result.stored_rows += self._store(leader, frame, source)
            except Exception as exc:  # noqa: BLE001 - one leader must not stop the rest
                logger.warning("[USOvernight] 写入 %s 失败: %s", leader.symbol, exc, exc_info=True)
                result.failures[leader.symbol] = f"写入 stock_daily 失败: {str(exc)[:120]}"

        result.moves, result_stale = self.load_moves(result.session)
        for symbol in result_stale:
            if symbol not in result.stale and symbol not in result.failures:
                result.stale.append(symbol)
        return result

    def _eastmoney_pass(
        self,
        pending: Sequence[UsLeader],
        start: date,
        end: date,
        as_of: datetime,
        run: "_RunState",
        candidates: Dict[str, List[Tuple[pd.DataFrame, str]]],
        errors: Dict[str, List[str]],
        result: UsOvernightResult,
    ) -> None:
        for leader in pending:
            if not (run.eastmoney and leader.em_api):
                continue
            try:
                frame = self._fetch_eastmoney(leader, start, end, as_of)
                run.em_failures = 0
                if frame is not None and not frame.empty:
                    source = SOURCE_EASTMONEY_INDEX if leader.is_index else SOURCE_EASTMONEY_STOCK
                    candidates[leader.symbol].append((frame, source))
                else:
                    errors[leader.symbol].append("东方财富无数据")
            except Exception as exc:  # noqa: BLE001 - timeout / busy / scraper break: fall back to the next source
                run.em_failures += 1
                errors[leader.symbol].append(f"东方财富: {str(exc)[:120]}")
                if run.em_failures >= EASTMONEY_MAX_CONSECUTIVE_FAILURES:
                    run.eastmoney = False
                    result.notes.append(f"东方财富连续 {run.em_failures} 次取数失败，其余龙头改用其他数据源")

    def _tushare_pass(
        self,
        pending: Sequence[UsLeader],
        expected: date,
        start: date,
        end: date,
        as_of: datetime,
        run: "_RunState",
        candidates: Dict[str, List[Tuple[pd.DataFrame, str]]],
        errors: Dict[str, List[str]],
        result: UsOvernightResult,
    ) -> None:
        """Tushare as a fallback: ONE ``us_daily`` call by ``trade_date`` for all pending stocks / ETFs, then
        ``index_global`` per pending index. Unavailable / rate limited -> nothing more is asked this run."""
        if not run.tushare:
            return
        stocks = [leader for leader in pending if leader.tushare_api == "us_daily"]
        indices = [leader for leader in pending if leader.tushare_api == "index_global"]
        # one date = one ``us_daily`` call: the expected weekday session (on a US holiday it simply has no rows)
        target = expected
        try:
            if stocks:
                self._tushare_stocks(stocks, target, as_of, candidates, errors)
            for leader in indices:
                try:
                    frame = self._fetch_tushare_index(leader, start, end, as_of)
                    if frame is not None and not frame.empty:
                        candidates[leader.symbol].append((frame, SOURCE_TUSHARE_INDEX))
                    else:
                        errors[leader.symbol].append("Tushare 无数据")
                except _TushareUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - per index; the fallback may still deliver
                    errors[leader.symbol].append(f"Tushare: {str(exc)[:120]}")
        except _TushareUnavailable as exc:
            run.tushare = False
            for leader in stocks + indices:
                if not candidates[leader.symbol] and str(exc) not in errors[leader.symbol]:
                    errors[leader.symbol].append(str(exc))
            result.notes.append(f"{exc}；其余龙头改用其他数据源")

    def _tushare_stocks(
        self,
        leaders: Sequence[UsLeader],
        trade_date: date,
        as_of: datetime,
        candidates: Dict[str, List[Tuple[pd.DataFrame, str]]],
        errors: Dict[str, List[str]],
    ) -> None:
        """One ``us_daily`` by ``trade_date``: every pending stock / ETF is taken from it."""
        fetcher = self._get_tushare()
        if fetcher is None:
            raise _TushareUnavailable("Tushare 未配置（缺少 TUSHARE_TOKEN）")
        note = ""
        try:
            raw = fetcher.fetch_us_daily_market(trade_date)
        except IncompleteMarketDataError as exc:  # e.g. rate limited on page 2: the rows of page 1 are still good
            raw, note = exc.partial, f"（{exc.reason}）"
        except Exception as exc:  # noqa: BLE001
            reason = _tushare_unavailable_reason("us_daily", exc)
            if reason:
                raise _TushareUnavailable(reason) from exc
            for leader in leaders:
                errors[leader.symbol].append(f"Tushare: {str(exc)[:120]}")
            return
        has_codes = raw is not None and not raw.empty and "ts_code" in raw.columns
        codes = raw["ts_code"].astype(str).str.upper() if has_codes else None
        for leader in leaders:
            frame = pd.DataFrame(columns=_FRAME_COLUMNS)
            if codes is not None:
                frame = _completed_only(_frame_from_tushare(raw[codes == str(leader.tushare_code).upper()]), as_of)
            if frame.empty:
                errors[leader.symbol].append(f"Tushare 无数据{note}")
            else:
                candidates[leader.symbol].append((frame, SOURCE_TUSHARE_STOCK))

    def _fetch_tushare_index(self, leader: UsLeader, start: date, end: date, as_of: datetime) -> Optional[pd.DataFrame]:
        fetcher = self._get_tushare()
        if fetcher is None:
            raise _TushareUnavailable("Tushare 未配置（缺少 TUSHARE_TOKEN）")
        try:
            raw = fetcher.fetch_index_global(leader.tushare_code, start, end)
        except Exception as exc:  # noqa: BLE001
            reason = _tushare_unavailable_reason("index_global", exc)
            if reason:
                raise _TushareUnavailable(reason) from exc
            raise
        return _completed_only(_frame_from_tushare(raw), as_of)

    def _fetch_eastmoney(self, leader: UsLeader, start: date, end: date, as_of: datetime) -> Optional[pd.DataFrame]:
        ak = self._get_akshare()
        if leader.is_index:
            spot = self._index_snapshot(ak)
            frame = _completed_only(_frame_from_eastmoney_spot(spot, leader.em or ""), as_of)
            if frame.empty:
                # an intraday snapshot, or one newer than ``as_of`` (replay of a past board day) is no usable
                # bar: the completed sessions come from the index history, addressed by the snapshot's ``名称``
                frame = self._index_history(ak, spot, leader.em or "", start, end, as_of)
            return frame
        em_code = leader.em or ""
        raw = call_with_timeout(
            f"us-em-hist:{em_code}",
            lambda: ak.stock_us_hist(
                symbol=em_code, period="daily",
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"), adjust="",
            ),
            self._em_timeout,
        )
        return _completed_only(_frame_from_eastmoney_hist(raw), as_of)

    def _index_history(self, ak: Any, spot: Any, em_code: str, start: date, end: date, as_of: datetime) -> pd.DataFrame:
        """``index_global_hist_em`` of one index (one call, the whole history; only the window is kept)."""
        name = _spot_name(spot, em_code)
        if not name:
            return pd.DataFrame(columns=_FRAME_COLUMNS)
        raw = call_with_timeout(f"{_EM_INDEX_HIST_KEY}:{name}", lambda: ak.index_global_hist_em(symbol=name), self._em_timeout)
        return _completed_only(_frame_from_eastmoney_index_hist(raw, start, end), as_of)

    def _index_snapshot(self, ak: Any) -> Any:
        """``index_global_spot_em`` once per refresh; a failure is remembered and re-raised for the other indices."""
        if self._em_spot is None:
            try:
                self._em_spot = call_with_timeout(_EM_INDEX_SPOT_KEY, lambda: ak.index_global_spot_em(), self._em_timeout)
            except Exception as exc:  # noqa: BLE001
                self._em_spot = exc
        if isinstance(self._em_spot, Exception):
            raise self._em_spot
        return self._em_spot

    def _fetch_yfinance(self, leader: UsLeader, start: date, end: date, as_of: datetime) -> Optional[pd.DataFrame]:
        raw = self._get_yfinance().get_daily_data(
            leader.yfinance_symbol,
            start_date=start.isoformat(),
            end_date=(end + timedelta(days=1)).isoformat(),  # yfinance end is exclusive
        )
        return _completed_only(_frame_from_yfinance(raw), as_of)

    def _store(self, leader: UsLeader, frame: pd.DataFrame, source: str) -> int:
        """Upsert the window into ``stock_daily``; a row of a higher-priority source is never overwritten."""
        rows = _with_indicators(frame) if len(frame) > 1 else frame  # one bar has no history for MA
        rank = source_rank(source)
        existing = self.db.get_data_range(leader.symbol, frame["date"].min(), frame["date"].max())
        protected = {bar.date for bar in existing if source_rank(getattr(bar, "data_source", None)) < rank}
        if protected:
            rows = rows[~rows["date"].isin(protected)]
        if rows.empty:
            return 0
        return int(self.db.save_daily_data(rows, leader.symbol, data_source=source) or 0)

    # ------------------------------------------------------------------
    # read side (shared by refresh and the pre-open context)
    # ------------------------------------------------------------------
    def load_moves(
        self,
        session: Optional[date] = None,
        *,
        as_of: Optional[datetime] = None,
    ) -> Tuple[List[UsMove], List[str]]:
        """Moves of ``session`` (default: newest stored completed session) read from ``stock_daily``.

        Returns ``(moves, stale_symbols)``; a leader without a bar on ``session`` is stale and has
        no move. With no ``session``, it is the newest stored date among the leaders that is
        already closed at ``as_of`` (default now).
        """
        as_of = _aware(as_of) if as_of is not None else self._now_fn()
        horizon = as_of.astimezone(_NY_TZ).date()
        bars: Dict[str, List[Any]] = {}
        for leader in self._leaders:
            try:
                rows = self.db.get_data_range(leader.symbol, horizon - timedelta(days=WINDOW_DAYS), horizon)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[USOvernight] 读取 %s 失败: %s", leader.symbol, exc)
                rows = []
            bars[leader.symbol] = [b for b in rows if session_closed(b.date, as_of)]
        if session is None:
            dates = [b[-1].date for b in bars.values() if b]
            if not dates:
                return [], []
            session = max(dates)
        moves: List[UsMove] = []
        stale: List[str] = []
        for leader in self._leaders:
            history = [b for b in bars[leader.symbol] if b.date <= session]
            if not history or history[-1].date != session:
                if history:
                    stale.append(leader.symbol)
                continue
            moves.append(self._move(leader, history))
        return moves, stale

    def _move(self, leader: UsLeader, history: List[Any]) -> UsMove:
        bar = history[-1]
        previous = history[-2] if len(history) >= 2 else None
        prev_close = _num(getattr(previous, "close", None)) if previous is not None else None
        close = _num(bar.close)
        pct = _num(bar.pct_chg)
        if pct is None and close is not None and prev_close:
            pct = (close / prev_close - 1.0) * 100.0
        open_ = _num(bar.open)
        gap = (open_ / prev_close - 1.0) * 100.0 if open_ is not None and prev_close else None
        threshold = self._thresholds.for_leader(leader)
        return UsMove(
            symbol=leader.symbol, name=leader.name, kind=leader.kind, trade_date=bar.date,
            close=close, pre_close=prev_close, pct_chg=pct, gap_pct=gap, threshold=threshold,
            crossed=pct is not None and abs(pct) >= threshold, source=getattr(bar, "data_source", None),
        )


class _TushareUnavailable(RuntimeError):
    """Tushare cannot serve this run (no token / no permission / frequency limit): stop asking for the other leaders."""


@dataclass
class _RunState:
    """Sources switched off for the rest of one ``refresh`` when they proved unusable."""

    eastmoney: bool = True
    tushare: bool = True
    em_failures: int = 0  # consecutive Eastmoney failures


_RATE_LIMIT_HINTS = ("频率超限", "频率", "每分钟", "too many", "rate limit")


def _tushare_unavailable_reason(api_name: str, exc: Exception) -> Optional[str]:
    """Why ``exc`` means Tushare is unusable for the rest of the run (None = an ordinary per-request failure)."""
    message = str(exc)
    if isinstance(exc, TushareRateLimitedError) or any(hint in message.lower() for hint in _RATE_LIMIT_HINTS):
        return f"Tushare {api_name} 频率超限，本次运行不再请求 Tushare"
    if "权限" in message or "积分" in message:
        return f"Tushare {api_name} 无权限或积分不足"
    return None


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


# ----------------------------------------------------------------------
# frame helpers
# ----------------------------------------------------------------------
def _frame_from_tushare(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_FRAME_COLUMNS)
    df = raw.copy()
    df["date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["volume"] = df["vol"] if "vol" in df.columns else float("nan")
    for column in ("open", "high", "low", "close", "amount", "pct_chg", "pre_close"):
        if column not in df.columns:
            df[column] = float("nan")
        df[column] = pd.to_numeric(df[column], errors="coerce")
    missing_pct = df["pct_chg"].isna() & df["pre_close"].notna() & (df["pre_close"] != 0)
    df.loc[missing_pct, "pct_chg"] = (df["close"] / df["pre_close"] - 1.0) * 100.0
    df = df.dropna(subset=["close"]).drop_duplicates(subset=["date"], keep="last")
    return df[_FRAME_COLUMNS].sort_values("date").reset_index(drop=True)


def _numeric_column(raw: pd.DataFrame, name: str) -> pd.Series:
    if name in raw.columns:
        return pd.to_numeric(raw[name], errors="coerce")
    return pd.Series(float("nan"), index=raw.index, dtype="float64")


def _frame_from_eastmoney_hist(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
    """``stock_us_hist`` daily bars (unadjusted): ``日期 开盘 收盘 最高 最低 成交量 成交额 涨跌幅 涨跌额``.

    Previous close = close - 涨跌额 (present on every row, also the first); percent change from 涨跌幅.
    """
    if raw is None or raw.empty or "日期" not in raw.columns:
        return pd.DataFrame(columns=_FRAME_COLUMNS)
    df = pd.DataFrame(index=raw.index)
    df["date"] = pd.to_datetime(raw["日期"], errors="coerce").dt.date
    for column, source in (
        ("open", "开盘"), ("high", "最高"), ("low", "最低"), ("close", "收盘"),
        ("volume", "成交量"), ("amount", "成交额"), ("pct_chg", "涨跌幅"),
    ):
        df[column] = _numeric_column(raw, source)
    change = _numeric_column(raw, "涨跌额")
    df["pre_close"] = df["close"] - change
    df = df.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last").sort_values("date")
    missing_prev = df["pre_close"].isna()
    df.loc[missing_prev, "pre_close"] = df["close"].shift(1)[missing_prev]
    missing_pct = df["pct_chg"].isna() & df["pre_close"].notna() & (df["pre_close"] != 0)
    df.loc[missing_pct, "pct_chg"] = (df["close"] / df["pre_close"] - 1.0) * 100.0
    return df[_FRAME_COLUMNS].reset_index(drop=True)


def _frame_from_eastmoney_spot(spot: Optional[pd.DataFrame], em_code: str) -> pd.DataFrame:
    """One bar from an ``index_global_spot_em`` row (``代码 最新价 涨跌幅 开盘价 最高价 最低价 昨收价 最新行情时间``).

    The session date is the New York date of ``最新行情时间`` (Beijing time, naive). A quote earlier than
    13:00 New York time is an intraday snapshot, not a close: it yields no bar.
    """
    empty = pd.DataFrame(columns=_FRAME_COLUMNS)
    if spot is None or spot.empty or "代码" not in spot.columns:
        return empty
    rows = spot[spot["代码"].astype(str).str.strip().str.upper() == em_code.strip().upper()]
    if rows.empty:
        return empty
    row = rows.iloc[0]
    stamp = pd.to_datetime(row.get("最新行情时间"), errors="coerce")
    if pd.isna(stamp):
        return empty
    quote = stamp.to_pydatetime()
    quote = quote.replace(tzinfo=CN_TZ) if quote.tzinfo is None else quote
    quote_ny = quote.astimezone(_NY_TZ)
    if quote_ny.time() < _EM_MIN_CLOSE:
        return empty
    close = _num(row.get("最新价"))
    if close is None:
        return empty
    pre_close = _num(row.get("昨收价"))
    pct = _num(row.get("涨跌幅"))
    if pct is None and pre_close:
        pct = (close / pre_close - 1.0) * 100.0
    nan = float("nan")
    data = {
        "date": quote_ny.date(), "open": _num(row.get("开盘价")), "high": _num(row.get("最高价")),
        "low": _num(row.get("最低价")), "close": close, "volume": nan, "amount": nan,
        "pct_chg": pct, "pre_close": pre_close,
    }
    return pd.DataFrame([{k: (nan if v is None else v) for k, v in data.items()}], columns=_FRAME_COLUMNS)


def _spot_name(spot: Optional[pd.DataFrame], em_code: str) -> str:
    """``名称`` of the ``index_global_spot_em`` row of ``em_code`` ("" when absent): the key of ``index_global_hist_em``."""
    if spot is None or spot.empty or "代码" not in spot.columns or "名称" not in spot.columns:
        return ""
    rows = spot[spot["代码"].astype(str).str.strip().str.upper() == em_code.strip().upper()]
    return "" if rows.empty else str(rows.iloc[0]["名称"]).strip()


def _frame_from_eastmoney_index_hist(raw: Optional[pd.DataFrame], start: date, end: date) -> pd.DataFrame:
    """``index_global_hist_em`` daily bars (``日期 代码 名称 今开 最新价 最高 最低 振幅``, ``最新价`` = close).

    There is no previous-close / percent column: both come from the preceding bar, so the first row is
    dropped. Only bars inside ``start .. end`` are kept (the interface returns the whole history).
    """
    if raw is None or raw.empty or "日期" not in raw.columns:
        return pd.DataFrame(columns=_FRAME_COLUMNS)
    df = pd.DataFrame(index=raw.index)
    df["date"] = pd.to_datetime(raw["日期"], errors="coerce").dt.date
    for column, source in (("open", "今开"), ("high", "最高"), ("low", "最低"), ("close", "最新价")):
        df[column] = _numeric_column(raw, source)
    df["volume"] = float("nan")
    df["amount"] = float("nan")
    df = df.dropna(subset=["date", "close"]).drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df["pre_close"] = df["close"].shift(1)
    df["pct_chg"] = (df["close"] / df["pre_close"] - 1.0) * 100.0
    df = df.iloc[1:]
    df = df[(df["date"] >= start) & (df["date"] <= end)]
    return df[_FRAME_COLUMNS].reset_index(drop=True)


def _frame_from_yfinance(raw: Optional[pd.DataFrame]) -> pd.DataFrame:
    """yfinance daily bars: previous close from the preceding bar; the first bar has none and is dropped."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_FRAME_COLUMNS)
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column not in df.columns:
            df[column] = float("nan")
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["close"]).drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df["pre_close"] = df["close"].shift(1)
    df["pct_chg"] = (df["close"] / df["pre_close"] - 1.0) * 100.0
    df = df.iloc[1:]
    return df[_FRAME_COLUMNS].reset_index(drop=True)


def _completed_only(frame: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    if frame.empty:
        return frame
    return frame[frame["date"].map(lambda d: session_closed(d, as_of))].reset_index(drop=True)


def _with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """MA5/10/20 and volume ratio like ``BaseFetcher._calculate_indicators`` (rows stay usable by the analysis)."""
    df = df.sort_values("date").copy()
    for window in (5, 10, 20):
        df[f"ma{window}"] = df["close"].rolling(window=window, min_periods=1).mean().round(2)
    avg_volume = df["volume"].rolling(window=5, min_periods=1).mean()
    df["volume_ratio"] = (df["volume"] / avg_volume.shift(1)).fillna(1.0).round(2)
    return df
