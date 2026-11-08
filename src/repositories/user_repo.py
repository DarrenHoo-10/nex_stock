# -*- coding: utf-8 -*-
"""Users, personal-layer settings and per-user data keys (docs/design/multi-user.md sections 2-3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select

from src.storage import AppUser, DatabaseManager, UserDataKey, UserSetting, utc_naive_now


class UserRepository:
    """Persistence for ``app_user``, ``user_setting`` and ``user_data_key``.

    Values passed in are stored as given: encryption of secret settings is done by
    ``src.user_secrets`` / ``UserSettingsService`` before they reach this layer.
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    # ------------------------------------------------------------------ app_user
    def touch_app_user(self, user_id: str, handle: str, display_name: str, role: str) -> None:
        """Insert the user or refresh its profile snapshot and ``last_seen_at``."""

        def _write(session) -> None:
            now = utc_naive_now()
            row = session.get(AppUser, user_id)
            if row is None:
                session.add(AppUser(
                    user_id=user_id, handle=handle, display_name=display_name, role=role,
                    first_seen_at=now, last_seen_at=now,
                ))
                return
            row.handle = handle
            row.display_name = display_name
            row.role = role
            row.last_seen_at = now

        self.db._run_write_transaction(f"app_user[{user_id}]", _write)

    def list_app_users(self) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(select(AppUser).order_by(AppUser.first_seen_at, AppUser.user_id)).scalars().all()
            return [
                {
                    "user_id": row.user_id,
                    "handle": row.handle,
                    "display_name": row.display_name,
                    "role": row.role,
                    "first_seen_at": row.first_seen_at,
                    "last_seen_at": row.last_seen_at,
                }
                for row in rows
            ]

    # ------------------------------------------------------------------ user_setting
    def list_settings(self, user_id: str) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(UserSetting).where(UserSetting.user_id == user_id).order_by(UserSetting.key)
            ).scalars().all()
            return [
                {"key": row.key, "value": row.value, "is_secret": bool(row.is_secret), "updated_at": row.updated_at}
                for row in rows
            ]

    def get_setting(self, user_id: str, key: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(UserSetting, (user_id, key))
            if row is None:
                return None
            return {"key": row.key, "value": row.value, "is_secret": bool(row.is_secret), "updated_at": row.updated_at}

    def upsert_setting(self, user_id: str, key: str, value: str, is_secret: bool) -> None:
        def _write(session) -> None:
            row = session.get(UserSetting, (user_id, key))
            if row is None:
                session.add(UserSetting(
                    user_id=user_id, key=key, value=value, is_secret=is_secret, updated_at=utc_naive_now(),
                ))
            else:
                row.value = value
                row.is_secret = is_secret
                row.updated_at = utc_naive_now()

        self.db._run_write_transaction(f"user_setting[{user_id}:{key}]", _write)

    def delete_setting(self, user_id: str, key: str) -> bool:
        def _write(session) -> bool:
            result = session.execute(
                delete(UserSetting).where(UserSetting.user_id == user_id, UserSetting.key == key)
            )
            return bool(result.rowcount)

        return self.db._run_write_transaction(f"user_setting_delete[{user_id}:{key}]", _write)

    def settings_signature(self, user_id: str) -> Tuple[int, Optional[datetime], Optional[datetime]]:
        """Cheap change marker: (setting count, newest update, data-key creation time).

        Lets cached per-user config views notice writes made by another process and a
        destroyed / re-created data key.
        """
        with self.db.get_session() as session:
            count, newest = session.execute(
                select(func.count(), func.max(UserSetting.updated_at)).where(UserSetting.user_id == user_id)
            ).one()
            key_created = session.execute(
                select(UserDataKey.created_at).where(UserDataKey.user_id == user_id)
            ).scalar_one_or_none()
            return int(count or 0), newest, key_created

    # ------------------------------------------------------------------ user_data_key
    def get_data_key(self, user_id: str) -> Optional[str]:
        with self.db.get_session() as session:
            row = session.get(UserDataKey, user_id)
            return row.wrapped_key if row is not None else None

    def create_data_key(self, user_id: str, wrapped_key: str) -> str:
        """Store the wrapped key unless one exists; returns the stored value (race-safe)."""

        def _write(session) -> str:
            row = session.get(UserDataKey, user_id)
            if row is not None:
                return row.wrapped_key
            session.add(UserDataKey(user_id=user_id, wrapped_key=wrapped_key, created_at=utc_naive_now()))
            return wrapped_key

        return self.db._run_write_transaction(f"user_data_key[{user_id}]", _write)

    def delete_data_key(self, user_id: str) -> bool:
        def _write(session) -> bool:
            result = session.execute(delete(UserDataKey).where(UserDataKey.user_id == user_id))
            return bool(result.rowcount)

        return self.db._run_write_transaction(f"user_data_key_delete[{user_id}]", _write)
