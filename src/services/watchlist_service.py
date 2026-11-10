# -*- coding: utf-8 -*-
"""Per-user watchlist: the only read/write entry (docs/design/multi-user.md section 2.3).

* primary user (``PRIMARY_USER_ID``): the watchlist is ``.env`` ``STOCK_LIST`` so the CLI and
  GitHub Actions keep working unchanged. Reads go through the injected ``SystemConfigService``
  (API path, raw codes as stored) or, without one, hot-reload ``STOCK_LIST`` the way the
  scheduled jobs always did (upper-cased); writes always go through ``SystemConfigService``.
* every other user: rows of ``user_watchlist(user_id, code, position, created_at)``.

Every method takes an explicit ``user_id``; callers never reach ``STOCK_LIST`` / ``config.stock_list``
for a user-facing purpose directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from sqlalchemy import delete, func, select

from data_provider.base import normalize_stock_code
from src.config import get_config, get_primary_user_id
from src.services.stock_list_parser import split_stock_list
from src.storage import DatabaseManager, UserWatchlist, utc_naive_now

logger = logging.getLogger(__name__)


def watchlist_match_key(code: str) -> str:
    """Equivalence key used for watchlist add/remove matching (HK/US/A-share variants)."""
    normalized = normalize_stock_code(code.strip())
    if re.fullmatch(r"\d{5}", normalized):
        return f"HK{normalized}"
    return normalized.upper()


class WatchlistService:
    def __init__(self, db_manager: Optional[DatabaseManager] = None, config_service: Any = None, config: Any = None):
        self._db = db_manager
        self._config_service = config_service
        # Config whose ``STOCK_LIST`` is hot-reloaded for the primary user when no config service is injected
        # (services that were built with an explicit Config pass it so they keep reading that same object).
        self._config = config

    @property
    def db(self) -> DatabaseManager:
        if self._db is None:
            self._db = DatabaseManager.get_instance()
        return self._db

    @staticmethod
    def is_primary(user_id: str) -> bool:
        return user_id == get_primary_user_id()

    # ------------------------------------------------------------------ reads
    def list_codes(self, user_id: str) -> List[str]:
        """Codes in insertion order, as stored (no normalization)."""
        if self.is_primary(user_id):
            return self._read_primary()
        with self.db.get_session() as session:
            rows = session.execute(
                select(UserWatchlist.code)
                .where(UserWatchlist.user_id == user_id)
                .order_by(UserWatchlist.position, UserWatchlist.id)
            ).scalars().all()
        return list(rows)

    def contains(self, user_id: str, code: str) -> bool:
        key = watchlist_match_key(code)
        return any(watchlist_match_key(item) == key for item in self.list_codes(user_id))

    # ------------------------------------------------------------------ writes
    def add(self, user_id: str, code: str) -> Tuple[List[str], bool]:
        """Append ``code`` unless an equivalent variant exists; returns (codes, added)."""
        code = code.strip()
        codes = self.list_codes(user_id)
        key = watchlist_match_key(code)
        if key in [watchlist_match_key(item) for item in codes]:
            return codes, False
        if self.is_primary(user_id):
            codes.append(code)
            self._write_primary(codes)
            return codes, True

        def _write(session) -> None:
            next_position = (
                session.execute(
                    select(func.max(UserWatchlist.position)).where(UserWatchlist.user_id == user_id)
                ).scalar()
                or 0
            ) + 1
            session.add(UserWatchlist(
                user_id=user_id, code=code, position=next_position, created_at=utc_naive_now(),
            ))

        self.db._run_write_transaction(f"user_watchlist_add[{user_id}]", _write)
        return self.list_codes(user_id), True

    def remove(self, user_id: str, code: str) -> Tuple[List[str], bool]:
        """Remove the entry equivalent to ``code``; returns (codes, removed)."""
        codes = self.list_codes(user_id)
        key = watchlist_match_key(code)
        keys = [watchlist_match_key(item) for item in codes]
        if key not in keys:
            return codes, False
        idx = keys.index(key)
        stored = codes[idx]
        if self.is_primary(user_id):
            codes.pop(idx)
            self._write_primary(codes)
            return codes, True

        def _write(session) -> None:
            session.execute(
                delete(UserWatchlist).where(UserWatchlist.user_id == user_id, UserWatchlist.code == stored)
            )

        self.db._run_write_transaction(f"user_watchlist_remove[{user_id}]", _write)
        return self.list_codes(user_id), True

    # ------------------------------------------------------------------ primary (.env STOCK_LIST)
    def _system_config_service(self):
        if self._config_service is None:
            from src.services.system_config_service import SystemConfigService

            self._config_service = SystemConfigService()
        return self._config_service

    def _read_primary(self) -> List[str]:
        if self._config_service is not None:
            config_data = self._config_service.get_config(include_schema=False)
            raw = ""
            for item in config_data.get("items", []):
                if item.get("key") == "STOCK_LIST":
                    raw = str(item.get("value", ""))
                    break
            return split_stock_list(raw)
        config = self._config if self._config is not None else get_config()
        refresh = getattr(config, "refresh_stock_list", None)
        if callable(refresh):
            try:
                refresh()
            except Exception as exc:  # noqa: BLE001 - keep the last loaded value
                logger.warning("[Watchlist] 刷新 STOCK_LIST 失败，沿用当前值: %s", exc)
        return list(getattr(config, "stock_list", None) or [])

    def _write_primary(self, codes: List[str]) -> None:
        service = self._system_config_service()
        config_data = service.get_config(include_schema=False)
        service.update(
            config_version=config_data.get("config_version", ""),
            items=[{"key": "STOCK_LIST", "value": ",".join(codes)}],
            mask_token="******",
            reload_now=True,
        )
