# -*- coding: utf-8 -*-
"""Persistence for the Feishu notification binding and message -> session links.

Two small tables (see ``src.storage``):

* ``feishu_binding``: the single chat that receives bot-app pushes. Binding a
  chat replaces the previous one in one transaction.
* ``feishu_message_session``: which assistant session a pushed / answered Feishu
  message belongs to, so a reply to it continues that session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import delete, select

from src.storage import DatabaseManager, FeishuBinding, FeishuMessageSession

MESSAGE_SESSION_RETENTION_DAYS = 30


def _binding_dict(row: FeishuBinding) -> Dict[str, Any]:
    return {
        "chat_id": row.chat_id,
        "chat_type": row.chat_type,
        "bound_by_open_id": row.bound_by_open_id,
        "bound_at": row.bound_at.isoformat() if row.bound_at else None,
        "session_id": row.session_id,
    }


class FeishuBindingRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # -- binding ---------------------------------------------------------

    def get_binding(self) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(FeishuBinding).order_by(FeishuBinding.id.desc()).limit(1)
            ).scalars().first()
            return _binding_dict(row) if row else None

    def set_binding(
        self,
        chat_id: str,
        chat_type: str,
        session_id: str,
        bound_by_open_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make ``chat_id`` THE notification target, replacing any previous binding."""

        def write(session) -> Dict[str, Any]:
            session.execute(delete(FeishuBinding))
            row = FeishuBinding(
                chat_id=chat_id,
                chat_type=chat_type,
                bound_by_open_id=bound_by_open_id or None,
                bound_at=datetime.now(),
                session_id=session_id,
            )
            session.add(row)
            session.flush()
            return _binding_dict(row)

        return self.db._run_write_transaction("feishu_binding_set", write)

    def clear_binding(self) -> bool:
        """Remove the binding; returns whether one existed."""

        def write(session) -> bool:
            return bool(session.execute(delete(FeishuBinding)).rowcount)

        return self.db._run_write_transaction("feishu_binding_clear", write)

    # -- message -> session ---------------------------------------------

    def remember_message_session(
        self, message_id: str, chat_id: str, session_id: str, kind: str = "answer",
    ) -> None:
        if not message_id:
            return

        def write(session) -> None:
            existing = session.get(FeishuMessageSession, message_id)
            if existing is not None:
                existing.chat_id = chat_id
                existing.session_id = session_id
                existing.kind = kind
                return
            session.add(FeishuMessageSession(
                message_id=message_id, chat_id=chat_id, session_id=session_id,
                kind=kind, created_at=datetime.now(),
            ))
            # Opportunistic retention: links older than the retention window are useless.
            cutoff = datetime.now() - timedelta(days=MESSAGE_SESSION_RETENTION_DAYS)
            session.execute(delete(FeishuMessageSession).where(FeishuMessageSession.created_at < cutoff))

        self.db._run_write_transaction("feishu_message_session_remember", write)

    def lookup_message_session(self, *message_ids: Optional[str]) -> Optional[str]:
        """Session of the first known message id (callers pass nearest-first)."""
        ids = [item for item in message_ids if item]
        if not ids:
            return None
        with self.db.get_session() as session:
            rows = {
                row.message_id: row.session_id
                for row in session.execute(
                    select(FeishuMessageSession).where(FeishuMessageSession.message_id.in_(ids))
                ).scalars()
            }
        for item in ids:
            if item in rows:
                return rows[item]
        return None
