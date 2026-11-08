# -*- coding: utf-8 -*-
"""Seeding helpers for the board metric / signal / anomaly tests (no network, in-memory DB).

``World`` builds a small universe straight into the local tables: 6 industry boards
(``I0``..``I5``) and 6 concept boards (``C0``..``C5``) with 5 members each. ``I0`` / ``C0``
are the *targets* whose daily return / turnover the tests script; the other boards are
"peers" with fixed small returns so a target return of a few percent ranks first and
a negative one ranks last (a pool of 6 boards: top = 91.7, bottom = 8.3 percentile).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from src.repositories.board_analysis_repo import BoardAnalysisRepository
from src.services.board_metrics_service import BoardMetricsService
from src.services.board_signal_service import BoardSignalService
from src.services.board_thresholds import effective_thresholds
from src.storage import (
    BoardCatalog,
    BoardMembership,
    DatabaseManager,
    MarketDaily,
    MarketStockDaily,
)

PEER_RETURNS = [-0.3, -0.1, 0.1, 0.3, 0.5]
END_DATE = date(2026, 11, 6)  # a Friday; Oct 19 - Nov 6 2026 has no exchange holiday


def weekdays(end: date, count: int) -> List[date]:
    days: List[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def thresholds(**overrides: Any) -> Dict[str, Any]:
    """Defaults with a small member floor so 5-stock boards qualify."""
    values = {"min_members": 3}
    values.update(overrides)
    return effective_thresholds(values)


class World:
    def __init__(
        self,
        db: DatabaseManager,
        days: int = 15,
        end: date = END_DATE,
        per_board: int = 5,
    ) -> None:
        self.db = db
        self.days = weekdays(end, days)
        self.per_board = per_board
        self.board_ids: Dict[str, int] = {}
        self.board_refs: Dict[str, str] = {}
        self.members: Dict[str, List[str]] = {}
        self.extra_stocks: Dict[str, Dict[str, Any]] = {}
        for index in range(6):
            self.add_board("I", index)
            self.add_board("C", index)

    # ------------------------------------------------------------------ boards
    def add_board(self, kind: str, index: int, first_seen: Optional[date] = None, name: Optional[str] = None,
                  member_from: Optional[date] = None) -> int:
        key = f"{kind}{index}"
        board_type = "industry" if kind == "I" else "concept"
        code_num = (1000 if kind == "I" else 2000) + index
        code = f"BK{code_num}.DC"
        first_seen = first_seen or self.days[0]
        codes = [f"{(10 if kind == 'I' else 20) + index:02d}{j:04d}" for j in range(self.per_board)]
        with self.db.session_scope() as session:
            board = BoardCatalog(
                source="dc", board_type=board_type, code=code, name=name or f"{board_type[:3]}{key}",
                first_seen_date=first_seen, last_seen_date=self.days[-1], status="active",
            )
            session.add(board)
            session.flush()
            board_id = board.id
            for stock in codes:
                session.add(BoardMembership(
                    board_id=board_id, stock_code=stock, valid_from=member_from or first_seen,
                    valid_to=None, origin="observed",
                ))
        self.board_ids[key] = board_id
        self.board_refs[key] = f"{board_type}:BK{code_num}"
        self.members[key] = codes
        return board_id

    def share_members(self, src: str, dst: str, count: int) -> None:
        """Make ``dst`` also contain the first ``count`` members of ``src``."""
        with self.db.session_scope() as session:
            for code in self.members[src][:count]:
                session.add(BoardMembership(
                    board_id=self.board_ids[dst], stock_code=code, valid_from=self.days[0], valid_to=None,
                    origin="observed",
                ))
        self.members[dst] = self.members[dst] + self.members[src][:count]

    # ------------------------------------------------------------------ seeding
    @staticmethod
    def peer_return(key: str) -> float:
        index = int(key[1:])
        return 0.0 if index == 0 else PEER_RETURNS[(index - 1) % len(PEER_RETURNS)]

    def seed_day(
        self,
        day: date,
        returns: Optional[Dict[str, float]] = None,
        amounts: Optional[Dict[str, float]] = None,
        *,
        benchmark: Optional[float] = 0.0,
        overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        float_mv: Optional[float] = 1e10,
        base_amount: float = 1e8,
        extra: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        returns = returns or {}
        amounts = amounts or {}
        overrides = overrides or {}
        rows: List[Dict[str, Any]] = []
        for key, codes in self.members.items():
            pct = returns.get(key, self.peer_return(key))
            multiplier = amounts.get(key, 1.0)
            for code in codes:
                row = self._stock_row(day, code, pct, base_amount * multiplier, float_mv)
                row.update(overrides.get(code, {}))
                if "pct_chg" in overrides.get(code, {}) and "close" not in overrides[code]:
                    row["close"] = round(10.0 * (1 + row["pct_chg"] / 100.0), 4)
                rows.append(row)
        for item in extra or []:
            row = self._stock_row(day, item["code"], item.get("pct_chg", 0.0), item.get("amount", base_amount), float_mv)
            row.update(item)
            rows.append(row)
        seen: Dict[str, Dict[str, Any]] = {}
        for row in rows:  # a stock shared by two boards is stored once
            seen[row["code"]] = row
        with self.db.session_scope() as session:
            session.query(MarketStockDaily).filter(MarketStockDaily.trade_date == day).delete()
            session.bulk_insert_mappings(MarketStockDaily, list(seen.values()))
            market = session.get(MarketDaily, day)
            if market is None:
                market = MarketDaily(trade_date=day)
                session.add(market)
            market.benchmark_code = "000985.CSI"
            market.benchmark_ret = benchmark

    @staticmethod
    def _stock_row(day: date, code: str, pct: float, amount: float, float_mv: Optional[float]) -> Dict[str, Any]:
        return {
            "trade_date": day, "code": code, "name": f"N{code}", "exchange_board": "main",
            "is_st": False, "is_suspended": False, "list_date": date(2010, 1, 1),
            "prev_close": 10.0, "close": None if pct is None else round(10.0 * (1 + pct / 100.0), 4),
            "pct_chg": pct, "amount": amount,
            "turnover_rate": 1.0, "float_mv": float_mv, "limit_state": "none", "touched_limit_up": False,
            "open_times": None, "consecutive_up": None, "data_source": "test",
        }

    def seed_flat(self, upto_index: int, **kwargs: Any) -> None:
        """Seed days[0:upto_index] with default (flat target, peer-spread) data."""
        for day in self.days[:upto_index]:
            self.seed_day(day, **kwargs)

    # ------------------------------------------------------------------ services
    def repo(self) -> BoardAnalysisRepository:
        return BoardAnalysisRepository(self.db)

    def metrics(self, th: Optional[Dict[str, Any]] = None, calendar_fn: Any = None) -> BoardMetricsService:
        return BoardMetricsService(repo=self.repo(), thresholds=th or thresholds(), calendar_fn=calendar_fn)

    def signals(self, th: Optional[Dict[str, Any]] = None, calendar_fn: Any = None) -> BoardSignalService:
        return BoardSignalService(repo=self.repo(), thresholds=th or thresholds(), calendar_fn=calendar_fn)

    def compute_all(self, th: Optional[Dict[str, Any]] = None, upto: Optional[date] = None) -> None:
        service = self.metrics(th)
        service.compute_range([d for d in self.days if upto is None or d <= upto])

    def judge(self, day: date, key: str, th: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Metrics for every seeded day, then the signal of board ``key`` on ``day``."""
        self.compute_all(th, upto=day)
        batch = self.signals(th).evaluate_trade_date(day)
        by_board = {s["board_id"]: s for s in batch.signals}
        signal = by_board[self.board_ids[key]]
        signal["_batch"] = batch
        return signal

    def board_daily(self, day: date, key: str) -> Dict[str, Any]:
        rows = self.repo().get_board_daily_range([day], [self.board_ids[key]])
        return rows[(day, self.board_ids[key])]
