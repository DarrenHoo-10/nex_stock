# -*- coding: utf-8 -*-
"""
Personal-layer settings (``user_setting``), docs/design/multi-user.md section 3.

Only keys whose registry scope is ``user`` can be stored, values are validated with the
same registry rules as the system settings, and secret values are encrypted with the
owner's own data key (``src/user_secrets.py``). The primary user's personal settings are
the ``.env`` values, so nothing is ever stored for the primary user.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional

from src.config import get_primary_user_id
from src.core.config_registry import SCOPE_USER, get_config_scope, get_field_definition
from src.repositories.user_repo import UserRepository
from src.services.system_config_service import SystemConfigService
from src.user_secrets import UserSecretError, UserSecretStore

logger = logging.getLogger(__name__)

MASK_TOKEN = "******"
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_USER_ID_MAX_LENGTH = 64
_MAX_VALUE_LENGTH = 16384
# Not flagged sensitive by the registry but carry credentials.
_EXTRA_SECRET_KEYS = frozenset({"CUSTOM_WEBHOOK_URLS"})
_EXTRA_HEADERS_KEY_RE = re.compile(r"^LLM_[A-Z0-9_]+_EXTRA_HEADERS$")


class UserSettingError(ValueError):
    """A personal setting was rejected; ``code`` is stable, ``issues`` never contain secret values."""

    def __init__(self, code: str, message: str, issues: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []


def is_secret_setting_key(key: str) -> bool:
    """Whether the value of ``key`` must be encrypted and masked."""
    key_upper = (key or "").strip().upper()
    if key_upper in _EXTRA_SECRET_KEYS or _EXTRA_HEADERS_KEY_RE.match(key_upper):
        return True
    return bool(get_field_definition(key_upper).get("is_sensitive", False))


def _clean_user_id(user_id: str) -> str:
    text = user_id.strip() if isinstance(user_id, str) else ""
    if not text or len(text) > _USER_ID_MAX_LENGTH:
        raise UserSettingError("invalid_user_id", "user_id must be 1-64 characters")
    return text


class UserSettingsService:
    """Get / set / delete one user's personal settings."""

    def __init__(
        self,
        repo: Optional[UserRepository] = None,
        secret_store: Optional[UserSecretStore] = None,
    ):
        self._repo = repo or UserRepository()
        self._secrets = secret_store or UserSecretStore(self._repo)

    # ------------------------------------------------------------------ reads
    def list_settings(self, user_id: str) -> List[Dict[str, Any]]:
        """Stored settings with secrets masked (safe to return from an API)."""
        user_id = _clean_user_id(user_id)
        return [
            {
                "key": row["key"],
                "value": MASK_TOKEN if row["is_secret"] else row["value"],
                "is_secret": row["is_secret"],
                "updated_at": row["updated_at"],
            }
            for row in self._repo.list_settings(user_id)
        ]

    def get_plain_values(self, user_id: str) -> Dict[str, str]:
        """Plaintext settings for building the user's config view. Internal: never expose.

        Secrets that cannot be decrypted (destroyed or damaged data key) are skipped and
        logged by key name only, so one user's broken key never affects anyone else.
        """
        user_id = _clean_user_id(user_id)
        values: Dict[str, str] = {}
        for row in self._repo.list_settings(user_id):
            key = row["key"]
            if get_config_scope(key) != SCOPE_USER:
                continue
            if not row["is_secret"]:
                values[key] = row["value"]
                continue
            try:
                values[key] = self._secrets.decrypt(user_id, row["value"])
            except UserSecretError as exc:
                logger.warning("Skipping unreadable secret setting %s of user %s: %s", key, user_id, exc)
        return values

    # ------------------------------------------------------------------ writes
    def set_setting(self, user_id: str, key: str, value: str) -> None:
        self.set_settings(user_id, {key: value})

    def set_settings(self, user_id: str, items: Mapping[str, str]) -> None:
        """Validate every item first, then store them; an invalid item stores nothing."""
        user_id = _clean_user_id(user_id)
        if user_id == get_primary_user_id():
            raise UserSettingError(
                "primary_user_uses_env",
                "The primary user's personal settings are the .env values; edit the system settings instead",
            )
        prepared = [self._prepare(key, value) for key, value in items.items()]
        for key, stored_value, is_secret in prepared:
            if stored_value is None:  # masked secret: keep the stored value
                continue
            if is_secret:
                stored_value = self._secrets.encrypt(user_id, stored_value)
            self._repo.upsert_setting(user_id, key, stored_value, is_secret)
        self._invalidate(user_id)

    def delete_setting(self, user_id: str, key: str) -> bool:
        """Remove one override (the user falls back to the system value, or to nothing for secrets)."""
        user_id = _clean_user_id(user_id)
        deleted = self._repo.delete_setting(user_id, (key or "").strip().upper())
        self._invalidate(user_id)
        return deleted

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _prepare(key: str, value: Any):
        key_upper = (key or "").strip().upper()
        if not _KEY_RE.match(key_upper):
            raise UserSettingError("invalid_key", "Setting key must be upper-case letters, digits and underscores")
        if get_config_scope(key_upper) != SCOPE_USER:
            raise UserSettingError(
                "scope_not_user",
                f"{key_upper} is a process-level setting that only the administrator can change",
            )
        if not isinstance(value, str):
            raise UserSettingError("invalid_value", f"{key_upper} must be a string")
        if len(value) > _MAX_VALUE_LENGTH:
            raise UserSettingError("invalid_value", f"{key_upper} is too long")

        is_secret = is_secret_setting_key(key_upper)
        if is_secret and value == MASK_TOKEN:
            return key_upper, None, True

        issues = SystemConfigService.validate_single_value(key_upper, value)
        errors = [issue for issue in issues if issue.get("severity") == "error"]
        if errors:
            if is_secret:  # issues echo the submitted value; never return a secret
                errors = [{k: v for k, v in issue.items() if k != "actual"} for issue in errors]
            raise UserSettingError("invalid_value", f"{key_upper} failed validation", errors)
        return key_upper, SystemConfigService.normalize_single_value(key_upper, value), is_secret

    @staticmethod
    def _invalidate(user_id: str) -> None:
        from src.services.user_config_service import invalidate_user_config

        invalidate_user_config(user_id)
