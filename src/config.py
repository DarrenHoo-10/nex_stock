# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 配置管理模块
===================================

职责：
1. 使用单例模式管理全局配置
2. 从 .env 文件加载敏感配置
3. 提供类型安全的配置访问接口
"""

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple
from urllib.parse import unquote, urlparse
from dotenv import load_dotenv, dotenv_values
from dataclasses import dataclass, field

from src.core.config_manager import unescape_compose_sensitive_env_value
from src.report_language import (
    is_supported_report_language_value,
    normalize_report_language,
)
from src.notification_routing import parse_notification_route_channels
from src.notification_noise import (
    NOTIFICATION_SEVERITIES,
    is_supported_notification_severity,
    parse_notification_quiet_hours,
    validate_notification_timezone,
)
from src.notification_contracts import (
    is_feishu_app_bot_configured,
    is_feishu_static_configured,
)
from src.services.stock_list_parser import split_stock_list
from src.llm.backend_registry import (
    AUTO_AGENT_BACKEND_ID,
    GENERATION_ONLY_BACKEND_IDS,
    LOCAL_CLI_GENERATION_BACKEND_IDS,
    LITELLM_BACKEND_ID,
    OPENCODE_CLI_BACKEND_ID,
    SUPPORTED_AGENT_GENERATION_BACKENDS,
    SUPPORTED_AGENT_UI_BACKENDS,
    SUPPORTED_GENERATION_BACKENDS,
)
from src.llm.local_cli_backend import (
    DEFAULT_GENERATION_BACKEND_MAX_CONCURRENCY,
    DEFAULT_LOCAL_CLI_BACKEND_MAX_CONCURRENCY,
    DEFAULT_LOCAL_CLI_MAX_OUTPUT_BYTES,
    DEFAULT_LOCAL_CLI_TIMEOUT_SECONDS,
    MAX_GENERATION_BACKEND_MAX_CONCURRENCY,
    MAX_LOCAL_CLI_BACKEND_MAX_CONCURRENCY,
    MAX_LOCAL_CLI_OUTPUT_BYTES,
    MAX_LOCAL_CLI_TIMEOUT_SECONDS,
)
from src.llm import generation_params as llm_generation_params
from src.llm.hermes import (
    HERMES_DEFAULT_BASE_URL,
    HERMES_DEFAULT_MODEL,
    HERMES_DEFAULT_PROTOCOL,
    HermesConfigIssue,
    hermes_blocked_route_candidates,
    hermes_model_info,
    is_reserved_hermes_name,
    parse_hermes_channel,
    route_identity_candidates,
    route_deployment_origins,
    route_has_hermes,
)
from src.scheduler import normalize_schedule_times
from src.utils.market_review_region import normalize_market_review_region_lenient

logger = logging.getLogger(__name__)

@dataclass
class ConfigIssue:
    """Structured configuration validation issue with a severity level.

    Attributes:
        severity: One of "error", "warning", or "info".
        message:  Human-readable description of the issue.
        field:    The environment variable / config field name most relevant to
                  this issue (empty string when not applicable).
    """

    severity: Literal["error", "warning", "info"]
    message: str
    field: str = ""
    code: str = ""

    def __str__(self) -> str:  # noqa: D105
        return self.message


_MANAGED_LITELLM_KEY_PROVIDERS = {"gemini", "vertex_ai", "anthropic", "openai", "deepseek"}
SUPPORTED_LLM_CHANNEL_PROTOCOLS = ("openai", "anthropic", "gemini", "vertex_ai", "deepseek", "ollama")
SUPPORTED_LLM_CHANNEL_API_SURFACES = ("chat_completions", "responses")
_FALLBACK_LITELLM_MODEL_PROVIDERS = _MANAGED_LITELLM_KEY_PROVIDERS | set(SUPPORTED_LLM_CHANNEL_PROTOCOLS) | {
    "minimax",
    "cohere",
    "huggingface",
    "bedrock",
    "sagemaker",
    "azure",
    "replicate",
    "together_ai",
    "palm",
    "text-completion-openai",
    "command-r",
    "groq",
    "cerebras",
    "fireworks_ai",
    "friendliai",
    "openrouter",
    "xai",
}
_FALSEY_ENV_VALUES = {"0", "false", "no", "off"}
PROMPT_CACHE_DIAGNOSTICS_LEVELS = {"off", "basic", "debug"}
SUPPORTED_AGENT_BACKENDS = {"auto", "litellm", "codex_app_server"}
TICKFLOW_KLINE_ADJUST_VALUES = {"none", "forward", "backward", "forward_additive", "backward_additive"}
# 沪深300 / 中证500 / 创业板 / 红利低波 / 纳指 / 黄金；防守资产为货币 ETF
DEFAULT_ETF_ROTATION_POOL = ("510300", "510500", "159915", "512890", "513100", "518880")
DEFAULT_ETF_ROTATION_SAFE_ASSET = "511880"
# Fallback defaults used when ANSPIRE_API_KEYS is reused as legacy OpenAI-compatible source.
# These are compatibility examples; actual availability should be validated by Anspire console/model entitlement.
ANSPIRE_LLM_BASE_URL_DEFAULT = "https://open-gateway.anspire.cn/v6"
ANSPIRE_LLM_MODEL_DEFAULT = "Doubao-Seed-2.0-lite"
MODELFLARE_LLM_BASE_URL_DEFAULT = "https://modelflare.dev/v1"
MODELFLARE_LLM_MODEL_DEFAULT = "openai/gpt-6.1-sol"


DEFAULT_PRIMARY_USER_ID = "local"


def _has_ntfy_topic_endpoint(value: Optional[str]) -> bool:
    """Return whether an ntfy URL points at a concrete topic endpoint."""
    raw_url = (value or "").strip()
    if not raw_url:
        return False
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    return any(unquote(segment).strip() for segment in parsed.path.split("/") if segment)


def _has_gotify_base_url(value: Optional[str]) -> bool:
    """Return whether a Gotify URL points at a server base URL, not /message."""
    raw_url = (value or "").strip().rstrip("/")
    if not raw_url:
        return False
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.query or parsed.fragment:
        return False
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    return not (path_segments and path_segments[-1].lower() == "message")


def normalize_tickflow_kline_adjust(value: Optional[str]) -> str:
    """Normalize TickFlow daily K-line adjustment mode."""
    normalized = (value or "none").strip().lower()
    if normalized in TICKFLOW_KLINE_ADJUST_VALUES:
        return normalized
    logger.warning(
        "Invalid TICKFLOW_KLINE_ADJUST=%r; falling back to none",
        value,
    )
    return "none"


def parse_prompt_cache_diagnostics_level(value: Optional[str]) -> str:
    """Parse prompt-cache diagnostics level with a conservative fallback."""
    normalized = (value or "off").strip().lower()
    if normalized in PROMPT_CACHE_DIAGNOSTICS_LEVELS:
        return normalized
    logger.warning(
        "Invalid LLM_PROMPT_CACHE_DIAGNOSTICS_LEVEL=%r; falling back to off",
        value,
    )
    return "off"


AGENT_MAX_STEPS_DEFAULT = 10
FUNDAMENTAL_STAGE_TIMEOUT_SECONDS_DEFAULT = 8.0
NEWS_STRATEGY_WINDOWS: Dict[str, int] = {
    "ultra_short": 1,
    "short": 3,
    "medium": 7,
    "long": 30,
}


@dataclass(frozen=True)
class AgentContextCompressionPreset:
    """Preset values for visible chat history compression."""

    trigger_tokens: int
    protected_turns: int
    summary_target_tokens: int
    # P1 reserves this budget for future prompt-size controls; it is not
    # enforced by the current rolling-summary state table.
    history_budget_tokens: int


AGENT_CONTEXT_COMPRESSION_DEFAULT_PROFILE = "balanced"
AGENT_CONTEXT_COMPRESSION_PROFILES: Dict[str, AgentContextCompressionPreset] = {
    "cost": AgentContextCompressionPreset(
        trigger_tokens=6000,
        protected_turns=2,
        summary_target_tokens=900,
        history_budget_tokens=4000,
    ),
    "balanced": AgentContextCompressionPreset(
        trigger_tokens=12000,
        protected_turns=4,
        summary_target_tokens=1500,
        history_budget_tokens=8000,
    ),
    "long_context_raw_first": AgentContextCompressionPreset(
        trigger_tokens=24000,
        protected_turns=6,
        summary_target_tokens=2600,
        history_budget_tokens=14000,
    ),
}


def parse_env_bool(value: Optional[str], default: bool = False) -> bool:
    """Parse common truthy/falsey environment-style values."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if not normalized:
        return default
    return normalized not in _FALSEY_ENV_VALUES


def parse_env_int(
    value: Optional[str],
    default: int,
    *,
    field_name: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Parse an integer env value with warning + fallback semantics."""
    raw_value = value
    if raw_value is None or not str(raw_value).strip():
        parsed = int(default)
    else:
        try:
            parsed = int(str(raw_value).strip())
        except (TypeError, ValueError):
            logger.warning(
                "%s=%r is not a valid integer; falling back to %s",
                field_name,
                raw_value,
                default,
            )
            parsed = int(default)

    if minimum is not None and parsed < minimum:
        logger.warning(
            "%s=%r is below minimum %s; clamping to %s",
            field_name,
            parsed,
            minimum,
            minimum,
        )
        parsed = minimum
    if maximum is not None and parsed > maximum:
        logger.warning(
            "%s=%r is above maximum %s; clamping to %s",
            field_name,
            parsed,
            maximum,
            maximum,
        )
        parsed = maximum
    return parsed


def _parse_reasoning_effort(value: Optional[str], field_name: str) -> str:
    """Parse a reasoning effort setting: blank -> ``""``; unknown value -> warning + ``""`` (not sent)."""
    text = (value or '').strip()
    if not text:
        return ''
    normalized = llm_generation_params.normalize_reasoning_effort(text)
    if not normalized:
        logger.warning(
            "%s=%r is not one of %s; the reasoning effort is not sent",
            field_name,
            value,
            ", ".join(llm_generation_params.REASONING_EFFORT_VALUES),
        )
    return normalized


def get_primary_reasoning_effort(config: Any) -> str:
    """``LLM_REASONING_EFFORT``: the primary analysis model and its fallbacks (``""`` = not sent)."""
    return llm_generation_params.normalize_reasoning_effort(getattr(config, "llm_reasoning_effort", ""))


def get_agent_reasoning_effort(config: Any) -> str:
    """``AGENT_REASONING_EFFORT`` for assistant / agent chat; empty follows ``LLM_REASONING_EFFORT``."""
    explicit = llm_generation_params.normalize_reasoning_effort(getattr(config, "agent_reasoning_effort", ""))
    return explicit or get_primary_reasoning_effort(config)


def get_light_reasoning_effort(config: Any) -> str:
    """``LITELLM_LIGHT_REASONING_EFFORT`` for light-model chores (``""`` = not sent)."""
    return llm_generation_params.normalize_reasoning_effort(getattr(config, "litellm_light_reasoning_effort", ""))


def _parse_optional_pct(value: Optional[str], *, field_name: str) -> Optional[float]:
    """Parse an optional percentage in (0, 100]; blank or invalid -> ``None`` (invalid logs a warning)."""
    text = (value or '').strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        parsed = 0.0
    if not 0 < parsed <= 100:
        logger.warning("%s=%r is not a percentage in (0, 100]; ignoring it", field_name, value)
        return None
    return parsed


def _parse_stop_mode(value: Optional[str]) -> str:
    """``POSITION_STOP_MODE``: ``fixed`` (default) or ``nearer``; unknown -> warning + ``fixed``."""
    text = (value or '').strip().lower()
    if not text:
        return 'fixed'
    if text not in ('fixed', 'nearer'):
        logger.warning("POSITION_STOP_MODE=%r is not fixed / nearer; falling back to fixed", value)
        return 'fixed'
    return text


def _parse_weekday(value: Optional[str], default: int, *, field_name: str) -> int:
    """Parse an ISO weekday (1=Monday ... 7=Sunday); blank -> default, invalid / out of range -> warning + default."""
    text = (value or '').strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError:
        parsed = 0
    if not 1 <= parsed <= 7:
        logger.warning("%s=%r is not an ISO weekday (1=Mon ... 7=Sun); falling back to %s", field_name, value, default)
        return default
    return parsed


def _parse_hhmm(value: Optional[str], default: str, *, field_name: str) -> str:
    """Parse an ``HH:MM`` env value; blank -> default, invalid -> warning + default."""
    text = (value or '').strip()
    if not text:
        return default
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', text):
        logger.warning("%s=%r is not a valid HH:MM time; falling back to %s", field_name, text, default)
        return default
    return text


def parse_env_float(
    value: Optional[str],
    default: float,
    *,
    field_name: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """Parse a float env value with warning + fallback semantics."""
    raw_value = value
    if raw_value is None or not str(raw_value).strip():
        parsed = float(default)
    else:
        try:
            parsed = float(str(raw_value).strip())
        except (TypeError, ValueError):
            logger.warning(
                "%s=%r is not a valid number; falling back to %s",
                field_name,
                raw_value,
                default,
            )
            parsed = float(default)

    if minimum is not None and parsed < minimum:
        logger.warning(
            "%s=%r is below minimum %s; clamping to %s",
            field_name,
            parsed,
            minimum,
            minimum,
        )
        parsed = minimum
    if maximum is not None and parsed > maximum:
        logger.warning(
            "%s=%r is above maximum %s; clamping to %s",
            field_name,
            parsed,
            maximum,
            maximum,
        )
        parsed = maximum
    return parsed


def normalize_news_strategy_profile(value: Optional[str]) -> str:
    """Normalize news strategy profile to known values."""
    candidate = (value or "short").strip().lower()
    return candidate if candidate in NEWS_STRATEGY_WINDOWS else "short"


def resolve_news_window_days(news_max_age_days: int, news_strategy_profile: Optional[str]) -> int:
    """Resolve effective news window days from profile and global max-age."""
    profile = normalize_news_strategy_profile(news_strategy_profile)
    profile_days = NEWS_STRATEGY_WINDOWS.get(profile, NEWS_STRATEGY_WINDOWS["short"])
    return max(1, min(max(1, int(news_max_age_days)), profile_days))


def normalize_agent_context_compression_profile(value: Optional[str]) -> str:
    """Normalize visible-chat context compression profile values."""
    candidate = (value or AGENT_CONTEXT_COMPRESSION_DEFAULT_PROFILE).strip().lower()
    if candidate in AGENT_CONTEXT_COMPRESSION_PROFILES:
        return candidate
    logger.warning(
        "Invalid AGENT_CONTEXT_COMPRESSION_PROFILE=%r; falling back to %s",
        value,
        AGENT_CONTEXT_COMPRESSION_DEFAULT_PROFILE,
    )
    return AGENT_CONTEXT_COMPRESSION_DEFAULT_PROFILE


def get_agent_context_compression_preset(profile: Optional[str]) -> AgentContextCompressionPreset:
    """Return the preset for a normalized profile, falling back to balanced."""
    normalized = normalize_agent_context_compression_profile(profile)
    return AGENT_CONTEXT_COMPRESSION_PROFILES[normalized]


def parse_agent_context_compression_int(
    value: Optional[str],
    default: int,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    """Parse compression integers; empty/invalid/out-of-range values follow preset defaults."""
    raw_value = value
    if raw_value is None or not str(raw_value).strip():
        return int(default)
    try:
        parsed = int(str(raw_value).strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a valid integer; falling back to preset default %s",
            field_name,
            raw_value,
            default,
        )
        return int(default)
    if parsed < minimum or parsed > maximum:
        logger.warning(
            "%s=%r is outside supported range [%s, %s]; falling back to preset default %s",
            field_name,
            parsed,
            minimum,
            maximum,
            default,
        )
        return int(default)
    return parsed


def canonicalize_llm_channel_protocol(value: Optional[str]) -> str:
    """Normalize a protocol label into a LiteLLM provider identifier."""
    candidate = (value or "").strip().lower().replace("-", "_")
    aliases = {
        "openai_compatible": "openai",
        "openai_compat": "openai",
        "claude": "anthropic",
        "google": "gemini",
        "vertex": "vertex_ai",
        "vertexai": "vertex_ai",
    }
    return aliases.get(candidate, candidate)


def canonicalize_llm_channel_api_surface(value: Optional[str]) -> str:
    """Normalize an LLM channel endpoint surface label."""
    candidate = (value or "").strip().lower().replace("-", "_")
    aliases = {
        "chat": "chat_completions",
        "chat_completion": "chat_completions",
        "completions": "chat_completions",
        "response": "responses",
        "responses_api": "responses",
    }
    return aliases.get(candidate, candidate)


def normalize_llm_channel_api_surface(value: Optional[str]) -> str:
    """Return a supported endpoint surface, defaulting to Chat Completions."""
    normalized = canonicalize_llm_channel_api_surface(value)
    if normalized in SUPPORTED_LLM_CHANNEL_API_SURFACES:
        return normalized
    return "chat_completions"


def is_supported_llm_channel_api_surface_value(value: Optional[str]) -> bool:
    """Return whether a raw API surface is blank or recognized."""
    canonical = canonicalize_llm_channel_api_surface(value)
    return not canonical or canonical in SUPPORTED_LLM_CHANNEL_API_SURFACES


@lru_cache(maxsize=1)
def get_litellm_model_providers() -> frozenset[str]:
    """Return provider identifiers from the installed LiteLLM routing enum.

    LiteLLM adds direct providers independently of this repository. Loading
    its enum keeps channel validation aligned with the actual router instead
    of relying on a permanently incomplete local allow-list. The fallback is
    only for lightweight test stubs or a broken optional import; a production
    installation gets the complete provider set from its pinned LiteLLM.
    """
    providers = set(_FALLBACK_LITELLM_MODEL_PROVIDERS)
    try:
        from litellm.types.utils import LlmProviders

        providers.update(
            str(provider.value).strip().lower()
            for provider in LlmProviders
            if str(getattr(provider, "value", "")).strip()
        )
    except (ImportError, AttributeError, TypeError):
        logger.debug("LiteLLM provider metadata unavailable; using the compatibility fallback")
    return frozenset(providers)


def get_explicit_llm_channel_model_provider(model: str) -> str:
    """Return the explicit LiteLLM provider prefix, if the model has one.

    A slash alone does not establish a provider: OpenAI-compatible gateways
    commonly expose provider-owned IDs such as ``Qwen/Qwen3`` or
    ``deepseek-ai/DeepSeek-V3``. Only prefixes understood as LiteLLM providers
    are treated as routing declarations.
    """
    normalized_model = (model or "").strip()
    if "/" not in normalized_model:
        return ""
    raw_prefix = normalized_model.split("/", 1)[0].lower()
    canonical_prefix = canonicalize_llm_channel_protocol(raw_prefix)
    providers = get_litellm_model_providers()
    if raw_prefix in providers:
        return raw_prefix
    if canonical_prefix in providers:
        return canonical_prefix
    return ""


def apply_litellm_api_surface(model: str, api_surface: Optional[str]) -> str:
    """Encode an explicit API surface in a LiteLLM wire model.

    LiteLLM's ``provider/responses/model`` convention keeps the public Router
    alias stable while letting ``completion()`` bridge messages, streaming,
    tools, responses, and usage through the provider's Responses endpoint.
    """
    normalized_model = (model or "").strip()
    if not normalized_model or normalize_llm_channel_api_surface(api_surface) != "responses":
        return normalized_model
    provider = get_explicit_llm_channel_model_provider(normalized_model)
    if provider != "openai":
        raise ValueError(
            "Responses API surface requires a normalized openai/<model> route; "
            f"got {normalized_model!r}"
        )
    provider, remainder = normalized_model.split("/", 1)
    if remainder.startswith("responses/"):
        return normalized_model
    return f"{provider}/responses/{remainder}"


def resolve_llm_channel_base_url(channel_name: str, base_url: Optional[str]) -> str:
    """Apply named-channel endpoint defaults without changing explicit configuration."""
    explicit = (base_url or "").strip()
    if explicit:
        return explicit
    if channel_name.strip().lower() == "modelflare":
        return MODELFLARE_LLM_BASE_URL_DEFAULT
    return ""


def resolve_llm_channel_names(config_map: Mapping[str, str]) -> str:
    """Infer the Modelflare channel from its key unless channels are explicit."""
    channels = (config_map.get("LLM_CHANNELS") or "").strip()
    if channels:
        return channels
    if any(
        key.strip()
        for field in ("MODELFLARE_KEY", "LLM_MODELFLARE_API_KEY", "LLM_MODELFLARE_API_KEYS")
        for key in (config_map.get(field) or "").split(',')
    ):
        return "modelflare"
    return ""


def resolve_llm_channel_api_keys(
    channel_name: str, api_keys: List[str], config_map: Mapping[str, str],
) -> List[str]:
    """Keep explicit channel keys first, with MODELFLARE_KEY as its shorthand."""
    if api_keys or channel_name.strip().lower() != "modelflare":
        return api_keys
    return [key.strip() for key in (config_map.get("MODELFLARE_KEY") or "").split(',') if key.strip()]


def resolve_llm_channel_models(
    channel_name: str,
    models: List[str],
    *,
    channel_names: str,
    primary_model: str = "",
    fallback_models: str = "",
) -> List[str]:
    """Resolve explicit routes or the default for a single Modelflare channel."""
    if models:
        return models
    names = [name.strip().lower() for name in channel_names.split(',') if name.strip()]
    primary = primary_model.strip() or MODELFLARE_LLM_MODEL_DEFAULT
    if (names != ["modelflare"] or channel_name.strip().lower() != "modelflare"
            or not primary.startswith("openai/") or not primary[7:].strip()):
        return []
    routes = [primary] + [model.strip() for model in fallback_models.split(',')]
    return list(dict.fromkeys(
        route for route in routes if route.startswith("openai/") and route[7:].strip()
    ))


def resolve_llm_channel_protocol(
    protocol: Optional[str],
    *,
    base_url: Optional[str] = None,
    models: Optional[List[str]] = None,
    channel_name: Optional[str] = None,
) -> str:
    """Resolve the effective protocol for a channel."""
    explicit = canonicalize_llm_channel_protocol(protocol)
    if explicit in SUPPORTED_LLM_CHANNEL_PROTOCOLS:
        return explicit

    # Requesty is OpenAI-compatible; its vendor/model IDs (anthropic/...,
    # vertex/...) are not LiteLLM protocol declarations.
    if is_requesty_gateway_base_url(base_url):
        return "openai"

    for model in models or []:
        if "/" not in model:
            continue
        prefix = canonicalize_llm_channel_protocol(model.split("/", 1)[0])
        if prefix in SUPPORTED_LLM_CHANNEL_PROTOCOLS:
            return prefix

    # Infer from channel name (e.g. "deepseek" -> deepseek, "gemini" -> gemini)
    if channel_name:
        name_protocol = canonicalize_llm_channel_protocol(channel_name)
        if name_protocol in SUPPORTED_LLM_CHANNEL_PROTOCOLS:
            return name_protocol

    if base_url:
        parsed = urlparse(base_url)
        if parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}:
            # Default to openai for local servers (vLLM, LM Studio, LocalAI, etc.).
            # Ollama users should set PROTOCOL=ollama explicitly or name the channel "ollama".
            return "openai"
        return "openai"

    return ""


def channel_allows_empty_api_key(protocol: Optional[str], base_url: Optional[str]) -> bool:
    """Return True when a channel can run without an API key."""
    resolved_protocol = resolve_llm_channel_protocol(protocol, base_url=base_url)
    if resolved_protocol == "ollama":
        return True
    parsed = urlparse(base_url or "")
    return parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"}


def is_requesty_gateway_base_url(base_url: Optional[str]) -> bool:
    """Return whether a Base URL points at the Requesty router (any region)."""
    raw_url = (base_url or "").strip()
    if not raw_url:
        return False
    try:
        hostname = (urlparse(raw_url).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "requesty.ai" or hostname.endswith(".requesty.ai")


def route_discovered_llm_model(model_id: str, base_url: Optional[str]) -> str:
    """Return the saved channel value for an ID listed by a gateway ``/models``.

    Requesty lists ``vendor/model`` IDs (``openai/gpt-4o-mini``,
    ``anthropic/claude-sonnet-4-6``, ``vertex/claude-sonnet-4-5``) and managed
    policy IDs without a slash. The vendor segment collides with LiteLLM
    provider names, so the saved value carries the OpenAI-compatible gateway
    route once (``openai/openai/gpt-4o-mini``); LiteLLM strips that layer and
    sends the full Requesty ID unchanged.
    """
    normalized_id = (model_id or "").strip()
    if not normalized_id or not is_requesty_gateway_base_url(base_url):
        return normalized_id
    return f"openai/{normalized_id}"


def normalize_llm_channel_model(model: str, protocol: Optional[str], base_url: Optional[str] = None) -> str:
    """Attach a provider prefix when the model omits it."""
    normalized_model = model.strip()
    if not normalized_model:
        return normalized_model

    resolved_protocol = resolve_llm_channel_protocol(protocol, base_url=base_url, models=[normalized_model])

    if (
        "/" in normalized_model
        and resolved_protocol == "openai"
        and is_requesty_gateway_base_url(base_url)
        and normalized_model.split("/", 1)[0].lower() != "openai"
    ):
        # Requesty vendor IDs such as anthropic/... or vertex/... must stay
        # intact behind the OpenAI-compatible gateway route instead of being
        # read as LiteLLM direct providers.
        return f"openai/{normalized_model}"

    if "/" in normalized_model:
        # The model already has a slash, e.g. 'deepseek-ai/DeepSeek-V3'.
        # Check if the prefix is a known LiteLLM provider; if so, keep it.
        # Otherwise (e.g. HuggingFace-style IDs on SiliconFlow), prepend
        # the resolved protocol so LiteLLM routes via the correct handler.
        raw_prefix, remainder = normalized_model.split("/", 1)
        prefix = raw_prefix.lower()
        canonical_prefix = canonicalize_llm_channel_protocol(prefix)
        providers = get_litellm_model_providers()
        if prefix in providers:
            return normalized_model
        if canonical_prefix in providers:
            return f"{canonical_prefix}/{remainder}"
        # Not a real provider prefix — add one so LiteLLM routes correctly.
        if resolved_protocol:
            return f"{resolved_protocol}/{normalized_model}"
        return normalized_model

    if not resolved_protocol:
        return normalized_model
    return f"{resolved_protocol}/{normalized_model}"


def find_incompatible_llm_channel_models(
    models: List[str],
    protocol: Optional[str],
    api_surface: Optional[str],
    base_url: Optional[str] = None,
) -> List[str]:
    """Return models whose actual LiteLLM route conflicts with the surface.

    Responses routing is implemented through LiteLLM's OpenAI bridge, so both
    the channel protocol and every normalized model route must resolve to the
    OpenAI provider. This is the shared invariant used by validation, runtime
    loading, diagnostics, and screening.
    """
    if normalize_llm_channel_api_surface(api_surface) != "responses":
        return []
    resolved_protocol = resolve_llm_channel_protocol(
        protocol,
        base_url=base_url,
        models=models,
    )
    if resolved_protocol != "openai":
        return [model for model in models if (model or "").strip()]
    incompatible: List[str] = []
    for model in models:
        normalized_model = normalize_llm_channel_model(model, resolved_protocol, base_url)
        if normalized_model and get_explicit_llm_channel_model_provider(normalized_model) != "openai":
            incompatible.append(model)
    return incompatible


def find_llm_channel_surface_conflicts(
    channels: List[Dict[str, Any]],
) -> Dict[str, Tuple[str, ...]]:
    """Return public route aliases declared with more than one API surface."""
    route_surfaces: Dict[str, set[str]] = {}
    for channel in channels:
        if not isinstance(channel, dict) or not channel.get("enabled", True):
            continue
        protocol = str(channel.get("protocol") or "")
        base_url = str(channel.get("base_url") or "")
        surface = normalize_llm_channel_api_surface(channel.get("api_surface"))
        for raw_model in channel.get("models") or []:
            model = normalize_llm_channel_model(str(raw_model), protocol, base_url)
            if model:
                route_surfaces.setdefault(model, set()).add(surface)
    return {
        model: tuple(sorted(surfaces))
        for model, surfaces in route_surfaces.items()
        if len(surfaces) > 1
    }


def get_configured_llm_models(model_list: List[Dict[str, Any]]) -> List[str]:
    """Return non-legacy model names declared in Router model_list order.

    Uses the top-level ``model_name`` (the routing alias that users set in
    LITELLM_MODEL) rather than ``litellm_params.model`` (the wire-level
    model identifier).  For channel-built entries both are identical, but
    YAML configs may define a friendly alias that differs from the
    underlying provider/model path.
    """
    models: List[str] = []
    seen: set = set()
    for entry in model_list or []:
        # Prefer top-level model_name (router routing key); fall back to
        # litellm_params.model for entries that omit it.
        name = str(entry.get("model_name") or "").strip()
        if not name:
            params = entry.get("litellm_params", {}) or {}
            name = str(params.get("model") or "").strip()
        if not name or name.startswith("__legacy_") or name in seen:
            continue
        seen.add(name)
        models.append(name)
    return models


def resolve_litellm_wire_model(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Resolve a router alias to its underlying LiteLLM wire model."""
    return llm_generation_params.resolve_litellm_wire_model(model, model_list)


def resolve_litellm_thinking_enabled(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """Resolve whether the outgoing LiteLLM request explicitly enables thinking."""
    return llm_generation_params.resolve_litellm_thinking_enabled(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )


def get_fixed_litellm_temperature(
    model: str,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Return a provider-mandated temperature for known strict models."""
    return llm_generation_params.get_fixed_litellm_temperature(
        model,
        model_list=model_list,
        request_overrides=request_overrides,
    )


def normalize_litellm_temperature(
    model: str,
    temperature: Optional[float],
    *,
    default: float = 0.7,
    model_list: Optional[List[Dict[str, Any]]] = None,
    request_overrides: Optional[Dict[str, Any]] = None,
) -> float:
    """Normalize temperature before sending a LiteLLM request."""
    return llm_generation_params.normalize_litellm_temperature(
        model,
        temperature,
        default=default,
        model_list=model_list,
        request_overrides=request_overrides,
    )


def resolve_unified_llm_temperature(model: str, env: Optional[Mapping[str, str]] = None) -> float:
    """Resolve the raw unified LLM temperature with backward-compatible fallbacks."""
    env = os.environ if env is None else env
    llm_temperature_raw = env.get("LLM_TEMPERATURE")
    if llm_temperature_raw and llm_temperature_raw.strip():
        try:
            return float(llm_temperature_raw)
        except (ValueError, TypeError):
            pass

    provider_temperature_env = {
        "gemini": "GEMINI_TEMPERATURE",
        "vertex_ai": "GEMINI_TEMPERATURE",
        "anthropic": "ANTHROPIC_TEMPERATURE",
        "openai": "OPENAI_TEMPERATURE",
        "deepseek": "OPENAI_TEMPERATURE",
    }
    preferred_env = provider_temperature_env.get(_get_litellm_provider(model))
    if preferred_env:
        preferred_value = env.get(preferred_env)
        if preferred_value and preferred_value.strip():
            try:
                return float(preferred_value)
            except (ValueError, TypeError):
                pass

    for env_name in ("GEMINI_TEMPERATURE", "ANTHROPIC_TEMPERATURE", "OPENAI_TEMPERATURE"):
        env_value = env.get(env_name)
        if env_value and env_value.strip():
            try:
                return float(env_value)
            except (ValueError, TypeError):
                continue

    return 0.7


def _get_litellm_provider(model: str) -> str:
    """Extract the LiteLLM provider prefix from a model string."""
    if not model:
        return ""
    if "/" in model:
        return model.split("/", 1)[0]
    return "openai"


def _uses_direct_env_provider(model: str) -> bool:
    """Whether runtime handles the model via direct litellm env/provider resolution."""
    provider = _get_litellm_provider(model)
    return bool(provider) and provider not in _MANAGED_LITELLM_KEY_PROVIDERS


def _matches_route_set(model: str, routes: set[str]) -> bool:
    """Loose safety match for Hermes/provenance checks, not normal route availability."""
    return bool(route_identity_candidates(model) & set(routes or set()))


def _matches_exact_route(model: str, routes: set[str]) -> bool:
    """Match the Router's top-level model_name exactly for normal availability checks."""
    normalized_model = str(model or "").strip()
    return bool(normalized_model) and normalized_model in set(routes or set())


def normalize_agent_litellm_model(
    model: str,
    configured_models: Optional[set[str]] = None,
) -> str:
    """Normalize AGENT_LITELLM_MODEL while preserving configured router aliases."""
    normalized_model = (model or "").strip()
    if not normalized_model:
        return ""
    if "/" not in normalized_model:
        if configured_models and normalized_model in configured_models:
            return normalized_model
        return f"openai/{normalized_model}"
    return normalized_model


def get_effective_agent_primary_model(config: "Config") -> str:
    """Return the effective Agent primary model with fallback inheritance."""
    configured_router_models = set(
        get_configured_llm_models(getattr(config, "llm_model_list", []) or [])
    )
    configured_agent_model = normalize_agent_litellm_model(
        getattr(config, "agent_litellm_model", ""),
        configured_models=configured_router_models,
    )
    if configured_agent_model:
        return configured_agent_model
    return (getattr(config, "litellm_model", "") or "").strip()


def get_effective_light_model(config: "Config") -> str:
    """Return the model for light chores (classification, tagging, short summaries).

    ``LITELLM_LIGHT_MODEL`` uses the same value format as ``LITELLM_MODEL`` (including channel
    model names from ``LLM_CHANNELS``); empty falls back to the effective primary model.
    """
    configured_light_model = (getattr(config, "litellm_light_model", "") or "").strip()
    if configured_light_model:
        return _resolve_against_configured_models(config, configured_light_model)
    return (getattr(config, "litellm_model", "") or "").strip()


def _resolve_against_configured_models(config: "Config", model: str) -> str:
    """Map a model typed as listed in a channel (``auto/x``) to its routed name (``openai/auto/x``).

    Channel models get a provider prefix when the channels are parsed, so the name a user copies
    from the channel's model list can differ from the routed name. A unique suffix match wins;
    Otherwise a name whose prefix is not a LiteLLM provider (e.g. a gateway group such as
    ``auto/x`` in the legacy single-endpoint setup) is routed through the OpenAI-compatible
    endpoint, the same default ``normalize_agent_litellm_model`` uses for bare names.
    """
    configured = set(get_configured_llm_models(getattr(config, "llm_model_list", []) or []))
    if model in configured:
        return model
    candidates = sorted(m for m in configured if "/" in m and m.split("/", 1)[1] == model)
    if len(candidates) == 1:
        return candidates[0]
    if "/" in model:
        prefix = model.split("/", 1)[0].lower()
        try:
            providers = get_litellm_model_providers()
        except Exception:  # noqa: BLE001 - provider list is best effort
            providers = set()
        if providers and prefix not in providers:
            return f"openai/{model}"
    return model


def get_effective_agent_models_to_try(config: "Config") -> List[str]:
    """Return Agent model try-order: primary + global fallbacks (deduped)."""
    configured_router_models = set(
        get_configured_llm_models(getattr(config, "llm_model_list", []) or [])
    )
    raw_models = [get_effective_agent_primary_model(config)] + (
        getattr(config, "litellm_fallback_models", []) or []
    )
    seen = set()
    ordered_models: List[str] = []
    for model in raw_models:
        normalized_model = (model or "").strip()
        if not normalized_model:
            continue
        dedupe_key = normalize_agent_litellm_model(
            normalized_model,
            configured_models=configured_router_models,
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        ordered_models.append(normalized_model)
    return ordered_models


def setup_env(override: bool = False):
    """
    Initialize environment variables from .env file.

    Args:
        override: If True, overwrite existing environment variables with values
                  from .env file. Set to True when reloading config after updates.
                  Default is False to preserve behavior on initial load where
                  system environment variables take precedence.
    """
    Config._capture_bootstrap_runtime_env_overrides()
    # src/config.py -> src/ -> root
    env_file = os.getenv("ENV_FILE")
    if env_file:
        env_path = Path(env_file)
    else:
        env_path = Path(__file__).parent.parent / '.env'
    compose_sensitive_keys = ("CUSTOM_WEBHOOK_BODY_TEMPLATE",)
    preexisting_compose_sensitive_keys = {
        key for key in compose_sensitive_keys if key in os.environ
    }
    load_dotenv(dotenv_path=env_path, override=override)
    try:
        raw_env_values = dotenv_values(env_path, interpolate=False)
    except Exception as exc:  # pragma: no cover - defensive branch
        logger.warning("Failed to read raw .env values from %s: %s", env_path, exc)
        return

    key = "CUSTOM_WEBHOOK_BODY_TEMPLATE"
    if key in raw_env_values and (
        override or key not in preexisting_compose_sensitive_keys
    ):
        raw_value = raw_env_values.get(key)
        os.environ[key] = unescape_compose_sensitive_env_value(
            key,
            "" if raw_value is None else str(raw_value),
        )


@dataclass
class Config:
    """
    系统配置类 - 单例模式
    
    设计说明：
    - 使用 dataclass 简化配置属性定义
    - 所有配置项从环境变量读取，支持默认值
    - 类方法 get_instance() 实现单例访问
    """
    
    # === 自选股配置 ===
    stock_list: List[str] = field(default_factory=list)

    # === 飞书云文档配置 ===
    feishu_app_id: Optional[str] = None
    feishu_app_secret: Optional[str] = None
    feishu_folder_token: Optional[str] = None  # 目标文件夹 Token

    # === 数据源 API Token ===
    tushare_token: Optional[str] = None
    biying_licence: Optional[str] = None
    biying_timeout_seconds: float = 8.0
    intraday_snapshot_ttl_seconds: int = 60
    tickflow_api_key: Optional[str] = None
    tickflow_kline_adjust: str = "none"
    tickflow_priority: int = 2
    tickflow_batch_daily_enabled: bool = True
    tickflow_batch_size: int = 100
    futu_opend_host: Optional[str] = None
    futu_opend_port: int = 11111
    futu_hk_realtime_source_priority: str = "futu,longbridge,akshare,yfinance"
    mx_apikey: Optional[str] = None
    mx_priority: int = 6
    finnhub_api_key: Optional[str] = None
    alphavantage_api_key: Optional[str] = None
    longbridge_app_key: Optional[str] = None
    longbridge_app_secret: Optional[str] = None
    longbridge_access_token: Optional[str] = None
    longbridge_oauth_client_id: Optional[str] = None
    stock_index_remote_update_enabled: bool = True

    # === Built-in stock screening ===
    screening_enabled: bool = False

    # === AI 分析配置 ===
    generation_backend: str = LITELLM_BACKEND_ID
    generation_fallback_backend: str = LITELLM_BACKEND_ID
    generation_backend_timeout_seconds: int = DEFAULT_LOCAL_CLI_TIMEOUT_SECONDS
    generation_backend_max_output_bytes: int = DEFAULT_LOCAL_CLI_MAX_OUTPUT_BYTES
    generation_backend_max_concurrency: int = DEFAULT_GENERATION_BACKEND_MAX_CONCURRENCY
    local_cli_backend_max_concurrency: int = DEFAULT_LOCAL_CLI_BACKEND_MAX_CONCURRENCY
    opencode_cli_model: str = ""
    # LiteLLM unified model config (provider/model format, e.g. gemini/gemini-3.1-pro-preview)
    litellm_model: str = ""  # Primary model; must include provider prefix when set explicitly
    litellm_light_model: str = ""  # Optional light model for cheap chores (classification / tagging / short summaries); empty falls back to the primary model
    litellm_fallback_models: List[str] = field(default_factory=list)  # Cross-model fallback list

    # Unified temperature for all LLM calls (LLM_TEMPERATURE); legacy per-provider temps are fallback only
    llm_temperature: float = 0.7
    # Standard ``reasoning_effort`` request parameter ("" = do not send / model default; low|medium|high|xhigh|max).
    llm_reasoning_effort: str = ""  # LLM_REASONING_EFFORT: primary analysis model + its fallbacks (analysis, market review)
    agent_reasoning_effort: str = ""  # AGENT_REASONING_EFFORT: assistant / agent chat; empty follows LLM_REASONING_EFFORT
    litellm_light_reasoning_effort: str = ""  # LITELLM_LIGHT_REASONING_EFFORT: light-model chores; empty = not sent

    # Provider prompt-cache controls. These do not control provider implicit cache.
    llm_prompt_cache_telemetry_enabled: bool = True
    llm_prompt_cache_hints_enabled: bool = False
    llm_prompt_cache_diagnostics_level: str = "off"

    # --- Multi-channel LLM config (new) ---
    # LITELLM_CONFIG: path to a standard litellm_config.yaml file (most powerful)
    litellm_config_path: Optional[str] = None
    # Internal metadata: which config layer actually produced llm_model_list
    llm_models_source: str = "legacy_env"
    # LLM_CHANNELS: list of channel dicts, each with name/base_url/api_keys/models
    llm_channels: List[Dict[str, Any]] = field(default_factory=list)
    # Raw channel names requested through LLM_CHANNELS, including channels that
    # were skipped during parsing because required channel fields were missing.
    llm_channel_names: List[str] = field(default_factory=list)
    # Structured parse issues raised while turning LLM_CHANNELS into deployments.
    llm_channel_config_issues: List[Dict[str, str]] = field(default_factory=list)
    # True when invalid explicit channel config must prevent legacy key inference.
    llm_blocks_legacy_fallback: bool = False
    # Canonical Hermes route names that were requested but blocked by atomic parse issues.
    llm_blocked_hermes_routes: List[str] = field(default_factory=list)
    # Pre-built LiteLLM Router model_list (populated from channels, YAML, or legacy keys)
    llm_model_list: List[Dict[str, Any]] = field(default_factory=list)

    # Multi-key support: each list is parsed from *_API_KEYS (comma-separated) with single-key fallback
    gemini_api_keys: List[str] = field(default_factory=list)
    anthropic_api_keys: List[str] = field(default_factory=list)
    openai_api_keys: List[str] = field(default_factory=list)
    deepseek_api_keys: List[str] = field(default_factory=list)

    # Legacy single-key fields (kept for backward compatibility; gemini_api_keys[0] when set)
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-3.1-pro-preview"  # 主模型
    gemini_model_fallback: str = "gemini-3-flash-preview"  # 备选模型
    gemini_temperature: float = 0.7  # 温度参数（0.0-2.0，控制输出随机性，默认0.7）

    # Gemini API 请求配置（防止 429 限流）
    gemini_request_delay: float = 2.0  # 请求间隔（秒）
    gemini_max_retries: int = 5  # 最大重试次数
    gemini_retry_delay: float = 5.0  # 重试基础延时（秒）

    # Anthropic Claude API（备选，当 Gemini 不可用时使用）
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-6"  # Claude model name
    anthropic_temperature: float = 0.7  # Anthropic temperature (0.0-1.0, default 0.7)
    anthropic_max_tokens: int = 8192  # Max tokens for Anthropic responses

    # OpenAI 兼容 API（备选，当 Gemini/Anthropic 不可用时使用）
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None  # 如: https://api.openai.com/v1
    openai_model: str = "gpt-5.5"  # OpenAI 兼容模型名称
    openai_vision_model: Optional[str] = None  # Deprecated: use VISION_MODEL instead
    openai_temperature: float = 0.7  # OpenAI 温度参数（0.0-2.0，默认0.7）

    # === Vision 配置 ===
    # VISION_MODEL: litellm model string used for image understanding calls.
    # Fallback chain: VISION_MODEL → OPENAI_VISION_MODEL → gemini/gemini-2.0-flash
    vision_model: str = ""
    # VISION_PROVIDER_PRIORITY: comma-separated provider order for Vision fallback.
    vision_provider_priority: str = "gemini,anthropic,openai"

    # === 搜索引擎配置（支持多 Key 负载均衡）===
    anspire_api_keys: List[str] = field(default_factory=list)  # Anspire Search API Keys
    bocha_api_keys: List[str] = field(default_factory=list)  # Bocha API Keys
    minimax_api_keys: List[str] = field(default_factory=list)  # MiniMax API Keys
    tavily_api_keys: List[str] = field(default_factory=list)  # Tavily API Keys
    brave_api_keys: List[str] = field(default_factory=list)  # Brave Search API Keys
    serpapi_keys: List[str] = field(default_factory=list)  # SerpAPI Keys
    searxng_base_urls: List[str] = field(default_factory=list)  # SearXNG instance URLs (self-hosted, no quota)
    searxng_public_instances_enabled: bool = False  # Opt in to public discovery when base URLs are absent
    searxng_timeout_seconds: int = 10  # 自建 SearXNG 单次搜索超时（秒）

    # === Social Sentiment (US stocks only, api.adanos.org) ===
    social_sentiment_api_key: Optional[str] = None
    social_sentiment_api_url: str = "https://api.adanos.org"

    # === 新闻与分析筛选配置 ===
    news_max_age_days: int = 3   # 新闻最大时效（天）
    news_strategy_profile: str = "short"  # 新闻窗口策略档位：ultra_short/short/medium/long
    news_intel_retention_days: int = 30  # 本地资讯池保留天数
    news_intel_fetch_timeout_sec: float = 8.0  # 单个资讯源拉取超时
    news_intel_max_items_per_source: int = 50  # 单次每个资讯源最多采集条数
    news_intel_auto_fetch_enabled: bool = False  # 是否在分析前自动初始化并拉取本地资讯源
    # AKShare 资讯池定时抓取与分类（仅在 NEWS_INTEL_AUTO_FETCH_ENABLED=true 且定时模式下生效）
    news_poll_interval_minutes: int = 10  # 交易日 08:00–15:30（中国时间）快讯轮询间隔（分钟）
    news_poll_offhours_interval_minutes: int = 30  # 其余时段快讯轮询间隔（分钟）
    news_akshare_cls_symbol: str = "全部"  # 财联社电报取数范围：全部（默认）/ 重点
    news_flash_retention_days: int = 3  # 快讯（市场范围）和财经早餐保留天数
    news_company_retention_days: int = 30  # 公告等个股事件保留天数
    news_dedup_window_minutes: int = 30  # 跨来源按标题近似去重的时间窗（分钟）
    news_fetch_timeout_sec: float = 30.0  # 单次 AKShare 接口调用的超时（秒）；超时后该来源本轮跳过
    news_announcement_mode: str = "auto"  # 公告取数方式：auto / by_code / market
    news_announcement_by_code_max: int = 12  # auto 模式下：自选股+持仓不超过该数量时逐只取公告，否则取全市场当日公告再过滤
    news_daily_fetch_time: str = "08:05"  # 每个交易日抓取公告与财经早餐的时间（HH:MM，本机时区）
    news_classify_enabled: bool = True  # 是否用 LLM 对新资讯分类（范围/重要性/板块/美股/突发）
    news_classify_batch_size: int = 20  # 每次 LLM 调用分类的资讯条数
    news_classify_max_items_per_run: int = 60  # 每轮最多分类的资讯条数
    news_classify_max_attempts: int = 5  # 单条资讯分类失败后的最大重试次数
    news_board_driver_min_importance: int = 4  # 资讯重要性达到该值才会成为板块异动的「消息」驱动
    newsnow_base_url: str = "https://newsnow.busiyi.world"  # NewsNow HTTP API base URL (数据源侧，不影响 LLM/provider base URL)
    bias_threshold: float = 5.0  # 乖离率阈值（%），超过此值提示不追高

    # === Agent 模式配置 ===
    agent_backend: str = "auto"
    agent_generation_backend: str = AUTO_AGENT_BACKEND_ID
    agent_litellm_model: str = ""  # Optional Agent-only primary model; empty inherits LITELLM_MODEL
    agent_mode: bool = False
    _agent_mode_explicit: bool = False  # True when AGENT_MODE was explicitly set in env
    agent_max_steps: int = AGENT_MAX_STEPS_DEFAULT
    agent_skills: List[str] = field(default_factory=list)
    agent_skill_dir: Optional[str] = None
    agent_nl_routing: bool = False  # Enable natural language routing in bot dispatcher
    agent_arch: str = "single"     # Agent architecture: 'single' (legacy) or 'multi' (orchestrator)
    agent_orchestrator_mode: str = "standard"  # Orchestrator mode: quick/standard/full/specialist
    agent_orchestrator_timeout_s: int = 600  # Cooperative timeout budget for the whole multi-agent pipeline
    agent_technical_agent_timeout_s: float = 0
    agent_intel_agent_timeout_s: float = 0
    agent_risk_agent_timeout_s: float = 0
    agent_decision_agent_timeout_s: float = 0
    agent_portfolio_agent_timeout_s: float = 0
    agent_skill_agent_timeout_s: float = 0
    # Per-category default timeouts for agent tool calls (seconds).
    # 0 / unset means "no category default" -> falls back to the global
    # tool_call_timeout_seconds budget.
    agent_data_tool_timeout_s: float = 0.0
    agent_search_tool_timeout_s: float = 0.0
    agent_analysis_tool_timeout_s: float = 0.0
    agent_action_tool_timeout_s: float = 0.0
    agent_skill_concurrency: int = 3
    agent_risk_override: bool = True  # Allow risk agent to veto buy signals
    agent_deep_research_budget: int = 30000  # Max token budget for deep research
    agent_deep_research_timeout: int = 180  # Max seconds for /research command before returning timeout
    agent_memory_enabled: bool = False  # Enable memory & calibration system
    agent_skill_autoweight: bool = True  # Weight skills by attributable Outcome performance
    agent_skill_routing: str = "auto"  # Skill routing: 'auto' (regime-based) or 'manual'
    agent_context_compression_enabled: bool = False  # Compress visible chat history before Agent calls
    agent_context_compression_profile: str = AGENT_CONTEXT_COMPRESSION_DEFAULT_PROFILE
    agent_context_compression_trigger_tokens: int = 12000
    agent_context_protected_turns: int = 4
    agent_event_monitor_enabled: bool = False  # Enable periodic event-driven alert checks in schedule mode
    # === 助手记忆（笔记 + 可插拔长期回忆）===
    assistant_recall_provider: str = "none"  # none | hindsight
    hindsight_base_url: str = ""
    hindsight_api_key: Optional[str] = None
    hindsight_bank_id: str = ""
    assistant_recall_timeout_s: float = 2.0  # 提问时 recall 的整体超时，超时即跳过
    assistant_recall_mask_amounts: bool = True  # 发送给回忆服务前对金额/股数/成本打码
    assistant_note_daily_proposal_limit: int = 2  # 助手每天最多主动提议修改笔记的次数
    assistant_note_inject_max_chars: int = 3000  # 笔记注入提示词的总长度上限
    agent_event_monitor_interval_minutes: int = 5  # Polling interval for event monitor background checks
    agent_event_alert_rules_json: str = ""  # JSON array of serialized EventMonitor rules
    alert_max_enabled_rules_per_user: int = 200  # Per-user cap on enabled alert rules (create / enable); the worker evaluates at most this many per user

    # === 通知配置（可同时配置多个，全部推送）===
    
    # 企业微信 Webhook
    wechat_webhook_url: Optional[str] = None
    
    # 飞书 Webhook
    feishu_webhook_url: Optional[str] = None
    feishu_webhook_secret: Optional[str] = None  # 自定义机器人签名密钥（可选）
    feishu_webhook_keyword: Optional[str] = None  # 自定义机器人关键词（可选）
    dingtalk_webhook_url: Optional[str] = None
    dingtalk_secret: Optional[str] = None

    # 飞书应用机器人（App Bot）通知
    feishu_chat_id: Optional[str] = None  # 目标群会话 chat_id（群聊模式），或用户 open_id（P2P 模式）
    feishu_receive_id_type: str = "chat_id"  # 接收者 ID 类型: "chat_id"(群聊) / "open_id"(私聊)
    feishu_domain: str = "feishu"  # 飞书域名: "feishu"(feishu.cn) / "lark"(larksuite.com)
    
    # Telegram 配置（需要同时配置 Bot Token 和 Chat ID）
    telegram_bot_token: Optional[str] = None  # Bot Token（@BotFather 获取）
    telegram_chat_id: Optional[str] = None  # Chat ID
    telegram_message_thread_id: Optional[str] = None  # Topic ID (Message Thread ID) for groups
    
    # 邮件配置（只需邮箱和授权码，SMTP 自动识别）
    email_sender: Optional[str] = None  # 发件人邮箱
    email_sender_name: str = "daily_stock_analysis股票分析助手"  # 发件人显示名称
    email_password: Optional[str] = None  # 邮箱密码/授权码
    email_receivers: List[str] = field(default_factory=list)  # 收件人列表（留空则发给自己）

    # Stock-to-email group routing (Issue #268): STOCK_GROUP_N + EMAIL_GROUP_N
    # When configured, each group's report is sent to that group's emails only.
    stock_email_groups: List[Tuple[List[str], List[str]]] = field(default_factory=list)

    # Pushover 配置（手机/桌面推送通知）
    pushover_user_key: Optional[str] = None  # 用户 Key（https://pushover.net 获取）
    pushover_api_token: Optional[str] = None  # 应用 API Token

    # ntfy 配置（完整 topic endpoint，例如 https://ntfy.sh/my-topic）
    ntfy_url: Optional[str] = None
    ntfy_token: Optional[str] = None

    # Gotify 配置（server base URL；sender 会拼接 /message）
    gotify_url: Optional[str] = None
    gotify_token: Optional[str] = None
    
    # 自定义 Webhook（支持多个，逗号分隔）
    # 适用于：钉钉、Discord、Slack、自建服务等任意支持 POST JSON 的 Webhook
    custom_webhook_urls: List[str] = field(default_factory=list)
    custom_webhook_bearer_token: Optional[str] = None  # Bearer Token（用于需要认证的 Webhook）
    custom_webhook_body_template: Optional[str] = None  # 自定义 Webhook JSON body 模板
    webhook_verify_ssl: bool = True  # Webhook HTTPS 证书校验，false 可支持自签名（有 MITM 风险）

    # Discord 通知配置
    discord_bot_token: Optional[str] = None  # Discord Bot Token
    discord_main_channel_id: Optional[str] = None  # Discord 主频道 ID
    discord_webhook_url: Optional[str] = None  # Discord Webhook URL
    discord_interactions_public_key: Optional[str] = None  # Discord Interaction 入站验签公钥

    # Slack 通知配置
    slack_webhook_url: Optional[str] = None  # Slack Incoming Webhook URL
    slack_bot_token: Optional[str] = None  # Slack Bot Token (xoxb-...)
    slack_channel_id: Optional[str] = None  # Slack 频道 ID (Bot 模式必填)

    # AstrBot 通知配置
    astrbot_token: Optional[str] = None
    astrbot_url: Optional[str] = None

    # 通知路由策略（Issue #1200 P3）：留空表示该类型使用全部已配置渠道
    notification_report_channels: List[str] = field(default_factory=list)
    notification_alert_channels: List[str] = field(default_factory=list)
    notification_system_error_channels: List[str] = field(default_factory=list)

    # 通知降噪机制（Issue #1200 P4）：默认全部关闭，仅对静态通知渠道生效
    notification_dedup_ttl_seconds: int = 0
    notification_cooldown_seconds: int = 0
    notification_quiet_hours: str = ""
    notification_timezone: str = ""
    notification_min_severity: str = ""
    notification_daily_digest_enabled: bool = False

    # 单股推送模式：每分析完一只股票立即推送，而不是汇总后推送
    single_stock_notify: bool = False

    # 报告类型：simple(精简) 或 full(完整)
    report_type: str = "simple"
    report_language: str = "zh"

    # 仅分析结果摘要：true 时只推送汇总，不含个股详情（Issue #262）
    report_summary_only: bool = False
    report_show_llm_model: bool = True

    # Report Engine P0: Jinja2 renderer and integrity checks
    report_templates_dir: str = "templates"  # Template directory (relative to project root)
    report_renderer_enabled: bool = False  # Enable Jinja2 rendering (default off for zero regression)
    report_integrity_enabled: bool = True  # Content integrity validation after LLM output
    report_integrity_retry: int = 1  # Retry count when mandatory fields missing (0 = placeholder only)
    report_history_compare_n: int = 0  # History comparison count (0 = disabled)

    # PushPlus 推送配置
    pushplus_token: Optional[str] = None  # PushPlus Token
    pushplus_topic: Optional[str] = None  # PushPlus 群组编码（一对多推送）

    # Server酱3 推送配置
    serverchan3_sendkey: Optional[str] = None  # Server酱3 SendKey

    # 分析间隔时间（秒）- 用于避免API限流
    analysis_delay: float = 0.0  # 个股分析与大盘分析之间的延迟

    # Merge stock + market report into one notification (Issue #190)
    merge_email_notification: bool = False

    # 消息长度限制（字节）- 超长自动分批发送
    feishu_max_bytes: int = 20000  # 飞书限制约 20KB，默认 20000 字节
    feishu_send_as_file: bool = False  # 飞书是否以文件形式发送报告（默认文字消息）
    wechat_max_bytes: int = 4000   # 企业微信限制 4096 字节，默认 4000 字节
    discord_max_words: int = 2000  # Discord 限制 2000 字，默认 2000 字
    wechat_msg_type: str = "markdown"  # 企业微信消息类型，默认 markdown 类型

    # Markdown 转图片（Issue #289）：对不支持 Markdown 的渠道以图片发送
    markdown_to_image_channels: List[str] = field(default_factory=list)  # 逗号分隔：telegram,wechat,custom,email
    markdown_to_image_max_chars: int = 15000  # 超过此长度不转换，避免超大图片
    md2img_engine: str = "wkhtmltoimage"  # wkhtmltoimage | markdown-to-file | playwright
    share_image_xiaohongshu_url: Optional[str] = None
    share_image_xiaohongshu_handle: Optional[str] = None
    share_image_xiaohongshu_id: Optional[str] = None
    share_image_xiaohongshu_qr_path: Optional[str] = None

    # 实时行情预取（Issue #455）：设为 false 可禁用，避免 efinance/akshare_em 全市场拉取
    prefetch_realtime_quotes: bool = True

    # === 数据库配置 ===
    database_path: str = "./data/stock_analysis.db"
    sqlite_wal_enabled: bool = True
    sqlite_busy_timeout_ms: int = 5000
    sqlite_write_retry_max: int = 3
    sqlite_write_retry_base_delay: float = 0.1

    # === 板块与全市场快照（docs/design/core-backend-design.md 第 2 节）===
    market_snapshot_retention_days: int = 250  # market_stock_daily 保留的交易日数
    market_snapshot_backfill_days: int = 60  # 冷启动回填的交易日数
    market_benchmark_code: str = "000985.CSI"  # market_daily 的基准指数（Tushare 代码）
    market_snapshot_requests_per_minute: int = 60  # 快照取数的 Tushare 每分钟请求上限
    market_snapshot_members_requests_per_minute: int = 200  # 逐板块 dc_member 成分请求的每分钟上限（仅成分刷新使用）
    board_anomaly_thresholds: str = ""  # 板块异动阈值覆盖（JSON 对象，空表示全部默认）
    board_pipeline_enabled: bool = True  # 定时模式下是否在交易日运行板块异动日度流程（还需 TUSHARE_TOKEN）
    board_pipeline_schedule_time: str = "17:00"  # 板块异动日度流程的运行时间（HH:MM，本机时区）
    board_membership_full_day_fallback: bool = False  # 逐板块概念成分全部失败时，是否再试一次全量成分查询（受 100000 偏移上限限制，通常无效）
    board_industry_membership_enabled: bool = True  # 板块流程开启时，是否每周刷新一次行业板块成分
    board_industry_membership_weekday: int = 6  # 每周刷新行业板块成分的星期（ISO：1=周一 … 6=周六 7=周日）
    board_industry_membership_time: str = "10:00"  # 每周行业板块成分刷新的运行时间（HH:MM，本机时区）
    us_leaders: str = ""  # 美股龙头名单（JSON 数组，空表示使用内置默认名单）
    us_move_stock_pct: float = 4.0  # 美股个股单日涨跌幅达到该绝对值（%）视为异动
    us_move_index_pct: float = 1.5  # 美股指数单日涨跌幅达到该绝对值（%）视为异动
    preopen_push_enabled: bool = False  # 定时模式下是否在交易日推送盘前版（隔夜美股 + 自选股盘前分析）
    preopen_push_time: str = "08:15"  # 盘前版开始运行时间（HH:MM，本机时区；约 8:45 送达）
    # === 次日计划（docs/daily-plan-api.md）：仓位规则与自动运行开关 ===
    daily_plan_enabled: bool = False  # 定时模式下是否在交易日按 SCHEDULE_TIME 自动生成次日计划并推送飞书
    position_stop_loss_pct: float = 10.0  # 止损幅度（%），从买入价或持仓成本起算
    position_max_total_pct: float = 100.0  # 总仓位上限（%，占总资金）
    position_max_holdings: int = 1  # 最多持股数
    position_max_single_pct: Optional[float] = None  # 单只仓位上限（%）；空 = 总仓位上限 ÷ 最多持股数
    position_stop_mode: str = "fixed"  # fixed 固定比例 / nearer 固定比例与技术支撑取较近者

    # 是否保存分析上下文快照（用于历史回溯）
    save_context_snapshot: bool = True

    # === 回测配置 ===
    backtest_enabled: bool = True
    backtest_eval_window_days: int = 10
    backtest_min_age_days: int = 14
    backtest_engine_version: str = "v1"
    backtest_neutral_band_pct: float = 2.0

    # === ETF 轮动配置（仅 --etf-rotation 使用）===
    etf_rotation_pool: List[str] = field(default_factory=lambda: list(DEFAULT_ETF_ROTATION_POOL))
    etf_rotation_safe_asset: str = DEFAULT_ETF_ROTATION_SAFE_ASSET
    etf_rotation_lookback_days: int = 60
    etf_rotation_rebalance: str = "weekly"
    etf_rotation_top_n: int = 2
    etf_rotation_switch_buffer_pct: float = 2.0
    etf_rotation_cost_bps: float = 10.0
    etf_rotation_backtest_years: int = 8
    
    # === 日志配置 ===
    log_dir: str = "./logs"  # 日志文件目录
    log_level: str = "INFO"  # 日志级别
    
    # === 系统配置 ===
    max_workers: int = 3  # 低并发防封禁
    analysis_max_concurrent_per_user: int = 1  # Async analysis slots each user is guaranteed while others wait (work-conserving fair queue); MAX_WORKERS stays the global cap
    debug: bool = False
    http_proxy: Optional[str] = None  # HTTP 代理 (例如: http://127.0.0.1:10809)
    https_proxy: Optional[str] = None # HTTPS 代理
    
    # === 定时任务配置 ===
    schedule_enabled: bool = False            # 是否启用定时任务
    schedule_time: str = "18:00"              # 每日推送时间（HH:MM 格式）
    schedule_times: List[str] = field(default_factory=lambda: ["18:00"])
    schedule_run_immediately: bool = True     # 启动时是否立即执行一次
    run_immediately: bool = True              # 启动时是否立即执行一次（非定时模式）
    market_review_enabled: bool = True        # 是否启用大盘复盘
    daily_market_context_enabled: bool = True   # 是否将大盘环境摘要用于个股分析 Prompt 与保守护栏
    # 大盘复盘市场区域：cn(A股)、hk(港股)、us(美股)、jp(日股)、kr(韩股)、both(全部市场)
    market_review_region: str = "cn"
    market_review_color_scheme: str = "green_up"
    # 交易日检查：默认启用，非交易日跳过执行；设为 false 或 --force-run 可强制执行（Issue #373）
    trading_day_check_enabled: bool = True

    # === 实时行情增强数据配置 ===
    # 实时行情开关（关闭后使用历史收盘价进行分析）
    enable_realtime_quote: bool = True
    # 盘中实时技术面：启用时用实时价计算 MA/多头排列（Issue #234）；关闭则用昨日收盘
    enable_realtime_technical_indicators: bool = True
    # 筹码分布开关（该接口不稳定，云端部署建议关闭）
    enable_chip_distribution: bool = True
    # 东财接口补丁开关
    enable_eastmoney_patch: bool = False
    # 实时行情数据源优先级（逗号分隔）
    # 推荐顺序：tencent > akshare_sina > efinance > akshare_em > tushare
    # - tencent: 腾讯财经，有量比/换手率/市盈率等，单股查询稳定（推荐）
    # - akshare_sina: 新浪财经，基本行情稳定，但无量比
    # - efinance/akshare_em: 东财全量接口，数据最全但容易被封
    # - tushare: Tushare Pro，需要2000积分，数据全面（付费用户可优先使用）
    realtime_source_priority: str = "tencent,akshare_sina,efinance,akshare_em"
    # 实时行情缓存时间（秒）
    realtime_cache_ttl: int = 600
    # 熔断器冷却时间（秒）
    circuit_breaker_cooldown: int = 300

    # === 基本面聚合开关与降级保护 ===
    # 全局总开关；关闭时返回 not_supported 并保持主流程无变化
    enable_fundamental_pipeline: bool = True
    # 基本面阶段总预算（秒）
    fundamental_stage_timeout_seconds: float = FUNDAMENTAL_STAGE_TIMEOUT_SECONDS_DEFAULT
    # 单能力源调用超时（秒）
    fundamental_fetch_timeout_seconds: float = 8.0
    # 单能力失败重试次数（已包含首次）
    fundamental_retry_max: int = 1
    # 基本面上下文短 TTL（秒）
    fundamental_cache_ttl_seconds: int = 120
    # 基本面缓存最大条目数（避免长时间运行内存增长）
    fundamental_cache_max_entries: int = 256

    # === Portfolio PR2: import/risk/fx settings ===
    portfolio_risk_concentration_alert_pct: float = 35.0
    portfolio_risk_drawdown_alert_pct: float = 15.0
    portfolio_risk_stop_loss_alert_pct: float = 10.0
    portfolio_risk_stop_loss_near_ratio: float = 0.8
    portfolio_risk_lookback_days: int = 180
    portfolio_fx_update_enabled: bool = True

    # Discord 机器人状态
    discord_bot_status: str = "A股智能分析 | /help"

    # === 流控配置（防封禁关键参数）===
    # Akshare 请求间隔范围（秒）
    akshare_sleep_min: float = 2.0
    akshare_sleep_max: float = 5.0
    
    # Tushare 每分钟最大请求数（免费配额）
    tushare_rate_limit_per_minute: int = 80
    
    # 重试配置
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    
    # === WebUI 配置 ===
    webui_enabled: bool = False
    webui_host: str = "127.0.0.1"
    webui_port: int = 8000
    
    # === 机器人配置 ===
    bot_enabled: bool = True              # 是否启用机器人功能
    bot_command_prefix: str = "/"         # 命令前缀
    bot_rate_limit_requests: int = 10     # 频率限制：窗口内最大请求数
    bot_rate_limit_window: int = 60       # 频率限制：窗口时间（秒）
    bot_admin_users: List[str] = field(default_factory=list)  # 管理员用户 ID 列表
    
    # 飞书机器人（事件订阅）- 已有 feishu_app_id, feishu_app_secret
    feishu_verification_token: Optional[str] = None  # 事件订阅验证 Token
    feishu_encrypt_key: Optional[str] = None         # 消息加密密钥（可选）
    feishu_stream_enabled: bool = False              # 是否启用 Stream 长连接模式（无需公网IP）
    
    # 钉钉机器人
    dingtalk_app_key: Optional[str] = None      # 应用 AppKey
    dingtalk_app_secret: Optional[str] = None   # 应用 AppSecret
    dingtalk_stream_enabled: bool = False       # 是否启用 Stream 模式（无需公网IP）
    
    # 企业微信机器人（回调模式）
    wecom_corpid: Optional[str] = None              # 企业 ID
    wecom_token: Optional[str] = None               # 回调 Token
    wecom_encoding_aes_key: Optional[str] = None    # 消息加解密密钥
    wecom_agent_id: Optional[str] = None            # 应用 AgentId
    
    # Telegram 机器人 - 已有 telegram_bot_token, telegram_chat_id
    telegram_webhook_secret: Optional[str] = None   # Webhook 密钥

    # === 配置校验模式 ===
    # CONFIG_VALIDATE_MODE=warn (default): log all issues but always continue startup
    # CONFIG_VALIDATE_MODE=strict: exit(1) when any "error" severity issue is found
    config_validate_mode: str = "warn"

    # === 多用户：主用户（docs/design/multi-user.md 2.3） ===
    # 既有历史数据、CLI/调度/机器人等无用户上下文入口都归属主用户；其个人设置就是 .env。
    primary_user_id: str = DEFAULT_PRIMARY_USER_ID

    # --- Post-init validation ---------------------------------------------------
    _VALID_AGENT_ARCH = {"single", "multi"}
    _VALID_ORCHESTRATOR_MODES = {"quick", "standard", "full", "specialist"}
    _VALID_SKILL_ROUTING = {"auto", "manual"}
    _WEBUI_RUNTIME_ENV_FILE_PRIORITY_KEYS = frozenset(
        {
            "STOCK_LIST",
            "RUN_IMMEDIATELY",
            "SCHEDULE_ENABLED",
            "SCHEDULE_TIME",
            "SCHEDULE_TIMES",
            "SCHEDULE_RUN_IMMEDIATELY",
        }
    )
    _BOOTSTRAP_RUNTIME_ENV_OVERRIDES_CAPTURED = False
    _BOOTSTRAP_RUNTIME_ENV_OVERRIDES = frozenset()
    _BOOTSTRAP_RUNTIME_ENV_PRESENT_KEYS = frozenset()

    def __post_init__(self) -> None:
        _log = logging.getLogger(__name__)
        if self.agent_arch not in self._VALID_AGENT_ARCH:
            _log.warning(
                "Invalid AGENT_ARCH=%r, falling back to 'single'. Valid: %s",
                self.agent_arch, self._VALID_AGENT_ARCH,
            )
            object.__setattr__(self, "agent_arch", "single")
        if self.agent_orchestrator_mode in {"strategy", "skill"}:
            _log.info(
                "AGENT_ORCHESTRATOR_MODE=%s is deprecated; normalizing to 'specialist'",
                self.agent_orchestrator_mode,
            )
            object.__setattr__(self, "agent_orchestrator_mode", "specialist")
        if self.agent_orchestrator_mode not in self._VALID_ORCHESTRATOR_MODES:
            _log.warning(
                "Invalid AGENT_ORCHESTRATOR_MODE=%r, falling back to 'standard'. Valid: %s",
                self.agent_orchestrator_mode, self._VALID_ORCHESTRATOR_MODES,
            )
            object.__setattr__(self, "agent_orchestrator_mode", "standard")
        if self.agent_skill_routing not in self._VALID_SKILL_ROUTING:
            _log.warning(
                "Invalid AGENT_SKILL_ROUTING=%r, falling back to 'auto'. Valid: %s",
                self.agent_skill_routing, self._VALID_SKILL_ROUTING,
            )
            object.__setattr__(self, "agent_skill_routing", "auto")
        normalized_profile = normalize_agent_context_compression_profile(
            self.agent_context_compression_profile
        )
        if normalized_profile != self.agent_context_compression_profile:
            object.__setattr__(self, "agent_context_compression_profile", normalized_profile)

    def __setattr__(self, name: str, value: Any) -> None:
        if self.__dict__.get("_user_view_frozen"):
            raise AttributeError(f"Config user view is read-only; cannot set {name!r}")
        object.__setattr__(self, name, value)

    def freeze_as_user_view(self, user_id: Optional[str] = None) -> None:
        """Make this instance read-only (per-user config views are shared between requests).

        ``user_id`` is the owner of the view; model / search / usage code reads it back through
        ``user_view_id`` to know that the config belongs to a non-primary user.
        """
        object.__setattr__(self, "_user_view_frozen", True)
        object.__setattr__(self, "_user_view_id", user_id)

    @property
    def user_view_id(self) -> Optional[str]:
        """Owner of this per-user view; ``None`` for the system config (the primary user's)."""
        value = self.__dict__.get("_user_view_id")
        return value if isinstance(value, str) and value else None

    # 单例实例存储
    _instance: Optional['Config'] = None
    
    @classmethod
    def get_instance(cls) -> 'Config':
        """
        获取配置单例实例
        
        单例模式确保：
        1. 全局只有一个配置实例
        2. 配置只从环境变量加载一次
        3. 所有模块共享相同配置
        """
        if cls._instance is None:
            cls._instance = cls._load_from_env()
        return cls._instance
    
    @classmethod
    def _load_from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        user_override_keys: frozenset = frozenset(),
    ) -> 'Config':
        """
        从 .env 文件加载配置
        
        加载优先级：
        1. 大多数配置保持系统环境变量优先
        2. WebUI 可写的运行期关键键优先复用持久化 `.env`，但保留启动时显式进程环境变量的 override
        3. 代码中的默认值

        ``env`` 默认是进程环境（``os.environ``），并执行加载 `.env`、设置代理等进程级
        副作用。传入映射时只用该映射解析配置、不产生任何进程级副作用（多用户配置视图使用，
        见 ``src/services/user_settings_service.py``）；``user_override_keys`` 是映射里
        来自用户个人层的键，解析时优先于持久化 `.env` 文件。
        """
        use_process_env = env is None
        if use_process_env:
            env = os.environ
            cls._capture_bootstrap_runtime_env_overrides()
        preexisting_report_language = env.get("REPORT_LANGUAGE")

        # 确保环境变量已加载
        if use_process_env:
            setup_env()

        # === 智能代理配置 (关键修复) ===
        # 如果配置了代理，自动设置 NO_PROXY 以排除国内数据源，避免行情获取失败
        http_proxy = (os.getenv('HTTP_PROXY') or os.getenv('http_proxy')) if use_process_env else None
        if http_proxy:
            # 国内金融数据源域名列表
            domestic_domains = [
                'eastmoney.com',   # 东方财富 (Efinance/Akshare)
                'sina.com.cn',     # 新浪财经 (Akshare)
                '163.com',         # 网易财经 (Akshare)
                'tushare.pro',     # Tushare
                'baostock.com',    # Baostock
                'sse.com.cn',      # 上交所
                'szse.cn',         # 深交所
                'csindex.com.cn',  # 中证指数
                'cninfo.com.cn',   # 巨潮资讯
                'localhost',
                '127.0.0.1'
            ]

            # 获取现有的 no_proxy
            current_no_proxy = os.getenv('NO_PROXY') or os.getenv('no_proxy') or ''
            existing_domains = current_no_proxy.split(',') if current_no_proxy else []

            # 合并去重
            final_domains = list(set(existing_domains + domestic_domains))
            final_no_proxy = ','.join(filter(None, final_domains))

            # 设置环境变量 (requests/urllib3/aiohttp 都会遵守此设置)
            os.environ['NO_PROXY'] = final_no_proxy
            os.environ['no_proxy'] = final_no_proxy

            # 确保 HTTP_PROXY 也被正确设置（以防仅在 .env 中定义但未导出）
            os.environ['HTTP_PROXY'] = http_proxy
            os.environ['http_proxy'] = http_proxy

            # HTTPS_PROXY 同理
            https_proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy')
            if https_proxy:
                os.environ['HTTPS_PROXY'] = https_proxy
                os.environ['https_proxy'] = https_proxy

        
        # 解析自选股列表（逗号分隔，统一为大写 Issue #355）
        stock_list_str = cls._resolve_env_value(
            'STOCK_LIST',
            default='',
            prefer_env_file=True,
            env=env,
            user_override_keys=user_override_keys,
        )
        stock_list = [
            (c or "").strip().upper()
            for c in split_stock_list(stock_list_str)
            if (c or "").strip()
        ]
        
        # === LiteLLM multi-key parsing ===
        # GEMINI_API_KEYS (comma-separated) > GEMINI_API_KEY (single)
        _gemini_keys_raw = env.get('GEMINI_API_KEYS', '')
        gemini_api_keys = [k.strip() for k in _gemini_keys_raw.split(',') if k.strip()]
        _single_gemini = env.get('GEMINI_API_KEY', '').strip()
        if not gemini_api_keys and _single_gemini:
            gemini_api_keys = [_single_gemini]

        # ANTHROPIC_API_KEYS > ANTHROPIC_API_KEY
        _anthropic_keys_raw = env.get('ANTHROPIC_API_KEYS', '')
        anthropic_api_keys = [k.strip() for k in _anthropic_keys_raw.split(',') if k.strip()]
        _single_anthropic = env.get('ANTHROPIC_API_KEY', '').strip()
        if not anthropic_api_keys and _single_anthropic:
            anthropic_api_keys = [_single_anthropic]

        # OPENAI_API_KEYS > AIHUBMIX_KEY > OPENAI_API_KEY
        _aihubmix = env.get('AIHUBMIX_KEY', '').strip()
        _openai_keys_raw = env.get('OPENAI_API_KEYS', '')
        openai_api_keys = [k.strip() for k in _openai_keys_raw.split(',') if k.strip()]
        if not openai_api_keys:
            _single_openai = env.get('OPENAI_API_KEY', '').strip()
            _fallback_key = _aihubmix or _single_openai
            if _fallback_key:
                openai_api_keys = [_fallback_key]
        openai_base_url = env.get('OPENAI_BASE_URL') or (
            'https://aihubmix.com/v1' if _aihubmix else None
        )

        # DEEPSEEK_API_KEYS > DEEPSEEK_API_KEY (independent from OpenAI-compatible layer)
        _deepseek_keys_raw = env.get('DEEPSEEK_API_KEYS', '')
        deepseek_api_keys = [k.strip() for k in _deepseek_keys_raw.split(',') if k.strip()]
        if not deepseek_api_keys:
            _single_deepseek = env.get('DEEPSEEK_API_KEY', '').strip()
            if _single_deepseek:
                deepseek_api_keys = [_single_deepseek]

        # Anspire Open shares the same key as Anspire Search and exposes an
        # OpenAI-compatible LLM gateway.  When no other OpenAI-compatible key is
        # configured, use ANSPIRE_API_KEYS as the legacy openai-compatible
        # provider so "one key" setups work without LLM_CHANNELS.
        anspire_keys_str = env.get('ANSPIRE_API_KEYS', '')
        anspire_api_keys = [k.strip() for k in anspire_keys_str.split(',') if k.strip()]
        anspire_llm_enabled = parse_env_bool(env.get('ANSPIRE_LLM_ENABLED'), default=True)
        anspire_llm_base_url = (
            env.get('ANSPIRE_LLM_BASE_URL') or ANSPIRE_LLM_BASE_URL_DEFAULT
        ).strip()
        _anspire_llm_model_env = env.get('ANSPIRE_LLM_MODEL', '').strip()
        anspire_channel_declared = False
        for _raw_channel in env.get('LLM_CHANNELS', '').split(','):
            if _raw_channel.strip().lower() != "anspire":
                continue
            anspire_channel_declared = True
            break
        using_anspire_llm_legacy = bool(
            anspire_llm_enabled
            and not anspire_channel_declared
            and anspire_api_keys
            and not openai_api_keys
        )
        if using_anspire_llm_legacy:
            openai_api_keys = list(anspire_api_keys)
            openai_base_url = anspire_llm_base_url

        # LITELLM_MODEL / LITELLM_FALLBACK_MODELS explicit values are recorded
        # before YAML/channels are parsed, but legacy inference is delayed until
        # the higher-priority sources and Hermes blocking issues are known.
        litellm_model_explicit = env.get('LITELLM_MODEL', '').strip()
        litellm_model = litellm_model_explicit
        inferred_legacy_deepseek_model = False
        _openai_model_env = env.get('OPENAI_MODEL', '').strip()
        if using_anspire_llm_legacy:
            _openai_model_name = _anspire_llm_model_env or _openai_model_env or ANSPIRE_LLM_MODEL_DEFAULT
        else:
            _openai_model_name = _openai_model_env or 'gpt-5.5'

        # LITELLM_FALLBACK_MODELS: comma-separated list of fallback models
        _fallback_str = env.get('LITELLM_FALLBACK_MODELS', '')
        litellm_fallback_models_explicit = bool(_fallback_str.strip())
        if _fallback_str.strip():
            litellm_fallback_models = [m.strip() for m in _fallback_str.split(',') if m.strip()]
        else:
            litellm_fallback_models = []

        # === LLM Channels + YAML config ===
        litellm_config_path = env.get('LITELLM_CONFIG', '').strip() or None
        llm_models_source = "legacy_env"
        llm_channels: List[Dict[str, Any]] = []
        llm_channel_names: List[str] = []
        llm_channel_config_issues: List[Dict[str, str]] = []
        llm_blocks_legacy_fallback = False
        llm_blocked_hermes_routes: List[str] = []
        llm_model_list: List[Dict[str, Any]] = []

        # Priority 1: LITELLM_CONFIG (standard LiteLLM YAML config file)
        if litellm_config_path:
            llm_model_list = cls._parse_litellm_yaml(litellm_config_path, env)
            if llm_model_list:
                llm_models_source = "litellm_config"

        # Priority 2: LLM_CHANNELS (env var based channel config)
        if not llm_model_list:
            _channels_str = resolve_llm_channel_names(env)
            if _channels_str:
                llm_channel_names = [
                    ch.strip().lower()
                    for ch in _channels_str.split(',')
                    if ch.strip()
                ]
                (
                    llm_channels,
                    hermes_issues,
                    llm_blocks_legacy_fallback,
                    llm_blocked_hermes_routes,
                ) = cls._parse_llm_channels_with_issues(_channels_str, env)
                llm_channel_config_issues = [issue.as_dict() for issue in hermes_issues]
                llm_model_list = cls._channels_to_model_list(llm_channels)
                if llm_model_list:
                    llm_models_source = "llm_channels"

        route_models = get_configured_llm_models(llm_model_list)
        if route_models:
            if not litellm_model:
                litellm_model = route_models[0]
            if not litellm_fallback_models and not litellm_fallback_models_explicit and litellm_model:
                _seen = {litellm_model}
                litellm_fallback_models = [
                    model for model in route_models
                    if model not in _seen and not _seen.add(model)  # type: ignore[func-returns-value]
                ]

        # Priority 3: Legacy env vars → auto-build model_list (backward compatible).
        # This is skipped when an explicit invalid Hermes channel blocks legacy fallback.
        if not llm_model_list and not llm_blocks_legacy_fallback:
            llm_model_list = cls._legacy_keys_to_model_list(
                gemini_api_keys, anthropic_api_keys, openai_api_keys,
                openai_base_url,
                deepseek_api_keys,
            )
            if llm_model_list:
                llm_models_source = "legacy_env"

            if not litellm_model:
                _gemini_model_name = env.get('GEMINI_MODEL', 'gemini-3.1-pro-preview').strip()
                _anthropic_model_name = env.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6').strip()
                if gemini_api_keys:
                    litellm_model = f'gemini/{_gemini_model_name}'
                elif anthropic_api_keys:
                    litellm_model = f'anthropic/{_anthropic_model_name}'
                elif deepseek_api_keys:
                    litellm_model = 'deepseek/deepseek-chat'
                    inferred_legacy_deepseek_model = True
                elif openai_api_keys:
                    # For openai-compatible models, add prefix only if not already prefixed
                    if '/' not in _openai_model_name:
                        litellm_model = f'openai/{_openai_model_name}'
                    else:
                        litellm_model = _openai_model_name

            if not litellm_fallback_models and not litellm_fallback_models_explicit:
                # Backward compat: use gemini_model_fallback when primary is gemini
                _gemini_fallback = env.get('GEMINI_MODEL_FALLBACK', 'gemini-3-flash-preview').strip()
                if litellm_model.startswith('gemini/') and _gemini_fallback:
                    _fb = f'gemini/{_gemini_fallback}' if '/' not in _gemini_fallback else _gemini_fallback
                    litellm_fallback_models = [_fb]

        if (
            inferred_legacy_deepseek_model
            and llm_models_source == "legacy_env"
            and litellm_model == 'deepseek/deepseek-chat'
        ):
            logger.warning(
                "Deprecation warning:\n"
                "deepseek-chat will be deprecated on 2026-07-24,\n"
                "please migrate to deepseek-v4-flash."
            )

        generation_backend = (
            env.get('GENERATION_BACKEND', LITELLM_BACKEND_ID).strip().lower()
            or LITELLM_BACKEND_ID
        )
        _generation_fallback_raw = env.get('GENERATION_FALLBACK_BACKEND')
        if _generation_fallback_raw is None:
            generation_fallback_backend = LITELLM_BACKEND_ID
        else:
            generation_fallback_backend = _generation_fallback_raw.strip().lower()
        agent_generation_backend = (
            env.get('AGENT_GENERATION_BACKEND', AUTO_AGENT_BACKEND_ID).strip().lower()
            or AUTO_AGENT_BACKEND_ID
        )
        generation_backend_timeout_seconds = parse_env_int(
            env.get('GENERATION_BACKEND_TIMEOUT_SECONDS'),
            DEFAULT_LOCAL_CLI_TIMEOUT_SECONDS,
            field_name='GENERATION_BACKEND_TIMEOUT_SECONDS',
            minimum=1,
            maximum=MAX_LOCAL_CLI_TIMEOUT_SECONDS,
        )
        generation_backend_max_output_bytes = parse_env_int(
            env.get('GENERATION_BACKEND_MAX_OUTPUT_BYTES'),
            DEFAULT_LOCAL_CLI_MAX_OUTPUT_BYTES,
            field_name='GENERATION_BACKEND_MAX_OUTPUT_BYTES',
            minimum=1,
            maximum=MAX_LOCAL_CLI_OUTPUT_BYTES,
        )
        generation_backend_max_concurrency = parse_env_int(
            env.get('GENERATION_BACKEND_MAX_CONCURRENCY'),
            DEFAULT_GENERATION_BACKEND_MAX_CONCURRENCY,
            field_name='GENERATION_BACKEND_MAX_CONCURRENCY',
            minimum=1,
            maximum=MAX_GENERATION_BACKEND_MAX_CONCURRENCY,
        )
        local_cli_backend_max_concurrency = parse_env_int(
            env.get('LOCAL_CLI_BACKEND_MAX_CONCURRENCY'),
            DEFAULT_LOCAL_CLI_BACKEND_MAX_CONCURRENCY,
            field_name='LOCAL_CLI_BACKEND_MAX_CONCURRENCY',
            minimum=1,
            maximum=MAX_LOCAL_CLI_BACKEND_MAX_CONCURRENCY,
        )
        opencode_cli_model = (env.get('OPENCODE_CLI_MODEL', '') or '').strip()

        agent_litellm_model = normalize_agent_litellm_model(
            env.get('AGENT_LITELLM_MODEL', ''),
            configured_models=set(get_configured_llm_models(llm_model_list)),
        )
        agent_context_compression_profile = normalize_agent_context_compression_profile(
            env.get('AGENT_CONTEXT_COMPRESSION_PROFILE')
        )
        agent_context_compression_preset = get_agent_context_compression_preset(
            agent_context_compression_profile
        )
        agent_context_compression_trigger_tokens = parse_agent_context_compression_int(
            env.get('AGENT_CONTEXT_COMPRESSION_TRIGGER_TOKENS'),
            agent_context_compression_preset.trigger_tokens,
            field_name='AGENT_CONTEXT_COMPRESSION_TRIGGER_TOKENS',
            minimum=1000,
            maximum=200000,
        )
        agent_context_protected_turns = parse_agent_context_compression_int(
            env.get('AGENT_CONTEXT_PROTECTED_TURNS'),
            agent_context_compression_preset.protected_turns,
            field_name='AGENT_CONTEXT_PROTECTED_TURNS',
            minimum=1,
            maximum=20,
        )

        # 解析搜索引擎 API Keys（支持多个 key，逗号分隔）
        bocha_keys_str = env.get('BOCHA_API_KEYS', '')
        bocha_api_keys = [k.strip() for k in bocha_keys_str.split(',') if k.strip()]

        minimax_keys_str = env.get('MINIMAX_API_KEYS', '')
        minimax_api_keys = [k.strip() for k in minimax_keys_str.split(',') if k.strip()]
        
        tavily_keys_str = env.get('TAVILY_API_KEYS', '')
        tavily_api_keys = [k.strip() for k in tavily_keys_str.split(',') if k.strip()]
        
        serpapi_keys_str = env.get('SERPAPI_API_KEYS', '')
        serpapi_keys = [k.strip() for k in serpapi_keys_str.split(',') if k.strip()]

        brave_keys_str = env.get('BRAVE_API_KEYS', '')
        brave_api_keys = [k.strip() for k in brave_keys_str.split(',') if k.strip()]

        _raw_urls = [u.strip() for u in env.get('SEARXNG_BASE_URLS', '').split(',') if u.strip()]
        searxng_base_urls = []
        invalid_searxng_urls = []
        for u in _raw_urls:
            p = urlparse(u)
            if p.scheme in ('http', 'https') and p.netloc:
                searxng_base_urls.append(u)
            else:
                invalid_searxng_urls.append(u)
        if invalid_searxng_urls:
            logger.warning(
                "SEARXNG_BASE_URLS 中存在无效 URL，已忽略: %s",
                ", ".join(invalid_searxng_urls[:3]),
            )
        searxng_public_instances_enabled = parse_env_bool(
            env.get('SEARXNG_PUBLIC_INSTANCES_ENABLED'),
            default=False,
        )

        # 企微消息类型与最大字节数逻辑
        wechat_msg_type = env.get('WECHAT_MSG_TYPE', 'markdown')
        wechat_msg_type_lower = wechat_msg_type.lower()
        wechat_max_bytes_env = env.get('WECHAT_MAX_BYTES')
        if wechat_max_bytes_env not in (None, ''):
            wechat_max_bytes = parse_env_int(
                wechat_max_bytes_env,
                2048 if wechat_msg_type_lower == 'text' else 4000,
                field_name='WECHAT_MAX_BYTES',
                minimum=1,
            )
        else:
            # 未显式配置时，根据消息类型选择默认字节数
            wechat_max_bytes = 2048 if wechat_msg_type_lower == 'text' else 4000

        # Preserve historical semantics for startup flags: only an explicit
        # literal "true" enables immediate execution; empty strings stay False.
        legacy_run_immediately_env = cls._resolve_env_value(
            'RUN_IMMEDIATELY',
            prefer_env_file=True,
            env=env,
            user_override_keys=user_override_keys,
        )
        legacy_run_immediately = (
            legacy_run_immediately_env.lower() == 'true'
            if legacy_run_immediately_env is not None
            else True
        )

        schedule_run_immediately_env = cls._resolve_env_value(
            'SCHEDULE_RUN_IMMEDIATELY',
            prefer_env_file=True,
            env=env,
            user_override_keys=user_override_keys,
        )
        # Keep backward compatibility for container/process overrides:
        # when RUN_IMMEDIATELY is explicitly provided by the runtime but the
        # schedule-specific alias is absent, schedule mode should inherit the
        # legacy process value instead of being pulled back to the persisted
        # `.env` copy of SCHEDULE_RUN_IMMEDIATELY.
        if (
            not cls._had_bootstrap_runtime_env_key('SCHEDULE_RUN_IMMEDIATELY')
            and cls._has_bootstrap_runtime_env_override('RUN_IMMEDIATELY')
        ):
            schedule_run_immediately = legacy_run_immediately
        else:
            schedule_run_immediately = (
                schedule_run_immediately_env.lower() == 'true'
                if schedule_run_immediately_env is not None
                else legacy_run_immediately
            )
        schedule_time_value = cls._resolve_env_value(
            'SCHEDULE_TIME',
            default='18:00',
            prefer_env_file=True,
            env=env,
            user_override_keys=user_override_keys,
        )
        schedule_times_value = cls._resolve_env_value(
            'SCHEDULE_TIMES',
            default='',
            prefer_env_file=True,
            env=env,
            user_override_keys=user_override_keys,
        )

        report_language_raw = cls._resolve_report_language_env_value(
            preexisting_report_language,
            env=env,
            user_override_keys=user_override_keys,
        )
        report_show_llm_model_raw = env.get('REPORT_SHOW_LLM_MODEL')
        report_show_llm_model = parse_env_bool(report_show_llm_model_raw, default=True)
        if report_show_llm_model_raw is not None and not report_show_llm_model_raw.strip():
            report_show_llm_model = False

        return cls(
            stock_list=stock_list,
            feishu_app_id=env.get('FEISHU_APP_ID'),
            feishu_app_secret=env.get('FEISHU_APP_SECRET'),
            feishu_folder_token=env.get('FEISHU_FOLDER_TOKEN'),
            tushare_token=env.get('TUSHARE_TOKEN'),
            biying_licence=env.get('BIYING_LICENCE'),
            biying_timeout_seconds=parse_env_float(env.get('BIYING_TIMEOUT_SECONDS'), 8.0, field_name='BIYING_TIMEOUT_SECONDS', minimum=1.0),
            intraday_snapshot_ttl_seconds=parse_env_int(env.get('INTRADAY_SNAPSHOT_TTL_SECONDS'), 60, field_name='INTRADAY_SNAPSHOT_TTL_SECONDS', minimum=0),
            tickflow_api_key=env.get('TICKFLOW_API_KEY'),
            tickflow_kline_adjust=normalize_tickflow_kline_adjust(env.get('TICKFLOW_KLINE_ADJUST')),
            tickflow_priority=parse_env_int(env.get('TICKFLOW_PRIORITY'), 2, field_name='TICKFLOW_PRIORITY', minimum=0),
            tickflow_batch_daily_enabled=parse_env_bool(env.get('TICKFLOW_BATCH_DAILY_ENABLED'), default=True),
            tickflow_batch_size=parse_env_int(env.get('TICKFLOW_BATCH_SIZE'), 100, field_name='TICKFLOW_BATCH_SIZE', minimum=1),
            futu_opend_host=env.get('FUTU_OPEND_HOST') or None,
            futu_opend_port=parse_env_int(env.get('FUTU_OPEND_PORT'), 11111, field_name='FUTU_OPEND_PORT', minimum=1, maximum=65535),
            futu_hk_realtime_source_priority=env.get('FUTU_HK_REALTIME_SOURCE_PRIORITY', 'futu,longbridge,akshare,yfinance'),
            mx_apikey=env.get('MX_APIKEY') or None,
            mx_priority=parse_env_int(env.get('MX_PRIORITY'), 6, field_name='MX_PRIORITY', minimum=0),
            finnhub_api_key=env.get('FINNHUB_API_KEY') or None,
            alphavantage_api_key=env.get('ALPHAVANTAGE_API_KEY') or None,
            longbridge_app_key=env.get('LONGBRIDGE_APP_KEY') or None,
            longbridge_app_secret=env.get('LONGBRIDGE_APP_SECRET') or None,
            longbridge_access_token=env.get('LONGBRIDGE_ACCESS_TOKEN') or None,
            longbridge_oauth_client_id=env.get('LONGBRIDGE_OAUTH_CLIENT_ID') or None,
            stock_index_remote_update_enabled=parse_env_bool(
                env.get('STOCK_INDEX_REMOTE_UPDATE_ENABLED'),
                default=True,
            ),
            generation_backend=generation_backend,
            generation_fallback_backend=generation_fallback_backend,
            generation_backend_timeout_seconds=generation_backend_timeout_seconds,
            generation_backend_max_output_bytes=generation_backend_max_output_bytes,
            generation_backend_max_concurrency=generation_backend_max_concurrency,
            local_cli_backend_max_concurrency=local_cli_backend_max_concurrency,
            opencode_cli_model=opencode_cli_model,
            litellm_model=litellm_model,
            litellm_light_model=normalize_agent_litellm_model(
                env.get('LITELLM_LIGHT_MODEL', ''),
                configured_models=set(get_configured_llm_models(llm_model_list)),
            ),
            litellm_fallback_models=litellm_fallback_models,
            llm_temperature=resolve_unified_llm_temperature(litellm_model, env),
            llm_reasoning_effort=_parse_reasoning_effort(env.get('LLM_REASONING_EFFORT'), 'LLM_REASONING_EFFORT'),
            agent_reasoning_effort=_parse_reasoning_effort(env.get('AGENT_REASONING_EFFORT'), 'AGENT_REASONING_EFFORT'),
            litellm_light_reasoning_effort=_parse_reasoning_effort(
                env.get('LITELLM_LIGHT_REASONING_EFFORT'), 'LITELLM_LIGHT_REASONING_EFFORT'
            ),
            litellm_config_path=litellm_config_path,
            llm_models_source=llm_models_source,
            llm_channels=llm_channels,
            llm_channel_names=llm_channel_names,
            llm_channel_config_issues=llm_channel_config_issues,
            llm_blocks_legacy_fallback=llm_blocks_legacy_fallback,
            llm_blocked_hermes_routes=llm_blocked_hermes_routes,
            llm_model_list=llm_model_list,
            llm_prompt_cache_telemetry_enabled=parse_env_bool(
                env.get("LLM_PROMPT_CACHE_TELEMETRY_ENABLED"),
                default=True,
            ),
            llm_prompt_cache_hints_enabled=parse_env_bool(
                env.get("LLM_PROMPT_CACHE_HINTS_ENABLED"),
                default=False,
            ),
            llm_prompt_cache_diagnostics_level=parse_prompt_cache_diagnostics_level(
                env.get("LLM_PROMPT_CACHE_DIAGNOSTICS_LEVEL")
            ),
            gemini_api_keys=gemini_api_keys,
            anthropic_api_keys=anthropic_api_keys,
            openai_api_keys=openai_api_keys,
            deepseek_api_keys=deepseek_api_keys,
            gemini_api_key=env.get('GEMINI_API_KEY'),
            gemini_model=env.get('GEMINI_MODEL', 'gemini-3.1-pro-preview'),
            gemini_model_fallback=env.get('GEMINI_MODEL_FALLBACK', 'gemini-3-flash-preview'),
            gemini_temperature=parse_env_float(env.get('GEMINI_TEMPERATURE'), 0.7, field_name='GEMINI_TEMPERATURE'),
            gemini_request_delay=parse_env_float(env.get('GEMINI_REQUEST_DELAY'), 2.0, field_name='GEMINI_REQUEST_DELAY', minimum=0.0),
            gemini_max_retries=parse_env_int(env.get('GEMINI_MAX_RETRIES'), 5, field_name='GEMINI_MAX_RETRIES', minimum=0),
            gemini_retry_delay=parse_env_float(env.get('GEMINI_RETRY_DELAY'), 5.0, field_name='GEMINI_RETRY_DELAY', minimum=0.0),
            anthropic_api_key=env.get('ANTHROPIC_API_KEY'),
            anthropic_model=env.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6'),
            anthropic_temperature=parse_env_float(env.get('ANTHROPIC_TEMPERATURE'), 0.7, field_name='ANTHROPIC_TEMPERATURE'),
            anthropic_max_tokens=parse_env_int(env.get('ANTHROPIC_MAX_TOKENS'), 8192, field_name='ANTHROPIC_MAX_TOKENS', minimum=1),
            # AIHubmix is the preferred OpenAI-compatible provider (one key, all models, no VPN required).
            # Within the OpenAI-compatible layer: AIHUBMIX_KEY takes priority over OPENAI_API_KEY.
            # Overall provider fallback order: Gemini > Anthropic > OpenAI-compatible (incl. AIHubmix).
            # base_url is auto-set to aihubmix.com/v1 when AIHUBMIX_KEY is used and no explicit
            # OPENAI_BASE_URL override is provided.
            # Model names match upstream (e.g. gemini-3.1-pro-preview, gpt-5.5, deepseek-v4-flash).
            openai_api_key=openai_api_keys[0] if openai_api_keys else None,
            openai_base_url=openai_base_url,
            openai_model=_openai_model_name,
            openai_vision_model=env.get('OPENAI_VISION_MODEL') or None,
            openai_temperature=parse_env_float(env.get('OPENAI_TEMPERATURE'), 0.7, field_name='OPENAI_TEMPERATURE'),
            # Vision model: VISION_MODEL > OPENAI_VISION_MODEL (alias) > default
            vision_model=(
                env.get('VISION_MODEL')
                or env.get('OPENAI_VISION_MODEL')
                or ""
            ),
            vision_provider_priority=env.get('VISION_PROVIDER_PRIORITY', 'gemini,anthropic,openai'),
            anspire_api_keys=anspire_api_keys,
            bocha_api_keys=bocha_api_keys,
            minimax_api_keys=minimax_api_keys,
            tavily_api_keys=tavily_api_keys,
            brave_api_keys=brave_api_keys,
            serpapi_keys=serpapi_keys,
            searxng_base_urls=searxng_base_urls,
            searxng_public_instances_enabled=searxng_public_instances_enabled,
            searxng_timeout_seconds=parse_env_int(
                env.get('SEARXNG_TIMEOUT_SECONDS'), 10, field_name='SEARXNG_TIMEOUT_SECONDS', minimum=1
            ),
            social_sentiment_api_key=env.get('SOCIAL_SENTIMENT_API_KEY') or None,
            social_sentiment_api_url=env.get('SOCIAL_SENTIMENT_API_URL', 'https://api.adanos.org').rstrip('/'),
            news_max_age_days=parse_env_int(env.get('NEWS_MAX_AGE_DAYS'), 3, field_name='NEWS_MAX_AGE_DAYS', minimum=1),
            news_strategy_profile=cls._parse_news_strategy_profile(
                env.get('NEWS_STRATEGY_PROFILE', 'short')
            ),
            news_intel_retention_days=parse_env_int(
                env.get('NEWS_INTEL_RETENTION_DAYS'),
                30,
                field_name='NEWS_INTEL_RETENTION_DAYS',
                minimum=1,
                maximum=365,
            ),
            news_intel_fetch_timeout_sec=parse_env_float(
                env.get('NEWS_INTEL_FETCH_TIMEOUT_SEC'),
                8.0,
                field_name='NEWS_INTEL_FETCH_TIMEOUT_SEC',
                minimum=1.0,
                maximum=30.0,
            ),
            news_intel_max_items_per_source=parse_env_int(
                env.get('NEWS_INTEL_MAX_ITEMS_PER_SOURCE'),
                50,
                field_name='NEWS_INTEL_MAX_ITEMS_PER_SOURCE',
                minimum=1,
                maximum=200,
            ),
            news_intel_auto_fetch_enabled=parse_env_bool(
                env.get('NEWS_INTEL_AUTO_FETCH_ENABLED'),
                False,
            ),
            news_poll_interval_minutes=parse_env_int(
                env.get('NEWS_POLL_INTERVAL_MINUTES'),
                10,
                field_name='NEWS_POLL_INTERVAL_MINUTES',
                minimum=1,
                maximum=240,
            ),
            news_poll_offhours_interval_minutes=parse_env_int(
                env.get('NEWS_POLL_OFFHOURS_INTERVAL_MINUTES'),
                30,
                field_name='NEWS_POLL_OFFHOURS_INTERVAL_MINUTES',
                minimum=1,
                maximum=1440,
            ),
            news_akshare_cls_symbol=cls._parse_news_cls_symbol(env.get('NEWS_AKSHARE_CLS_SYMBOL')),
            news_flash_retention_days=parse_env_int(
                env.get('NEWS_FLASH_RETENTION_DAYS'),
                3,
                field_name='NEWS_FLASH_RETENTION_DAYS',
                minimum=1,
                maximum=365,
            ),
            news_company_retention_days=parse_env_int(
                env.get('NEWS_COMPANY_RETENTION_DAYS'),
                30,
                field_name='NEWS_COMPANY_RETENTION_DAYS',
                minimum=1,
                maximum=365,
            ),
            news_dedup_window_minutes=parse_env_int(
                env.get('NEWS_DEDUP_WINDOW_MINUTES'),
                30,
                field_name='NEWS_DEDUP_WINDOW_MINUTES',
                minimum=1,
                maximum=720,
            ),
            news_fetch_timeout_sec=parse_env_float(
                env.get('NEWS_FETCH_TIMEOUT_SEC'),
                30.0,
                field_name='NEWS_FETCH_TIMEOUT_SEC',
                minimum=3.0,
            ),
            news_announcement_mode=cls._parse_news_announcement_mode(env.get('NEWS_ANNOUNCEMENT_MODE')),
            news_announcement_by_code_max=parse_env_int(
                env.get('NEWS_ANNOUNCEMENT_BY_CODE_MAX'),
                12,
                field_name='NEWS_ANNOUNCEMENT_BY_CODE_MAX',
                minimum=1,
                maximum=200,
            ),
            news_daily_fetch_time=_parse_hhmm(
                env.get('NEWS_DAILY_FETCH_TIME'),
                default='08:05',
                field_name='NEWS_DAILY_FETCH_TIME',
            ),
            news_classify_enabled=parse_env_bool(env.get('NEWS_CLASSIFY_ENABLED'), True),
            news_classify_batch_size=parse_env_int(
                env.get('NEWS_CLASSIFY_BATCH_SIZE'),
                20,
                field_name='NEWS_CLASSIFY_BATCH_SIZE',
                minimum=1,
                maximum=50,
            ),
            news_classify_max_items_per_run=parse_env_int(
                env.get('NEWS_CLASSIFY_MAX_ITEMS_PER_RUN'),
                60,
                field_name='NEWS_CLASSIFY_MAX_ITEMS_PER_RUN',
                minimum=1,
                maximum=500,
            ),
            news_classify_max_attempts=parse_env_int(
                env.get('NEWS_CLASSIFY_MAX_ATTEMPTS'),
                5,
                field_name='NEWS_CLASSIFY_MAX_ATTEMPTS',
                minimum=1,
                maximum=20,
            ),
            news_board_driver_min_importance=parse_env_int(
                env.get('NEWS_BOARD_DRIVER_MIN_IMPORTANCE'),
                4,
                field_name='NEWS_BOARD_DRIVER_MIN_IMPORTANCE',
                minimum=1,
                maximum=5,
            ),
            newsnow_base_url=((env.get('NEWSNOW_BASE_URL') or '').strip().rstrip('/') or 'https://newsnow.busiyi.world'),
            bias_threshold=parse_env_float(env.get('BIAS_THRESHOLD'), 5.0, field_name='BIAS_THRESHOLD', minimum=1.0),
            agent_backend=(env.get('AGENT_BACKEND', 'auto') or 'auto').strip().lower(),
            agent_generation_backend=agent_generation_backend,
            agent_litellm_model=agent_litellm_model,
            agent_mode=env.get('AGENT_MODE', 'false').lower() == 'true',
            _agent_mode_explicit=env.get('AGENT_MODE') is not None,
            agent_max_steps=parse_env_int(
                env.get('AGENT_MAX_STEPS'),
                AGENT_MAX_STEPS_DEFAULT,
                field_name='AGENT_MAX_STEPS',
                minimum=1,
            ),
            agent_skills=[s.strip() for s in env.get('AGENT_SKILLS', '').split(',') if s.strip()],
            agent_skill_dir=env.get('AGENT_SKILL_DIR') or env.get('AGENT_STRATEGY_DIR'),
            agent_nl_routing=env.get('AGENT_NL_ROUTING', 'false').lower() == 'true',
            agent_arch=env.get('AGENT_ARCH', 'single').lower(),
            agent_orchestrator_mode=env.get('AGENT_ORCHESTRATOR_MODE', 'standard').lower(),
            agent_orchestrator_timeout_s=parse_env_int(
                env.get('AGENT_ORCHESTRATOR_TIMEOUT_S'),
                600,
                field_name='AGENT_ORCHESTRATOR_TIMEOUT_S',
                minimum=0,
            ),
            agent_technical_agent_timeout_s=parse_env_float(
                env.get('AGENT_TECHNICAL_AGENT_TIMEOUT_S'), 0,
                field_name='AGENT_TECHNICAL_AGENT_TIMEOUT_S', minimum=0,
            ),
            agent_intel_agent_timeout_s=parse_env_float(
                env.get('AGENT_INTEL_AGENT_TIMEOUT_S'), 0,
                field_name='AGENT_INTEL_AGENT_TIMEOUT_S', minimum=0,
            ),
            agent_risk_agent_timeout_s=parse_env_float(
                env.get('AGENT_RISK_AGENT_TIMEOUT_S'), 0,
                field_name='AGENT_RISK_AGENT_TIMEOUT_S', minimum=0,
            ),
            agent_decision_agent_timeout_s=parse_env_float(
                env.get('AGENT_DECISION_AGENT_TIMEOUT_S'), 0,
                field_name='AGENT_DECISION_AGENT_TIMEOUT_S', minimum=0,
            ),
            agent_portfolio_agent_timeout_s=parse_env_float(
                env.get('AGENT_PORTFOLIO_AGENT_TIMEOUT_S'), 0,
                field_name='AGENT_PORTFOLIO_AGENT_TIMEOUT_S', minimum=0,
            ),
            agent_skill_agent_timeout_s=parse_env_float(
                env.get('AGENT_SKILL_AGENT_TIMEOUT_S'), 0,
                field_name='AGENT_SKILL_AGENT_TIMEOUT_S', minimum=0,
            ),
            agent_data_tool_timeout_s=parse_env_float(
                env.get('AGENT_DATA_TOOL_TIMEOUT_S'), 0.0,
                field_name='AGENT_DATA_TOOL_TIMEOUT_S', minimum=0.0,
            ),
            agent_search_tool_timeout_s=parse_env_float(
                env.get('AGENT_SEARCH_TOOL_TIMEOUT_S'), 0.0,
                field_name='AGENT_SEARCH_TOOL_TIMEOUT_S', minimum=0.0,
            ),
            agent_analysis_tool_timeout_s=parse_env_float(
                env.get('AGENT_ANALYSIS_TOOL_TIMEOUT_S'), 0.0,
                field_name='AGENT_ANALYSIS_TOOL_TIMEOUT_S', minimum=0.0,
            ),
            agent_action_tool_timeout_s=parse_env_float(
                env.get('AGENT_ACTION_TOOL_TIMEOUT_S'), 0.0,
                field_name='AGENT_ACTION_TOOL_TIMEOUT_S', minimum=0.0,
            ),
            agent_skill_concurrency=parse_env_int(
                env.get('AGENT_SKILL_CONCURRENCY'),
                3,
                field_name='AGENT_SKILL_CONCURRENCY',
                minimum=1,
                maximum=4,
            ),
            agent_risk_override=env.get('AGENT_RISK_OVERRIDE', 'true').lower() == 'true',
            agent_deep_research_budget=parse_env_int(
                env.get('AGENT_DEEP_RESEARCH_BUDGET'),
                30000,
                field_name='AGENT_DEEP_RESEARCH_BUDGET',
                minimum=5000,
            ),
            agent_deep_research_timeout=parse_env_int(
                env.get('AGENT_DEEP_RESEARCH_TIMEOUT'),
                180,
                field_name='AGENT_DEEP_RESEARCH_TIMEOUT',
                minimum=30,
            ),
            agent_memory_enabled=env.get('AGENT_MEMORY_ENABLED', 'false').lower() == 'true',
            agent_skill_autoweight=(
                env.get('AGENT_SKILL_AUTOWEIGHT')
                or env.get('AGENT_STRATEGY_AUTOWEIGHT', 'true')
            ).lower() == 'true',
            agent_skill_routing=(
                env.get('AGENT_SKILL_ROUTING')
                or env.get('AGENT_STRATEGY_ROUTING', 'auto')
            ).lower(),
            agent_context_compression_enabled=parse_env_bool(
                env.get('AGENT_CONTEXT_COMPRESSION_ENABLED'),
                default=False,
            ),
            agent_context_compression_profile=agent_context_compression_profile,
            agent_context_compression_trigger_tokens=agent_context_compression_trigger_tokens,
            agent_context_protected_turns=agent_context_protected_turns,
            agent_event_monitor_enabled=env.get('AGENT_EVENT_MONITOR_ENABLED', 'false').lower() == 'true',
            assistant_recall_provider=(env.get('ASSISTANT_RECALL_PROVIDER') or 'none').strip().lower() or 'none',
            hindsight_base_url=(env.get('HINDSIGHT_BASE_URL') or '').strip(),
            hindsight_api_key=(env.get('HINDSIGHT_API_KEY') or '').strip() or None,
            hindsight_bank_id=(env.get('HINDSIGHT_BANK_ID') or '').strip(),
            assistant_recall_timeout_s=parse_env_float(
                env.get('ASSISTANT_RECALL_TIMEOUT_S'),
                2.0,
                field_name='ASSISTANT_RECALL_TIMEOUT_S',
                minimum=0.2,
                maximum=30.0,
            ),
            assistant_recall_mask_amounts=parse_env_bool(
                env.get('ASSISTANT_RECALL_MASK_AMOUNTS'), default=True,
            ),
            assistant_note_daily_proposal_limit=parse_env_int(
                env.get('ASSISTANT_NOTE_DAILY_PROPOSAL_LIMIT'),
                2,
                field_name='ASSISTANT_NOTE_DAILY_PROPOSAL_LIMIT',
                minimum=0,
                maximum=50,
            ),
            assistant_note_inject_max_chars=parse_env_int(
                env.get('ASSISTANT_NOTE_INJECT_MAX_CHARS'),
                3000,
                field_name='ASSISTANT_NOTE_INJECT_MAX_CHARS',
                minimum=200,
                maximum=20000,
            ),
            agent_event_monitor_interval_minutes=parse_env_int(
                env.get('AGENT_EVENT_MONITOR_INTERVAL_MINUTES'),
                5,
                field_name='AGENT_EVENT_MONITOR_INTERVAL_MINUTES',
                minimum=1,
            ),
            agent_event_alert_rules_json=env.get('AGENT_EVENT_ALERT_RULES_JSON', ''),
            alert_max_enabled_rules_per_user=parse_env_int(
                env.get('ALERT_MAX_ENABLED_RULES_PER_USER'),
                200,
                field_name='ALERT_MAX_ENABLED_RULES_PER_USER',
                minimum=1,
                maximum=1000,
            ),
            wechat_webhook_url=env.get('WECHAT_WEBHOOK_URL'),
            feishu_webhook_url=env.get('FEISHU_WEBHOOK_URL'),
            feishu_webhook_secret=env.get('FEISHU_WEBHOOK_SECRET'),
            feishu_webhook_keyword=env.get('FEISHU_WEBHOOK_KEYWORD'),
            dingtalk_webhook_url=env.get('DINGTALK_WEBHOOK_URL'),
            dingtalk_secret=env.get('DINGTALK_SECRET'),
            

            feishu_chat_id=env.get('FEISHU_CHAT_ID'),
            feishu_receive_id_type=env.get('FEISHU_RECEIVE_ID_TYPE', 'chat_id'),
            feishu_domain=env.get('FEISHU_DOMAIN', 'feishu'),
            telegram_bot_token=env.get('TELEGRAM_BOT_TOKEN'),
            telegram_chat_id=env.get('TELEGRAM_CHAT_ID'),
            telegram_message_thread_id=env.get('TELEGRAM_MESSAGE_THREAD_ID'),
            email_sender=env.get('EMAIL_SENDER'),
            email_sender_name=env.get('EMAIL_SENDER_NAME', 'daily_stock_analysis股票分析助手'),
            email_password=env.get('EMAIL_PASSWORD'),
            email_receivers=[r.strip() for r in env.get('EMAIL_RECEIVERS', '').split(',') if r.strip()],
            stock_email_groups=cls._parse_stock_email_groups(env),
            pushover_user_key=env.get('PUSHOVER_USER_KEY'),
            pushover_api_token=env.get('PUSHOVER_API_TOKEN'),
            ntfy_url=env.get('NTFY_URL'),
            ntfy_token=env.get('NTFY_TOKEN'),
            gotify_url=env.get('GOTIFY_URL'),
            gotify_token=env.get('GOTIFY_TOKEN'),
            pushplus_token=env.get('PUSHPLUS_TOKEN'),
            pushplus_topic=env.get('PUSHPLUS_TOPIC'),
            serverchan3_sendkey=env.get('SERVERCHAN3_SENDKEY'),
            custom_webhook_urls=[u.strip() for u in env.get('CUSTOM_WEBHOOK_URLS', '').split(',') if u.strip()],
            custom_webhook_bearer_token=env.get('CUSTOM_WEBHOOK_BEARER_TOKEN'),
            custom_webhook_body_template=unescape_compose_sensitive_env_value(
                'CUSTOM_WEBHOOK_BODY_TEMPLATE',
                env.get('CUSTOM_WEBHOOK_BODY_TEMPLATE') or '',
            ) or None,
            webhook_verify_ssl=env.get('WEBHOOK_VERIFY_SSL', 'true').lower() == 'true',
            discord_bot_token=env.get('DISCORD_BOT_TOKEN'),
            discord_main_channel_id=(
                env.get('DISCORD_MAIN_CHANNEL_ID')
                or env.get('DISCORD_CHANNEL_ID')
            ),
            discord_webhook_url=env.get('DISCORD_WEBHOOK_URL'),
            discord_interactions_public_key=env.get('DISCORD_INTERACTIONS_PUBLIC_KEY'),
            slack_webhook_url=env.get('SLACK_WEBHOOK_URL'),
            slack_bot_token=env.get('SLACK_BOT_TOKEN'),
            slack_channel_id=env.get('SLACK_CHANNEL_ID'),
            astrbot_url=env.get('ASTRBOT_URL'),
            astrbot_token=env.get('ASTRBOT_TOKEN'),
            notification_report_channels=parse_notification_route_channels(
                env.get('NOTIFICATION_REPORT_CHANNELS')
            ),
            notification_alert_channels=parse_notification_route_channels(
                env.get('NOTIFICATION_ALERT_CHANNELS')
            ),
            notification_system_error_channels=parse_notification_route_channels(
                env.get('NOTIFICATION_SYSTEM_ERROR_CHANNELS')
            ),
            notification_dedup_ttl_seconds=parse_env_int(
                env.get('NOTIFICATION_DEDUP_TTL_SECONDS'),
                0,
                field_name='NOTIFICATION_DEDUP_TTL_SECONDS',
                minimum=0,
            ),
            notification_cooldown_seconds=parse_env_int(
                env.get('NOTIFICATION_COOLDOWN_SECONDS'),
                0,
                field_name='NOTIFICATION_COOLDOWN_SECONDS',
                minimum=0,
            ),
            notification_quiet_hours=(env.get('NOTIFICATION_QUIET_HOURS') or '').strip(),
            notification_timezone=(env.get('NOTIFICATION_TIMEZONE') or '').strip(),
            notification_min_severity=(env.get('NOTIFICATION_MIN_SEVERITY') or '').strip().lower(),
            notification_daily_digest_enabled=parse_env_bool(
                env.get('NOTIFICATION_DAILY_DIGEST_ENABLED'),
                default=False,
            ),
            single_stock_notify=env.get('SINGLE_STOCK_NOTIFY', 'false').lower() == 'true',
            report_type=cls._parse_report_type(env.get('REPORT_TYPE', 'simple')),
            report_language=cls._parse_report_language(report_language_raw),
            report_summary_only=env.get('REPORT_SUMMARY_ONLY', 'false').lower() == 'true',
            report_show_llm_model=report_show_llm_model,
            report_templates_dir=env.get('REPORT_TEMPLATES_DIR', 'templates'),
            report_renderer_enabled=env.get('REPORT_RENDERER_ENABLED', 'false').lower() == 'true',
            report_integrity_enabled=env.get('REPORT_INTEGRITY_ENABLED', 'true').lower() == 'true',
            report_integrity_retry=parse_env_int(env.get('REPORT_INTEGRITY_RETRY'), 1, field_name='REPORT_INTEGRITY_RETRY', minimum=0),
            report_history_compare_n=parse_env_int(env.get('REPORT_HISTORY_COMPARE_N'), 0, field_name='REPORT_HISTORY_COMPARE_N', minimum=0),
            analysis_delay=parse_env_float(env.get('ANALYSIS_DELAY'), 0.0, field_name='ANALYSIS_DELAY', minimum=0.0),
            merge_email_notification=env.get('MERGE_EMAIL_NOTIFICATION', 'false').lower() == 'true',
            feishu_max_bytes=parse_env_int(env.get('FEISHU_MAX_BYTES'), 20000, field_name='FEISHU_MAX_BYTES', minimum=1),
            feishu_send_as_file=env.get('FEISHU_SEND_AS_FILE', '').lower() in ('true', '1', 'yes'),
            wechat_max_bytes=wechat_max_bytes,
            wechat_msg_type=wechat_msg_type_lower,
            discord_max_words=parse_env_int(env.get('DISCORD_MAX_WORDS'), 2000, field_name='DISCORD_MAX_WORDS', minimum=1),
            markdown_to_image_channels=[
                c.strip().lower()
                for c in env.get('MARKDOWN_TO_IMAGE_CHANNELS', '').split(',')
                if c.strip()
            ],
            markdown_to_image_max_chars=parse_env_int(
                env.get('MARKDOWN_TO_IMAGE_MAX_CHARS'),
                15000,
                field_name='MARKDOWN_TO_IMAGE_MAX_CHARS',
                minimum=1,
            ),
            md2img_engine=cls._parse_md2img_engine(env.get('MD2IMG_ENGINE', 'wkhtmltoimage')),
            share_image_xiaohongshu_url=(env.get('SHARE_IMAGE_XIAOHONGSHU_URL') or '').strip() or None,
            share_image_xiaohongshu_handle=(env.get('SHARE_IMAGE_XIAOHONGSHU_HANDLE') or '').strip() or None,
            share_image_xiaohongshu_id=(env.get('SHARE_IMAGE_XIAOHONGSHU_ID') or '').strip() or None,
            share_image_xiaohongshu_qr_path=(env.get('SHARE_IMAGE_XIAOHONGSHU_QR_PATH') or '').strip() or None,
            prefetch_realtime_quotes=env.get('PREFETCH_REALTIME_QUOTES', 'true').lower() == 'true',
            database_path=env.get('DATABASE_PATH', './data/stock_analysis.db'),
            sqlite_wal_enabled=env.get('SQLITE_WAL_ENABLED', 'true').lower() == 'true',
            sqlite_busy_timeout_ms=parse_env_int(
                env.get('SQLITE_BUSY_TIMEOUT_MS'),
                5000,
                field_name='SQLITE_BUSY_TIMEOUT_MS',
                minimum=0,
            ),
            sqlite_write_retry_max=parse_env_int(
                env.get('SQLITE_WRITE_RETRY_MAX'),
                3,
                field_name='SQLITE_WRITE_RETRY_MAX',
                minimum=0,
            ),
            sqlite_write_retry_base_delay=parse_env_float(
                env.get('SQLITE_WRITE_RETRY_BASE_DELAY'),
                0.1,
                field_name='SQLITE_WRITE_RETRY_BASE_DELAY',
                minimum=0.0,
            ),
            market_snapshot_retention_days=parse_env_int(
                env.get('MARKET_SNAPSHOT_RETENTION_DAYS'),
                250,
                field_name='MARKET_SNAPSHOT_RETENTION_DAYS',
                minimum=30,
            ),
            market_snapshot_backfill_days=parse_env_int(
                env.get('MARKET_SNAPSHOT_BACKFILL_DAYS'),
                60,
                field_name='MARKET_SNAPSHOT_BACKFILL_DAYS',
                minimum=1,
            ),
            market_benchmark_code=(env.get('MARKET_BENCHMARK_CODE') or '').strip() or '000985.CSI',
            market_snapshot_requests_per_minute=parse_env_int(
                env.get('MARKET_SNAPSHOT_REQUESTS_PER_MINUTE'),
                60,
                field_name='MARKET_SNAPSHOT_REQUESTS_PER_MINUTE',
                minimum=1,
            ),
            market_snapshot_members_requests_per_minute=parse_env_int(
                env.get('MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE'),
                200,
                field_name='MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE',
                minimum=1,
            ),
            board_anomaly_thresholds=(env.get('BOARD_ANOMALY_THRESHOLDS') or '').strip(),
            board_pipeline_enabled=parse_env_bool(env.get('BOARD_PIPELINE_ENABLED'), default=True),
            board_pipeline_schedule_time=_parse_hhmm(
                env.get('BOARD_PIPELINE_SCHEDULE_TIME'),
                default='17:00',
                field_name='BOARD_PIPELINE_SCHEDULE_TIME',
            ),
            board_membership_full_day_fallback=parse_env_bool(
                env.get('BOARD_MEMBERSHIP_FULL_DAY_FALLBACK'), default=False
            ),
            board_industry_membership_enabled=parse_env_bool(
                env.get('BOARD_INDUSTRY_MEMBERSHIP_ENABLED'), default=True
            ),
            board_industry_membership_weekday=_parse_weekday(
                env.get('BOARD_INDUSTRY_MEMBERSHIP_WEEKDAY'),
                default=6,
                field_name='BOARD_INDUSTRY_MEMBERSHIP_WEEKDAY',
            ),
            board_industry_membership_time=_parse_hhmm(
                env.get('BOARD_INDUSTRY_MEMBERSHIP_TIME'),
                default='10:00',
                field_name='BOARD_INDUSTRY_MEMBERSHIP_TIME',
            ),
            us_leaders=(env.get('US_LEADERS') or '').strip(),
            us_move_stock_pct=parse_env_float(
                env.get('US_MOVE_STOCK_PCT'),
                4.0,
                field_name='US_MOVE_STOCK_PCT',
                minimum=0.1,
            ),
            us_move_index_pct=parse_env_float(
                env.get('US_MOVE_INDEX_PCT'),
                1.5,
                field_name='US_MOVE_INDEX_PCT',
                minimum=0.1,
            ),
            preopen_push_enabled=parse_env_bool(env.get('PREOPEN_PUSH_ENABLED'), default=False),
            preopen_push_time=_parse_hhmm(
                env.get('PREOPEN_PUSH_TIME'),
                default='08:15',
                field_name='PREOPEN_PUSH_TIME',
            ),
            daily_plan_enabled=parse_env_bool(env.get('DAILY_PLAN_ENABLED'), default=False),
            position_stop_loss_pct=parse_env_float(
                env.get('POSITION_STOP_LOSS_PCT'), 10.0, field_name='POSITION_STOP_LOSS_PCT',
                minimum=0.1, maximum=99.9,
            ),
            position_max_total_pct=parse_env_float(
                env.get('POSITION_MAX_TOTAL_PCT'), 100.0, field_name='POSITION_MAX_TOTAL_PCT',
                minimum=0.1, maximum=100.0,
            ),
            position_max_holdings=parse_env_int(
                env.get('POSITION_MAX_HOLDINGS'), 1, field_name='POSITION_MAX_HOLDINGS', minimum=1,
            ),
            position_max_single_pct=_parse_optional_pct(
                env.get('POSITION_MAX_SINGLE_PCT'), field_name='POSITION_MAX_SINGLE_PCT',
            ),
            position_stop_mode=_parse_stop_mode(env.get('POSITION_STOP_MODE')),
            save_context_snapshot=env.get('SAVE_CONTEXT_SNAPSHOT', 'true').lower() == 'true',
            backtest_enabled=env.get('BACKTEST_ENABLED', 'true').lower() == 'true',
            backtest_eval_window_days=parse_env_int(env.get('BACKTEST_EVAL_WINDOW_DAYS'), 10, field_name='BACKTEST_EVAL_WINDOW_DAYS', minimum=1),
            backtest_min_age_days=parse_env_int(env.get('BACKTEST_MIN_AGE_DAYS'), 14, field_name='BACKTEST_MIN_AGE_DAYS', minimum=1),
            backtest_engine_version=env.get('BACKTEST_ENGINE_VERSION', 'v1'),
            backtest_neutral_band_pct=parse_env_float(
                env.get('BACKTEST_NEUTRAL_BAND_PCT'),
                2.0,
                field_name='BACKTEST_NEUTRAL_BAND_PCT',
                minimum=0.0,
            ),
            etf_rotation_pool=cls._parse_etf_rotation_pool(env.get('ETF_ROTATION_POOL')),
            etf_rotation_safe_asset=(
                env.get('ETF_ROTATION_SAFE_ASSET', DEFAULT_ETF_ROTATION_SAFE_ASSET) or ''
            ).strip(),
            etf_rotation_lookback_days=parse_env_int(
                env.get('ETF_ROTATION_LOOKBACK_DAYS'), 60,
                field_name='ETF_ROTATION_LOOKBACK_DAYS', minimum=5, maximum=500,
            ),
            etf_rotation_rebalance=cls._parse_etf_rotation_rebalance(env.get('ETF_ROTATION_REBALANCE')),
            etf_rotation_top_n=parse_env_int(
                env.get('ETF_ROTATION_TOP_N'), 2,
                field_name='ETF_ROTATION_TOP_N', minimum=1, maximum=10,
            ),
            etf_rotation_switch_buffer_pct=parse_env_float(
                env.get('ETF_ROTATION_SWITCH_BUFFER_PCT'), 2.0,
                field_name='ETF_ROTATION_SWITCH_BUFFER_PCT', minimum=0.0, maximum=50.0,
            ),
            etf_rotation_cost_bps=parse_env_float(
                env.get('ETF_ROTATION_COST_BPS'), 10.0,
                field_name='ETF_ROTATION_COST_BPS', minimum=0.0, maximum=500.0,
            ),
            etf_rotation_backtest_years=parse_env_int(
                env.get('ETF_ROTATION_BACKTEST_YEARS'), 8,
                field_name='ETF_ROTATION_BACKTEST_YEARS', minimum=1, maximum=30,
            ),
            log_dir=env.get('LOG_DIR', './logs'),
            log_level=env.get('LOG_LEVEL', 'INFO'),
            max_workers=parse_env_int(env.get('MAX_WORKERS'), 3, field_name='MAX_WORKERS', minimum=1),
            analysis_max_concurrent_per_user=parse_env_int(
                env.get('ANALYSIS_MAX_CONCURRENT_PER_USER'),
                1,
                field_name='ANALYSIS_MAX_CONCURRENT_PER_USER',
                minimum=1,
                maximum=20,
            ),
            debug=env.get('DEBUG', 'false').lower() == 'true',
            config_validate_mode=env.get('CONFIG_VALIDATE_MODE', 'warn').lower(),
            primary_user_id=(env.get('PRIMARY_USER_ID') or '').strip() or DEFAULT_PRIMARY_USER_ID,
            http_proxy=env.get('HTTP_PROXY'),
            https_proxy=env.get('HTTPS_PROXY'),
            schedule_enabled=cls._resolve_env_value(
                'SCHEDULE_ENABLED',
                default='false',
                prefer_env_file=True,
                env=env,
                user_override_keys=user_override_keys,
            ).lower() == 'true',
            schedule_time=(schedule_time_value or '18:00').strip() or '18:00',
            schedule_times=normalize_schedule_times(
                schedule_times_value,
                fallback_time=(schedule_time_value or '18:00').strip() or '18:00',
            ),
            schedule_run_immediately=schedule_run_immediately,
            run_immediately=legacy_run_immediately,
            market_review_enabled=env.get('MARKET_REVIEW_ENABLED', 'true').lower() == 'true',
            daily_market_context_enabled=env.get('DAILY_MARKET_CONTEXT_ENABLED', 'true').lower() == 'true',
            market_review_region=cls._parse_market_review_region(
                env.get('MARKET_REVIEW_REGION', 'cn')
            ),
            market_review_color_scheme=cls._parse_market_review_color_scheme(
                env.get('MARKET_REVIEW_COLOR_SCHEME', 'green_up')
            ),
            trading_day_check_enabled=env.get('TRADING_DAY_CHECK_ENABLED', 'true').lower() != 'false',
            webui_enabled=env.get('WEBUI_ENABLED', 'false').lower() == 'true',
            webui_host=env.get('WEBUI_HOST', '127.0.0.1'),
            webui_port=parse_env_int(env.get('WEBUI_PORT'), 8000, field_name='WEBUI_PORT', minimum=1, maximum=65535),
            # 机器人配置
            bot_enabled=env.get('BOT_ENABLED', 'true').lower() == 'true',
            bot_command_prefix=env.get('BOT_COMMAND_PREFIX', '/'),
            bot_rate_limit_requests=parse_env_int(env.get('BOT_RATE_LIMIT_REQUESTS'), 10, field_name='BOT_RATE_LIMIT_REQUESTS', minimum=1),
            bot_rate_limit_window=parse_env_int(env.get('BOT_RATE_LIMIT_WINDOW'), 60, field_name='BOT_RATE_LIMIT_WINDOW', minimum=1),
            bot_admin_users=[u.strip() for u in env.get('BOT_ADMIN_USERS', '').split(',') if u.strip()],
            # 飞书机器人
            feishu_verification_token=env.get('FEISHU_VERIFICATION_TOKEN'),
            feishu_encrypt_key=env.get('FEISHU_ENCRYPT_KEY'),
            feishu_stream_enabled=env.get('FEISHU_STREAM_ENABLED', 'false').lower() == 'true',
            # 钉钉机器人
            dingtalk_app_key=env.get('DINGTALK_APP_KEY'),
            dingtalk_app_secret=env.get('DINGTALK_APP_SECRET'),
            dingtalk_stream_enabled=env.get('DINGTALK_STREAM_ENABLED', 'false').lower() == 'true',
            # 企业微信机器人
            wecom_corpid=env.get('WECOM_CORPID'),
            wecom_token=env.get('WECOM_TOKEN'),
            wecom_encoding_aes_key=env.get('WECOM_ENCODING_AES_KEY'),
            wecom_agent_id=env.get('WECOM_AGENT_ID'),
            # Telegram
            telegram_webhook_secret=env.get('TELEGRAM_WEBHOOK_SECRET'),
            # Discord 机器人扩展配置
            discord_bot_status=env.get('DISCORD_BOT_STATUS', 'A股智能分析 | /help'),
            # 实时行情增强数据配置
            enable_realtime_quote=env.get('ENABLE_REALTIME_QUOTE', 'true').lower() == 'true',
            enable_realtime_technical_indicators=env.get(
                'ENABLE_REALTIME_TECHNICAL_INDICATORS', 'true'
            ).lower() == 'true',
            enable_chip_distribution=env.get('ENABLE_CHIP_DISTRIBUTION', 'true').lower() == 'true',
            # 东财接口补丁开关
            enable_eastmoney_patch=env.get('ENABLE_EASTMONEY_PATCH', 'false').lower() == 'true',
            # 实时行情数据源优先级：
            # - tencent: 腾讯财经，有量比/换手率/PE/PB等，单股查询稳定（推荐）
            # - akshare_sina: 新浪财经，基本行情稳定，但无量比
            # - efinance/akshare_em: 东财全量接口，数据最全但容易被封
            # - tushare: Tushare Pro，需要2000积分，数据全面
            realtime_source_priority=cls._resolve_realtime_source_priority(env),
            realtime_cache_ttl=parse_env_int(env.get('REALTIME_CACHE_TTL'), 600, field_name='REALTIME_CACHE_TTL', minimum=0),
            circuit_breaker_cooldown=parse_env_int(env.get('CIRCUIT_BREAKER_COOLDOWN'), 300, field_name='CIRCUIT_BREAKER_COOLDOWN', minimum=0),
            enable_fundamental_pipeline=env.get('ENABLE_FUNDAMENTAL_PIPELINE', 'true').lower() == 'true',
            fundamental_stage_timeout_seconds=parse_env_float(
                env.get('FUNDAMENTAL_STAGE_TIMEOUT_SECONDS'),
                FUNDAMENTAL_STAGE_TIMEOUT_SECONDS_DEFAULT,
                field_name='FUNDAMENTAL_STAGE_TIMEOUT_SECONDS',
                minimum=0.0,
            ),
            fundamental_fetch_timeout_seconds=parse_env_float(
                env.get('FUNDAMENTAL_FETCH_TIMEOUT_SECONDS'),
                8.0,
                field_name='FUNDAMENTAL_FETCH_TIMEOUT_SECONDS',
                minimum=0.0,
            ),
            fundamental_retry_max=parse_env_int(env.get('FUNDAMENTAL_RETRY_MAX'), 1, field_name='FUNDAMENTAL_RETRY_MAX', minimum=0),
            fundamental_cache_ttl_seconds=parse_env_int(
                env.get('FUNDAMENTAL_CACHE_TTL_SECONDS'),
                120,
                field_name='FUNDAMENTAL_CACHE_TTL_SECONDS',
                minimum=0,
            ),
            fundamental_cache_max_entries=parse_env_int(
                env.get('FUNDAMENTAL_CACHE_MAX_ENTRIES'),
                256,
                field_name='FUNDAMENTAL_CACHE_MAX_ENTRIES',
                minimum=1,
            ),
            portfolio_risk_concentration_alert_pct=parse_env_float(
                env.get('PORTFOLIO_RISK_CONCENTRATION_ALERT_PCT'),
                35.0,
                field_name='PORTFOLIO_RISK_CONCENTRATION_ALERT_PCT',
                minimum=0.0,
            ),
            portfolio_risk_drawdown_alert_pct=parse_env_float(
                env.get('PORTFOLIO_RISK_DRAWDOWN_ALERT_PCT'),
                15.0,
                field_name='PORTFOLIO_RISK_DRAWDOWN_ALERT_PCT',
                minimum=0.0,
            ),
            portfolio_risk_stop_loss_alert_pct=parse_env_float(
                env.get('PORTFOLIO_RISK_STOP_LOSS_ALERT_PCT'),
                10.0,
                field_name='PORTFOLIO_RISK_STOP_LOSS_ALERT_PCT',
                minimum=0.0,
            ),
            portfolio_risk_stop_loss_near_ratio=parse_env_float(
                env.get('PORTFOLIO_RISK_STOP_LOSS_NEAR_RATIO'),
                0.8,
                field_name='PORTFOLIO_RISK_STOP_LOSS_NEAR_RATIO',
                minimum=0.0,
            ),
            portfolio_risk_lookback_days=parse_env_int(
                env.get('PORTFOLIO_RISK_LOOKBACK_DAYS'),
                180,
                field_name='PORTFOLIO_RISK_LOOKBACK_DAYS',
                minimum=1,
            ),
            portfolio_fx_update_enabled=env.get('PORTFOLIO_FX_UPDATE_ENABLED', 'true').lower() == 'true',
            screening_enabled=parse_env_bool(env.get('SCREENING_ENABLED'), default=False),
        )
    
    @classmethod
    def _parse_litellm_yaml(
        cls,
        config_path: str,
        env: Optional[Mapping[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """Parse a standard LiteLLM config YAML file into Router model_list.

        Supports the ``os.environ/VAR_NAME`` syntax for secret references.
        Returns an empty list on any error (logged, never raises).
        """
        import logging
        _logger = logging.getLogger(__name__)
        try:
            import yaml
        except ImportError:
            _logger.warning("PyYAML not installed; LITELLM_CONFIG ignored. Install with: pip install pyyaml")
            return []

        path = Path(config_path)
        if not path.is_absolute():
            path = Path(__file__).parent.parent / path
        if not path.exists():
            _logger.warning(f"LITELLM_CONFIG file not found: {path}")
            return []

        try:
            with open(path, encoding='utf-8') as f:
                yaml_config = yaml.safe_load(f) or {}
        except Exception as e:
            _logger.warning(f"Failed to parse LITELLM_CONFIG: {e}")
            return []

        model_list = yaml_config.get('model_list', [])
        if not isinstance(model_list, list):
            _logger.warning("LITELLM_CONFIG: model_list must be a list")
            return []

        # Resolve os.environ/ references in string params
        for entry in model_list:
            params = entry.get('litellm_params', {})
            for key in list(params.keys()):
                val = params.get(key)
                if isinstance(val, str) and val.startswith('os.environ/'):
                    env_name = val.split('/', 1)[1]
                    params[key] = (os.environ if env is None else env).get(env_name, '')

        _logger.info(f"LITELLM_CONFIG: loaded {len(model_list)} model deployment(s) from {path}")
        return model_list

    @classmethod
    def _parse_llm_channels(cls, channels_str: str) -> List[Dict[str, Any]]:
        """Backward-compatible channel parser returning only valid channels."""
        channels, _issues, _blocks, _blocked_routes = cls._parse_llm_channels_with_issues(channels_str)
        return channels

    @classmethod
    def _parse_llm_channels_with_issues(
        cls,
        channels_str: str,
        env: Optional[Mapping[str, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[HermesConfigIssue], bool, List[str]]:
        """Parse LLM_CHANNELS env var and per-channel env vars.

        Format:
            LLM_CHANNELS=aihubmix,deepseek,gemini
            LLM_AIHUBMIX_PROTOCOL=openai
            LLM_AIHUBMIX_API_SURFACE=chat_completions
            LLM_AIHUBMIX_BASE_URL=https://aihubmix.com/v1
            LLM_AIHUBMIX_API_KEY=sk-xxx           (or LLM_AIHUBMIX_API_KEYS=k1,k2)
            LLM_AIHUBMIX_MODELS=gpt-5.5,claude-sonnet-4-6
            LLM_AIHUBMIX_ENABLED=true
        """
        import logging
        _logger = logging.getLogger(__name__)
        env = os.environ if env is None else env

        channels: List[Dict[str, Any]] = []
        issues: List[HermesConfigIssue] = []
        blocks_legacy_fallback = False
        blocked_hermes_routes: List[str] = []

        def record_blocked_hermes_routes(raw_models: List[str]) -> None:
            nonlocal blocks_legacy_fallback
            blocks_legacy_fallback = True
            for raw_model in raw_models or [HERMES_DEFAULT_MODEL]:
                for route_name in hermes_blocked_route_candidates(raw_model):
                    if route_name not in blocked_hermes_routes:
                        blocked_hermes_routes.append(route_name)

        for raw_name in channels_str.split(','):
            ch_name = raw_name.strip()
            if not ch_name:
                continue
            ch_lower = ch_name.lower()
            ch_upper = ch_name.upper()

            base_url = resolve_llm_channel_base_url(
                ch_name, env.get(f'LLM_{ch_upper}_BASE_URL', '')
            ) or None
            if ch_lower == "anspire" and not base_url:
                base_url = (
                    env.get('ANSPIRE_LLM_BASE_URL') or ANSPIRE_LLM_BASE_URL_DEFAULT
                ).strip() or None
            protocol_raw = env.get(f'LLM_{ch_upper}_PROTOCOL', '').strip()
            if ch_lower == "anspire" and not protocol_raw:
                protocol_raw = "openai"
            api_surface_raw = env.get(f'LLM_{ch_upper}_API_SURFACE', '').strip()
            enabled_raw = env.get(f'LLM_{ch_upper}_ENABLED')
            if ch_lower == "anspire" and (enabled_raw is None or not enabled_raw.strip()):
                enabled_raw = env.get('ANSPIRE_LLM_ENABLED')
            enabled = parse_env_bool(enabled_raw, default=True)

            # API keys: LLM_{NAME}_API_KEYS (multi) > LLM_{NAME}_API_KEY (single)
            api_keys_raw = env.get(f'LLM_{ch_upper}_API_KEYS', '')
            api_keys = [k.strip() for k in api_keys_raw.split(',') if k.strip()]
            single_key = env.get(f'LLM_{ch_upper}_API_KEY', '').strip()
            if not api_keys:
                if single_key:
                    api_keys = [single_key]
            if not api_keys and ch_lower == "anspire":
                anspire_keys_raw = env.get('ANSPIRE_API_KEYS', '')
                api_keys = [k.strip() for k in anspire_keys_raw.split(',') if k.strip()]
            api_keys = resolve_llm_channel_api_keys(ch_name, api_keys, env)

            # Models
            models_raw = env.get(f'LLM_{ch_upper}_MODELS', '')
            raw_models = [m.strip() for m in models_raw.split(',') if m.strip()]
            raw_models = resolve_llm_channel_models(
                ch_name, raw_models, channel_names=channels_str,
                primary_model=env.get('LITELLM_MODEL', ''),
                fallback_models=env.get('LITELLM_FALLBACK_MODELS', ''),
            )
            if not raw_models and ch_lower == "anspire":
                anspire_model = (
                    env.get('ANSPIRE_LLM_MODEL') or ANSPIRE_LLM_MODEL_DEFAULT
                ).strip()
                if anspire_model:
                    raw_models = [anspire_model]

            # Disabled channels are inert. In particular, stale values such as
            # LLM_HERMES_API_SURFACE=responses must not block valid legacy
            # deployments after Hermes has been explicitly disabled.
            if not enabled:
                _logger.info("LLM channel '%s': disabled, skipped", ch_name)
                continue

            if not is_supported_llm_channel_api_surface_value(api_surface_raw):
                issues.append(HermesConfigIssue(
                    f"LLM_{ch_upper}_API_SURFACE",
                    "invalid_api_surface",
                    (
                        f"Unsupported LLM API surface '{api_surface_raw}'. "
                        f"Supported: {', '.join(SUPPORTED_LLM_CHANNEL_API_SURFACES)}"
                    ),
                ))
                if is_reserved_hermes_name(ch_name):
                    record_blocked_hermes_routes(raw_models)
                _logger.warning(
                    "LLM_%s_API_SURFACE=%s is unsupported; channel skipped",
                    ch_upper,
                    api_surface_raw,
                )
                continue
            api_surface = normalize_llm_channel_api_surface(api_surface_raw)

            if is_reserved_hermes_name(ch_name):
                if api_surface == "responses":
                    issues.append(HermesConfigIssue(
                        f"LLM_{ch_upper}_API_SURFACE",
                        "hermes_responses_unsupported",
                        "The reserved Hermes channel does not support the Responses API surface",
                    ))
                    record_blocked_hermes_routes(raw_models)
                    _logger.warning(
                        "LLM_%s_API_SURFACE=responses is unsupported for reserved Hermes channel; channel skipped",
                        ch_upper,
                    )
                    continue
                if not raw_models:
                    raw_models = [HERMES_DEFAULT_MODEL]
                result = parse_hermes_channel(
                    enabled=enabled,
                    protocol=protocol_raw or HERMES_DEFAULT_PROTOCOL,
                    base_url=base_url or HERMES_DEFAULT_BASE_URL,
                    api_key=single_key,
                    api_keys_raw=api_keys_raw,
                    extra_headers_raw=env.get(f'LLM_{ch_upper}_EXTRA_HEADERS', ''),
                    models=raw_models,
                )
                issues.extend(result.issues)
                blocks_legacy_fallback = blocks_legacy_fallback or result.blocks_legacy_fallback
                for route_name in result.blocked_route_names:
                    if route_name not in blocked_hermes_routes:
                        blocked_hermes_routes.append(route_name)
                if result.channel is None:
                    if not enabled:
                        _logger.info("LLM channel '%s': disabled, skipped", ch_name)
                    else:
                        _logger.warning("LLM channel '%s': invalid reserved Hermes channel, skipped", ch_name)
                    continue
                channels.append(result.channel)
                _logger.info("LLM channel '%s': Hermes preset with %d model(s)", ch_name, len(result.channel["models"]))
                continue

            protocol = resolve_llm_channel_protocol(protocol_raw, base_url=base_url, models=raw_models, channel_name=ch_name)
            if api_surface == "responses" and protocol != "openai":
                issues.append(HermesConfigIssue(
                    f"LLM_{ch_upper}_API_SURFACE",
                    "responses_requires_openai_protocol",
                    "Responses API surface currently requires the openai protocol",
                ))
                _logger.warning(
                    "LLM_%s_API_SURFACE=responses requires protocol=openai; channel skipped",
                    ch_upper,
                )
                continue
            incompatible_models = find_incompatible_llm_channel_models(
                raw_models,
                protocol,
                api_surface,
                base_url,
            )
            if incompatible_models:
                issues.append(HermesConfigIssue(
                    f"LLM_{ch_upper}_MODELS",
                    "responses_requires_openai_model_provider",
                    (
                        "Responses API surface requires every model to use the OpenAI "
                        f"provider route; incompatible: {', '.join(incompatible_models[:3])}"
                    ),
                ))
                _logger.warning(
                    "LLM_%s_API_SURFACE=responses has non-OpenAI model routes (%s); channel skipped",
                    ch_upper,
                    ", ".join(incompatible_models[:3]),
                )
                continue
            models = [normalize_llm_channel_model(m, protocol, base_url) for m in raw_models]

            # Extra headers (JSON string, optional)
            extra_headers_raw = env.get(f'LLM_{ch_upper}_EXTRA_HEADERS', '').strip()
            extra_headers = None
            if extra_headers_raw:
                try:
                    extra_headers = json.loads(extra_headers_raw)
                except json.JSONDecodeError:
                    _logger.warning(f"LLM_{ch_upper}_EXTRA_HEADERS: invalid JSON, ignored")

            if protocol_raw and canonicalize_llm_channel_protocol(protocol_raw) not in SUPPORTED_LLM_CHANNEL_PROTOCOLS:
                _logger.warning(
                    "LLM_%s_PROTOCOL=%s is unsupported; auto-detected protocol=%s",
                    ch_upper,
                    protocol_raw,
                    protocol or "unknown",
                )

            if not api_keys and channel_allows_empty_api_key(protocol, base_url):
                api_keys = [""]

            if not api_keys:
                _logger.warning(f"LLM channel '{ch_name}': no API key configured, skipped")
                continue
            if not models:
                _logger.warning(f"LLM channel '{ch_name}': no models configured, skipped")
                continue

            channels.append({
                'name': ch_name.lower(),
                'protocol': protocol,
                'api_surface': api_surface,
                'enabled': enabled,
                'base_url': base_url,
                'api_keys': api_keys,
                'models': models,
                'extra_headers': extra_headers,
            })
            _logger.info(f"LLM channel '{ch_name}': {len(models)} model(s), {len(api_keys)} key(s)")

        surface_conflicts = find_llm_channel_surface_conflicts(channels)
        if surface_conflicts:
            conflicting_models = set(surface_conflicts)
            for model, surfaces in surface_conflicts.items():
                issues.append(HermesConfigIssue(
                    "LLM_CHANNELS",
                    "mixed_api_surfaces_for_route",
                    (
                        f"LLM route alias '{model}' is declared with multiple API surfaces: "
                        f"{', '.join(surfaces)}"
                    ),
                ))
                _logger.warning(
                    "LLM route alias '%s' mixes API surfaces (%s); conflicting channels skipped",
                    model,
                    ", ".join(surfaces),
                )
            channels = [
                channel
                for channel in channels
                if not {
                    normalize_llm_channel_model(
                        str(model),
                        str(channel.get("protocol") or ""),
                        str(channel.get("base_url") or ""),
                    )
                    for model in channel.get("models") or []
                }.intersection(conflicting_models)
            ]

        return channels, issues, blocks_legacy_fallback, blocked_hermes_routes

    @classmethod
    def _channels_to_model_list(cls, channels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert parsed LLM channels to LiteLLM Router model_list format.

        Mapping follows:
        - LiteLLM providers: https://docs.litellm.ai/docs/providers
        - LiteLLM model_list 语义: https://docs.litellm.ai/docs/proxy/configs#the-model_list-key
        """
        surface_conflicts = find_llm_channel_surface_conflicts(channels)
        if surface_conflicts:
            raise ValueError(
                "LLM route aliases cannot mix API surfaces: "
                + ", ".join(sorted(surface_conflicts))
            )
        model_list: List[Dict[str, Any]] = []
        for ch in channels:
            hermes_refs = {
                str(ref.get("route_model") or ""): ref
                for ref in (ch.get("model_refs") or [])
                if isinstance(ref, dict)
            }
            for model_name in ch['models']:
                for api_key in ch['api_keys']:
                    model_ref = hermes_refs.get(str(model_name))
                    wire_model = str((model_ref or {}).get("wire_model") or model_name)
                    api_surface = normalize_llm_channel_api_surface(ch.get("api_surface"))
                    wire_model = apply_litellm_api_surface(wire_model, api_surface)
                    litellm_params: Dict[str, Any] = {
                        'model': wire_model,
                    }
                    if api_key:
                        litellm_params['api_key'] = api_key
                    if ch['base_url']:
                        litellm_params['api_base'] = ch['base_url']
                    # Auto-inject aihubmix sponsored header
                    headers = dict(ch.get('extra_headers') or {})
                    if ch['base_url'] and 'aihubmix.com' in ch['base_url']:
                        headers.setdefault('APP-Code', 'GPIJ3886')
                    if headers:
                        litellm_params['extra_headers'] = headers

                    entry: Dict[str, Any] = {
                        'model_name': model_name,
                        'litellm_params': litellm_params,
                    }
                    if ch.get("is_hermes") or is_reserved_hermes_name(str(ch.get("name") or "")):
                        entry["model_info"] = hermes_model_info(
                            str((model_ref or {}).get("display_model") or "")
                        )
                    elif api_surface == "responses":
                        entry["model_info"] = {"dsa_api_surface": "responses"}
                    model_list.append(entry)
        return model_list

    @classmethod
    def _legacy_keys_to_model_list(
        cls,
        gemini_keys: List[str],
        anthropic_keys: List[str],
        openai_keys: List[str],
        openai_base_url: Optional[str],
        deepseek_keys: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Build Router model_list from legacy per-provider keys (backward compat).

        Returns a model_list where each provider's keys are expanded into
        deployments, keyed by placeholder model_name tokens.  The analyzer
        resolves actual model_names at call time from LITELLM_MODEL /
        LITELLM_FALLBACK_MODELS.

        Compatibility note:
        - LiteLLM OpenAI-compatible 约定: https://docs.litellm.ai/docs/providers/openai_compatible
        - OpenAI 请求与鉴权约定: https://platform.openai.com/docs/api-reference/making-requests
          / https://platform.openai.com/docs/api-reference/authentication
        """
        model_list: List[Dict[str, Any]] = []

        # Gemini keys
        for k in gemini_keys:
            if k and len(k) >= 8:
                model_list.append({
                    'model_name': '__legacy_gemini__',
                    'litellm_params': {'model': '__legacy_gemini__', 'api_key': k},
                })

        # Anthropic keys
        for k in anthropic_keys:
            if k and len(k) >= 8:
                model_list.append({
                    'model_name': '__legacy_anthropic__',
                    'litellm_params': {'model': '__legacy_anthropic__', 'api_key': k},
                })

        # OpenAI-compatible keys
        for k in openai_keys:
            if k and len(k) >= 8:
                params: Dict[str, Any] = {'model': '__legacy_openai__', 'api_key': k}
                if openai_base_url:
                    params['api_base'] = openai_base_url
                if openai_base_url and 'aihubmix.com' in openai_base_url:
                    params['extra_headers'] = {'APP-Code': 'GPIJ3886'}
                model_list.append({
                    'model_name': '__legacy_openai__',
                    'litellm_params': params,
                })

        # DeepSeek keys (native litellm provider — auto-resolves api_base)
        for k in (deepseek_keys or []):
            if k and len(k) >= 8:
                model_list.append({
                    'model_name': '__legacy_deepseek__',
                    'litellm_params': {
                        'model': '__legacy_deepseek__',
                        'api_key': k,
                    },
                })

        return model_list

    @classmethod
    def _parse_stock_email_groups(
        cls,
        env: Optional[Mapping[str, str]] = None,
    ) -> List[Tuple[List[str], List[str]]]:
        """
        Parse STOCK_GROUP_N and EMAIL_GROUP_N from environment.
        Returns [(stocks, emails), ...] ordered by group index.
        Stock codes are canonicalized via normalize_stock_code so that
        runtime routing matches the same equivalence used in validation.
        """
        from data_provider.base import normalize_stock_code

        env = os.environ if env is None else env
        groups: dict = {}
        stock_re = re.compile(r'^STOCK_GROUP_(\d+)$', re.IGNORECASE)
        email_re = re.compile(r'^EMAIL_GROUP_(\d+)$', re.IGNORECASE)
        for key in env:
            m = stock_re.match(key)
            if m:
                idx = int(m.group(1))
                val = env[key].strip()
                groups.setdefault(idx, {})['stocks'] = [
                    normalize_stock_code(c.strip())
                    for c in val.split(',') if c.strip()
                ]
            m = email_re.match(key)
            if m:
                idx = int(m.group(1))
                val = env[key].strip()
                groups.setdefault(idx, {})['emails'] = [e.strip() for e in val.split(',') if e.strip()]
        result = []
        for idx in sorted(groups.keys()):
            g = groups[idx]
            if 'stocks' in g and 'emails' in g and g['stocks'] and g['emails']:
                result.append((g['stocks'], g['emails']))
        return result

    @classmethod
    def _parse_report_type(cls, value: str) -> str:
        """Parse REPORT_TYPE, fallback to simple for invalid values (supports brief)."""
        v = (value or 'simple').strip().lower()
        if v in ('simple', 'full', 'brief'):
            return v
        import logging
        logging.getLogger(__name__).warning(
            f"REPORT_TYPE '{value}' invalid, fallback to 'simple' (valid: simple/full/brief)"
        )
        return 'simple'

    @classmethod
    def _get_env_file_value(cls, key: str) -> Optional[str]:
        """Read one config key directly from the active `.env` file."""
        env_file = os.getenv("ENV_FILE")
        env_path = Path(env_file) if env_file else (Path(__file__).parent.parent / ".env")
        if not env_path.exists():
            return None

        try:
            env_values = dotenv_values(env_path)
        except Exception as exc:  # pragma: no cover - defensive branch
            logging.getLogger(__name__).warning(
                "Failed to read %s while resolving %s: %s",
                env_path,
                key,
                exc,
            )
            return None

        value = env_values.get(key)
        if value is None:
            return None
        return unescape_compose_sensitive_env_value(key, str(value))

    @classmethod
    def _resolve_env_value(
        cls,
        key: str,
        *,
        default: Optional[str] = None,
        prefer_env_file: bool = False,
        env: Optional[Mapping[str, str]] = None,
        user_override_keys: frozenset = frozenset(),
    ) -> Optional[str]:
        """Resolve one env value, optionally preferring the persisted `.env` copy.

        ``user_override_keys`` are keys whose value in ``env`` comes from a user's
        personal layer; they always win over the persisted `.env` copy.
        """
        env = os.environ if env is None else env
        env_value = env.get(key)
        if key in user_override_keys and env_value is not None:
            return env_value
        file_value = cls._get_env_file_value(key)

        should_prefer_file = prefer_env_file or key in cls._WEBUI_RUNTIME_ENV_FILE_PRIORITY_KEYS
        if should_prefer_file and file_value is not None:
            if env_value is not None and cls._has_bootstrap_runtime_env_override(key):
                return env_value
            return file_value
        if env_value is not None:
            return env_value
        if file_value is not None:
            return file_value
        return default

    @classmethod
    def _capture_bootstrap_runtime_env_overrides(cls) -> None:
        """Remember process-provided runtime env overrides before dotenv mutates os.environ.

        Called by ``setup_env()`` **before** ``load_dotenv()``, so ``os.environ``
        only contains genuine process-level values (Docker ``environment:``,
        Dockerfile ``ENV``, shell exports, etc.).

        A key is treated as an explicit override when it is present in
        ``os.environ`` and either:
        * absent from the persisted ``.env`` file, **or**
        * present with a **different** value.

        When both values are identical, the distinction is irrelevant and we
        do **not** flag the key, so that a later ``.env`` update by WebUI can
        take effect on config reload without requiring a container restart.
        """
        if cls._BOOTSTRAP_RUNTIME_ENV_OVERRIDES_CAPTURED:
            return

        explicit_overrides = set()
        present_keys = set()
        for key in cls._WEBUI_RUNTIME_ENV_FILE_PRIORITY_KEYS:
            env_value = os.environ.get(key)
            if env_value is None:
                continue

            present_keys.add(key)
            file_value = cls._get_env_file_value(key)
            if file_value is None or env_value != file_value:
                explicit_overrides.add(key)

        cls._BOOTSTRAP_RUNTIME_ENV_OVERRIDES = frozenset(explicit_overrides)
        cls._BOOTSTRAP_RUNTIME_ENV_PRESENT_KEYS = frozenset(present_keys)
        cls._BOOTSTRAP_RUNTIME_ENV_OVERRIDES_CAPTURED = True

    @classmethod
    def _has_bootstrap_runtime_env_override(cls, key: str) -> bool:
        cls._capture_bootstrap_runtime_env_overrides()
        return key in cls._BOOTSTRAP_RUNTIME_ENV_OVERRIDES

    @classmethod
    def _had_bootstrap_runtime_env_key(cls, key: str) -> bool:
        cls._capture_bootstrap_runtime_env_overrides()
        return key in cls._BOOTSTRAP_RUNTIME_ENV_PRESENT_KEYS

    @classmethod
    def _resolve_report_language_env_value(
        cls,
        preexisting_env_value: Optional[str],
        env: Optional[Mapping[str, str]] = None,
        user_override_keys: frozenset = frozenset(),
    ) -> str:
        """Resolve REPORT_LANGUAGE while preserving real process env overrides."""
        env = os.environ if env is None else env
        if "REPORT_LANGUAGE" in user_override_keys and env.get("REPORT_LANGUAGE") is not None:
            return env["REPORT_LANGUAGE"]
        file_value = cls._get_env_file_value("REPORT_LANGUAGE")
        env_value = env.get("REPORT_LANGUAGE")

        if preexisting_env_value is not None:
            env_text = preexisting_env_value.strip()
            file_text = (file_value or "").strip()
            if file_text and env_text and env_text.lower() != file_text.lower():
                env_file = os.getenv("ENV_FILE") or str(Path(__file__).parent.parent / ".env")
                logging.getLogger(__name__).warning(
                    "REPORT_LANGUAGE environment value '%s' overrides %s ('%s')",
                    preexisting_env_value,
                    env_file,
                    file_value,
                )
            return preexisting_env_value

        if file_value is not None:
            return file_value

        return env_value or "zh"

    @classmethod
    def _parse_report_language(cls, value: Optional[str]) -> str:
        """Parse REPORT_LANGUAGE, fallback to zh for invalid values."""
        normalized = normalize_report_language(value, default="zh")
        raw = (value or "").strip()
        if raw and not is_supported_report_language_value(raw):
            logging.getLogger(__name__).warning(
                "REPORT_LANGUAGE '%s' invalid, fallback to 'zh' (valid: zh/en/ko)",
                value,
            )
        return normalized

    @classmethod
    def _parse_news_strategy_profile(cls, value: Optional[str]) -> str:
        """Parse NEWS_STRATEGY_PROFILE, fallback to short for invalid values."""
        normalized = normalize_news_strategy_profile(value)
        raw = (value or "short").strip().lower()
        if raw != normalized:
            logging.getLogger(__name__).warning(
                "NEWS_STRATEGY_PROFILE '%s' invalid, fallback to 'short' "
                "(valid: ultra_short/short/medium/long)",
                value,
            )
        return normalized

    def get_effective_news_window_days(self) -> int:
        """Return effective news window days after profile + max-age merge."""
        return resolve_news_window_days(
            news_max_age_days=self.news_max_age_days,
            news_strategy_profile=self.news_strategy_profile,
        )

    @staticmethod
    def _parse_news_announcement_mode(value: Optional[str]) -> str:
        normalized = (value or '').strip().lower()
        if not normalized:
            return 'auto'
        if normalized in ('auto', 'by_code', 'market'):
            return normalized
        logger.warning("NEWS_ANNOUNCEMENT_MODE=%r is invalid (expected auto / by_code / market); falling back to auto", value)
        return 'auto'

    @staticmethod
    def _parse_news_cls_symbol(value: Optional[str]) -> str:
        normalized = (value or '').strip()
        if not normalized:
            return '全部'
        if normalized in ('重点', '全部'):
            return normalized
        logger.warning("NEWS_AKSHARE_CLS_SYMBOL=%r is invalid (expected 全部 or 重点); falling back to 全部", value)
        return '全部'

    @staticmethod
    def _parse_etf_rotation_pool(value: Optional[str]) -> List[str]:
        if value is None or not value.strip():
            return list(DEFAULT_ETF_ROTATION_POOL)
        codes: List[str] = []
        for raw in value.split(','):
            code = raw.strip()
            if code and code not in codes:
                codes.append(code)
        return codes

    @staticmethod
    def _parse_etf_rotation_rebalance(value: Optional[str]) -> str:
        normalized = (value or 'weekly').strip().lower()
        if normalized in ('weekly', 'monthly'):
            return normalized
        logger.warning("ETF_ROTATION_REBALANCE=%r is invalid; falling back to weekly", value)
        return 'weekly'

    @classmethod
    def _parse_market_review_region(cls, value: str) -> str:
        """解析大盘复盘市场区域，非法值记录警告后回退为 cn"""
        normalized = normalize_market_review_region_lenient(value)
        if normalized is not None:
            return normalized

        logging.getLogger(__name__).warning(
            f"MARKET_REVIEW_REGION 配置值 '{value}' 无效，已回退为默认值 'cn'（合法值：cn / hk / us / jp / kr / both；支持逗号分隔有效值）"
        )
        return 'cn'

    @classmethod
    def _parse_market_review_color_scheme(cls, value: str) -> str:
        """Parse market-review index change color scheme."""
        import logging
        v = (value or 'green_up').strip().lower().replace('-', '_')
        if v in ('green_up', 'red_up'):
            return v
        logging.getLogger(__name__).warning(
            "MARKET_REVIEW_COLOR_SCHEME 配置值 '%s' 无效，已回退为默认值 'green_up'（合法值：green_up / red_up）",
            value,
        )
        return 'green_up'

    @classmethod
    def _parse_md2img_engine(cls, value: str) -> str:
        """Parse MD2IMG_ENGINE, fallback to wkhtmltoimage for invalid values (Issue #455)."""
        v = (value or 'wkhtmltoimage').strip().lower()
        if v in ('wkhtmltoimage', 'markdown-to-file', 'playwright'):
            return v
        if v:
            import logging
            logging.getLogger(__name__).warning(
                f"MD2IMG_ENGINE '{value}' invalid, fallback to 'wkhtmltoimage' "
                "(valid: wkhtmltoimage | markdown-to-file | playwright)"
            )
        return 'wkhtmltoimage'

    @classmethod
    def _resolve_realtime_source_priority(cls, env: Optional[Mapping[str, str]] = None) -> str:
        """
        Resolve realtime source priority with automatic tushare injection.

        When TUSHARE_TOKEN is configured but REALTIME_SOURCE_PRIORITY is not
        explicitly set, automatically prepend 'tushare' to the default priority
        so that the paid data source is utilized for realtime quotes as well.
        """
        env = os.environ if env is None else env
        explicit = env.get('REALTIME_SOURCE_PRIORITY')
        default_priority = 'tencent,akshare_sina,efinance,akshare_em'

        if explicit:
            # User explicitly set priority, respect it
            return explicit

        tushare_token = env.get('TUSHARE_TOKEN', '').strip()
        biying_licence = env.get('BIYING_LICENCE', '').strip()
        if tushare_token or biying_licence:
            # Paid sources configured but no explicit priority override:
            # tushare -> biying -> free sources (only the configured ones are injected)
            import logging
            logger = logging.getLogger(__name__)
            injected = [
                name for name, configured in (('tushare', tushare_token), ('biying', biying_licence))
                if configured
            ]
            resolved = ','.join(injected + [default_priority])
            logger.info(
                "Paid realtime source(s) detected, auto-injecting into realtime priority: %s",
                resolved,
            )
            return resolved

        return default_priority

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（主要用于测试）"""
        cls._instance = None
        cls._BOOTSTRAP_RUNTIME_ENV_OVERRIDES_CAPTURED = False
        cls._BOOTSTRAP_RUNTIME_ENV_OVERRIDES = frozenset()
        cls._BOOTSTRAP_RUNTIME_ENV_PRESENT_KEYS = frozenset()

    def has_searxng_enabled(self) -> bool:
        """Whether SearXNG fallback is enabled via self-hosted or public mode."""
        return bool(self.searxng_base_urls) or bool(self.searxng_public_instances_enabled)

    def has_search_capability_enabled(self) -> bool:
        """Whether any search provider is configured or SearXNG fallback is enabled."""
        return bool(
            self.anspire_api_keys
            or self.bocha_api_keys
            or self.minimax_api_keys
            or self.tavily_api_keys
            or self.brave_api_keys
            or self.serpapi_keys
            or self.has_searxng_enabled()
        )

    def is_agent_available(self) -> bool:
        """Check whether agent capabilities are usable.

        Decision table:

        +-----------------------+----------------------------+-----------------+
        | AGENT_MODE env        | Agent-safe route available | Result          |
        +-----------------------+----------------------------+-----------------+
        | ``false`` (explicit)  | any                        | False           |
        | ``true``              | yes                        | True            |
        | ``true``              | no                         | False           |
        | not set (default)     | yes                        | True            |
        | not set (default)     | no                         | False           |
        +-----------------------+----------------------------+-----------------+

        ``AGENT_MODE=true`` expresses user intent, but Phase 3 Hermes safety
        still requires a non-Hermes Agent route. Hermes-only deployments cannot
        satisfy Agent tool roundtrip support; mixed routes are usable only via
        their non-Hermes deployments. ``AGENT_MODE=false`` remains an explicit
        kill-switch. Explicit local CLI Agent backends are unavailable because
        they are text generation backends, not Agent tool-calling runtimes.
        """
        if (self.agent_generation_backend or AUTO_AGENT_BACKEND_ID).strip().lower() in GENERATION_ONLY_BACKEND_IDS:
            return False
        # Phase 3 no longer lets AGENT_MODE=true bypass tool-route safety.
        if self._agent_mode_explicit:
            if not self.agent_mode:
                return False
            primary_model = get_effective_agent_primary_model(self)
            origins = route_deployment_origins(self.llm_model_list, primary_model)
            return not origins.is_hermes_only
        # Auto-detect: Agent inherits global model when AGENT_LITELLM_MODEL is empty.
        primary_model = get_effective_agent_primary_model(self)
        if not primary_model:
            return False
        origins = route_deployment_origins(self.llm_model_list, primary_model)
        return not origins.is_hermes_only

    def refresh_stock_list(self) -> None:
        """
        热读取 STOCK_LIST 环境变量并更新配置中的自选股列表
        
        支持两种配置方式：
        1. .env 文件（本地开发、定时任务模式） - 修改后下次执行自动生效
        2. 系统环境变量（GitHub Actions、Docker） - 启动时固定，运行中不变
        """
        # 优先从 .env 文件读取最新配置，这样即使在容器环境中修改了 .env 文件，
        # 也能获取到最新的股票列表配置
        env_file = os.getenv("ENV_FILE")
        env_path = Path(env_file) if env_file else (Path(__file__).parent.parent / '.env')
        stock_list_str = ''
        if env_path.exists():
            # 直接从 .env 文件读取最新的配置
            env_values = dotenv_values(env_path)
            stock_list_str = (env_values.get('STOCK_LIST') or '').strip()

        # 如果 .env 文件不存在或未配置，才尝试从系统环境变量读取
        if not stock_list_str:
            stock_list_str = os.getenv('STOCK_LIST', '')

        stock_list = [
            (c or "").strip().upper()
            for c in split_stock_list(stock_list_str)
            if (c or "").strip()
        ]

        self.stock_list = stock_list
    
    def validate_structured(self) -> List[ConfigIssue]:
        """Return structured validation issues with severity levels.

        Covers all three LLM configuration tiers introduced by PR #494:
        - LITELLM_CONFIG (YAML)
        - LLM_CHANNELS (env)
        - Legacy per-provider keys

        Returns:
            List of ConfigIssue objects, each carrying a severity
            ("error" | "warning" | "info"), a human-readable message, and the
            primary environment variable / field name it relates to.
        """
        issues: List[ConfigIssue] = []

        # --- Stock list ---
        if not self.stock_list:
            issues.append(ConfigIssue(
                severity="error",
                message="未配置 STOCK_LIST。请设置至少一个股票代码，例如：600519,hk00700,AAPL。",
                field="STOCK_LIST",
            ))
        elif self.stock_email_groups:
            from data_provider.base import normalize_stock_code
            configured_stock_set = {
                normalize_stock_code(code)
                for code in self.stock_list
                if (code or "").strip()
            }
            missing_group_stocks_dict: Dict[str, None] = {}
            for stocks, _emails in self.stock_email_groups:
                for stock in stocks:
                    raw = (stock or "").strip()
                    if not raw:
                        continue
                    normalized_stock = normalize_stock_code(stock)
                    if normalized_stock in configured_stock_set:
                        continue
                    if normalized_stock in missing_group_stocks_dict:
                        continue
                    missing_group_stocks_dict[normalized_stock] = None
            missing_group_stocks = list(missing_group_stocks_dict.keys())
            if missing_group_stocks:
                issues.append(ConfigIssue(
                    severity="warning",
                    message=(
                        "检测到 STOCK_GROUP_N 中存在未包含在 STOCK_LIST 内的股票："
                        f"{', '.join(missing_group_stocks[:6])}。"
                        "STOCK_GROUP_N 仅用于邮件路由，不会扩大分析范围；"
                        "请先将这些股票加入 STOCK_LIST。"
                    ),
                    field="STOCK_GROUP_N",
                ))

        # --- Data sources (informational only) ---
        if not self.tushare_token:
            issues.append(ConfigIssue(
                severity="info",
                message="未配置 Tushare Token，将使用其他数据源",
                field="TUSHARE_TOKEN",
            ))

        # --- Generation backend selection ---
        generation_backend = (self.generation_backend or LITELLM_BACKEND_ID).strip().lower()
        generation_fallback_backend = str(self.generation_fallback_backend or "").strip().lower()
        agent_generation_backend = (
            self.agent_generation_backend or AUTO_AGENT_BACKEND_ID
        ).strip().lower()
        agent_backend = (self.agent_backend or "auto").strip().lower()
        if generation_backend not in SUPPORTED_GENERATION_BACKENDS:
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    "GENERATION_BACKEND 当前支持 "
                    f"{'、'.join(sorted(SUPPORTED_GENERATION_BACKENDS))}。"
                    f"已配置的值为：{generation_backend}。"
                ),
                field="GENERATION_BACKEND",
            ))
        if generation_fallback_backend and generation_fallback_backend == generation_backend:
            generation_fallback_backend = ""
        if generation_fallback_backend and generation_fallback_backend != LITELLM_BACKEND_ID:
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    "GENERATION_FALLBACK_BACKEND 当前支持 litellm、与 primary 相同的 no-op 值，或空字符串。"
                    f"已配置的值为：{generation_fallback_backend}。"
                ),
                field="GENERATION_FALLBACK_BACKEND",
            ))
        if agent_generation_backend not in SUPPORTED_AGENT_GENERATION_BACKENDS:
            agent_ui_backends = "、".join(sorted(SUPPORTED_AGENT_UI_BACKENDS))
            local_toolless_backends = "、".join(sorted(GENERATION_ONLY_BACKEND_IDS))
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    f"AGENT_GENERATION_BACKEND 当前支持 {agent_ui_backends}；"
                    f"local CLI backend（{local_toolless_backends}）仅作为显式 unsupported diagnostic 保留，"
                    "不支持 Agent 工具调用。"
                    f"已配置的值为：{agent_generation_backend}。"
                ),
                field="AGENT_GENERATION_BACKEND",
            ))
        if agent_backend not in SUPPORTED_AGENT_BACKENDS:
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    "AGENT_BACKEND 当前支持 auto、litellm、codex_app_server。"
                    f"已配置的值为：{agent_backend}。"
                ),
                field="AGENT_BACKEND",
                code="capability_unsupported",
            ))
        if agent_backend == "codex_app_server" and self.agent_arch != "single":
            issues.append(ConfigIssue(
                severity="error",
                message="Codex 本地 Agent 当前只支持单 Agent 问股，请将 AGENT_ARCH 设为 single。",
                field="AGENT_ARCH",
                code="unsupported_agent_arch",
            ))
        litellm_model_lower = (self.litellm_model or "").strip().lower()
        local_model_prefix = next(
            (
                backend_id
                for backend_id in GENERATION_ONLY_BACKEND_IDS
                if litellm_model_lower.startswith(f"{backend_id}/")
            ),
            "",
        )
        if local_model_prefix:
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    f"{local_model_prefix} 是 GENERATION_BACKEND，不是 LiteLLM provider。"
                    f"请不要使用 LITELLM_MODEL={local_model_prefix}/...。"
                ),
                field="LITELLM_MODEL",
            ))
        if generation_backend == OPENCODE_CLI_BACKEND_ID:
            opencode_model = (self.opencode_cli_model or "").strip()
            unsafe_model = bool(opencode_model) and (
                any(ch.isspace() for ch in opencode_model)
                or any(
                    marker in opencode_model
                    for marker in ("|", ">", "<", ";", "`", "&&", "||", "$")
                )
            )
            if unsafe_model:
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "OPENCODE_CLI_MODEL 是可选的 OpenCode 模型覆盖值。"
                        "配置时会作为单个 --model 参数传给 OpenCode，不能包含空白或 shell 元字符；"
                        "不配置时 DSA 将使用 OpenCode 自身默认模型。"
                    ),
                    field="OPENCODE_CLI_MODEL",
                ))

        # --- LLM availability ---
        for raw_issue in self.llm_channel_config_issues or []:
            issues.append(ConfigIssue(
                severity=raw_issue.get("severity", "error"),  # type: ignore[arg-type]
                message=raw_issue.get("message", "LLM channel configuration is invalid"),
                field=raw_issue.get("field", "LLM_CHANNELS"),
                code=raw_issue.get("code", "invalid_channel_config"),
            ))

        # llm_model_list is populated for YAML / channels / managed legacy keys.
        # Other LiteLLM-native providers (for example cohere/*) run through the
        # direct litellm env path and therefore do not populate llm_model_list.
        has_direct_env_model = bool(self.litellm_model) and _uses_direct_env_provider(self.litellm_model)
        local_generation_backend = generation_backend in LOCAL_CLI_GENERATION_BACKEND_IDS
        if not local_generation_backend and not self.llm_model_list and not has_direct_env_model:
            if self.litellm_config_path:
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "已配置 LITELLM_CONFIG，但未解析出可用模型。"
                        "请检查 YAML 中的 model_list、litellm_params 和环境变量引用。"
                    ),
                    field="LITELLM_CONFIG",
                ))
            elif self.llm_channel_names:
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "已配置 LLM_CHANNELS，但未解析出可用模型渠道。"
                        "请检查对应 LLM_<CHANNEL>_API_KEY(S)、"
                        "LLM_<CHANNEL>_MODELS、LLM_<CHANNEL>_PROTOCOL 或 Base URL。"
                    ),
                    field="LLM_CHANNELS",
                ))
            else:
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "未配置任何可用的 AI 模型接入。请至少配置 ANSPIRE_API_KEYS、"
                        "AIHUBMIX_KEY、GEMINI_API_KEY、ANTHROPIC_API_KEY、"
                        "OPENAI_API_KEY 或 DEEPSEEK_API_KEY 中的一个，或配置 "
                        "LITELLM_CONFIG / LLM_CHANNELS 可用模型渠道。"
                    ),
                    field="LITELLM_CONFIG",
                ))
        elif not local_generation_backend and not self.litellm_model:
            issues.append(ConfigIssue(
                severity="info",
                message=(
                    "尚未明确指定主模型，系统将自动从可用 API Key 推断。"
                    "建议尽早配置主模型（格式如 gemini/gemini-3.1-pro-preview）"
                ),
                field="LITELLM_MODEL",
            ))

        available_router_models = get_configured_llm_models(self.llm_model_list)
        available_router_model_set = set(available_router_models)

        def _has_runtime_source_for_model(model: str) -> bool:
            if not model or _uses_direct_env_provider(model):
                return True
            provider = _get_litellm_provider(model)
            if provider in {"gemini", "vertex_ai"}:
                return any(k and len(k) >= 8 for k in (self.gemini_api_keys or []))
            if provider == "anthropic":
                return any(k and len(k) >= 8 for k in (self.anthropic_api_keys or []))
            if provider == "deepseek":
                return any(k and len(k) >= 8 for k in (self.deepseek_api_keys or []))
            if provider == "openai":
                return any(k and len(k) >= 8 for k in (self.openai_api_keys or []))
            return False

        configured_agent_primary_model = bool((self.agent_litellm_model or "").strip())
        effective_agent_primary_model = get_effective_agent_primary_model(self)

        if available_router_model_set:
            if self.litellm_model:
                origins = route_deployment_origins(self.llm_model_list, self.litellm_model)
                if origins.is_mixed:
                    issues.append(ConfigIssue(
                        severity="error",
                        message=(
                            "Hermes/non-Hermes mixed generation routes are not supported in Phase 3. "
                            "请选择纯 Hermes 或纯非 Hermes 主模型。"
                        ),
                        field="LITELLM_MODEL",
                        code="mixed_hermes_route_unsupported",
                    ))
            if (
                self.litellm_model
                and not _uses_direct_env_provider(self.litellm_model)
                and not _matches_exact_route(self.litellm_model, available_router_model_set)
            ):
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "已配置的主模型未出现在当前渠道或高级模型路由配置中。"
                        f" 当前可用模型：{', '.join(available_router_models[:6])}"
                    ),
                    field="LITELLM_MODEL",
                ))

            if configured_agent_primary_model and effective_agent_primary_model:
                origins = route_deployment_origins(self.llm_model_list, effective_agent_primary_model)
                if origins.is_hermes_only:
                    issues.append(ConfigIssue(
                        severity="error",
                        message=(
                            "Hermes-only route 不能作为 Agent 主模型。"
                            "请选择包含非 Hermes deployment 的 Agent-safe route。"
                        ),
                        field="AGENT_LITELLM_MODEL",
                        code="explicit_agent_model_no_safe_deployment",
                    ))

            if (
                configured_agent_primary_model
                and effective_agent_primary_model
                and not _uses_direct_env_provider(effective_agent_primary_model)
                and not _matches_exact_route(effective_agent_primary_model, available_router_model_set)
            ):
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "已配置的 Agent 主模型未出现在当前渠道或高级模型路由配置中。"
                        f" 当前可用模型：{', '.join(available_router_models[:6])}"
                    ),
                    field="AGENT_LITELLM_MODEL",
                ))

            mixed_fallbacks = [
                model for model in (self.litellm_fallback_models or [])
                if route_deployment_origins(self.llm_model_list, model).is_mixed
            ]
            if mixed_fallbacks:
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "Hermes/non-Hermes mixed generation routes are not supported as fallback models in Phase 3: "
                        f"{', '.join(mixed_fallbacks[:3])}"
                    ),
                    field="LITELLM_FALLBACK_MODELS",
                    code="mixed_hermes_route_unsupported",
                ))

            invalid_fallbacks = [
                model for model in (self.litellm_fallback_models or [])
                if model and not _matches_exact_route(model, available_router_model_set)
                and not _uses_direct_env_provider(model)
            ]
            if invalid_fallbacks:
                issues.append(ConfigIssue(
                    severity="warning",
                    message=(
                        "备选模型中包含未在当前渠道或高级模型路由配置中声明的模型："
                        f"{', '.join(invalid_fallbacks[:3])}"
                    ),
                    field="LITELLM_FALLBACK_MODELS",
                ))

            if (
                self.vision_model
                and not _uses_direct_env_provider(self.vision_model)
                and not _matches_exact_route(self.vision_model, available_router_model_set)
            ):
                issues.append(ConfigIssue(
                    severity="warning",
                    message=(
                        "VISION_MODEL 未出现在当前渠道声明中。"
                        f" 当前可用模型：{', '.join(available_router_models[:6])}"
                    ),
                    field="VISION_MODEL",
                ))
            if self.vision_model and route_has_hermes(self.llm_model_list, self.vision_model):
                issues.append(ConfigIssue(
                    severity="error",
                    message=(
                        "Hermes Phase 3 未验证 Vision 能力，VISION_MODEL 不能选择包含 Hermes deployment 的 route。"
                    ),
                    field="VISION_MODEL",
                    code="hermes_vision_unsupported",
                ))
        elif (
            configured_agent_primary_model
            and effective_agent_primary_model
            and not _has_runtime_source_for_model(effective_agent_primary_model)
        ):
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    "已配置 Agent 主模型，但未找到可用的运行时来源"
                    "（启用渠道或匹配的 API Key）。"
                ),
                field="AGENT_LITELLM_MODEL",
            ))

        # --- Search engine (informational only) ---
        if not self.has_search_capability_enabled():
            issues.append(ConfigIssue(
                severity="info",
                message="未配置搜索引擎能力 (Bocha/MiniMax/Tavily/Brave/SerpAPI/SearXNG)，新闻搜索功能将不可用",
                field="BOCHA_API_KEYS",
            ))

        # --- Notification channels ---
        has_notification = bool(
            self.wechat_webhook_url
            or self.dingtalk_webhook_url
            or self.feishu_webhook_url
            or (
                (self.feishu_app_id or "")
                and (self.feishu_app_secret or "")
                and (self.feishu_chat_id or "")
            )
            or (self.telegram_bot_token and self.telegram_chat_id)
            or (self.email_sender and self.email_password)
            or (self.pushover_user_key and self.pushover_api_token)
            or _has_ntfy_topic_endpoint(self.ntfy_url)
            or (
                self.gotify_url
                and (self.gotify_token or "").strip()
                and _has_gotify_base_url(self.gotify_url)
            )
            or self.pushplus_token
            or self.serverchan3_sendkey
            or self.custom_webhook_urls
            or self.astrbot_url
            or (self.discord_bot_token and self.discord_main_channel_id)
            or self.discord_webhook_url
            or self.slack_webhook_url
            or (self.slack_bot_token and self.slack_channel_id)
        )

        if not has_notification:
            issues.append(ConfigIssue(
                severity="warning",
                message="未配置通知渠道，将不发送推送通知",
                field="WECHAT_WEBHOOK_URL",
            ))

        has_telegram_token = bool((self.telegram_bot_token or "").strip())
        has_telegram_chat_id = bool((self.telegram_chat_id or "").strip())
        if has_telegram_token != has_telegram_chat_id:
            issues.append(ConfigIssue(
                severity="error",
                message="Telegram 通知配置不完整：TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID 必须同时配置。",
                field="TELEGRAM_CHAT_ID" if has_telegram_token else "TELEGRAM_BOT_TOKEN",
            ))

        has_email_sender = bool((self.email_sender or "").strip())
        has_email_password = bool((self.email_password or "").strip())
        if has_email_sender != has_email_password:
            issues.append(ConfigIssue(
                severity="error",
                message="邮件通知配置不完整：EMAIL_SENDER 和 EMAIL_PASSWORD 必须同时配置。",
                field="EMAIL_PASSWORD" if has_email_sender else "EMAIL_SENDER",
            ))

        def _warn_if_webhook_url_invalid(field: str, value: Optional[str]) -> None:
            raw_url = (value or "").strip()
            if not raw_url:
                return
            parsed = urlparse(raw_url)
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
                return
            issues.append(ConfigIssue(
                severity="warning",
                message=f"{field} 看起来不是有效 URL，请确认是否以 http:// 或 https:// 开头。",
                field=field,
            ))

        for field, value in (
            ("WECHAT_WEBHOOK_URL", self.wechat_webhook_url),
            ("FEISHU_WEBHOOK_URL", self.feishu_webhook_url),
            ("DINGTALK_WEBHOOK_URL", self.dingtalk_webhook_url),
            ("DISCORD_WEBHOOK_URL", self.discord_webhook_url),
            ("SLACK_WEBHOOK_URL", self.slack_webhook_url),
            ("ASTRBOT_URL", self.astrbot_url),
        ):
            _warn_if_webhook_url_invalid(field, value)

        for custom_url in self.custom_webhook_urls:
            _warn_if_webhook_url_invalid("CUSTOM_WEBHOOK_URLS", custom_url)

        if self.ntfy_url and not _has_ntfy_topic_endpoint(self.ntfy_url):
            issues.append(ConfigIssue(
                severity="error",
                message="NTFY_URL 必须包含 topic path，例如 https://ntfy.sh/my-topic",
                field="NTFY_URL",
            ))

        if self.gotify_url and not _has_gotify_base_url(self.gotify_url):
            issues.append(ConfigIssue(
                severity="error",
                message="GOTIFY_URL 必须是 Gotify server base URL，不包含 /message，例如 https://gotify.example",
                field="GOTIFY_URL",
            ))

        if (
            self.gotify_url
            and _has_gotify_base_url(self.gotify_url)
            and not (self.gotify_token or "").strip()
        ):
            issues.append(ConfigIssue(
                severity="warning",
                message="已配置 GOTIFY_URL，但缺少 GOTIFY_TOKEN，Gotify 渠道不会启用",
                field="GOTIFY_TOKEN",
            ))

        if self.notification_quiet_hours:
            try:
                parse_notification_quiet_hours(self.notification_quiet_hours)
            except ValueError as exc:
                issues.append(ConfigIssue(
                    severity="error",
                    message=f"通知静默时段配置无效：{exc}",
                    field="NOTIFICATION_QUIET_HOURS",
                ))

        if self.notification_timezone:
            try:
                validate_notification_timezone(self.notification_timezone)
            except ValueError as exc:
                issues.append(ConfigIssue(
                    severity="error",
                    message=f"通知时区配置无效：{exc}",
                    field="NOTIFICATION_TIMEZONE",
                ))

        if self.notification_min_severity and not is_supported_notification_severity(self.notification_min_severity):
            issues.append(ConfigIssue(
                severity="error",
                message=(
                    "通知最低级别配置无效，允许值："
                    f"{', '.join(NOTIFICATION_SEVERITIES)}"
                ),
                field="NOTIFICATION_MIN_SEVERITY",
            ))

        if self.notification_daily_digest_enabled:
            issues.append(ConfigIssue(
                severity="warning",
                message=(
                    "NOTIFICATION_DAILY_DIGEST_ENABLED 当前为预留配置；"
                    "P4 不会发送每日摘要或持久化摘要内容。"
                ),
                field="NOTIFICATION_DAILY_DIGEST_ENABLED",
            ))

        has_feishu_app_id = bool((self.feishu_app_id or "").strip())
        has_feishu_app_secret = bool((self.feishu_app_secret or "").strip())
        has_feishu_app_credentials_complete = has_feishu_app_id and has_feishu_app_secret
        has_feishu_app_credentials = has_feishu_app_id or has_feishu_app_secret
        has_feishu_doc_token = bool((self.feishu_folder_token or "").strip())
        has_feishu_full_cloud_doc_credentials = (
            has_feishu_app_credentials_complete
            and has_feishu_doc_token
        )
        has_feishu_stream_route = bool(self.feishu_stream_enabled and has_feishu_app_credentials_complete)
        has_feishu_app_notification_route = is_feishu_app_bot_configured(self)
        if (
            has_feishu_app_credentials
            and not has_feishu_full_cloud_doc_credentials
            and not is_feishu_static_configured(self)
            and not has_feishu_stream_route
            and not has_feishu_app_notification_route
        ):
            suggestions = []
            if has_feishu_app_credentials_complete:
                suggestions.append("配置 FEISHU_CHAT_ID 开启 App Bot 主动推送")
                suggestions.append("开启 FEISHU_STREAM_ENABLED 使用应用机器人事件订阅")
            else:
                suggestions.append("补齐 FEISHU_APP_ID / FEISHU_APP_SECRET 后配置 FEISHU_CHAT_ID 开启 App Bot 主动推送")
            suggestions.append("配置 FEISHU_WEBHOOK_URL 使用自定义机器人 Webhook 推送")
            issues.append(ConfigIssue(
                severity="warning",
                message="仅配置 FEISHU_APP_ID / FEISHU_APP_SECRET 不会开启飞书静态通知。"
                        + " 请选择以下方式之一："
                        + "；".join(suggestions) + "。",
                field="FEISHU_CHAT_ID",
            ))

        # --- Deprecated field migration hints ---
        if os.getenv("OPENAI_VISION_MODEL"):
            issues.append(ConfigIssue(
                severity="info",
                message=(
                    "OPENAI_VISION_MODEL 已废弃，请改用 VISION_MODEL。"
                    "当前值已自动迁移，建议更新配置文件以消除此提示。"
                ),
                field="OPENAI_VISION_MODEL",
            ))

        # --- Vision key availability ---
        # Only warn when user explicitly set VISION_MODEL (or OPENAI_VISION_MODEL alias).
        # Skipped when vision_model is empty (Vision not intentionally configured).
        if self.vision_model:
            # Maps provider prefix → the corresponding key list tracked by Config.
            # vertex_ai shares gemini keys; other LiteLLM-native providers are not
            # in this map (their keys come from env vars, which we cannot inspect here).
            _VISION_KEY_MAP = {
                "gemini": self.gemini_api_keys,
                "vertex_ai": self.gemini_api_keys,
                "anthropic": self.anthropic_api_keys,
                "openai": self.openai_api_keys,
                "deepseek": self.deepseek_api_keys,
            }
            # Derive the primary model's provider prefix so that its key is also
            # checked even when the provider is absent from VISION_PROVIDER_PRIORITY.
            _primary_prefix = (
                self.vision_model.split("/")[0]
                if "/" in self.vision_model
                else "openai"
            )
            _priority_providers = [
                p.strip().lower()
                for p in self.vision_provider_priority.split(",")
                if p.strip()
            ]
            # Union: fallback providers + primary model's own provider
            _all_providers = {_primary_prefix} | set(_priority_providers)

            # Align with get_api_keys_for_model: keys must be non-empty and len >= 8
            _has_any_key = any(
                any(k and len(k) >= 8 for k in (_VISION_KEY_MAP.get(p) or []))
                for p in _all_providers
                if p in _VISION_KEY_MAP
            )
            if not _has_any_key:
                _checked = sorted(_all_providers & _VISION_KEY_MAP.keys())
                issues.append(ConfigIssue(
                    severity="warning",
                    message=(
                        "VISION_MODEL 已配置，但未找到可用的 Vision API Key "
                        f"（已检查：{', '.join(_checked)}）。"
                        "图片股票代码提取功能将不可用，请配置对应的 API Key。"
                    ),
                    field="VISION_MODEL",
                ))

        return issues

    def validate(self) -> List[str]:
        """Return validation messages as plain strings (backward-compatible).

        Internally delegates to validate_structured().  Callers that only need
        the human-readable strings can continue to use this method unchanged.

        Returns:
            List of message strings, one per ConfigIssue.
        """
        return [issue.message for issue in self.validate_structured()]
    
    def get_db_url(self) -> str:
        """
        获取 SQLAlchemy 数据库连接 URL
        
        自动创建数据库目录（如果不存在）
        """
        db_path = Path(self.database_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path.absolute()}"


# === 便捷的配置访问函数 ===
def get_config() -> Config:
    """获取全局配置实例的快捷方式"""
    return Config.get_instance()


def get_primary_user_id() -> str:
    """Return PRIMARY_USER_ID (default ``local``): the owner of legacy data and of header-less entry points."""
    value = getattr(get_config(), "primary_user_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_PRIMARY_USER_ID


# ============================================================
# Shared LLM helpers (used by both analyzer and agent/llm_adapter)
# ============================================================

def get_api_keys_for_model(model: str, config: Config) -> List[str]:
    """Return explicitly managed API keys for a litellm model (legacy path only).

    When llm_model_list is populated (channels / YAML), the Router handles key
    selection, so this function is not needed.  Kept for backward compat when
    no Router is built and a direct litellm.completion() call is needed.
    """
    provider = _get_litellm_provider(model)
    if provider in {"gemini", "vertex_ai"}:
        return [k for k in config.gemini_api_keys if k and len(k) >= 8]
    if provider == "anthropic":
        return [k for k in config.anthropic_api_keys if k and len(k) >= 8]
    if provider == "deepseek":
        return [k for k in config.deepseek_api_keys if k and len(k) >= 8]
    if provider == "openai":
        return [k for k in config.openai_api_keys if k and len(k) >= 8]
    # Other LiteLLM-native providers – API key resolved from env vars
    return []


def extra_litellm_params(model: str, config: Config) -> Dict[str, Any]:
    """Build extra litellm params for a model (legacy path only).

    When llm_model_list is populated, the Router already carries api_base
    and headers per-deployment, so this is not called.
    """
    params: Dict[str, Any] = {}
    # deepseek/ provider: litellm auto-resolves api_base, no manual override needed
    if model.startswith("deepseek/"):
        return params
    if model.startswith("openai/") or "/" not in model:
        if config.openai_base_url:
            params["api_base"] = config.openai_base_url
        if config.openai_base_url and "aihubmix.com" in config.openai_base_url:
            params["extra_headers"] = {"APP-Code": "GPIJ3886"}
    return params


if __name__ == "__main__":
    # 测试配置加载
    config = get_config()
    print("=== 配置加载测试 ===")
    print(f"自选股列表: {config.stock_list}")
    print(f"数据库路径: {config.database_path}")
    print(f"最大并发数: {config.max_workers}")
    print(f"调试模式: {config.debug}")
    
    # 验证配置
    warnings = config.validate()
    if warnings:
        print("\n配置验证结果:")
        for w in warnings:
            print(f"  - {w}")
