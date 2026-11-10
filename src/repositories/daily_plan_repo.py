# -*- coding: utf-8 -*-
"""Persistence for the next-day plan: account snapshot, runs and generated stock plans.

Also the read-side queries the plan needs on the local market tables (``market_stock_daily``, ``stock_daily``,
``board_membership``); nothing here fetches from the network. Every write is one transaction through
``DatabaseManager._run_write_transaction``.

Account, runs and stock plans are personal data (docs/design/multi-user.md section 4): every method that touches
them takes an explicit ``user_id`` and filters / stamps rows with it. The market-data queries below are shared.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import and_, delete, func, select

from src.repositories.board_analysis_repo import board_ref
from src.storage import (
    BoardCatalog,
    BoardMembership,
    DailyPlanAccount,
    DailyPlanHolding,
    DailyPlanRun,
    DailyPlanStock,
    DatabaseManager,
    MarketStockDaily,
    StockDaily,
    utc_naive_now,
)

ACTIVE_RUN_STATUSES = ("pending", "running")


def _iso_utc(value: Any) -> Optional[str]:
    return f"{value.isoformat()}Z" if value is not None else None


def _loads(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class DailyPlanRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------ account
    def get_account(self, user_id: str) -> Dict[str, Any]:
        """``{"cash", "updated_at", "holdings": [...]}``; never saved -> cash 0, no holdings, ``updated_at`` None."""
        with self.db.get_session() as session:
            account = session.execute(
                select(DailyPlanAccount).where(DailyPlanAccount.user_id == user_id)
            ).scalar_one_or_none()
            rows = session.execute(
                select(DailyPlanHolding)
                .where(DailyPlanHolding.user_id == user_id)
                .order_by(DailyPlanHolding.position, DailyPlanHolding.id)
            ).scalars().all()
            return {
                "cash": float(account.cash) if account else 0.0,
                "updated_at": _iso_utc(account.updated_at) if account else None,
                "holdings": [
                    {"code": r.code, "shares": int(r.shares), "cost": float(r.cost), "buy_date": r.buy_date,
                     "horizon": r.horizon}
                    for r in rows
                ],
            }

    def replace_account(self, user_id: str, cash: float, holdings: Sequence[Dict[str, Any]]) -> None:
        """Replace the whole snapshot (cash + holdings) of one user in one transaction."""
        def _write(session) -> None:
            now = utc_naive_now()
            account = session.execute(
                select(DailyPlanAccount).where(DailyPlanAccount.user_id == user_id)
            ).scalar_one_or_none()
            if account is None:
                session.add(DailyPlanAccount(user_id=user_id, cash=float(cash), updated_at=now))
            else:
                account.cash = float(cash)
                account.updated_at = now
            session.execute(delete(DailyPlanHolding).where(DailyPlanHolding.user_id == user_id))
            for position, item in enumerate(holdings):
                session.add(DailyPlanHolding(
                    user_id=user_id, code=item["code"], shares=int(item["shares"]), cost=float(item["cost"]),
                    buy_date=item.get("buy_date"), horizon=item.get("horizon"), position=position,
                ))

        self.db._run_write_transaction(f"replace plan account[{user_id}]", _write)

    # ------------------------------------------------------------------ runs
    def create_run(self, user_id: str, run_id: str, trade_date: date, trigger: str, steps: List[Dict[str, Any]],
                   push: Dict[str, Any]) -> None:
        def _write(session) -> None:
            session.add(DailyPlanRun(
                user_id=user_id, run_id=run_id, trade_date=trade_date, status="running", trigger=trigger,
                started_at=utc_naive_now(), steps_json=_dumps(steps), push_json=_dumps(push),
                failures_json="[]", model_calls_json="[]", data_gaps_json="[]",
            ))

        self.db._run_write_transaction(f"create plan_run[{run_id}]", _write)

    _RUN_FIELDS = {
        "status", "finished_at", "stock_total", "stock_succeeded", "stock_failed", "push_preview", "error",
    }
    _RUN_JSON_FIELDS = {
        "steps": "steps_json", "failures": "failures_json", "push": "push_json",
        "model_calls": "model_calls_json", "data_gaps": "data_gaps_json",
    }

    def update_run(self, user_id: str, run_id: str, **fields: Any) -> None:
        def _write(session) -> None:
            row = session.execute(
                select(DailyPlanRun).where(DailyPlanRun.run_id == run_id, DailyPlanRun.user_id == user_id)
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(f"plan_run {run_id} 不存在")
            for key, value in fields.items():
                if key in self._RUN_FIELDS:
                    setattr(row, key, value)
                elif key in self._RUN_JSON_FIELDS:
                    setattr(row, self._RUN_JSON_FIELDS[key], _dumps(value))

        self.db._run_write_transaction(f"update plan_run[{run_id}]", _write)

    def get_run(self, user_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """The run of ``user_id`` with this id; another user's run id is simply not found."""
        with self.db.get_session() as session:
            row = session.execute(
                select(DailyPlanRun).where(DailyPlanRun.run_id == run_id, DailyPlanRun.user_id == user_id)
            ).scalar_one_or_none()
            return self._run_dict(row) if row is not None else None

    def latest_run(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(DailyPlanRun).where(DailyPlanRun.user_id == user_id).order_by(DailyPlanRun.id.desc()).limit(1)
            ).scalar_one_or_none()
            return self._run_dict(row) if row is not None else None

    def fail_interrupted(self, user_id: str, keep_run_id: Optional[str], error: str) -> int:
        """Mark one user's active runs other than ``keep_run_id`` (the one this process is executing) as failed.

        Scoped to the user so one user's request never fails another user's run that is still executing.
        """
        def _write(session) -> int:
            stmt = select(DailyPlanRun).where(
                DailyPlanRun.status.in_(ACTIVE_RUN_STATUSES), DailyPlanRun.user_id == user_id,
            )
            count = 0
            for row in session.execute(stmt).scalars():
                if keep_run_id is not None and row.run_id == keep_run_id:
                    continue
                row.status = "failed"
                row.error = error
                row.finished_at = utc_naive_now()
                count += 1
            return count

        return self.db._run_write_transaction(f"fail interrupted plan_runs[{user_id}]", _write)

    @staticmethod
    def _run_dict(row: DailyPlanRun) -> Dict[str, Any]:
        return {
            "run_id": row.run_id,
            "trade_date": row.trade_date,
            "status": row.status,
            "trigger": row.trigger,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "steps": _loads(row.steps_json, []),
            "stock_total": int(row.stock_total or 0),
            "stock_succeeded": int(row.stock_succeeded or 0),
            "stock_failed": int(row.stock_failed or 0),
            "failures": _loads(row.failures_json, []),
            "push": _loads(row.push_json, {}),
            "push_preview": row.push_preview,
            "model_calls": _loads(row.model_calls_json, []),
            "data_gaps": _loads(row.data_gaps_json, []),
            "error": row.error,
        }

    # ------------------------------------------------------------------ stock plans
    def save_stock_plan(self, user_id: str, run_id: Optional[str], trade_date: date, code: str, status: str,
                        fingerprint: str, payload: Dict[str, Any], meta: Dict[str, Any]) -> int:
        """Insert one plan row of ``user_id`` and return its id."""
        def _write(session) -> int:
            row = DailyPlanStock(
                user_id=user_id, run_id=run_id, trade_date=trade_date, code=code, status=status, fingerprint=fingerprint,
                payload_json=_dumps(payload), meta_json=_dumps(meta), created_at=utc_naive_now(),
            )
            session.add(row)
            session.flush()
            return int(row.id)

        return self.db._run_write_transaction(f"save plan_stock[{code}]", _write)

    def update_stock_plan_payload(self, user_id: str, row_id: int, payload: Dict[str, Any]) -> None:
        def _write(session) -> None:
            row = session.get(DailyPlanStock, row_id)
            if row is not None and row.user_id == user_id:
                row.payload_json = _dumps(payload)

        self.db._run_write_transaction(f"update plan_stock[{row_id}]", _write)

    def latest_stock_plans(self, user_id: str, codes: Sequence[str], trade_date: date) -> Dict[str, Dict[str, Any]]:
        """``user_id``'s newest stored plan per code for ``trade_date``: ``{code: {id, run_id, status, ...}}``."""
        if not codes:
            return {}
        with self.db.get_session() as session:
            newest = (
                select(func.max(DailyPlanStock.id))
                .where(
                    DailyPlanStock.user_id == user_id,
                    DailyPlanStock.trade_date == trade_date,
                    DailyPlanStock.code.in_(list(codes)),
                )
                .group_by(DailyPlanStock.code)
            )
            rows = session.execute(select(DailyPlanStock).where(DailyPlanStock.id.in_(newest))).scalars().all()
            return {
                r.code: {
                    "id": r.id, "run_id": r.run_id, "status": r.status, "fingerprint": r.fingerprint,
                    "payload": _loads(r.payload_json, {}), "meta": _loads(r.meta_json, {}),
                    "created_at": _iso_utc(r.created_at),
                }
                for r in rows
            }

    # ------------------------------------------------------------------ market data (local, read only)
    def latest_ingested_date(self, upto: Optional[date] = None) -> Optional[date]:
        with self.db.get_session() as session:
            stmt = select(func.max(MarketStockDaily.trade_date))
            if upto is not None:
                stmt = stmt.where(MarketStockDaily.trade_date <= upto)
            return session.execute(stmt).scalar_one_or_none()

    def stock_history(self, codes: Sequence[str], upto: date, limit: int) -> Dict[str, List[Dict[str, Any]]]:
        """Ascending ``market_stock_daily`` rows (at most ``limit`` newest sessions <= ``upto``) per code."""
        if not codes:
            return {}
        columns = ("trade_date", "code", "name", "exchange_board", "is_st", "is_suspended", "prev_close", "close",
                   "pct_chg", "amount", "turnover_rate", "limit_state")
        result: Dict[str, List[Dict[str, Any]]] = {code: [] for code in codes}
        with self.db.get_session() as session:
            sessions = session.execute(
                select(MarketStockDaily.trade_date)
                .where(MarketStockDaily.trade_date <= upto)
                .group_by(MarketStockDaily.trade_date)
                .order_by(MarketStockDaily.trade_date.desc())
                .limit(limit)
            ).scalars().all()
            if not sessions:
                return result
            rows = session.execute(
                select(*[getattr(MarketStockDaily, c) for c in columns]).where(
                    MarketStockDaily.code.in_(list(codes)), MarketStockDaily.trade_date >= min(sessions),
                    MarketStockDaily.trade_date <= upto,
                ).order_by(MarketStockDaily.trade_date)
            ).all()
        for row in rows:
            item = dict(zip(columns, row))
            result[item["code"]].append(item)
        return result

    def latest_rows(self, codes: Sequence[str], upto: Optional[date] = None) -> Dict[str, Dict[str, Any]]:
        """Each code's newest ``market_stock_daily`` row (<= ``upto``)."""
        if not codes:
            return {}
        columns = ("trade_date", "code", "name", "exchange_board", "is_st", "is_suspended", "close", "pct_chg")
        with self.db.get_session() as session:
            newest = select(MarketStockDaily.code, func.max(MarketStockDaily.trade_date).label("d")).where(
                MarketStockDaily.code.in_(list(codes))
            )
            if upto is not None:
                newest = newest.where(MarketStockDaily.trade_date <= upto)
            newest = newest.group_by(MarketStockDaily.code).subquery()
            rows = session.execute(
                select(*[getattr(MarketStockDaily, c) for c in columns]).join(
                    newest, and_(MarketStockDaily.code == newest.c.code, MarketStockDaily.trade_date == newest.c.d)
                )
            ).all()
        return {row[1]: dict(zip(columns, row)) for row in rows}

    def ohlc_bars(self, codes: Sequence[str], start: date, end: date) -> Dict[str, Dict[date, Dict[str, Any]]]:
        """``stock_daily`` OHLC rows (present for stocks the daily analysis has fetched): ``{code: {date: row}}``."""
        if not codes:
            return {}
        result: Dict[str, Dict[date, Dict[str, Any]]] = {}
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily.code, StockDaily.date, StockDaily.open, StockDaily.high, StockDaily.low,
                       StockDaily.close, StockDaily.pct_chg, StockDaily.amount).where(
                    StockDaily.code.in_(list(codes)), StockDaily.date >= start, StockDaily.date <= end,
                )
            ).all()
        for code, day, open_, high, low, close, pct, amount in rows:
            result.setdefault(code, {})[day] = {
                "open": open_, "high": high, "low": low, "close": close, "pct_chg": pct, "amount": amount,
            }
        return result

    def boards_of_stocks(self, codes: Sequence[str], on_date: date) -> Dict[str, List[str]]:
        """Board refs (``industry:BK0448``) each stock belongs to on ``on_date``."""
        if not codes:
            return {}
        result: Dict[str, List[str]] = {}
        with self.db.get_session() as session:
            rows = session.execute(
                select(BoardMembership.stock_code, BoardCatalog.board_type, BoardCatalog.code)
                .join(BoardCatalog, BoardCatalog.id == BoardMembership.board_id)
                .where(
                    BoardMembership.stock_code.in_(list(codes)),
                    BoardMembership.valid_from <= on_date,
                    (BoardMembership.valid_to.is_(None)) | (BoardMembership.valid_to >= on_date),
                    BoardCatalog.board_type.in_(("industry", "concept")),
                )
            ).all()
        for code, board_type, board_code in rows:
            result.setdefault(code, []).append(board_ref(board_type, board_code))
        return result
