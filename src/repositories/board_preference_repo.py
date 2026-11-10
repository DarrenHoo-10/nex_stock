# -*- coding: utf-8 -*-
"""Followed boards (``board_follow``) and recommended boards per board run (``board_recommendation``)."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import delete, select

from src.storage import BoardFollow, BoardRecommendation, DatabaseManager, utc_naive_now


def _iso_utc(value: Any) -> Optional[str]:
    return f"{value.isoformat()}Z" if value is not None else None


class BoardPreferenceRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------ follows (per user)
    def list_follows(self, user_id: str) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(BoardFollow)
                .where(BoardFollow.user_id == user_id)
                .order_by(BoardFollow.created_at, BoardFollow.id)
            ).scalars().all()
            return [
                {
                    "board_ref": row.board_ref,
                    "board_name": row.board_name,
                    "board_type": row.board_type,
                    "followed_at": _iso_utc(row.created_at),
                }
                for row in rows
            ]

    def follow(self, user_id: str, board_ref: str, board_name: str, board_type: str) -> bool:
        """Idempotent; returns True when a new row was added."""
        def _write(session) -> bool:
            row = session.execute(
                select(BoardFollow).where(BoardFollow.user_id == user_id, BoardFollow.board_ref == board_ref)
            ).scalar_one_or_none()
            if row is not None:
                row.board_name = board_name
                row.board_type = board_type
                return False
            session.add(BoardFollow(user_id=user_id, board_ref=board_ref, board_name=board_name, board_type=board_type,
                                    created_at=utc_naive_now()))
            return True

        return self.db._run_write_transaction(f"board_follow[{user_id}:{board_ref}]", _write)

    def unfollow(self, user_id: str, board_ref: str) -> bool:
        def _write(session) -> bool:
            result = session.execute(
                delete(BoardFollow).where(BoardFollow.user_id == user_id, BoardFollow.board_ref == board_ref)
            )
            return bool(result.rowcount)

        return self.db._run_write_transaction(f"board_unfollow[{user_id}:{board_ref}]", _write)

    # ------------------------------------------------------------------ recommendations
    def replace_recommendations(
        self,
        run_id: int,
        trade_date: date,
        items: Sequence[Dict[str, Any]],
        *,
        source: str,
        model: Optional[str],
        note: Optional[str],
    ) -> int:
        """Replace the recommendations of one run (re-generation overwrites)."""
        def _write(session) -> int:
            session.execute(delete(BoardRecommendation).where(BoardRecommendation.run_id == run_id))
            now = utc_naive_now()
            for rank, item in enumerate(items, start=1):
                session.add(BoardRecommendation(
                    run_id=run_id,
                    trade_date=trade_date,
                    rank=rank,
                    board_ref=item["board_ref"],
                    board_name=item["board_name"],
                    board_type=item["board_type"],
                    reason=item.get("reason") or "",
                    source=source,
                    model=model,
                    note=note,
                    created_at=now,
                ))
            return len(items)

        return self.db._run_write_transaction(f"board_recommendation[{run_id}]", _write)

    def get_recommendations(self, run_id: int) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(BoardRecommendation)
                .where(BoardRecommendation.run_id == run_id)
                .order_by(BoardRecommendation.rank)
            ).scalars().all()
            return [
                {
                    "rank": row.rank,
                    "board_ref": row.board_ref,
                    "board_name": row.board_name,
                    "board_type": row.board_type,
                    "reason": row.reason or "",
                    "source": row.source,
                    "model": row.model,
                    "note": row.note,
                    "created_at": _iso_utc(row.created_at),
                }
                for row in rows
            ]
