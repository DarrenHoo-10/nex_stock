# -*- coding: utf-8 -*-
"""Persistence for board metrics, signals and runs (reads ``market_stock_daily`` facts).

Companion of ``market_snapshot_repo`` (ingestion). Tables touched here:
``board_daily`` (calculated columns), ``market_daily`` (calculated columns + the
``metrics`` quality block), ``board_catalog.merge_group``, ``daily_runs`` and
``board_signal``. Every write is one transaction and idempotent per trade date / run.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import delete, func, select

from src.storage import (
    BoardCatalog,
    BoardDaily,
    BoardMembership,
    BoardSignal,
    DailyRun,
    DatabaseManager,
    MarketDaily,
    MarketStockDaily,
    utc_naive_now,
)

logger = logging.getLogger(__name__)

BOARD_SOURCE_DC = "dc"
QUALITY_METRICS_KEY = "metrics"
RUN_OK_STATUSES = ("succeeded", "partial")

# Calculated ``board_daily`` columns (everything except the keys and the vendor reference).
BOARD_DAILY_CALC_COLUMNS: Tuple[str, ...] = (
    "member_count", "valid_member_count", "excluded_reason",
    "ret_cap", "ret_eq", "excess_ret", "excess_vs_median", "rank_pct",
    "ret_5d", "excess_5d", "rank_5d_pct",
    "amount", "amount_ma5_prev", "amount_ratio_5d", "amount_share", "amount_peak_20d",
    "up_count", "down_count", "up_ratio",
    "limit_up_count", "limit_down_count", "one_word_count", "touched_up_count",
    "max_consecutive", "break_rate", "drop7_ratio",
    "top_contrib_code", "top_contrib_share", "leader_code", "top_amount_code",
    "quality", "calc_version", "computed_at",
)
MARKET_DAILY_CALC_COLUMNS: Tuple[str, ...] = (
    "total_amount", "up_count", "down_count", "limit_up_count", "limit_down_count",
    "touched_up_count", "break_rate", "all_a_ret",
)
_STOCK_COLUMNS: Tuple[str, ...] = (
    "code", "name", "exchange_board", "is_st", "is_suspended", "list_date",
    "prev_close", "close", "pct_chg", "amount", "turnover_rate", "float_mv",
    "limit_state", "touched_limit_up", "open_times", "consecutive_up",
)


def loads_json(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def board_ref(board_type: str, code: str) -> str:
    """API id of a board: ``industry:BK0448`` (the Tushare ``.DC`` suffix is dropped)."""
    return f"{board_type}:{str(code).split('.')[0]}"


def _row_dict(obj: Any, columns: Iterable[str]) -> Dict[str, Any]:
    return {column: getattr(obj, column) for column in columns}


class BoardAnalysisRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # market facts (read)
    # ------------------------------------------------------------------
    def list_ingested_dates(self, upto: Optional[date] = None, limit: Optional[int] = None) -> List[date]:
        """Dates present in ``market_stock_daily``, newest first."""
        with self.db.get_session() as session:
            stmt = select(MarketStockDaily.trade_date).group_by(MarketStockDaily.trade_date)
            if upto is not None:
                stmt = stmt.where(MarketStockDaily.trade_date <= upto)
            stmt = stmt.order_by(MarketStockDaily.trade_date.desc())
            if limit:
                stmt = stmt.limit(limit)
            return list(session.execute(stmt).scalars())

    def load_stock_day(self, trade_date: date) -> List[Dict[str, Any]]:
        columns = [getattr(MarketStockDaily, name) for name in _STOCK_COLUMNS]
        with self.db.get_session() as session:
            rows = session.execute(select(*columns).where(MarketStockDaily.trade_date == trade_date)).all()
        return [dict(zip(_STOCK_COLUMNS, row)) for row in rows]

    def get_suspended_codes(self, trade_date: date) -> Set[str]:
        with self.db.get_session() as session:
            return set(
                session.execute(
                    select(MarketStockDaily.code).where(
                        MarketStockDaily.trade_date == trade_date, MarketStockDaily.is_suspended.is_(True)
                    )
                ).scalars()
            )

    def get_stock_rows(self, trade_date: date, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        if not codes:
            return {}
        columns = [getattr(MarketStockDaily, name) for name in _STOCK_COLUMNS]
        with self.db.get_session() as session:
            rows = session.execute(
                select(*columns).where(
                    MarketStockDaily.trade_date == trade_date, MarketStockDaily.code.in_(list(codes))
                )
            ).all()
        return {row[0]: dict(zip(_STOCK_COLUMNS, row)) for row in rows}

    def get_market_daily(self, trade_date: date) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(MarketDaily, trade_date)
            if row is None:
                return None
            data = _row_dict(
                row,
                ("trade_date", "benchmark_code", "benchmark_ret", "total_amount", "up_count", "down_count",
                 "limit_up_count", "limit_down_count", "touched_up_count", "break_rate", "all_a_ret"),
            )
            data["quality"] = loads_json(row.quality)
        return data

    def get_market_daily_range(self, dates: Sequence[date]) -> Dict[date, Dict[str, Any]]:
        if not dates:
            return {}
        with self.db.get_session() as session:
            rows = session.execute(select(MarketDaily).where(MarketDaily.trade_date.in_(list(dates)))).scalars().all()
            return {
                row.trade_date: {
                    **_row_dict(
                        row,
                        ("trade_date", "benchmark_code", "benchmark_ret", "total_amount", "up_count",
                         "down_count", "limit_up_count", "limit_down_count", "touched_up_count",
                         "break_rate", "all_a_ret"),
                    ),
                    "quality": loads_json(row.quality),
                }
                for row in rows
            }

    # ------------------------------------------------------------------
    # catalog / membership
    # ------------------------------------------------------------------
    def list_catalog(
        self,
        board_types: Optional[Sequence[str]] = None,
        source: str = BOARD_SOURCE_DC,
    ) -> List[Dict[str, Any]]:
        """All boards (any status); callers filter by the trade date they evaluate."""
        with self.db.get_session() as session:
            stmt = select(BoardCatalog).where(BoardCatalog.source == source)
            if board_types:
                stmt = stmt.where(BoardCatalog.board_type.in_(list(board_types)))
            rows = session.execute(stmt.order_by(BoardCatalog.id)).scalars().all()
            return [
                {
                    **_row_dict(
                        b,
                        ("id", "code", "name", "board_type", "level", "first_seen_date", "last_seen_date",
                         "status", "merge_group", "merge_computed_on", "membership_checked_date"),
                    ),
                    "ref": board_ref(b.board_type, b.code),
                }
                for b in rows
            ]

    def find_boards(self, query: str, source: str = BOARD_SOURCE_DC) -> List[Dict[str, Any]]:
        """Resolve ``industry:BK0448`` / ``BK0448`` / ``BK0448.DC`` / a name (exact, then substring)."""
        text = (query or "").strip()
        if not text:
            return []
        boards = self.list_catalog(source=source)
        by_ref = [b for b in boards if b["ref"].lower() == text.lower()]
        if by_ref:
            return by_ref
        code = text.split(":")[-1].split(".")[0].upper()
        by_code = [b for b in boards if b["code"].split(".")[0].upper() == code]
        if by_code:
            return by_code
        exact = [b for b in boards if b["name"] == text]
        if exact:
            return exact
        return [b for b in boards if text in b["name"]]

    def get_all_members_on(self, on_date: date) -> Dict[int, Dict[str, str]]:
        """``{board_id: {stock_code: origin}}`` for every board on ``on_date``."""
        with self.db.get_session() as session:
            rows = session.execute(
                select(BoardMembership.board_id, BoardMembership.stock_code, BoardMembership.origin).where(
                    BoardMembership.valid_from <= on_date,
                    (BoardMembership.valid_to.is_(None)) | (BoardMembership.valid_to >= on_date),
                )
            ).all()
        result: Dict[int, Dict[str, str]] = {}
        for board_id, code, origin in rows:
            result.setdefault(board_id, {})[code] = origin or "observed"
        return result

    def get_members_on(self, board_id: int, on_date: date) -> Set[str]:
        """Members of one board on ``on_date`` (same rule as the ingestion repository)."""
        from src.repositories.market_snapshot_repo import MarketSnapshotRepository

        return MarketSnapshotRepository(self.db).get_members_on(board_id, on_date)

    def set_merge_groups(self, on_date: date, groups: Dict[int, Optional[str]]) -> None:
        """Write ``merge_group`` / ``merge_computed_on`` for the given concept boards."""

        def _write(session) -> None:
            for board_id, group in groups.items():
                board = session.get(BoardCatalog, board_id)
                if board is not None:
                    board.merge_group = group
                    board.merge_computed_on = on_date

        self.db._run_write_transaction(f"set board merge groups[{on_date}]", _write)

    # ------------------------------------------------------------------
    # board_daily / market_daily (calculated columns)
    # ------------------------------------------------------------------
    def get_board_daily_range(
        self,
        dates: Sequence[date],
        board_ids: Optional[Sequence[int]] = None,
    ) -> Dict[Tuple[date, int], Dict[str, Any]]:
        if not dates:
            return {}
        columns = ("trade_date", "board_id", "vendor_ret", "vendor_amount") + BOARD_DAILY_CALC_COLUMNS
        with self.db.get_session() as session:
            stmt = select(BoardDaily).where(BoardDaily.trade_date.in_(list(dates)))
            if board_ids is not None:
                stmt = stmt.where(BoardDaily.board_id.in_(list(board_ids)))
            rows = session.execute(stmt).scalars().all()
            return {(row.trade_date, row.board_id): _row_dict(row, columns) for row in rows}

    def get_board_daily_series(self, board_id: int, upto: date, limit: int) -> List[Dict[str, Any]]:
        """Newest-first ``board_daily`` rows of one board up to ``upto``."""
        columns = ("trade_date", "board_id", "vendor_ret", "vendor_amount") + BOARD_DAILY_CALC_COLUMNS
        with self.db.get_session() as session:
            rows = session.execute(
                select(BoardDaily)
                .where(BoardDaily.board_id == board_id, BoardDaily.trade_date <= upto)
                .order_by(BoardDaily.trade_date.desc())
                .limit(limit)
            ).scalars().all()
            return [_row_dict(row, columns) for row in rows]

    def count_computed_board_rows(self, trade_date: date, calc_version: str) -> int:
        with self.db.get_session() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(BoardDaily)
                    .where(BoardDaily.trade_date == trade_date, BoardDaily.calc_version == calc_version)
                ).scalar_one()
            )

    def write_board_daily(self, trade_date: date, rows: Dict[int, Dict[str, Any]]) -> int:
        """Idempotent recompute write.

        Calculated columns of every existing row of the date are reset first, so a board
        that no longer qualifies does not keep stale numbers; vendor reference columns
        are never touched.
        """

        def _write(session) -> int:
            existing = {
                row.board_id: row
                for row in session.execute(select(BoardDaily).where(BoardDaily.trade_date == trade_date)).scalars()
            }
            for row in existing.values():
                for column in BOARD_DAILY_CALC_COLUMNS:
                    setattr(row, column, None)
            for board_id, values in rows.items():
                row = existing.get(board_id)
                if row is None:
                    row = BoardDaily(trade_date=trade_date, board_id=board_id)
                    session.add(row)
                for column in BOARD_DAILY_CALC_COLUMNS:
                    setattr(row, column, values.get(column))
            return len(rows)

        return self.db._run_write_transaction(f"write board_daily[{trade_date}]", _write)

    def write_market_metrics(self, trade_date: date, values: Dict[str, Any], quality_block: Dict[str, Any]) -> None:
        """Write calculated ``market_daily`` columns; ingestion columns and quality keys are kept."""

        def _write(session) -> None:
            row = session.get(MarketDaily, trade_date)
            if row is None:
                row = MarketDaily(trade_date=trade_date)
                session.add(row)
            for column in MARKET_DAILY_CALC_COLUMNS:
                setattr(row, column, values.get(column))
            if row.benchmark_code is None and values.get("benchmark_code"):
                row.benchmark_code = values["benchmark_code"]
            quality = loads_json(row.quality)
            quality[QUALITY_METRICS_KEY] = quality_block
            row.quality = dumps_json(quality)

        self.db._run_write_transaction(f"write market_daily metrics[{trade_date}]", _write)

    # ------------------------------------------------------------------
    # runs / signals
    # ------------------------------------------------------------------
    def start_run(self, trade_date: date, thresholds_snapshot: Dict[str, Any]) -> int:
        def _write(session) -> int:
            run = DailyRun(
                trade_date=trade_date,
                status="running",
                started_at=utc_naive_now(),
                thresholds_snapshot=dumps_json(thresholds_snapshot),
                steps=dumps_json([]),
            )
            session.add(run)
            session.flush()
            return int(run.id)

        return self.db._run_write_transaction(f"start daily_run[{trade_date}]", _write)

    def update_run(
        self,
        run_id: int,
        *,
        status: Optional[str] = None,
        steps: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        def _write(session) -> None:
            run = session.get(DailyRun, run_id)
            if run is None:
                raise ValueError(f"daily_runs id {run_id} 不存在")
            if status is not None:
                run.status = status
            if steps is not None:
                run.steps = dumps_json(steps)
            if error is not None:
                run.error = error
            if finished:
                run.finished_at = utc_naive_now()

        self.db._run_write_transaction(f"update daily_run[{run_id}]", _write)

    def find_run(
        self,
        trade_date: date,
        statuses: Optional[Sequence[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Newest run of the date (optionally restricted to ``statuses``)."""
        with self.db.get_session() as session:
            stmt = select(DailyRun).where(DailyRun.trade_date == trade_date)
            if statuses:
                stmt = stmt.where(DailyRun.status.in_(list(statuses)))
            run = session.execute(stmt.order_by(DailyRun.id.desc()).limit(1)).scalars().first()
            return self._run_dict(run) if run else None

    def latest_completed_run(self, trade_date: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """Newest completed (succeeded / partial) run, of ``trade_date`` or of the latest date."""
        with self.db.get_session() as session:
            stmt = select(DailyRun).where(DailyRun.status.in_(RUN_OK_STATUSES))
            if trade_date is not None:
                stmt = stmt.where(DailyRun.trade_date == trade_date)
                stmt = stmt.order_by(DailyRun.id.desc())
            else:
                stmt = stmt.order_by(DailyRun.trade_date.desc(), DailyRun.id.desc())
            run = session.execute(stmt.limit(1)).scalars().first()
            return self._run_dict(run) if run else None

    @staticmethod
    def _run_dict(run: DailyRun) -> Dict[str, Any]:
        return {
            "id": run.id,
            "trade_date": run.trade_date,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "thresholds_snapshot": loads_json(run.thresholds_snapshot),
            "steps": _loads_list(run.steps),
            "error": run.error,
        }

    def replace_signals(self, run_id: int, trade_date: date, rows: Sequence[Dict[str, Any]]) -> int:
        """Replace all signals of one run (idempotent inside the run)."""

        def _write(session) -> int:
            session.execute(delete(BoardSignal).where(BoardSignal.run_id == run_id))
            for item in rows:
                session.add(
                    BoardSignal(
                        run_id=run_id,
                        trade_date=trade_date,
                        board_id=item["board_id"],
                        signal_type=item["signal_type"],
                        flags=dumps_json(item.get("flags") or {}),
                        evidence=dumps_json(item.get("evidence") or {}),
                        driver=item.get("driver") or "unknown",
                        reason=item.get("reason") or "",
                    )
                )
            return len(rows)

        return self.db._run_write_transaction(f"replace board_signal[run {run_id}]", _write)

    def get_signals(self, run_id: int, board_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(BoardSignal).where(BoardSignal.run_id == run_id)
            if board_id is not None:
                stmt = stmt.where(BoardSignal.board_id == board_id)
            rows = session.execute(stmt.order_by(BoardSignal.id)).scalars().all()
            return [
                {
                    "board_id": row.board_id,
                    "trade_date": row.trade_date,
                    "signal_type": row.signal_type,
                    "flags": loads_json(row.flags),
                    "evidence": loads_json(row.evidence),
                    "driver": row.driver or "unknown",
                    "reason": row.reason or "",
                }
                for row in rows
            ]

    def prune_stale_running(self, older_than: timedelta, error: str) -> int:
        """Mark ``running`` runs older than ``older_than`` as failed (crashed process)."""
        cutoff = utc_naive_now() - older_than

        def _write(session) -> int:
            count = 0
            for run in session.execute(
                select(DailyRun).where(DailyRun.status == "running", DailyRun.started_at < cutoff)
            ).scalars():
                run.status = "failed"
                run.error = error
                run.finished_at = utc_naive_now()
                count += 1
            return count

        return self.db._run_write_transaction("prune stale daily_runs", _write)


def _loads_list(raw: Optional[str]) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
