# -*- coding: utf-8 -*-
"""Personal settings page backend (``/api/v1/me/settings``, docs/design/multi-user.md 3.3 / 3.6).

* Lists the ``scope=user`` registry fields grouped as in design 3.3, each with its effective value, where that
  value comes from (``user`` / ``system_default`` / ``unset``) and whether the key falls back to the system
  default when the user has none. Secrets are masked; plaintext never leaves this module. An inherited
  system default is reported as ``source`` / ``configured`` only (empty ``value``): it may be an endpoint or
  account id of the server. The primary user's read-only view still shows its own values (secrets masked).
* The primary user's personal settings are the ``.env`` values: they are returned read-only and writes are
  rejected by ``UserSettingsService`` (``primary_user_uses_env``).
* ``run_model_test`` makes one minimal completion with the user's own model configuration.

Writing, validation (registry rules + SSRF guard) and cache invalidation stay in ``UserSettingsService``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from src.config import get_primary_user_id
from src.core.config_registry import (
    SCOPE_USER,
    get_config_scope,
    get_field_definition,
    get_registered_field_keys,
    get_user_scope_keys,
)
from src.llm.credentials_guard import ModelKeyRequiredError, require_explicit_credentials, user_call_scope
from src.services.user_config_service import (
    DATA_SOURCE_CREDENTIAL_KEYS,
    SEARCH_KEYS,
    inherits_from_system,
    require_user_model,
)
from src.services.user_settings_service import (
    MASK_TOKEN,
    UserSettingError,
    UserSettingsService,
    is_secret_setting_key,
)

logger = logging.getLogger(__name__)

# (group id, title) in display order; the ids are stable API values.
SETTING_GROUPS = (
    ("model", "模型"),
    ("search", "搜索"),
    ("data_source", "数据源凭据"),
    ("agent", "Agent"),
    ("push", "推送"),
    ("position", "仓位规则"),
    ("report", "报告偏好"),
)
# User-scope keys that are not edited here: the watchlist has its own endpoints (``/stocks/watchlist``).
MANAGED_ELSEWHERE_KEYS = frozenset({"STOCK_LIST"})
_AGENT_PREFIXES = ("AGENT_", "ASSISTANT_", "HINDSIGHT_")
_DYNAMIC_LLM_KEY_RE = re.compile(r"^LLM_[A-Z0-9]+(?:_[A-Z0-9]+)*$")
_PUSH_KEYS = frozenset({"DAILY_PLAN_ENABLED", "SINGLE_STOCK_NOTIFY", "MERGE_EMAIL_NOTIFICATION"})


def setting_group(key: str) -> str:
    """Settings-page group of a user-scope key (design 3.3 table)."""
    key_upper = key.upper()
    if key_upper in SEARCH_KEYS:
        return "search"
    if key_upper in DATA_SOURCE_CREDENTIAL_KEYS:
        return "data_source"
    if key_upper.startswith("POSITION_"):
        return "position"
    if key_upper.startswith("REPORT_") or key_upper == "MARKET_REVIEW_COLOR_SCHEME":
        return "report"
    if key_upper.startswith(_AGENT_PREFIXES):
        return "agent"
    if key_upper.startswith("PREOPEN_PUSH_") or key_upper in _PUSH_KEYS:
        return "push"
    category = get_field_definition(key_upper).get("category")
    if category == "notification":
        return "push"
    if key_upper == "OLLAMA_API_BASE" or category == "ai_model" or _DYNAMIC_LLM_KEY_RE.match(key_upper):
        return "model"
    return "model" if key_upper.startswith(("LLM_", "LITELLM_")) else "agent"


def editable_setting_keys() -> List[str]:
    """Registered user-scope keys the settings page shows (registry ``scope`` is the single source)."""
    keys = {key for key in get_registered_field_keys() if get_config_scope(key) == SCOPE_USER}
    keys |= get_user_scope_keys()  # user-scoped keys that have no registry field of their own (e.g. PREOPEN_PUSH_TIME)
    return sorted(keys - MANAGED_ELSEWHERE_KEYS)


def _system_values() -> Dict[str, str]:
    """Effective system-layer (``.env``) values by key, as shown on the system settings page."""
    from src.services.system_config_service import SystemConfigService

    payload = SystemConfigService().get_config(include_schema=False)
    return {str(item["key"]).upper(): str(item.get("value") or "") for item in payload.get("items", [])}


class MeSettingsService:
    """Read model of one user's personal settings."""

    def __init__(
        self,
        settings: Optional[UserSettingsService] = None,
        system_values: Optional[Callable[[], Mapping[str, str]]] = None,
    ):
        self._settings = settings or UserSettingsService()
        self._system_values = system_values or _system_values

    # ------------------------------------------------------------------ read
    def describe(self, user_id: str) -> Dict[str, Any]:
        is_primary = user_id == get_primary_user_id()
        system = {k: v for k, v in self._system_values().items()}
        stored = {} if is_primary else {row["key"]: row for row in self._settings.list_settings(user_id)}

        keys = set(editable_setting_keys())
        # Dynamic LLM_<NAME>_* channel keys: the ones the user saved (or the .env defines, for the primary user).
        dynamic_source: Iterable[str] = system.keys() if is_primary else stored.keys()
        keys.update(
            key for key in dynamic_source
            if _DYNAMIC_LLM_KEY_RE.match(key) and get_config_scope(key) == SCOPE_USER and key not in MANAGED_ELSEWHERE_KEYS
        )

        grouped: Dict[str, List[Dict[str, Any]]] = {group_id: [] for group_id, _ in SETTING_GROUPS}
        for key in sorted(keys):
            grouped.setdefault(setting_group(key), []).append(
                self._field(key, is_primary=is_primary, stored=stored.get(key), system_value=system.get(key, ""))
            )
        return {
            "user_id": user_id,
            "is_primary": is_primary,
            "managed_in_system_settings": is_primary,
            "groups": [
                {"id": group_id, "title": title, "fields": grouped.get(group_id, [])}
                for group_id, title in SETTING_GROUPS
            ],
        }

    def _field(
        self, key: str, *, is_primary: bool, stored: Optional[Mapping[str, Any]], system_value: str,
    ) -> Dict[str, Any]:
        definition = get_field_definition(key)
        secret = is_secret_setting_key(key)
        inherits = inherits_from_system(key)

        def shown(value: str) -> str:
            return MASK_TOKEN if (secret and value) else value

        if is_primary:  # the personal layer of the primary user is .env itself
            configured = bool(system_value)
            source, value, updated_at = ("user" if configured else "unset"), shown(system_value), None
        elif stored is not None:
            source, value, updated_at = "user", str(stored["value"]), stored.get("updated_at")
        elif inherits and system_value:
            # Only that the system default applies (``source`` / ``configured``), never what it is: it can be an
            # endpoint or an account id of the server (HINDSIGHT_BASE_URL, LONGBRIDGE_OAUTH_CLIENT_ID ...).
            source, value, updated_at = "system_default", "", None
        else:
            source, value, updated_at = "unset", "", None
        return {
            "key": key,
            "title": definition.get("title") or key,
            "description": definition.get("description") or "",
            "group": setting_group(key),
            "data_type": definition.get("data_type") or "string",
            "ui_control": definition.get("ui_control") or "text",
            "options": list(definition.get("options") or []),
            "is_secret": secret,
            "value": value,
            "source": source,
            "inherits": inherits,
            "configured": source != "unset",
            "editable": not is_primary,
            "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
        }

    # ------------------------------------------------------------------ write
    def update(self, user_id: str, items: Iterable[Mapping[str, Any]]) -> None:
        """Partial update; an empty secret keeps the stored value. All items are validated before any is stored."""
        prepared: Dict[str, str] = {}
        for item in items:
            key = str(item.get("key") or "").strip().upper()
            value = item.get("value")
            if key in MANAGED_ELSEWHERE_KEYS:
                raise UserSettingError("managed_elsewhere", f"{key} is edited through the watchlist endpoints")
            if value is None:
                raise UserSettingError("invalid_value", f"{key} needs a value")
            if isinstance(value, str) and value == "" and is_secret_setting_key(key):
                continue  # empty secret = keep what is stored
            prepared[key] = value
        if prepared:
            self._settings.set_settings(user_id, prepared)

    def reset(self, user_id: str, key: str) -> bool:
        """Drop the user's own value so the default applies again (the system value, or nothing for secrets)."""
        key_upper = (key or "").strip().upper()
        if user_id == get_primary_user_id():
            raise UserSettingError(
                "primary_user_uses_env",
                "The primary user's personal settings are the .env values; edit the system settings instead",
            )
        if key_upper in MANAGED_ELSEWHERE_KEYS:
            raise UserSettingError("managed_elsewhere", f"{key_upper} is edited through the watchlist endpoints")
        if get_config_scope(key_upper) != SCOPE_USER:
            raise UserSettingError(
                "scope_not_user",
                f"{key_upper} is a process-level setting that only the administrator can change",
            )
        return self._settings.delete_setting(user_id, key_upper)


# ---------------------------------------------------------------------- model connectivity test

_TEST_PROMPT = "Reply with OK"
_TEST_TIMEOUT_SECONDS = 20.0


def _pick_deployment(config: Any) -> Optional[Dict[str, Any]]:
    """The deployment of the user's primary model (else their first one) as plain LiteLLM params."""
    deployments = [d for d in (getattr(config, "llm_model_list", None) or []) if isinstance(d, dict)]
    wanted = str(getattr(config, "litellm_model", "") or "").strip()
    chosen = next((d for d in deployments if str(d.get("model_name") or "").strip() == wanted), None) if wanted else None
    chosen = chosen or (deployments[0] if deployments else None)
    params = chosen.get("litellm_params") if chosen else None
    return dict(params) if isinstance(params, dict) else None


_TEST_FAILURE_MESSAGES = {
    "auth_failed": "The provider rejected the API key (authentication failed)",
    "unreachable": "The endpoint could not be reached",
    "timeout": "The endpoint did not answer in time",
    "bad_response": "The endpoint answered, but not with a usable model response",
}


def _failure_category(exc: BaseException) -> str:
    """Generic category of a failed test call; nothing of the upstream error text is ever returned.

    A personal Base URL is a host the user typed, and the test runs from the server: an error message or body
    reflected from it would turn this endpoint into a window onto whatever that URL (or a redirect) reaches.
    """
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return "auth_failed"
    name = type(exc).__name__.lower()
    if "timeout" in name or isinstance(exc, TimeoutError):
        return "timeout"
    if "authentication" in name or "permission" in name:
        return "auth_failed"
    if "connection" in name or "unavailable" in name or isinstance(exc, OSError):
        return "unreachable"
    return "bad_response"


def run_model_test(user_id: str) -> Dict[str, Any]:
    """One minimal completion with ``user_id``'s own model configuration.

    A non-primary user without a model (or whose endpoint fails the SSRF re-check) is refused before LiteLLM
    is reached (``ModelKeyRequiredError`` / ``UnsafeUrlError``); a provider failure is reported as
    ``success=False`` with a generic category (``auth_failed`` / ``unreachable`` / ``timeout`` /
    ``bad_response``) and the HTTP status code at most, never the upstream message or body.
    """
    import litellm

    from src.llm.errors import call_litellm_with_param_recovery

    config = require_user_model(user_id)
    params = _pick_deployment(config)
    if params is None:
        model = str(getattr(config, "litellm_model", "") or "").strip()
        if not model:
            raise ModelKeyRequiredError("No model is configured")
        params = {"model": model}  # primary user on provider variables of the server's own environment
    model = str(params.get("model") or "").strip()
    call_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": _TEST_PROMPT}],
        "max_tokens": 128,
        "timeout": _TEST_TIMEOUT_SECONDS,
    }
    for name in ("api_key", "api_base", "extra_headers"):
        if params.get(name):
            call_kwargs[name] = params[name]
    require_explicit_credentials(config, call_kwargs, model=model)  # no env-key fallback for personal accounts

    started = time.perf_counter()
    try:
        with user_call_scope(config):  # a personal Base URL must not bounce the server to another host
            call_litellm_with_param_recovery(
                lambda kwargs: litellm.completion(**kwargs),
                model=model,
                call_kwargs=call_kwargs,
                logger=logger,
                log_label="[personal model test]",
            )
    except Exception as exc:  # noqa: BLE001 - reported as a generic category only
        category = _failure_category(exc)
        status = getattr(exc, "status_code", None)
        status = status if isinstance(status, int) and 100 <= status <= 599 else None
        logger.warning("[personal model test] failed for user %s: %s (HTTP %s)", user_id, category, status)
        return {
            "success": False,
            "model": model,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error_code": "model_call_failed",
            "error_category": category,
            "http_status": status,
            "message": _TEST_FAILURE_MESSAGES[category] + (f" (HTTP {status})" if status else ""),
        }
    return {
        "success": True,
        "model": model,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error_code": None,
        "message": "OK",  # the model's own reply is not echoed: it is whatever the configured endpoint returned
    }
