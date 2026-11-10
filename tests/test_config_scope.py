# -*- coding: utf-8 -*-
"""Config key scope (docs/design/multi-user.md 3.2 / 3.3): ``user`` vs ``system``."""

import pytest

from api.v1.schemas.system_config import SystemConfigFieldSchema
from src.config import Config
from src.core.config_registry import (
    CONFIG_SCOPES,
    SCOPE_SYSTEM,
    SCOPE_USER,
    _is_sensitive_key,
    build_schema_response,
    get_config_scope,
    get_field_definition,
    get_registered_field_keys,
    infer_config_scope,
)

USER_KEYS = [
    # model
    "LLM_CHANNELS", "LITELLM_MODEL", "AGENT_LITELLM_MODEL", "LITELLM_LIGHT_MODEL", "LITELLM_FALLBACK_MODELS",
    "OPENAI_API_KEY", "OPENAI_API_KEYS", "OPENAI_BASE_URL", "GEMINI_API_KEYS", "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY", "AIHUBMIX_KEY", "LLM_TEMPERATURE", "LLM_REASONING_EFFORT",
    # search
    "TAVILY_API_KEYS", "SERPAPI_API_KEYS", "BRAVE_API_KEYS", "BOCHA_API_KEYS", "ANSPIRE_API_KEYS",
    "MINIMAX_API_KEYS", "SEARXNG_BASE_URLS",
    # data-source credentials
    "TUSHARE_TOKEN", "TICKFLOW_API_KEY", "BIYING_LICENCE", "MX_APIKEY",
    # agent
    "AGENT_MODE", "AGENT_SKILLS", "AGENT_SKILL_ROUTING", "AGENT_CONTEXT_COMPRESSION_PROFILE",
    "AGENT_MEMORY_ENABLED", "HINDSIGHT_BASE_URL", "HINDSIGHT_API_KEY",
    # notification channel credentials (not bot apps)
    "FEISHU_WEBHOOK_URL", "DINGTALK_WEBHOOK_URL", "WECHAT_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "EMAIL_SENDER", "EMAIL_PASSWORD", "EMAIL_RECEIVERS", "PUSHPLUS_TOKEN", "SERVERCHAN3_SENDKEY",
    "CUSTOM_WEBHOOK_URLS", "NTFY_URL", "GOTIFY_URL", "DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL",
    # push switches / times, position rules, report preferences, watchlist
    "DAILY_PLAN_ENABLED", "PREOPEN_PUSH_ENABLED", "PREOPEN_PUSH_TIME", "POSITION_STOP_LOSS_PCT",
    "POSITION_MAX_TOTAL_PCT", "POSITION_MAX_HOLDINGS", "POSITION_MAX_SINGLE_PCT", "POSITION_STOP_MODE",
    "REPORT_TYPE", "REPORT_LANGUAGE", "REPORT_SUMMARY_ONLY", "MARKET_REVIEW_COLOR_SCHEME", "STOCK_LIST",
    # unregistered keys that are still per-user
    "FINNHUB_API_KEY", "ALPHAVANTAGE_API_KEY", "LONGBRIDGE_APP_KEY", "OLLAMA_API_BASE",
    "LLM_MYPROXY_API_KEY", "LLM_MYPROXY_BASE_URL", "LLM_MYPROXY_MODELS", "LLM_MYPROXY_PROTOCOL",
    "LLM_MYPROXY_API_SURFACE", "LLM_MYPROXY_ENABLED", "LLM_MYPROXY_EXTRA_HEADERS", "LLM_AI_HUB_API_KEYS",
]

SYSTEM_KEYS = [
    # deploy / runtime
    "WEBUI_HOST", "WEBUI_PORT", "LOG_LEVEL", "LOG_DIR", "DEBUG", "HTTP_PROXY", "MAX_WORKERS", "DATABASE_PATH",
    "LLM_USAGE_HMAC_SECRET", "LLM_USAGE_HMAC_KEY_VERSION", "LLM_PROMPT_CACHE_HINTS_ENABLED",
    "DSA_RUNTIME_SCHEDULER_TIMEOUT_SECONDS", "SCHEDULE_ENABLED", "SCHEDULE_TIME", "RUN_IMMEDIATELY",
    # auth
    "AUTH_MODE", "PUBLIC_ORIGIN", "USER_CENTER_URL", "USER_CENTER_REQUIRED_ROLE", "TRUST_X_FORWARDED_FOR",
    "ADMIN_AUTH_ENABLED", "PRIMARY_USER_ID", "USER_SECRET_KEY",
    # bot apps and stream switches
    "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_STREAM_ENABLED", "DINGTALK_APP_KEY", "DINGTALK_APP_SECRET",
    "DINGTALK_STREAM_ENABLED", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN",
    # local daemons
    "FUTU_OPEND_HOST", "FUTU_OPEND_PORT", "PYTDX_HOST", "PYTDX_PORT", "PYTDX_SERVERS",
    # shared jobs
    "BOARD_PIPELINE_ENABLED", "BOARD_ANOMALY_THRESHOLDS", "NEWS_POLL_INTERVAL_MINUTES", "NEWS_INTEL_AUTO_FETCH_ENABLED",
    "MARKET_SNAPSHOT_RETENTION_DAYS", "MARKET_REVIEW_ENABLED", "MARKET_REVIEW_REGION", "DAILY_MARKET_CONTEXT_ENABLED",
    "BACKTEST_ENABLED", "SCREENING_ENABLED",
    # data semantics
    "TICKFLOW_PRIORITY", "TICKFLOW_KLINE_ADJUST", "REALTIME_SOURCE_PRIORITY", "FUTU_HK_REALTIME_SOURCE_PRIORITY",
    "TUSHARE_PRIORITY", "EFINANCE_PRIORITY", "ENABLE_REALTIME_QUOTE", "ENABLE_CHIP_DISTRIBUTION",
    "ENABLE_REALTIME_TECHNICAL_INDICATORS",
    # server-side paths / local CLI backends
    "LITELLM_CONFIG", "GENERATION_BACKEND", "AGENT_BACKEND", "AGENT_GENERATION_BACKEND", "AGENT_SKILL_DIR",
    "OPENCODE_CLI_MODEL", "REPORT_TEMPLATES_DIR", "WEBHOOK_VERIFY_SSL",
    # unknown / unregistered
    "SOME_UNKNOWN_KEY", "LLM_MAX_TOKENS", "LLM_TIMEOUT_SEC",
]


@pytest.mark.parametrize("key", USER_KEYS)
def test_user_scope_keys(key):
    assert get_config_scope(key) == SCOPE_USER
    assert get_field_definition(key)["scope"] == SCOPE_USER


@pytest.mark.parametrize("key", SYSTEM_KEYS)
def test_system_scope_keys(key):
    assert get_config_scope(key) == SCOPE_SYSTEM
    assert get_field_definition(key)["scope"] == SCOPE_SYSTEM


def test_every_registered_field_has_a_valid_scope():
    keys = get_registered_field_keys()
    assert keys
    for key in keys:
        definition = get_field_definition(key)
        assert definition["scope"] in CONFIG_SCOPES, key
        assert definition["scope"] == infer_config_scope(key), key


def test_unregistered_keys_default_to_system_and_scope_is_case_insensitive():
    assert infer_config_scope("TOTALLY_NEW_KEY") == SCOPE_SYSTEM
    assert infer_config_scope("") == SCOPE_SYSTEM
    assert infer_config_scope("report_language") == SCOPE_USER
    assert get_field_definition("TOTALLY_NEW_KEY")["scope"] == SCOPE_SYSTEM


def test_registered_llm_keys_are_not_mistaken_for_dynamic_channel_keys():
    # Registered keys follow their explicit classification, not the LLM_<NAME>_* pattern.
    for key in ("LLM_PROMPT_CACHE_TELEMETRY_ENABLED", "LLM_PROMPT_CACHE_HINTS_ENABLED", "LLM_USAGE_HMAC_KEY_VERSION"):
        assert get_config_scope(key) == SCOPE_SYSTEM


def test_schema_response_exposes_scope_for_every_field():
    schema = build_schema_response()
    fields = [field for category in schema["categories"] for field in category["fields"]]

    assert fields
    assert all(field["scope"] in CONFIG_SCOPES for field in fields)
    by_key = {field["key"]: field for field in fields}
    assert by_key["OPENAI_API_KEY"]["scope"] == SCOPE_USER
    assert by_key["MAX_WORKERS"]["scope"] == SCOPE_SYSTEM
    # The API model keeps the attribute (and defaults to system for older payloads).
    assert SystemConfigFieldSchema.model_validate(by_key["REPORT_LANGUAGE"]).scope == SCOPE_USER
    legacy_payload = {k: v for k, v in by_key["MAX_WORKERS"].items() if k != "scope"}
    assert SystemConfigFieldSchema.model_validate(legacy_payload).scope == SCOPE_SYSTEM


# Secrets read by Config that are deliberately process-level: bot app credentials / stream
# secrets, webhook-callback verification, the Feishu doc exporter token, and the system's own paid
# third-party data keys (blanked in every non-primary user view, see ``_BLANKED_SYSTEM_KEYS``).
SYSTEM_SECRETS_READ_BY_CONFIG = {
    "ASHARE_API_KEY", "DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DISCORD_BOT_TOKEN", "DISCORD_INTERACTIONS_PUBLIC_KEY",
    "FEISHU_APP_SECRET", "FEISHU_ENCRYPT_KEY", "FEISHU_FOLDER_TOKEN", "FEISHU_VERIFICATION_TOKEN",
    "SLACK_BOT_TOKEN", "SOCIAL_SENTIMENT_API_KEY", "TELEGRAM_WEBHOOK_SECRET", "WECOM_ENCODING_AES_KEY",
    "WECOM_TOKEN",
}
MODEL_PREFIXES = ("GEMINI_", "OPENAI_", "ANTHROPIC_", "LITELLM_", "LLM_", "DEEPSEEK_", "AIHUBMIX_", "ANSPIRE_",
                  "TAVILY_", "SERPAPI_", "BRAVE_", "BOCHA_", "MINIMAX_", "SEARXNG_")
SYSTEM_MODEL_PREFIXED_KEYS_READ_BY_CONFIG = {
    "GEMINI_MAX_RETRIES", "GEMINI_REQUEST_DELAY", "GEMINI_RETRY_DELAY", "LITELLM_CONFIG",
    "LLM_PROMPT_CACHE_DIAGNOSTICS_LEVEL", "LLM_PROMPT_CACHE_HINTS_ENABLED", "LLM_PROMPT_CACHE_TELEMETRY_ENABLED",
    "SEARXNG_TIMEOUT_SECONDS",
}


class _RecordingEnv(dict):
    """Env mapping that remembers every key the config loader looks at."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)

    def __iter__(self):
        return iter(list(super().keys()))


def test_every_credential_or_model_key_the_config_loader_reads_is_classified_deliberately():
    """Drift guard: a new secret / model / search key read by ``Config`` must be classified.

    A key that silently falls into the ``system`` default would be inherited by every user
    (the admin's credential used on behalf of someone else), so each sensitive or
    model-like key read by the loader must be ``user`` scope or be on the audited
    process-level allow-lists above.
    """
    env = _RecordingEnv({"LLM_CHANNELS": "probe", "STOCK_LIST": "", "SCHEDULE_ENABLED": "false"})
    Config._load_from_env(env, user_override_keys=frozenset({"STOCK_LIST"}))
    assert len(env.read) > 100

    unclassified_secrets = sorted(
        key for key in env.read
        if _is_sensitive_key(key)
        and get_config_scope(key) == SCOPE_SYSTEM
        and key not in SYSTEM_SECRETS_READ_BY_CONFIG
    )
    unclassified_model_keys = sorted(
        key for key in env.read
        if key.startswith(MODEL_PREFIXES)
        and get_config_scope(key) == SCOPE_SYSTEM
        and key not in SYSTEM_MODEL_PREFIXED_KEYS_READ_BY_CONFIG
    )
    assert unclassified_secrets == []
    assert unclassified_model_keys == []
    # The dynamic channel keys are reached through the probe channel and are per-user.
    assert {"LLM_PROBE_API_KEY", "LLM_PROBE_BASE_URL", "LLM_PROBE_MODELS"} <= env.read
