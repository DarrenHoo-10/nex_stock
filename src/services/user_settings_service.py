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
from src.services.user_schedule_service import SCHEDULE_TIME_KEYS, ScheduleTimeError, validate_schedule_time
from src.user_secrets import UserSecretError, UserSecretStore
from src.utils.url_safety import UnsafeUrlError, check_public_https_url, split_url_list

logger = logging.getLogger(__name__)

MASK_TOKEN = "******"
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_USER_ID_MAX_LENGTH = 64
_MAX_VALUE_LENGTH = 16384
# Not flagged sensitive by the registry but carry credentials.
_EXTRA_SECRET_KEYS = frozenset({"CUSTOM_WEBHOOK_URLS"})
_EXTRA_HEADERS_KEY_RE = re.compile(r"^LLM_[A-Z0-9_]+_EXTRA_HEADERS$")
# User-scope keys whose value is an address the server will call (model Base URLs incl. ``LLM_<NAME>_BASE_URL``,
# SearXNG, Hindsight, notification webhooks ...). Matched by name so a newly registered endpoint key is
# covered without touching this list; every such value must be a public https URL (src/utils/url_safety.py).
# P2 stored the system's per-channel ``.env`` keys as personal settings, but nothing reads them for a non-primary user:
# a personal push goes to the channels of ``/me/notification-channels`` (``user_notification_service``) only. They are
# no longer accepted (``managed_elsewhere``) nor listed; values saved earlier stay inert and can be removed with a reset.
LEGACY_CHANNEL_KEYS = frozenset({
    "ASTRBOT_TOKEN", "ASTRBOT_URL",
    "CUSTOM_WEBHOOK_BEARER_TOKEN", "CUSTOM_WEBHOOK_BODY_TEMPLATE", "CUSTOM_WEBHOOK_URLS",
    "DINGTALK_SECRET", "DINGTALK_WEBHOOK_URL", "DISCORD_WEBHOOK_URL",
    "EMAIL_PASSWORD", "EMAIL_RECEIVERS", "EMAIL_SENDER",
    "FEISHU_WEBHOOK_KEYWORD", "FEISHU_WEBHOOK_SECRET", "FEISHU_WEBHOOK_URL",
    "GOTIFY_TOKEN", "GOTIFY_URL", "NTFY_TOKEN", "NTFY_URL",
    "PUSHOVER_API_TOKEN", "PUSHOVER_USER_KEY", "PUSHPLUS_TOKEN", "PUSHPLUS_TOPIC", "SERVERCHAN3_SENDKEY",
    "SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_MESSAGE_THREAD_ID",
    "WECHAT_WEBHOOK_URL",
})
_URL_KEY_SUFFIXES = ("_URL", "_URLS", "_BASE_URL", "_BASE_URLS", "_API_BASE")
_URL_LIST_KEY_SUFFIXES = ("_URLS", "_BASE_URLS")


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


def is_url_setting_key(key: str) -> bool:
    """Whether the value of ``key`` is a URL the server will call (so it must pass the SSRF check)."""
    return (key or "").strip().upper().endswith(_URL_KEY_SUFFIXES)


def validate_url_setting(key: str, value: str) -> None:
    """SSRF guard for a URL-valued user setting; raises :class:`UserSettingError` ``unsafe_url``.

    The message and issues name the key and the reason, never the submitted value.
    """
    key_upper = key.upper()
    urls = split_url_list(value) if key_upper.endswith(_URL_LIST_KEY_SUFFIXES) else [value.strip()]
    for url in urls:
        if not url:
            continue
        try:
            check_public_https_url(url)
        except UnsafeUrlError as exc:
            raise UserSettingError(
                "unsafe_url",
                f"{key_upper} must be a public https URL ({exc.reason})",
                [{"key": key_upper, "code": exc.reason, "message": exc.message, "severity": "error"}],
            ) from exc


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
        self._validate_model_routes(user_id, prepared)
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
    def _validate_model_routes(self, user_id: str, prepared: List[Any]) -> None:
        """Refuse a save that would make the user's model routes use a provider / endpoint they may not use.

        Only checked when the batch touches a model-route key; the stored settings + this batch are validated as
        a whole (``validate_user_model_routes``), because the wire model depends on several keys at once
        (``LLM_<NAME>_PROTOCOL`` / ``_BASE_URL`` / ``_MODELS``, ``LITELLM_MODEL`` ...).
        """
        from src.llm.credentials_guard import ModelKeyRequiredError
        from src.services.user_config_service import is_model_route_setting_key, validate_user_model_routes

        route_keys = [key for key, _value, _secret in prepared if is_model_route_setting_key(key)]
        if not route_keys:
            return
        pending = {key: value for key, value, _secret in prepared if value is not None}
        try:
            validate_user_model_routes(self.get_plain_values(user_id), pending)
        except ModelKeyRequiredError as exc:
            raise UserSettingError(
                exc.code,
                exc.message,
                [{"key": route_keys[0], "code": exc.code, "message": exc.message, "severity": "error"}],
            ) from exc

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
        if key_upper in LEGACY_CHANNEL_KEYS:
            raise UserSettingError(
                "managed_elsewhere",
                f"{key_upper} is not a personal setting; add push channels through /api/v1/me/notification-channels",
                [{"key": key_upper, "code": "managed_elsewhere", "message": "Use the personal notification channels", "severity": "error"}],
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
        if is_url_setting_key(key_upper) and value.strip():
            validate_url_setting(key_upper, value)
        if key_upper in SCHEDULE_TIME_KEYS:
            value = UserSettingsService._validate_schedule_time(key_upper, value)
        return key_upper, SystemConfigService.normalize_single_value(key_upper, value), is_secret

    @staticmethod
    def _validate_schedule_time(key: str, value: str) -> str:
        """Personal plan / pre-open times must fit the system's data-ready and market-open times (user_schedule_service)."""
        try:
            return validate_schedule_time(key, value)
        except ScheduleTimeError as exc:
            issue = {"key": key, "code": exc.code, "message": exc.message, "severity": "error"}
            if exc.limit_name:
                issue[exc.limit_name] = exc.limit
            raise UserSettingError(exc.code, exc.message, [issue]) from exc

    @staticmethod
    def _invalidate(user_id: str) -> None:
        from src.services.user_config_service import invalidate_user_config

        invalidate_user_config(user_id)
