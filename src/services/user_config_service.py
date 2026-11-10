# -*- coding: utf-8 -*-
"""
Per-user configuration view (``get_user_config``), docs/design/multi-user.md section 3.

The view is the system config (``.env``) overlaid with the user's personal settings,
minus whatever must never be inherited:

* ``scope=system`` keys always come from the system layer;
* ``scope=user`` keys: the user's own value wins; without one, data-source credentials
  and non-secret preferences inherit the system value, while model / search keys and
  endpoints, the model-channel definitions, secrets and notification channel
  configuration never do (an unconfigured user has no model and no push channel).

The primary user is the system layer itself (its settings are ``.env``). Parsing reuses
``Config._load_from_env`` with an explicit environment mapping, so nothing is read from
or written to ``os.environ`` and views can be built concurrently. NOT wired into any
service yet (P2).

P2b-1 wires the view into every model / search call made on behalf of a user:
``resolve_user_config`` picks the config of the acting user and ``require_usable_model`` refuses a
non-primary user without a model of their own *before* anything reaches LiteLLM.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional, Tuple

from src.config import Config, get_config, get_configured_llm_models, get_primary_user_id
from src.core.config_registry import (
    SCOPE_USER,
    get_config_scope,
    get_field_definition,
)
from src.llm.backend_registry import LOCAL_CLI_GENERATION_BACKEND_IDS
from src.llm.credentials_guard import (
    USER_OWN_ENDPOINT_PROVIDERS,
    USER_PROVIDER_DEFAULT_BASES,
    ModelKeyRequiredError,
    ModelRouteNotAllowedError,
    is_user_view,
    validate_user_deployments,
    validate_user_endpoints,
    wire_provider,
)
from src.repositories.user_repo import UserRepository
from src.services.user_settings_service import UserSettingsService, is_secret_setting_key

# Data-source credentials: the only secrets a user inherits from the system layer. The
# data they fetch lands in the shared cache, and priority / adjust mode stay system-wide.
DATA_SOURCE_CREDENTIAL_KEYS: FrozenSet[str] = frozenset({
    "TUSHARE_TOKEN", "BIYING_LICENCE", "TICKFLOW_API_KEY", "MX_APIKEY",
    "FINNHUB_API_KEY", "ALPHAVANTAGE_API_KEY", "LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN", "LONGBRIDGE_OAUTH_CLIENT_ID",
})
# Search keys and endpoints: users bring their own.
SEARCH_KEYS: FrozenSet[str] = frozenset({
    "ANSPIRE_API_KEYS", "TAVILY_API_KEYS", "SERPAPI_API_KEYS", "BRAVE_API_KEYS", "BOCHA_API_KEYS",
    "MINIMAX_API_KEYS", "SEARXNG_BASE_URLS", "SEARXNG_PUBLIC_INSTANCES_ENABLED",
})
# Model keys outside the ``ai_model`` registry category.
_EXTRA_MODEL_KEYS: FrozenSet[str] = frozenset({"OLLAMA_API_BASE"})
# Model-name keys outside the ``LLM_*`` / ``LITELLM_*`` families (they decide which provider route is called).
_MODEL_NAME_KEYS: FrozenSet[str] = frozenset({"AGENT_LITELLM_MODEL", "VISION_MODEL", "OPENAI_VISION_MODEL"})
# User-scope keys that are per-user data / identity even though they are not secrets:
# the system watchlist is the primary user's, and a shared memory bank would mix users.
_NEVER_INHERIT_KEYS: FrozenSet[str] = frozenset({"STOCK_LIST", "HINDSIGHT_BANK_ID"})
# Notification preferences inherit; every other notification key is channel configuration.
_NOTIFICATION_PREFERENCE_PREFIXES = ("REPORT_", "NOTIFICATION_")
_NOTIFICATION_PREFERENCE_KEYS = frozenset({"SINGLE_STOCK_NOTIFY", "MERGE_EMAIL_NOTIFICATION"})
# System-scope keys that are blanked for non-primary users: server-side file / local CLI, and the system's own
# paid third-party data keys (a personal request must not spend them: US social sentiment stays off, the opening
# auction falls back to the free sources).
_BLANKED_SYSTEM_KEYS = ("LITELLM_CONFIG", "OPENCODE_CLI_MODEL", "SOCIAL_SENTIMENT_API_KEY", "ASHARE_API_KEY")
_ALLOWED_AGENT_BACKENDS = frozenset({"", "auto", "litellm"})


def inherits_from_system(key: str) -> bool:
    """Whether a non-primary user without their own value for a ``scope=user`` key gets the system value."""
    key_upper = key.upper()
    if key_upper in DATA_SOURCE_CREDENTIAL_KEYS:
        return True
    if key_upper in _NEVER_INHERIT_KEYS or key_upper in SEARCH_KEYS or key_upper in _EXTRA_MODEL_KEYS:
        return False
    field = get_field_definition(key_upper)
    category = field.get("category")
    if category == "ai_model":  # includes dynamic LLM_<NAME>_* channel keys
        return False
    if is_secret_setting_key(key_upper):
        return False
    if category == "notification" and not (
        key_upper.startswith(_NOTIFICATION_PREFERENCE_PREFIXES) or key_upper in _NOTIFICATION_PREFERENCE_KEYS
    ):
        return False
    return True


def has_usable_model(config: Config) -> bool:
    """Whether ``config`` can call a model: at least one channel / key-backed deployment, or a local CLI backend."""
    if config.llm_model_list:
        return True
    return (config.generation_backend or "").strip().lower() in LOCAL_CLI_GENERATION_BACKEND_IDS


def _build_env(user_values: Dict[str, str]) -> Dict[str, str]:
    """Environment mapping of a non-primary user: system env minus non-inherited keys plus own values."""
    env: Dict[str, str] = {}
    for key, value in os.environ.copy().items():
        if get_config_scope(key) == SCOPE_USER and not inherits_from_system(key):
            continue
        env[key] = value
    for key in _BLANKED_SYSTEM_KEYS:
        env.pop(key, None)
    # Never run a local CLI generation backend / Codex Agent on behalf of a non-primary user.
    env["GENERATION_BACKEND"] = "litellm"
    env["GENERATION_FALLBACK_BACKEND"] = "litellm"
    for key in ("AGENT_BACKEND", "AGENT_GENERATION_BACKEND"):
        if (env.get(key) or "").strip().lower() not in _ALLOWED_AGENT_BACKENDS:
            env[key] = "litellm"
    env["STOCK_LIST"] = ""
    env.update(user_values)
    return env


def is_model_route_setting_key(key: str) -> bool:
    """Whether changing ``key`` can change which provider route / endpoint a personal model call uses."""
    key_upper = key.upper()
    return (
        key_upper.startswith(("LLM_", "LITELLM_"))
        or key_upper in _EXTRA_MODEL_KEYS
        or key_upper in _MODEL_NAME_KEYS
        or get_field_definition(key_upper).get("category") == "ai_model"
    )


def validate_user_model_routes(current: Dict[str, str], pending: Dict[str, str]) -> None:
    """Raise ``ModelRouteNotAllowedError`` when ``current`` + ``pending`` personal settings would route a model
    call through a provider / endpoint a non-primary user may not use (``src/llm/credentials_guard.py``).

    Runs the real view build on the merged values (the same code that builds the view used at call time), so the
    wire models - protocol prefix, bare names, legacy keys - are exactly the ones LiteLLM would receive. Every
    deployment is route-checked; a configured model name that no deployment serves (``LITELLM_MODEL``, the
    fallback / light / Agent / vision model) must at least name an allowed provider. The endpoint DNS check is
    left to the call-time gate.
    """
    values = {**current, **pending}
    config = Config._load_from_env(_build_env(values), user_override_keys=frozenset(values) | {"STOCK_LIST"})
    validate_user_deployments(config)
    served = set(get_configured_llm_models(config.llm_model_list))
    names = [
        config.litellm_model, config.litellm_light_model, config.agent_litellm_model,
        *(config.litellm_fallback_models or []),
    ]
    if "OPENAI_VISION_MODEL" in values:  # ``VISION_MODEL`` itself is a system key: the user's view inherits it
        names.append(config.vision_model)
    for name in names:
        name = str(name or "").strip()
        if not name or name in served:
            continue
        provider = wire_provider(name)
        if provider not in USER_PROVIDER_DEFAULT_BASES and provider not in USER_OWN_ENDPOINT_PROVIDERS:
            raise ModelRouteNotAllowedError(
                "This model provider is not available for personal models (it uses the server's own credentials "
                "or local services); use a provider with your own API key, or an OpenAI-compatible endpoint of your own"
            )


def _build_user_config(user_id: str, system_config: Config) -> Config:
    user_values = UserSettingsService().get_plain_values(user_id)
    override_keys = frozenset(user_values) | {"STOCK_LIST"}
    config = Config._load_from_env(_build_env(user_values), user_override_keys=override_keys)
    # Data semantics are system-wide: the auto-injected realtime priority depends on which
    # paid tokens exist, so it must follow the system layer, not this user's credentials.
    config.realtime_source_priority = system_config.realtime_source_priority
    config.freeze_as_user_view(user_id)
    return config


@dataclass
class _CacheEntry:
    system_config: Config
    signature: Tuple[Any, ...]
    config: Config


_cache: Dict[str, _CacheEntry] = {}
_cache_lock = threading.Lock()


def get_user_config(user_id: str) -> Config:
    """Read-only, ``Config``-compatible view of ``user_id``'s effective configuration.

    The primary user gets the global config unchanged. Other users get a cached view that
    is rebuilt when their settings (or data key) change, or when the system config is reloaded.
    """
    user_id = (user_id or "").strip() if isinstance(user_id, str) else ""
    if not user_id:
        raise ValueError("user_id is required")
    system_config = get_config()
    if user_id == get_primary_user_id():
        return system_config

    signature = UserRepository().settings_signature(user_id)
    with _cache_lock:
        entry = _cache.get(user_id)
        if entry is not None and entry.system_config is system_config and entry.signature == signature:
            return entry.config
    config = _build_user_config(user_id, system_config)
    with _cache_lock:
        _cache[user_id] = _CacheEntry(system_config, signature, config)
    return config


def invalidate_user_config(user_id: Optional[str] = None) -> None:
    """Drop the cached view of one user (or of everyone when ``user_id`` is None)."""
    with _cache_lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


def user_model_key_configured(user_id: str) -> bool:
    """Whether the user's effective config has a usable model (drives the ``/me`` prompt)."""
    return has_usable_model(get_user_config(user_id))


def resolve_user_config(user_id: Optional[str]) -> Config:
    """Config of the acting user; ``None`` (CLI, bots, scheduled jobs) is the primary user, i.e. the system config."""
    if user_id is None:
        return get_config()
    return get_user_config(user_id)


def require_usable_model(config: Config) -> Config:
    """Gate in front of every model call made for a user; returns ``config`` when the call may proceed.

    The primary user's (system) config passes unchanged. A non-primary view must have a model of its
    own - every deployment with its own key - and its endpoints must still be public https hosts
    (re-checked now, the DNS answer may have changed since the settings were saved). Raises
    :class:`ModelKeyRequiredError` / :class:`UnsafeUrlError` before anything is sent to a provider.
    """
    if not is_user_view(config):
        return config
    if not has_usable_model(config):
        raise ModelKeyRequiredError()
    validate_user_deployments(config)
    validate_user_endpoints(config)
    return config


def require_user_model(user_id: Optional[str]) -> Config:
    """``resolve_user_config`` + :func:`require_usable_model`."""
    return require_usable_model(resolve_user_config(user_id))
