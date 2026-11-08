# -*- coding: utf-8 -*-
"""Persistence for the board / whole-market snapshot tables.

Tables: ``board_catalog``, ``board_membership`` (zipper), ``market_stock_daily``,
``market_daily`` and the vendor reference columns of ``board_daily``
(see docs/design/core-backend-design.md section 2).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from sqlalchemy import delete, func, select, update

from src.storage import (
    BoardCatalog,
    BoardDaily,
    BoardMembership,
    DatabaseManager,
    MarketDaily,
    MarketStockDaily,
    utc_naive_now,
)

logger = logging.getLogger(__name__)

BOARD_SOURCE_DC = "dc"
QUALITY_INGESTION_KEY = "ingestion"


class MarketSnapshotRepository:
    """Idempotent writes for one trade date at a time; every write is one transaction."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------
    # market_stock_daily / market_daily
    # ------------------------------------------------------------------
    def replace_stock_day(self, trade_date: date, rows: Sequence[Dict[str, Any]]) -> int:
        """Replace all ``market_stock_daily`` rows of ``trade_date`` atomically."""
        values = [dict(row, trade_date=trade_date) for row in rows]

        def _write(session) -> int:
            session.execute(delete(MarketStockDaily).where(MarketStockDaily.trade_date == trade_date))
            if values:
                session.bulk_insert_mappings(MarketStockDaily, values)
            return len(values)

        return self.db._run_write_transaction(f"replace market_stock_daily[{trade_date}]", _write)

    def count_stock_rows(self, trade_date: date) -> int:
        with self.db.get_session() as session:
            return int(
                session.execute(
                    select(func.count()).select_from(MarketStockDaily).where(MarketStockDaily.trade_date == trade_date)
                ).scalar_one()
            )

    def get_market_daily_quality(self, trade_date: date) -> Dict[str, Any]:
        with self.db.get_session() as session:
            raw = session.execute(
                select(MarketDaily.quality).where(MarketDaily.trade_date == trade_date)
            ).scalar_one_or_none()
        return _loads(raw)

    def get_ingestion_quality(self, trade_date: date) -> Optional[Dict[str, Any]]:
        quality = self.get_market_daily_quality(trade_date).get(QUALITY_INGESTION_KEY)
        return quality if isinstance(quality, dict) else None

    def upsert_market_daily(
        self,
        trade_date: date,
        *,
        ingestion_quality: Dict[str, Any],
        benchmark_code: Optional[str] = None,
        benchmark_ret: Optional[float] = None,
    ) -> None:
        """Write ingestion fields and merge the ``ingestion`` quality block.

        Other top-level quality keys (owned by the metric calculation) are kept.
        A benchmark that is unavailable in this run does not wipe a previous value.
        """

        def _write(session) -> None:
            row = session.get(MarketDaily, trade_date)
            if row is None:
                row = MarketDaily(trade_date=trade_date)
                session.add(row)
            if benchmark_ret is not None:
                row.benchmark_code = benchmark_code
                row.benchmark_ret = benchmark_ret
            elif row.benchmark_code is None and benchmark_code:
                row.benchmark_code = benchmark_code
            quality = _loads(row.quality)
            quality[QUALITY_INGESTION_KEY] = ingestion_quality
            row.quality = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.db._run_write_transaction(f"upsert market_daily[{trade_date}]", _write)

    def merge_ingestion_quality(self, trade_date: date, patch: Dict[str, Any]) -> None:
        """Merge ``patch`` into the ``ingestion`` quality block of ``trade_date`` (row created if absent)."""

        def _write(session) -> None:
            row = session.get(MarketDaily, trade_date)
            if row is None:
                row = MarketDaily(trade_date=trade_date)
                session.add(row)
            quality = _loads(row.quality)
            block = quality.get(QUALITY_INGESTION_KEY)
            block = dict(block) if isinstance(block, dict) else {}
            block.update(patch)
            quality[QUALITY_INGESTION_KEY] = block
            row.quality = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.db._run_write_transaction(f"merge ingestion quality[{trade_date}]", _write)

    def merge_ingestion_quality_entry(self, trade_date: date, block: str, key: str, value: Any) -> None:
        """Set ``ingestion[block][key] = value`` for ``trade_date`` in one transaction.

        Lets independent writers (concept / industry membership runs of the same date) share one
        sub-block without overwriting each other. A non-dict ``block`` is replaced by a dict.
        """

        def _write(session) -> None:
            row = session.get(MarketDaily, trade_date)
            if row is None:
                row = MarketDaily(trade_date=trade_date)
                session.add(row)
            quality = _loads(row.quality)
            ingestion = quality.get(QUALITY_INGESTION_KEY)
            ingestion = dict(ingestion) if isinstance(ingestion, dict) else {}
            section = ingestion.get(block)
            section = dict(section) if isinstance(section, dict) else {}
            section[key] = value
            ingestion[block] = section
            quality[QUALITY_INGESTION_KEY] = ingestion
            row.quality = json.dumps(quality, ensure_ascii=False, sort_keys=True)

        self.db._run_write_transaction(f"merge ingestion quality[{trade_date}].{block}.{key}", _write)

    def get_previous_stock_day(self, trade_date: date) -> tuple:
        """``(date, {code: row})`` of the newest ingested day before ``trade_date`` (or ``(None, {})``).

        Only the columns needed to derive turnover / float cap / limit streaks are returned.
        """
        columns = ("code", "is_suspended", "close", "float_mv", "limit_state", "consecutive_up")
        with self.db.get_session() as session:
            prev = session.execute(
                select(func.max(MarketStockDaily.trade_date)).where(MarketStockDaily.trade_date < trade_date)
            ).scalar_one_or_none()
            if prev is None:
                return None, {}
            rows = session.execute(
                select(*[getattr(MarketStockDaily, c) for c in columns]).where(MarketStockDaily.trade_date == prev)
            ).all()
        return prev, {row[0]: dict(zip(columns, row)) for row in rows}

    def prune_stock_daily(self, keep_trading_days: int) -> int:
        """Delete ``market_stock_daily`` rows older than the newest ``keep_trading_days`` dates."""
        keep = max(int(keep_trading_days), 1)

        def _write(session) -> int:
            cutoff = session.execute(
                select(MarketStockDaily.trade_date)
                .group_by(MarketStockDaily.trade_date)
                .order_by(MarketStockDaily.trade_date.desc())
                .offset(keep - 1)
                .limit(1)
            ).scalar_one_or_none()
            if cutoff is None:
                return 0
            result = session.execute(delete(MarketStockDaily).where(MarketStockDaily.trade_date < cutoff))
            return int(result.rowcount or 0)

        return self.db._run_write_transaction("prune market_stock_daily", _write)

    # ------------------------------------------------------------------
    # board_catalog
    # ------------------------------------------------------------------
    def upsert_catalog(
        self,
        trade_date: date,
        board_type: str,
        rows: Iterable[Dict[str, Any]],
        source: str = BOARD_SOURCE_DC,
    ) -> Dict[str, int]:
        """Insert new boards / refresh seen boards listed on ``trade_date``."""
        items = [r for r in rows if r.get("code")]

        def _write(session) -> Dict[str, int]:
            existing = {
                b.code: b
                for b in session.execute(
                    select(BoardCatalog).where(
                        BoardCatalog.source == source, BoardCatalog.board_type == board_type
                    )
                ).scalars()
            }
            inserted = updated = 0
            for item in items:
                board = existing.get(item["code"])
                if board is None:
                    session.add(
                        BoardCatalog(
                            source=source,
                            board_type=board_type,
                            code=item["code"],
                            name=item.get("name") or item["code"],
                            level=item.get("level"),
                            first_seen_date=trade_date,
                            last_seen_date=trade_date,
                            status="active",
                        )
                    )
                    inserted += 1
                    continue
                if trade_date < board.first_seen_date:
                    board.first_seen_date = trade_date
                if trade_date >= board.last_seen_date:
                    board.last_seen_date = trade_date
                    board.status = "active"
                    if item.get("name"):
                        board.name = item["name"]
                    if item.get("level"):
                        board.level = item["level"]
                updated += 1
            return {"inserted": inserted, "updated": updated}

        return self.db._run_write_transaction(f"upsert board_catalog[{board_type}@{trade_date}]", _write)

    def retire_stale_boards(
        self,
        trade_date: date,
        board_types: Sequence[str],
        after_days: int,
        source: str = BOARD_SOURCE_DC,
    ) -> int:
        """Mark boards not listed for ``after_days`` calendar days as retired.

        Only evaluated when ``trade_date`` is the newest date seen for these board
        types, so back-filling an old date never retires anything.
        """
        if not board_types:
            return 0

        def _write(session) -> int:
            newest = session.execute(
                select(func.max(BoardCatalog.last_seen_date)).where(
                    BoardCatalog.source == source, BoardCatalog.board_type.in_(list(board_types))
                )
            ).scalar_one_or_none()
            if newest is None or trade_date < newest:
                return 0
            cutoff = trade_date - timedelta(days=after_days)
            result = session.execute(
                update(BoardCatalog)
                .where(
                    BoardCatalog.source == source,
                    BoardCatalog.board_type.in_(list(board_types)),
                    BoardCatalog.status == "active",
                    BoardCatalog.last_seen_date < cutoff,
                )
                .values(status="retired", updated_at=utc_naive_now())
            )
            return int(result.rowcount or 0)

        return self.db._run_write_transaction(f"retire board_catalog[{trade_date}]", _write)

    def get_board_code_map(self, source: str = BOARD_SOURCE_DC) -> Dict[str, int]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(BoardCatalog.code, BoardCatalog.id).where(BoardCatalog.source == source)
            ).all()
        return {code: board_id for code, board_id in rows}

    def list_boards(
        self,
        board_types: Optional[Sequence[str]] = None,
        status: Optional[str] = "active",
        source: str = BOARD_SOURCE_DC,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(BoardCatalog).where(BoardCatalog.source == source)
            if board_types:
                stmt = stmt.where(BoardCatalog.board_type.in_(list(board_types)))
            if status:
                stmt = stmt.where(BoardCatalog.status == status)
            rows = session.execute(stmt.order_by(BoardCatalog.id)).scalars().all()
            return [
                {
                    "id": b.id,
                    "code": b.code,
                    "name": b.name,
                    "board_type": b.board_type,
                    "first_seen_date": b.first_seen_date,
                    "last_seen_date": b.last_seen_date,
                    "status": b.status,
                    "membership_checked_date": b.membership_checked_date,
                }
                for b in rows
            ]

    # ------------------------------------------------------------------
    # board_daily (vendor reference columns only)
    # ------------------------------------------------------------------
    def upsert_board_vendor(self, trade_date: date, rows: Sequence[Dict[str, Any]]) -> int:
        """Write ``vendor_ret`` / ``vendor_amount``; leave calculated columns untouched."""

        def _write(session) -> int:
            existing = {
                r.board_id: r
                for r in session.execute(select(BoardDaily).where(BoardDaily.trade_date == trade_date)).scalars()
            }
            for item in rows:
                row = existing.get(item["board_id"])
                if row is None:
                    row = BoardDaily(trade_date=trade_date, board_id=item["board_id"])
                    session.add(row)
                row.vendor_ret = item.get("vendor_ret")
                row.vendor_amount = item.get("vendor_amount")
            return len(rows)

        return self.db._run_write_transaction(f"upsert board_daily vendor[{trade_date}]", _write)

    def count_board_vendor_rows(self, trade_date: date) -> int:
        with self.db.get_session() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(BoardDaily)
                    .where(BoardDaily.trade_date == trade_date, BoardDaily.vendor_ret.is_not(None))
                ).scalar_one()
            )

    # ------------------------------------------------------------------
    # board_membership (zipper table)
    # ------------------------------------------------------------------
    def apply_membership_snapshots(
        self,
        board_id: int,
        snapshots: Dict[date, Set[str]],
        origin: str = "observed",
    ) -> Dict[str, Any]:
        """Fold daily member sets into the zipper table (forward only).

        - Snapshot dates on or before the board's ``membership_checked_date`` are
          already folded in and skipped, so re-running a date is a no-op.
        - An empty snapshot is treated as "not observed" (vendor hole), never as
          "everyone left".
        - A stock missing from a snapshot gets ``valid_to`` = the previous snapshot
          date (its last observed day); a stock that returns later opens a new row.
        """

        def _write(session) -> Dict[str, Any]:
            board = session.get(BoardCatalog, board_id)
            if board is None:
                raise ValueError(f"board_catalog id {board_id} 不存在")
            checked = board.membership_checked_date
            open_rows = {
                row.stock_code: row
                for row in session.execute(
                    select(BoardMembership).where(
                        BoardMembership.board_id == board_id, BoardMembership.valid_to.is_(None)
                    )
                ).scalars()
            }
            prev = checked
            stats: Dict[str, Any] = {"opened": 0, "closed": 0, "skipped_dates": [], "empty_dates": [], "applied_dates": 0}
            for snap_date in sorted(snapshots):
                if checked is not None and snap_date <= checked:
                    stats["skipped_dates"].append(snap_date)
                    continue
                members = {str(code) for code in snapshots[snap_date]}
                if not members:
                    stats["empty_dates"].append(snap_date)
                    continue
                for code, row in list(open_rows.items()):
                    if code not in members:
                        row.valid_to = prev
                        del open_rows[code]
                        stats["closed"] += 1
                session.flush()
                for code in sorted(members):
                    if code not in open_rows:
                        row = BoardMembership(
                            board_id=board_id,
                            stock_code=code,
                            valid_from=snap_date,
                            valid_to=None,
                            origin=origin,
                            fetched_at=utc_naive_now(),
                        )
                        session.add(row)
                        open_rows[code] = row
                        stats["opened"] += 1
                prev = snap_date
                stats["applied_dates"] += 1
            if prev is not None and (checked is None or prev > checked):
                board.membership_checked_date = prev
            return stats

        return self.db._run_write_transaction(f"apply board_membership[{board_id}]", _write)

    def count_members_on(self, on_date: date, board_ids: Iterable[int]) -> int:
        """Number of (board, stock) membership pairs valid on ``on_date`` for the given boards."""
        ids = list(board_ids)
        if not ids:
            return 0
        with self.db.get_session() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(BoardMembership)
                    .where(
                        BoardMembership.board_id.in_(ids),
                        BoardMembership.valid_from <= on_date,
                        (BoardMembership.valid_to.is_(None)) | (BoardMembership.valid_to >= on_date),
                    )
                ).scalar_one()
            )

    def get_members_on(self, board_id: int, on_date: date) -> Set[str]:
        """Members of a board on ``on_date``: ``valid_from <= D and (valid_to is null or valid_to >= D)``."""
        with self.db.get_session() as session:
            rows = session.execute(
                select(BoardMembership.stock_code).where(
                    BoardMembership.board_id == board_id,
                    BoardMembership.valid_from <= on_date,
                    (BoardMembership.valid_to.is_(None)) | (BoardMembership.valid_to >= on_date),
                )
            ).scalars()
            return set(rows)


def _loads(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
