# -*- coding: utf-8 -*-
"""Whole-market / board snapshot ingestion (docs/design/core-backend-design.md section 2).

For one trade date this service:

1. fetches ``daily`` + ``daily_basic`` + ``limit_list_d`` + ``suspend_d`` + ``stock_basic``
   and writes ``market_stock_daily`` (amounts in yuan);
2. fetches the benchmark index and records it in ``market_daily``;
3. upserts ``board_catalog`` from ``dc_index`` and writes the vendor reference columns of
   ``board_daily`` from ``dc_daily``;
4. folds one day of ``dc_member`` into ``board_membership`` PER BOARD
   (``refresh_board_membership``: one ``dc_member(ts_code, trade_date)`` call per board, <= 5000
   rows each; concept boards daily in the 17:00 pipeline, industry boards weekly), or refreshes
   every board over a date range (``refresh_all_membership``, cold start / manual repair). The
   all-boards-in-one-query form (``refresh_membership_for_day``) is an opt-in fallback only: Tushare
   caps the paging offset at 100000 and one day has more than 105k rows, so it cannot finish.

Missing or late interfaces are recorded under ``market_daily.quality["ingestion"]`` and the
affected columns stay NULL; they are never written as zero. With ``allow_derived=True`` (the
17:00 board pipeline) a ``daily_basic`` / ``limit_list_d`` that is not published yet is
replaced by values derived from ``daily`` + ``stk_limit`` + the previous day's rows; the source
of each is recorded (``basic_source`` / ``limit_source``), the date gets the status
``provisional`` and is re-ingested with the real data on the next run
(``ingest_trade_date`` of that date again). A date whose ingestion is ``complete`` is skipped
on re-run (use ``force=True`` to re-ingest), so a resumed backfill makes no repeated requests.
Only the ingestion logic lives here; computing ``board_daily`` metrics and signals is out of scope.
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set
from zoneinfo import ZoneInfo

import pandas as pd

from data_provider.base import DataFetchError, is_st_stock
from data_provider.tushare_market_data import (
    DC_IDX_TYPE_BY_BOARD_TYPE,
    IncompleteMarketDataError,
    infer_exchange_board,
)
from src.config import get_config
from src.repositories.market_snapshot_repo import MarketSnapshotRepository
from src.services.board_common import CalendarFn, default_calendar_fn
from src.storage import utc_naive_now

logger = logging.getLogger(__name__)

DATA_SOURCE = "tushare"
# dc_member 只有 2024-12-20 以来的逐日成分。
DC_MEMBER_START_DATE = date(2024, 12, 20)
# 第一版只做行业和概念板块，地域板块不参与异动判定。
INGEST_BOARD_TYPES = ("industry", "concept")
# 连续这么多个自然日没有出现在 dc_index 的板块视为下线。
BOARD_RETIRE_AFTER_DAYS = 10
STOCK_BASIC_TTL = timedelta(days=7)
# 推算换手率 / 流通市值最多回看多少个自然日（更早的上一份快照股本可能已变）。
DERIVE_MAX_GAP_DAYS = 10
# A 股最小报价单位 0.01 元，涨跌停判断留半个 tick 的容差。
LIMIT_PRICE_TOLERANCE = 0.005
# 全量成分条数低于上一次成分条数的这个比例，视为接口返回不完整，不折叠进拉链表。
MEMBERSHIP_MIN_RATIO = 0.5
# 单个板块当前成分至少这么多只时才做「骤减」校验（小板块波动大，不适用）。
MEMBERSHIP_SHRINK_GUARD_MIN = 10
# 逐板块刷新连续失败这么多个板块后中止（接口不可用 / 权限不足时不把几百次请求都耗光）。
MEMBERSHIP_MAX_CONSECUTIVE_FAILURES = 10
# 质量记录里最多列出多少个失败 / 空返回的板块代码。
MEMBERSHIP_QUALITY_CODE_LIMIT = 20
# market_daily.quality.ingestion.membership: one sub-record per scope ("concept" / "industry" / "full_day").
MEMBERSHIP_FULL_DAY_KEY = "full_day"
_EMPTY_RESPONSE = "empty response"
# B 股代码段（上交所 900、深交所 200），不属于「全部 A 股」。
_B_SHARE_PREFIXES = ("900", "200")

# daily_basic.limit_status: 0 平盘 1 上涨(不含涨停) 2 涨停 3 一字涨停 4 下跌 5 跌停 6 一字跌停
_LIMIT_STATE_BY_STATUS = {
    0: "none",
    1: "none",
    2: "up",
    3: "one_word_up",
    4: "none",
    5: "down",
    6: "one_word_down",
}
_UP_STATES = ("up", "one_word_up")


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return datetime.strptime(text, "%Y-%m-%d").date()


def _china_today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _clean(value: Any) -> Any:
    """NaN / NaT / pd.NA -> None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _float(value: Any) -> Optional[float]:
    value = _clean(value)
    if value is None:
        return None
    number = float(value)
    return None if math.isinf(number) else number


def _int(value: Any) -> Optional[int]:
    number = _float(value)
    return None if number is None else int(number)


def _derive_stock_fields(code: str, d: Dict[str, Any], is_st: bool, ctx: "_Derivation") -> Dict[str, Any]:
    """Derive the ``daily_basic`` / ``limit_list_d`` fields of one stock from its ``daily`` row.

    * float cap = previous float cap x (1 + pct_chg / 100); turnover (%) = ``vol`` (lots of 100
      shares) / float shares, float shares = previous float cap / previous close.
    * limit state / touched: close / high against ``stk_limit`` prices with a half-tick tolerance;
      ``touched`` is only set for non-ST stocks (``limit_list_d`` does not cover ST).
    * consecutive limit-up days continue the previous day's streak only when the previous row is
      the previous session, not suspended and has a known state; otherwise they stay unknown.
    """
    out: Dict[str, Any] = {}
    prev = ctx.prev_rows.get(code)
    close, high, low, open_ = (_float(d.get(k)) for k in ("close", "high", "low", "open"))

    if ctx.cap and prev is not None:
        prev_mv, prev_close = _float(prev.get("float_mv")), _float(prev.get("close"))
        pct = _float(d.get("pct_chg"))
        if prev_mv is not None and prev_mv > 0:
            if pct is not None and pct > -100.0:
                out["float_mv"] = prev_mv * (1.0 + pct / 100.0)
            vol = _float(d.get("vol"))
            if vol is not None and prev_close is not None and prev_close > 0:
                out["turnover_rate"] = vol * 1e4 / (prev_mv / prev_close)

    price = ctx.prices.get(code)
    if price is None:
        return out
    up, down = price
    tol = LIMIT_PRICE_TOLERANCE
    if ctx.state and close is not None:
        if close >= up - tol:
            at_limit = open_ is not None and low is not None and open_ >= up - tol and low >= up - tol
            out["limit_state"] = "one_word_up" if at_limit else "up"
        elif close <= down + tol:
            at_limit = open_ is not None and high is not None and open_ <= down + tol and high <= down + tol
            out["limit_state"] = "one_word_down" if at_limit else "down"
        else:
            out["limit_state"] = "none"

    if ctx.limits and not is_st and high is not None:
        out["touched_limit_up"] = high >= up - tol
        if close is not None and close >= up - tol and ctx.contiguous and prev is not None:
            prev_state = prev.get("limit_state")
            if not prev.get("is_suspended") and prev_state is not None:
                if prev_state in _UP_STATES:
                    streak = _int(prev.get("consecutive_up"))
                    out["consecutive_up"] = streak + 1 if streak is not None else None
                else:
                    out["consecutive_up"] = 1
    return out


@dataclass
class IngestReport:
    """Outcome of ingesting one trade date."""

    trade_date: date
    # complete / provisional (derived fields or boards pending) / partial / no_data / skipped
    status: str = "pending"
    stock_rows: int = 0
    catalog_inserted: int = 0
    vendor_rows: int = 0
    pruned_rows: int = 0
    missing: List[str] = field(default_factory=list)
    incomplete: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    derived: List[str] = field(default_factory=list)  # interfaces replaced by derived values
    boards_done: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "status": self.status,
            "stock_rows": self.stock_rows,
            "catalog_inserted": self.catalog_inserted,
            "vendor_rows": self.vendor_rows,
            "pruned_rows": self.pruned_rows,
            "missing": list(self.missing),
            "incomplete": list(self.incomplete),
            "errors": dict(self.errors),
            "derived": sorted(set(self.derived)),
            "boards_done": self.boards_done,
        }


@dataclass
class _Derivation:
    """Inputs for deriving ``daily_basic`` / ``limit_list_d`` fields from ``daily`` data."""

    prev_day: Optional[date]
    prev_rows: Dict[str, Dict[str, Any]]
    contiguous: bool  # prev_day is the session right before the day being ingested
    prices: Dict[str, Any]  # code -> (up_limit, down_limit)
    cap: bool  # derive turnover_rate / float_mv from the previous day
    state: bool  # derive limit_state
    limits: bool  # derive touched_limit_up / consecutive_up


@dataclass
class BackfillReport:
    """Outcome of a cold-start backfill run."""

    dates: List[str] = field(default_factory=list)
    ingested: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    no_data: List[str] = field(default_factory=list)
    partial: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    aborted: bool = False
    membership: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dates": self.dates,
            "ingested": self.ingested,
            "skipped": self.skipped,
            "no_data": self.no_data,
            "partial": self.partial,
            "failed": self.failed,
            "aborted": self.aborted,
            "membership": self.membership,
        }


class MarketSnapshotService:
    """Orchestrates Tushare fetches and the snapshot repository."""

    def __init__(
        self,
        fetcher: Optional[Any] = None,
        repo: Optional[MarketSnapshotRepository] = None,
        config: Optional[Any] = None,
        calendar_fn: Optional[CalendarFn] = None,
    ) -> None:
        self._config = config or get_config()
        self._fetcher = fetcher
        self._repo = repo
        self._calendar_fn = calendar_fn or default_calendar_fn
        self._stock_basic: Optional[Dict[str, Dict[str, Any]]] = None
        self._stock_basic_at: Optional[datetime] = None
        self._stock_basic_misses: Set[str] = set()

    # ------------------------------------------------------------------
    # 依赖
    # ------------------------------------------------------------------
    @property
    def repo(self) -> MarketSnapshotRepository:
        if self._repo is None:
            self._repo = MarketSnapshotRepository()
        return self._repo

    @property
    def fetcher(self) -> Any:
        if self._fetcher is None:
            from data_provider.tushare_fetcher import TushareFetcher

            fetcher = TushareFetcher(
                rate_limit_per_minute=self._config.market_snapshot_requests_per_minute
            )
            if not fetcher.is_available():
                raise DataFetchError("Tushare 未配置（缺少 TUSHARE_TOKEN），无法获取板块 / 全市场快照")
            self._fetcher = fetcher
        return self._fetcher

    # ------------------------------------------------------------------
    # 每日入库
    # ------------------------------------------------------------------
    def ingest_trade_date(
        self,
        trade_date: Any,
        *,
        force: bool = False,
        allow_derived: bool = False,
        boards: bool = True,
    ) -> IngestReport:
        """Ingest one trade date; idempotent and safe to re-run.

        ``allow_derived`` replaces a not-yet-published ``daily_basic`` / ``limit_list_d`` by
        derived values (status ``provisional``). ``boards=False`` skips ``dc_index`` /
        ``dc_daily`` (published ~20:30, after the 17:00 run); the date then stays
        ``provisional`` until it is ingested again with ``boards=True``.
        """
        day = _as_date(trade_date)
        report = IngestReport(trade_date=day)

        previous = self.repo.get_ingestion_quality(day)
        if (
            not force
            and previous
            and previous.get("status") == "complete"
            and self.repo.count_stock_rows(day) > 0
        ):
            report.status = "skipped"
            report.stock_rows = int(previous.get("stock_rows") or 0)
            logger.info("[MarketSnapshot] %s 已完整入库，跳过", day)
        else:
            self._ingest_stocks_and_boards(day, report, allow_derived=allow_derived, boards=boards)
        return report

    def _ingest_stocks_and_boards(
        self,
        day: date,
        report: IngestReport,
        *,
        allow_derived: bool = False,
        boards: bool = True,
    ) -> None:
        fetcher = self.fetcher

        daily = self._guard(report, "daily", lambda: fetcher.fetch_market_daily(day))
        if daily is None or daily.empty:
            # 没有日线就没有可写的快照（非交易日或数据未入库）；不动已有数据。
            report.status = "no_data"
            logger.warning("[MarketSnapshot] %s daily 无数据，未入库: %s", day, report.errors.get("daily"))
            return

        basic = self._guard(report, "daily_basic", lambda: fetcher.fetch_market_daily_basic(day))
        limits = self._guard(report, "limit_list_d", lambda: fetcher.fetch_limit_list(day))
        suspended = self._guard(
            report, "suspend_d", lambda: fetcher.fetch_suspended(day), empty_is_missing=False
        )
        stock_basic = self._get_stock_basic(set(daily["code"]), report)

        derivation: Optional[_Derivation] = None
        if allow_derived and (basic is None or limits is None):
            derivation = self._prepare_derivation(
                day, report, derive_basic=basic is None, derive_limits=limits is None
            )

        rows = self._build_stock_rows(
            day,
            daily=daily,
            basic=basic,
            limits=limits,
            limits_complete="limit_list_d" not in report.incomplete,
            suspended=suspended,
            stock_basic=stock_basic,
            derivation=derivation,
        )
        report.stock_rows = self.repo.replace_stock_day(day, rows)

        benchmark_code = self._config.market_benchmark_code
        benchmark_ret = self._fetch_benchmark(day, benchmark_code, report)

        report.boards_done = boards
        if boards:
            self._ingest_boards(day, report)

        derived = sorted(set(report.derived))
        previous_quality = self.repo.get_ingestion_quality(day) or {}
        ingestion_quality = {
            "status": self._ingestion_status(report),
            "missing": sorted(set(report.missing)),
            "incomplete": sorted(set(report.incomplete)),
            "errors": dict(report.errors),
            "derived": derived,
            "basic_source": "derived" if "daily_basic" in derived else ("daily_basic" if basic is not None else None),
            "limit_source": "derived" if "limit_list_d" in derived else ("limit_list_d" if limits is not None else None),
            "derived_from": derivation.prev_day.isoformat() if derived and derivation and derivation.prev_day else None,
            "boards": "done" if boards else "pending",
            "stock_rows": report.stock_rows,
            "vendor_rows": report.vendor_rows,
            "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        }
        if isinstance(previous_quality.get("membership"), dict):  # membership is independent of the stock facts
            ingestion_quality["membership"] = previous_quality["membership"]
        self.repo.upsert_market_daily(
            day,
            ingestion_quality=ingestion_quality,
            benchmark_code=benchmark_code,
            benchmark_ret=benchmark_ret,
        )
        report.status = ingestion_quality["status"]
        report.pruned_rows = self.repo.prune_stock_daily(self._config.market_snapshot_retention_days)
        logger.info(
            "[MarketSnapshot] %s 入库完成: status=%s rows=%d missing=%s incomplete=%s derived=%s",
            day, report.status, report.stock_rows, report.missing, report.incomplete, derived,
        )

    @staticmethod
    def _ingestion_status(report: IngestReport) -> str:
        if report.missing or report.incomplete:
            return "partial"
        if report.derived or not report.boards_done:
            return "provisional"  # nothing failed; fields are derived / boards come with the next run
        return "complete"

    # ------------------------------------------------------------------
    # 取数辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _guard(
        report: IngestReport,
        name: str,
        call: Callable[[], pd.DataFrame],
        *,
        empty_is_missing: bool = True,
    ) -> Optional[pd.DataFrame]:
        """Run one interface; record failures as quality flags instead of raising.

        A single failed interface must not take down the whole day (spec 7: fallback
        stability). The failure is kept in ``report`` and persisted in ``market_daily.quality``.
        """
        try:
            df = call()
        except IncompleteMarketDataError as exc:
            report.incomplete.append(name)
            report.errors[name] = str(exc)[:200]
            logger.warning("[MarketSnapshot] %s 数据不完整: %s", name, exc)
            return exc.partial
        except Exception as exc:  # noqa: BLE001 - 取数异常统一降级为质量标记
            report.missing.append(name)
            report.errors[name] = str(exc)[:200]
            logger.warning("[MarketSnapshot] %s 获取失败: %s", name, exc)
            return None
        if empty_is_missing and (df is None or df.empty):
            report.missing.append(name)
            report.errors[name] = _EMPTY_RESPONSE
            return None
        return df

    def _get_stock_basic(self, needed: Set[str], report: IngestReport) -> Dict[str, Dict[str, Any]]:
        now = datetime.now()
        cache = self._stock_basic
        fresh = (
            cache is not None
            and self._stock_basic_at is not None
            and now - self._stock_basic_at < STOCK_BASIC_TTL
        )
        if fresh and not (needed - set(cache) - self._stock_basic_misses):
            return cache  # type: ignore[return-value]
        return self.refresh_stock_basic(needed=needed, report=report)

    def refresh_stock_basic(
        self,
        needed: Optional[Set[str]] = None,
        report: Optional[IngestReport] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch ``stock_basic`` (weekly cadence via the in-process TTL cache)."""
        fetcher = self.fetcher
        try:
            df = fetcher.fetch_stock_basic_snapshot()
            if df is None or df.empty:
                raise DataFetchError("stock_basic 返回为空")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MarketSnapshot] stock_basic 获取失败: %s", exc)
            if report is not None:
                report.missing.append("stock_basic")
                report.errors["stock_basic"] = str(exc)[:200]
            return self._stock_basic or {}
        cache = {rec["code"]: rec for rec in df.to_dict("records")}
        self._stock_basic = cache
        self._stock_basic_at = datetime.now()
        self._stock_basic_misses = set(needed or ()) - set(cache)
        return cache

    def _fetch_benchmark(self, day: date, code: str, report: IngestReport) -> Optional[float]:
        df = self._guard(report, "index_daily", lambda: self.fetcher.fetch_index_daily(code, day, day))
        if df is None:
            return None
        match = df[df["trade_date"] == day]
        if match.empty:
            report.missing.append("index_daily")
            report.errors["index_daily"] = f"{code} 当日无行情"
            return None
        return _float(match.iloc[-1]["pct_chg"])

    # ------------------------------------------------------------------
    # market_stock_daily 行合并
    # ------------------------------------------------------------------
    @staticmethod
    def _build_stock_rows(
        day: date,
        *,
        daily: pd.DataFrame,
        basic: Optional[pd.DataFrame],
        limits: Optional[pd.DataFrame],
        limits_complete: bool,
        suspended: Optional[pd.DataFrame],
        stock_basic: Dict[str, Dict[str, Any]],
        derivation: Optional[_Derivation] = None,
    ) -> List[Dict[str, Any]]:
        daily_by_code = {rec["code"]: rec for rec in daily.to_dict("records")}
        basic_by_code = (
            {rec["code"]: rec for rec in basic.to_dict("records")} if basic is not None else {}
        )
        suspended_codes = set(suspended["code"]) if suspended is not None and not suspended.empty else set()

        limit_by_code: Dict[str, Dict[str, Any]] = {}
        if limits is not None and not limits.empty:
            for rec in limits.to_dict("records"):
                current = limit_by_code.get(rec["code"])
                # 同一只股票若同时在多个榜，优先保留涨停 / 炸板记录。
                if current is None or (current.get("limit") == "D" and rec.get("limit") in ("U", "Z")):
                    limit_by_code[rec["code"]] = rec
        limits_known = limits is not None  # 接口取到了（含不完整的部分榜单）

        codes = sorted(
            code
            for code in set(daily_by_code) | suspended_codes
            if not code.startswith(_B_SHARE_PREFIXES)
        )
        ingested_at = utc_naive_now()
        rows: List[Dict[str, Any]] = []
        for code in codes:
            d = daily_by_code.get(code)
            b = basic_by_code.get(code)
            lim = limit_by_code.get(code)
            sb = stock_basic.get(code)

            name = _clean(sb.get("name")) if sb else None
            if not name and lim:
                name = _clean(lim.get("name"))
            is_st = is_st_stock(name or "")
            # 全天停牌：daily 不返回该股；盘中停牌的股票 daily 仍有行情，不算停牌行。
            is_suspended = d is None and code in suspended_codes

            turnover_rate = _float(b.get("turnover_rate")) if b else None
            float_mv = _float(b.get("float_mv")) if b else None
            derived_fields: Dict[str, Any] = {}
            if derivation is not None and d is not None:
                derived_fields = _derive_stock_fields(code, d, is_st, derivation)
                if derivation.cap and b is None:
                    turnover_rate = derived_fields.get("turnover_rate")
                    float_mv = derived_fields.get("float_mv")

            limit_state: Optional[str] = None
            if b is not None:
                status = _int(b.get("limit_status"))
                limit_state = _LIMIT_STATE_BY_STATUS.get(status) if status is not None else None
            if limit_state is None and derivation is not None and derivation.state and b is None:
                limit_state = derived_fields.get("limit_state")
            if limit_state is None and lim is not None:
                limit_state = {"U": "up", "D": "down", "Z": "none"}.get(lim.get("limit"))

            touched: Optional[bool] = None
            open_times: Optional[int] = None
            consecutive_up: Optional[int] = None
            if not is_st:  # limit_list_d 不统计 ST，ST 的涨停相关字段保持为空
                if lim is not None:
                    touched = lim.get("limit") in ("U", "Z")
                    if touched:
                        open_times = _int(lim.get("open_times"))
                        consecutive_up = _int(lim.get("limit_times"))
                elif limits_known and limits_complete:
                    touched = False
                elif derivation is not None and derivation.limits and limits is None:
                    # limit_list_d 尚未发布：由日线高点与涨停价推算；open_times 需要榜单，保持为空
                    touched = derived_fields.get("touched_limit_up")
                    consecutive_up = derived_fields.get("consecutive_up")

            rows.append(
                {
                    "code": code,
                    "name": name,
                    "exchange_board": (sb.get("exchange_board") if sb else None) or infer_exchange_board(code),
                    "is_st": bool(is_st),
                    "is_suspended": bool(is_suspended),
                    "list_date": _clean(sb.get("list_date")) if sb else None,
                    "prev_close": _float(d.get("pre_close")) if d else None,
                    "close": _float(d.get("close")) if d else None,
                    "pct_chg": _float(d.get("pct_chg")) if d else None,
                    "amount": _float(d.get("amount")) if d else None,
                    "turnover_rate": turnover_rate,
                    "float_mv": float_mv,
                    "limit_state": limit_state,
                    "touched_limit_up": touched,
                    "open_times": open_times,
                    "consecutive_up": consecutive_up,
                    "data_source": DATA_SOURCE,
                    "ingested_at": ingested_at,
                }
            )
        return rows

    # ------------------------------------------------------------------
    # 推算：daily_basic / limit_list_d 尚未发布时（17:00 运行）
    # ------------------------------------------------------------------
    def _prepare_derivation(
        self,
        day: date,
        report: IngestReport,
        *,
        derive_basic: bool,
        derive_limits: bool,
    ) -> Optional[_Derivation]:
        """Collect the inputs for deriving the unpublished interfaces; ``None`` if nothing is derivable.

        * ``daily_basic`` (turnover, float cap, limit state) needs the previous ingested day
          (within ``DERIVE_MAX_GAP_DAYS``) for the share count / cap, and ``stk_limit`` for the
          limit state.
        * ``limit_list_d`` (touched limit-up, streak) needs ``stk_limit`` and the daily high.
        An interface counts as derived only when the substitution really happened; a failed
        request (as opposed to an empty, not-yet-published response) stays in ``missing``.
        """
        prev_day, prev_rows = self.repo.get_previous_stock_day(day)
        if prev_day is not None and (day - prev_day).days > DERIVE_MAX_GAP_DAYS:
            prev_day, prev_rows = None, {}

        prices: Dict[str, Any] = {}
        frame = self._guard(report, "stk_limit", lambda: self.fetcher.fetch_stk_limit(day))
        if frame is not None:
            for rec in frame.to_dict("records"):
                up, down = _float(rec.get("up_limit")), _float(rec.get("down_limit"))
                if up is not None and down is not None and up > 0 and down > 0 and up > down:
                    prices[rec["code"]] = (up, down)

        cap = derive_basic and bool(prev_rows)
        state = derive_basic and cap and bool(prices)
        limits = derive_limits and bool(prices)
        if not (cap or limits):
            return None
        for name, used in (("daily_basic", cap), ("limit_list_d", limits)):
            if used:
                report.derived.append(name)
                if report.errors.get(name) == _EMPTY_RESPONSE:  # not published yet, not a failure
                    report.errors.pop(name, None)
                    if name in report.missing:
                        report.missing.remove(name)
        return _Derivation(
            prev_day=prev_day,
            prev_rows=prev_rows,
            contiguous=prev_day is not None and self._is_previous_session(prev_day, day),
            prices=prices,
            cap=cap,
            state=state,
            limits=limits,
        )

    def _is_previous_session(self, prev_day: date, day: date) -> bool:
        """True when no trading session lies between ``prev_day`` and ``day``."""
        sessions = None
        try:
            sessions = self._calendar_fn(prev_day, day)
        except Exception as exc:  # noqa: BLE001 - the calendar is best effort
            logger.debug("trading calendar lookup failed: %s", exc)
        if sessions:
            return not any(prev_day < s < day for s in sessions)
        gap = (day - prev_day).days  # no calendar: only unambiguous gaps count (no holidays assumed)
        return gap == 1 or (gap == 3 and day.weekday() == 0)

    # ------------------------------------------------------------------
    # 板块目录 + 板块行情参考列
    # ------------------------------------------------------------------
    def _ingest_boards(self, day: date, report: IngestReport) -> None:
        fetcher = self.fetcher
        succeeded_types: List[str] = []
        for board_type in INGEST_BOARD_TYPES:
            idx_type = DC_IDX_TYPE_BY_BOARD_TYPE[board_type]
            df = self._guard(
                report, f"dc_index:{board_type}", lambda t=idx_type: fetcher.fetch_dc_index(day, t)
            )
            if df is None:
                continue
            items = [
                {"code": rec["ts_code"], "name": _clean(rec.get("name")), "level": _clean(rec.get("level"))}
                for rec in df.to_dict("records")
            ]
            stats = self.repo.upsert_catalog(day, board_type, items)
            report.catalog_inserted += stats["inserted"]
            succeeded_types.append(board_type)
        if succeeded_types:
            self.repo.retire_stale_boards(day, succeeded_types, BOARD_RETIRE_AFTER_DAYS)

        vendor = self._guard(report, "dc_daily", lambda: fetcher.fetch_dc_daily(day))
        if vendor is None:
            return
        code_to_id = self.repo.get_board_code_map()
        rows = [
            {
                "board_id": code_to_id[rec["ts_code"]],
                "vendor_ret": _float(rec.get("pct_change")),
                "vendor_amount": _float(rec.get("amount")),
            }
            for rec in vendor.to_dict("records")
            if rec["ts_code"] in code_to_id  # 地域板块等未入目录的板块不写
        ]
        report.vendor_rows = self.repo.upsert_board_vendor(day, rows)

    # ------------------------------------------------------------------
    # 板块成分（拉链表）
    # ------------------------------------------------------------------
    @contextmanager
    def _members_rate_limit(self) -> Iterator[None]:
        """Use ``MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE`` for the per-board ``dc_member`` calls.

        The fetcher's limiter (``MARKET_SNAPSHOT_REQUESTS_PER_MINUTE``, default 60) stays in force for
        every other interface; only the per-board membership loop (~500 tiny calls) gets the higher cap,
        and the previous cap is restored afterwards. Fetchers without a limiter attribute are untouched.
        """
        try:
            fetcher = self.fetcher
        except DataFetchError:  # no token: the caller reports it when it first uses the fetcher
            yield
            return
        configured = getattr(self._config, "market_snapshot_members_requests_per_minute", None)
        previous = getattr(fetcher, "rate_limit_per_minute", None)
        applicable = isinstance(configured, int) and not isinstance(configured, bool) and configured >= 1
        if previous is None or not applicable:
            yield
            return
        fetcher.rate_limit_per_minute = configured
        try:
            yield
        finally:
            fetcher.rate_limit_per_minute = previous

    def refresh_daily_membership(self, trade_date: Any, *, progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
        """The 17:00 pipeline's membership step: fold ``trade_date`` (D-1) for the active concept boards.

        One ``dc_member(ts_code, trade_date)`` per board (see ``refresh_board_membership``). Only when
        ``BOARD_MEMBERSHIP_FULL_DAY_FALLBACK`` is on and that refresh produced nothing (status
        ``failed`` / ``error``) is the all-boards query tried; its result then carries
        ``fallback=True`` and the per-board result under ``per_board``.
        """
        result = self.refresh_board_membership(trade_date, "concept", progress=progress)
        if result.get("status") not in ("failed", "error"):
            return result
        if not getattr(self._config, "board_membership_full_day_fallback", False):
            return result
        logger.warning("[MarketSnapshot] 逐板块成分刷新失败，按 BOARD_MEMBERSHIP_FULL_DAY_FALLBACK 尝试全量成分查询")
        fallback = self.refresh_membership_for_day(trade_date)
        fallback["fallback"] = True
        fallback["per_board"] = {
            k: result.get(k) for k in ("status", "ok", "failed", "empty", "skipped", "error") if k in result
        }
        return fallback

    def refresh_board_membership(
        self,
        trade_date: Any,
        board_type: str = "concept",
        *,
        max_consecutive_failures: int = MEMBERSHIP_MAX_CONSECUTIVE_FAILURES,
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Fold one trade date of ``dc_member`` PER BOARD for the active boards of ``board_type``.

        One ``dc_member(ts_code=<board>, trade_date=D)`` call per board (<= 5000 rows, so no paging
        beyond Tushare's 100000 offset cap), through the membership rate limit
        (``MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE``) with the fetcher's rate-limit / timeout
        backoff. Rules, per board:

        * ``membership_checked_date >= D``: already folded, no request (``skipped``) - re-running a day
          is free and resumes where an interrupted run stopped;
        * the call fails (``DataFetchError`` / ``IncompleteMarketDataError`` / network), or returns
          fewer than ``MEMBERSHIP_MIN_RATIO`` of a board that has >= ``MEMBERSHIP_SHRINK_GUARD_MIN``
          members: the board keeps its existing membership, nothing is closed (``failed``);
        * the call returns no rows for D: existing membership is kept (``empty``);
        * otherwise the members fold into the zipper table (``ok``).

        ``max_consecutive_failures`` boards in a row failing aborts the run (Tushare down / no
        permission); the untouched boards are reported as ``remaining``. The summary is stored under
        ``market_daily.quality["ingestion"]["membership"][board_type]`` of D and returned:
        ``{"status": ok / skipped / partial / failed / error / no_boards, "boards", "ok", "failed",
        "empty", "skipped", "rows", "opened", "closed", ...}``. ``status`` is ``ok`` when boards folded
        and none failed (boards with no rows are normal: retired / not published), ``partial`` when
        some failed, ``failed`` when no board could be folded (all failed or all empty).
        """
        day = _as_date(trade_date)
        base = {"board_type": board_type, "trade_date": day.isoformat()}
        if day < DC_MEMBER_START_DATE:
            return dict(base, status="skipped", reason=f"dc_member 只有 {DC_MEMBER_START_DATE} 以来的逐日成分")
        boards = self.repo.list_boards(board_types=(board_type,), status="active")
        if not boards:
            return dict(base, status="no_boards", reason="板块目录为空")
        pending = [b for b in boards if b["membership_checked_date"] is None or b["membership_checked_date"] < day]
        counts = {"boards": len(boards), "ok": 0, "failed": 0, "empty": 0, "skipped": len(boards) - len(pending)}
        if not pending:
            return dict(base, status="skipped", reason="该日成分已折叠入拉链表", rows=0, opened=0, closed=0, **counts)
        try:
            with self._members_rate_limit():
                result = self._fold_boards(day, pending, counts, max_consecutive_failures, progress)
        except DataFetchError as exc:  # Tushare unavailable / not configured: nothing was requested
            return dict(base, status="error", error=str(exc)[:200], **counts)
        result = dict(base, **result)
        result["at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        try:
            self.repo.merge_ingestion_quality_entry(day, "membership", board_type, result)
        except Exception as exc:  # noqa: BLE001 - the record is bookkeeping, not the data
            logger.warning("[MarketSnapshot] %s %s 成分状态写入失败: %s", day, board_type, exc)
        logger.info(
            "[MarketSnapshot] %s %s 成分刷新: status=%s ok=%d failed=%d empty=%d skipped=%d opened=%d closed=%d",
            day, board_type, result["status"], result["ok"], result["failed"], result["empty"],
            result["skipped"], result["opened"], result["closed"],
        )
        return result

    def _fold_boards(
        self,
        day: date,
        pending: List[Dict[str, Any]],
        counts: Dict[str, int],
        max_consecutive_failures: int,
        progress: Optional[Callable[[str], None]],
    ) -> Dict[str, Any]:
        fetcher = self.fetcher  # raises DataFetchError when TUSHARE_TOKEN is missing
        rows = opened = closed = consecutive = 0
        failed_codes: List[str] = []
        empty_codes: List[str] = []
        errors: Dict[str, str] = {}
        aborted = False
        attempted = 0
        for board in pending:
            attempted += 1
            outcome = self._fold_one_board_day(fetcher, board, day)
            status = outcome["status"]
            if status == "ok":
                counts["ok"] += 1
                rows += outcome["rows"]
                opened += outcome["opened"]
                closed += outcome["closed"]
                consecutive = 0
            elif status == "empty":
                counts["empty"] += 1
                empty_codes.append(board["code"])
                consecutive = 0
            else:
                counts["failed"] += 1
                failed_codes.append(board["code"])
                errors.setdefault(board["code"], outcome.get("error", ""))
                consecutive += 1
                if consecutive >= max_consecutive_failures:
                    aborted = True
                    logger.error("[MarketSnapshot] 成分刷新连续失败 %d 个板块，中止", consecutive)
                    break
            if progress:
                progress(f"membership {attempted}/{len(pending)} {board['code']} {status}")
        if counts["ok"] == 0:
            overall = "failed"  # nothing could be folded: failures, or every board answered empty (not published yet)
        elif counts["failed"] > 0:
            overall = "partial"
        else:
            overall = "ok"
        result: Dict[str, Any] = {
            "status": overall,
            **counts,
            "rows": rows,
            "opened": opened,
            "closed": closed,
            "aborted": aborted,
            "remaining": len(pending) - attempted,
        }
        if failed_codes:
            result["failed_boards"] = failed_codes[:MEMBERSHIP_QUALITY_CODE_LIMIT]
            result["errors"] = {code: errors[code] for code in failed_codes[:3]}
        if empty_codes:
            result["empty_boards"] = empty_codes[:MEMBERSHIP_QUALITY_CODE_LIMIT]
        return result

    def _fold_one_board_day(self, fetcher: Any, board: Dict[str, Any], day: date) -> Dict[str, Any]:
        """One board, one day: ``ok`` / ``empty`` / ``failed``; never raises, never closes rows on a bad answer."""
        code = board["code"]
        try:
            df = fetcher.fetch_dc_member(code, trade_date=day)
        except Exception as exc:  # noqa: BLE001 - one board must not take the batch down
            logger.warning("[MarketSnapshot] %s %s 成分获取失败，保留现有成分: %s", day, code, exc)
            return {"status": "failed", "error": str(exc)[:200]}
        members: Set[str] = set()
        if df is not None and not df.empty:
            members = {rec["code"] for rec in df.to_dict("records") if rec["trade_date"] == day}
        if not members:
            return {"status": "empty"}
        checked = board["membership_checked_date"]
        if checked is not None:
            current = self.repo.count_members_on(checked, [board["id"]])
            if current >= MEMBERSHIP_SHRINK_GUARD_MIN and len(members) < current * MEMBERSHIP_MIN_RATIO:
                reason = f"成分 {len(members)} 只，现有 {current} 只，判定不完整"
                logger.warning("[MarketSnapshot] %s %s %s，保留现有成分", day, code, reason)
                return {"status": "failed", "error": reason}
        try:
            stats = self.repo.apply_membership_snapshots(board["id"], {day: members})
        except Exception as exc:  # noqa: BLE001 - e.g. SQLite lock: the board stays as it was
            logger.warning("[MarketSnapshot] %s %s 成分写入失败: %s", day, code, exc)
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}
        return {"status": "ok", "rows": len(members), "opened": stats["opened"], "closed": stats["closed"]}

    def refresh_membership_for_day(self, trade_date: Any, *, force: bool = False) -> Dict[str, Any]:
        """Opt-in FALLBACK: fold one day of ``dc_member`` for every board from one all-boards query.

        ``dc_member(trade_date=D)`` without ``ts_code`` returns all boards' members in one paged
        query, but Tushare rejects paging offsets above 100000 ("offset不能大于100000") and one day
        has more than 105k rows, so it normally ends ``incomplete`` / ``error``. The default path is
        ``refresh_board_membership`` (one call per board); this method only runs when
        ``BOARD_MEMBERSHIP_FULL_DAY_FALLBACK`` is on and the per-board concept refresh failed
        outright. Safety rails, in this order:

        * an ``IncompleteMarketDataError`` (paging could not be confirmed) or an empty response
          changes nothing;
        * fewer (board, stock) pairs than ``MEMBERSHIP_MIN_RATIO`` of the pairs known at the last
          folded snapshot are treated as an incomplete answer: yesterday's membership is kept and
          no zipper row is closed;
        * a date already folded (``quality["ingestion"]["membership"]["full_day"]["status"] == "ok"``)
          is skipped without a request unless ``force``.

        The outcome is stored under ``market_daily.quality["ingestion"]["membership"]["full_day"]``
        of that date. Returns ``{"status": ok / skipped / incomplete / empty / error / no_boards, ...}``.
        """
        day = _as_date(trade_date)
        if day < DC_MEMBER_START_DATE:
            return {"status": "skipped", "reason": f"dc_member 只有 {DC_MEMBER_START_DATE} 以来的逐日成分"}
        membership_quality = (self.repo.get_ingestion_quality(day) or {}).get("membership")
        marker = membership_quality.get(MEMBERSHIP_FULL_DAY_KEY) if isinstance(membership_quality, dict) else None
        if not force and isinstance(marker, dict) and marker.get("status") == "ok":
            return dict(marker, status="skipped", reason="该日成分已折叠入拉链表")

        boards = self.repo.list_boards(board_types=INGEST_BOARD_TYPES, status="active")
        if not boards:
            return {"status": "no_boards", "reason": "板块目录为空"}
        by_code = {b["code"]: b for b in boards}
        checked = [b["membership_checked_date"] for b in boards if b["membership_checked_date"] is not None]
        baseline_date = max(checked) if checked else None
        baseline = self.repo.count_members_on(baseline_date, [b["id"] for b in boards]) if baseline_date else 0

        result = self._fold_member_day(day, by_code, baseline_date, baseline)
        result["at"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        try:
            self.repo.merge_ingestion_quality_entry(day, "membership", MEMBERSHIP_FULL_DAY_KEY, result)
        except Exception as exc:  # noqa: BLE001 - the marker is bookkeeping, not the data
            logger.warning("[MarketSnapshot] %s 成分状态写入失败: %s", day, exc)
        return result

    def _fold_member_day(
        self,
        day: date,
        by_code: Dict[str, Dict[str, Any]],
        baseline_date: Optional[date],
        baseline: int,
    ) -> Dict[str, Any]:
        try:
            df = self.fetcher.fetch_dc_member(None, trade_date=day)
        except IncompleteMarketDataError as exc:
            logger.warning("[MarketSnapshot] %s 全量成分翻页不完整，保留上一日成分: %s", day, exc.reason)
            return {"status": "incomplete", "reason": exc.reason, "rows": len(exc.partial), "baseline": baseline}
        except Exception as exc:  # noqa: BLE001 - recorded on the pipeline step
            logger.warning("[MarketSnapshot] %s 全量成分获取失败: %s", day, exc)
            return {"status": "error", "error": str(exc)[:200]}
        if df is None or df.empty:
            return {"status": "empty", "baseline": baseline}

        snapshots: Dict[int, Set[str]] = {}
        unknown = 0
        for rec in df.to_dict("records"):
            if rec["trade_date"] != day:
                continue
            board = by_code.get(rec["ts_code"])
            if board is None:
                unknown += 1  # 地域板块或目录里没有的板块
                continue
            snapshots.setdefault(board["id"], set()).add(rec["code"])
        pairs = sum(len(members) for members in snapshots.values())
        if pairs == 0:
            return {"status": "empty", "rows": 0, "baseline": baseline}
        if baseline and pairs < baseline * MEMBERSHIP_MIN_RATIO:
            logger.warning(
                "[MarketSnapshot] %s 全量成分只有 %d 条，上一份 (%s) 为 %d 条，判定不完整，保留上一日成分",
                day, pairs, baseline_date, baseline,
            )
            return {
                "status": "incomplete",
                "reason": f"成分条数 {pairs} 低于上一份 {baseline} 的 {MEMBERSHIP_MIN_RATIO:.0%}",
                "rows": pairs,
                "baseline": baseline,
                "baseline_date": baseline_date.isoformat() if baseline_date else None,
            }

        opened = closed = 0
        for board_id, members in snapshots.items():
            stats = self.repo.apply_membership_snapshots(board_id, {day: members})
            opened += stats["opened"]
            closed += stats["closed"]
        return {
            "status": "ok",
            "boards": len(snapshots),
            "rows": pairs,
            "unknown_rows": unknown,
            "opened": opened,
            "closed": closed,
            "baseline": baseline,
        }

    def refresh_all_membership(
        self,
        end_date: Any,
        *,
        board_types: Sequence[str] = INGEST_BOARD_TYPES,
        max_consecutive_failures: int = 5,
        progress: Optional[Callable[[str], None]] = None,
        since: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Cold-start / manual-repair refresh: each active board once, covering every date since
        its last refresh (or ``since``, default 2024-12-20). Resumable: finished boards are not
        re-requested.

        ``since`` bounds a never-refreshed board's history to the backfill window: fetching every
        board back to 2024-12-20 is thousands of paged requests and tens of millions of rows, while
        metrics only need membership for the dates being backfilled.

        The daily pipeline uses ``refresh_daily_membership`` (concept boards, one day) and the weekly
        job ``refresh_board_membership`` (industry boards, one day) instead. Calls go through the
        membership rate limit (``MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE``)."""
        end = _as_date(end_date)
        floor = DC_MEMBER_START_DATE if since is None else max(DC_MEMBER_START_DATE, _as_date(since))
        boards = self.repo.list_boards(board_types=board_types, status="active")
        summary: Dict[str, Any] = {"boards": len(boards), "refreshed": 0, "up_to_date": 0, "failed": {}, "aborted": False}
        consecutive_failures = 0
        with self._members_rate_limit():
            for index, board in enumerate(boards, start=1):
                checked = board["membership_checked_date"]
                start = floor if checked is None else checked + timedelta(days=1)
                if start > end:
                    summary["up_to_date"] += 1
                    continue
                result = self._refresh_one_board(board, start=start, end=end)
                if result["status"] == "error":
                    summary["failed"][board["code"]] = result.get("error", "")
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        summary["aborted"] = True
                        logger.error("[MarketSnapshot] 成分刷新连续失败 %d 次，中止", consecutive_failures)
                        break
                else:
                    consecutive_failures = 0
                    summary["refreshed"] += 1
                if progress:
                    progress(f"membership {index}/{len(boards)} {board['code']} {result['status']}")
        return summary

    def _fetch_member_range(self, code: str, start: date, end: date) -> Any:
        """``dc_member`` for one board over a date range, halving the range on Tushare's offset cap.

        Tushare rejects paging past offset 100000 ("offset不能大于100000"). Very large boards
        (沪股通、深股通、融资融券… with thousands of members) exceed it over a 60-day range, so
        the range is split until each piece fits. A single day that still exceeds it is re-raised.
        """
        try:
            return self.fetcher.fetch_dc_member(code, start_date=start, end_date=end)
        except Exception as exc:  # noqa: BLE001 - only the offset cap is handled here
            if "offset" not in str(exc) or start >= end:
                raise
        middle = start + timedelta(days=(end - start).days // 2)
        logger.info("[MarketSnapshot] %s 成分区间超出 Tushare 分页上限，拆分为 %s~%s 与 %s~%s",
                    code, start, middle, middle + timedelta(days=1), end)
        parts = [self._fetch_member_range(code, start, middle),
                 self._fetch_member_range(code, middle + timedelta(days=1), end)]
        frames = [p for p in parts if p is not None and not p.empty]
        if not frames:
            return parts[0]
        return pd.concat(frames, ignore_index=True)

    def _refresh_one_board(
        self,
        board: Dict[str, Any],
        *,
        start: date,
        end: date,
    ) -> Dict[str, Any]:
        try:
            df = self._fetch_member_range(board["code"], start, end)
        except Exception as exc:  # noqa: BLE001 - 单个板块失败不拖垮整批
            logger.warning("[MarketSnapshot] %s 成分获取失败: %s", board["code"], exc)
            return {"status": "error", "error": str(exc)[:200]}
        if df is None or df.empty:
            return {"status": "empty"}
        snapshots: Dict[date, Set[str]] = {}
        for rec in df.to_dict("records"):
            snapshots.setdefault(rec["trade_date"], set()).add(rec["code"])
        stats = self.repo.apply_membership_snapshots(board["id"], snapshots)
        return {
            "status": "ok",
            "opened": stats["opened"],
            "closed": stats["closed"],
            "applied_dates": stats["applied_dates"],
        }

    # ------------------------------------------------------------------
    # 冷启动回填（可断点续跑）
    # ------------------------------------------------------------------
    def backfill(
        self,
        *,
        end_date: Optional[Any] = None,
        days: Optional[int] = None,
        include_members: bool = True,
        force: bool = False,
        max_consecutive_failures: int = 3,
        progress: Optional[Callable[[str], None]] = None,
    ) -> BackfillReport:
        """Backfill the last ``days`` trading days, then board membership for the same window.

        Progress lives in the database itself: a date with a ``complete`` ingestion record is
        skipped without any request, and a board whose ``membership_checked_date`` has reached
        the end date is skipped, so re-running after an interruption resumes where it stopped.
        """
        report = BackfillReport()
        retention = int(self._config.market_snapshot_retention_days)
        wanted = int(days if days is not None else self._config.market_snapshot_backfill_days)
        if wanted > retention:
            logger.warning("[MarketSnapshot] 回填天数 %d 超过保留天数 %d，按保留天数回填", wanted, retention)
            wanted = retention
        end = _as_date(end_date) if end_date is not None else _china_today()

        calendar_start = end - timedelta(days=int(wanted * 1.7) + 14)
        trading_dates = self.fetcher.fetch_trading_dates(calendar_start, end)
        window = trading_dates[-wanted:]
        report.dates = [d.isoformat() for d in window]
        if not window:
            raise DataFetchError("trade_cal 没有返回交易日，无法回填")

        consecutive_failures = 0
        for index, day in enumerate(window, start=1):
            try:
                result = self.ingest_trade_date(day, force=force)
            except Exception as exc:  # noqa: BLE001 - 记录并继续，连续失败才中止
                report.failed[day.isoformat()] = str(exc)[:200]
                consecutive_failures += 1
                logger.warning("[MarketSnapshot] 回填 %s 失败: %s", day, exc)
                if consecutive_failures >= max_consecutive_failures:
                    report.aborted = True
                    break
                continue
            consecutive_failures = 0
            if result.status == "skipped":
                report.skipped.append(day.isoformat())
            elif result.status == "no_data":
                report.no_data.append(day.isoformat())
            else:
                report.ingested.append(day.isoformat())
                if result.status == "partial":
                    report.partial.append(day.isoformat())
            if progress:
                progress(f"stocks {index}/{len(window)} {day} {result.status}")

        if include_members and not report.aborted:
            # 成分截止到最后一个确有数据的交易日（当天数据未入库时不把它当终点）。
            unavailable = set(report.no_data) | set(report.failed)
            member_end = next((d for d in reversed(window) if d.isoformat() not in unavailable), None)
            if member_end is not None:
                # Membership history only for the backfilled window, not back to 2024-12-20.
                report.membership = self.refresh_all_membership(member_end, progress=progress, since=window[0])
        return report
