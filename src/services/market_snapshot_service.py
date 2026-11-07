# -*- coding: utf-8 -*-
"""Whole-market / board snapshot ingestion (docs/design/core-backend-design.md section 2).

For one trade date this service:

1. fetches ``daily`` + ``daily_basic`` + ``limit_list_d`` + ``suspend_d`` + ``stock_basic``
   and writes ``market_stock_daily`` (amounts in yuan);
2. fetches the benchmark index and records it in ``market_daily``;
3. upserts ``board_catalog`` from ``dc_index`` and writes the vendor reference columns of
   ``board_daily`` from ``dc_daily``;
4. optionally refreshes ``board_membership`` for a list of candidate boards (daily) or for
   all boards (weekly / cold start).

Missing or late interfaces are recorded under ``market_daily.quality["ingestion"]`` and the
affected columns stay NULL; they are never written as zero. A date whose ingestion is
``complete`` is skipped on re-run (use ``force=True`` to re-ingest), so a resumed backfill
makes no repeated requests. Only the ingestion logic lives here; computing ``board_daily``
metrics and signals is out of scope.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set
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


@dataclass
class IngestReport:
    """Outcome of ingesting one trade date."""

    trade_date: date
    status: str = "pending"  # complete / partial / no_data / skipped
    stock_rows: int = 0
    catalog_inserted: int = 0
    vendor_rows: int = 0
    pruned_rows: int = 0
    missing: List[str] = field(default_factory=list)
    incomplete: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    membership: Dict[str, Dict[str, Any]] = field(default_factory=dict)

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
            "membership": self.membership,
        }


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
    ) -> None:
        self._config = config or get_config()
        self._fetcher = fetcher
        self._repo = repo
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
        candidate_board_ids: Optional[Sequence[int]] = None,
        force: bool = False,
    ) -> IngestReport:
        """Ingest one trade date; idempotent and safe to re-run."""
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
            self._ingest_stocks_and_boards(day, report)

        if candidate_board_ids and report.status != "no_data":
            report.membership = self.refresh_membership_for_date(day, candidate_board_ids)
        return report

    def _ingest_stocks_and_boards(self, day: date, report: IngestReport) -> None:
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

        rows = self._build_stock_rows(
            day,
            daily=daily,
            basic=basic,
            limits=limits,
            limits_complete="limit_list_d" not in report.incomplete,
            suspended=suspended,
            stock_basic=stock_basic,
        )
        report.stock_rows = self.repo.replace_stock_day(day, rows)

        benchmark_code = self._config.market_benchmark_code
        benchmark_ret = self._fetch_benchmark(day, benchmark_code, report)

        self._ingest_boards(day, report)

        ingestion_quality = {
            "status": "complete" if not report.missing and not report.incomplete else "partial",
            "missing": sorted(set(report.missing)),
            "incomplete": sorted(set(report.incomplete)),
            "errors": dict(report.errors),
            "stock_rows": report.stock_rows,
            "vendor_rows": report.vendor_rows,
            "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        }
        self.repo.upsert_market_daily(
            day,
            ingestion_quality=ingestion_quality,
            benchmark_code=benchmark_code,
            benchmark_ret=benchmark_ret,
        )
        report.status = ingestion_quality["status"]
        report.pruned_rows = self.repo.prune_stock_daily(self._config.market_snapshot_retention_days)
        logger.info(
            "[MarketSnapshot] %s 入库完成: status=%s rows=%d missing=%s incomplete=%s",
            day, report.status, report.stock_rows, report.missing, report.incomplete,
        )

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
            report.errors[name] = "empty response"
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

            limit_state: Optional[str] = None
            if b is not None:
                status = _int(b.get("limit_status"))
                limit_state = _LIMIT_STATE_BY_STATUS.get(status) if status is not None else None
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
                    "turnover_rate": _float(b.get("turnover_rate")) if b else None,
                    "float_mv": _float(b.get("float_mv")) if b else None,
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
    def refresh_membership_for_date(
        self,
        trade_date: Any,
        board_ids: Iterable[int],
    ) -> Dict[str, Dict[str, Any]]:
        """Daily refresh: one ``dc_member`` request per candidate board for ``trade_date``."""
        day = _as_date(trade_date)
        by_id = {b["id"]: b for b in self.repo.list_boards(status=None)}
        results: Dict[str, Dict[str, Any]] = {}
        for board_id in board_ids:
            board = by_id.get(board_id)
            if board is None:
                results[str(board_id)] = {"status": "unknown_board"}
                continue
            results[board["code"]] = self._refresh_one_board(
                board, start=day, end=day, single_date=True
            )
        return results

    def refresh_all_membership(
        self,
        end_date: Any,
        *,
        board_types: Sequence[str] = INGEST_BOARD_TYPES,
        max_consecutive_failures: int = 5,
        progress: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Weekly / cold-start refresh: each active board once, covering every date since
        its last refresh (or 2024-12-20). Resumable: finished boards are not re-requested."""
        end = _as_date(end_date)
        boards = self.repo.list_boards(board_types=board_types, status="active")
        summary: Dict[str, Any] = {"boards": len(boards), "refreshed": 0, "up_to_date": 0, "failed": {}, "aborted": False}
        consecutive_failures = 0
        for index, board in enumerate(boards, start=1):
            checked = board["membership_checked_date"]
            start = DC_MEMBER_START_DATE if checked is None else checked + timedelta(days=1)
            if start > end:
                summary["up_to_date"] += 1
                continue
            result = self._refresh_one_board(board, start=start, end=end, single_date=False)
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

    def _refresh_one_board(
        self,
        board: Dict[str, Any],
        *,
        start: date,
        end: date,
        single_date: bool,
    ) -> Dict[str, Any]:
        try:
            if single_date:
                df = self.fetcher.fetch_dc_member(board["code"], trade_date=end)
            else:
                df = self.fetcher.fetch_dc_member(board["code"], start_date=start, end_date=end)
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
        """Backfill the last ``days`` trading days, then board membership since 2024-12-20.

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
                report.membership = self.refresh_all_membership(member_end, progress=progress)
        return report
