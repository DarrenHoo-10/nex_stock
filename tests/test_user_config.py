# -*- coding: utf-8 -*-
"""Per-user settings and the ``get_user_config`` view (docs/design/multi-user.md section 3).

Nothing here calls a model or a data source: the view is built from environment mappings.
"""

import os
import threading

import pytest

from src.config import Config, get_config
from src.core.config_registry import SCOPE_USER, get_config_scope, get_registered_field_keys
from src.repositories.user_repo import UserRepository
from src.services.user_config_service import (
    DATA_SOURCE_CREDENTIAL_KEYS,
    get_user_config,
    has_usable_model,
    inherits_from_system,
    invalidate_user_config,
    user_model_key_configured,
)
from src.services.user_settings_service import UserSettingError, UserSettingsService, is_secret_setting_key
from src.storage import DatabaseManager
from src.user_secrets import UserSecretStore

PRIMARY = "admin-uid"

SYSTEM_ENV = {
    # model keys / channels (must never reach other users)
    "OPENAI_API_KEY": "sk-system-openai",
    "OPENAI_BASE_URL": "https://system.example.test/v1",
    "GEMINI_API_KEYS": "g-system-1,g-system-2",
    "ANTHROPIC_API_KEY": "sk-system-anthropic",
    "DEEPSEEK_API_KEY": "sk-system-deepseek",
    "AIHUBMIX_KEY": "sk-system-aihubmix",
    "LLM_CHANNELS": "sysch",
    "LLM_SYSCH_PROTOCOL": "openai",
    "LLM_SYSCH_BASE_URL": "https://sysch.example.test/v1",
    "LLM_SYSCH_API_KEY": "sk-system-channel",
    "LLM_SYSCH_MODELS": "sys-model",
    "LITELLM_MODEL": "openai/sys-model",
    "AGENT_LITELLM_MODEL": "openai/sys-model",
    # search keys
    "TAVILY_API_KEYS": "tvly-system",
    "SERPAPI_API_KEYS": "serp-system",
    "BRAVE_API_KEYS": "brave-system",
    "SEARXNG_BASE_URLS": "https://searx.system.test",
    # data-source credentials (inherited) and data semantics (system-wide)
    "TUSHARE_TOKEN": "ts-system",
    "TICKFLOW_API_KEY": "tf-system",
    "BIYING_LICENCE": "by-system",
    "MX_APIKEY": "mx-system",
    "TICKFLOW_KLINE_ADJUST": "forward",
    "REALTIME_SOURCE_PRIORITY": "tencent,efinance",
    # notification channels (never inherited) and preferences (inherited)
    "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/hook/system",
    "TELEGRAM_BOT_TOKEN": "tg-system",
    "TELEGRAM_CHAT_ID": "-100123",
    "EMAIL_SENDER": "ops@system.test",
    "EMAIL_PASSWORD": "mail-system",
    "EMAIL_RECEIVERS": "ops@system.test",
    "CUSTOM_WEBHOOK_URLS": "https://hooks.system.test/x",
    "REPORT_LANGUAGE": "en",
    "REPORT_SUMMARY_ONLY": "true",
    "NOTIFICATION_MIN_SEVERITY": "warning",
    "POSITION_STOP_LOSS_PCT": "7",
    "POSITION_MAX_HOLDINGS": "4",
    "DAILY_PLAN_ENABLED": "true",
    "PREOPEN_PUSH_TIME": "08:30",
    # watchlist / memory bank / agent
    "STOCK_LIST": "600519,000001",
    "HINDSIGHT_BASE_URL": "https://hindsight.system.test",
    "HINDSIGHT_API_KEY": "hs-system",
    "HINDSIGHT_BANK_ID": "system-bank",
    "AGENT_MAX_STEPS": "9",
    # process-level
    "MAX_WORKERS": "5",
    "WEBUI_PORT": "9123",
    "LITELLM_CONFIG": "/etc/litellm/system.yaml",
    "GENERATION_BACKEND": "codex_cli",
    "AGENT_BACKEND": "codex_app_server",
    "FEISHU_APP_ID": "cli_system_app",
    "FEISHU_APP_SECRET": "feishu-app-secret",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stock.db"))
    monkeypatch.setenv("PRIMARY_USER_ID", PRIMARY)
    monkeypatch.delenv("USER_SECRET_KEY", raising=False)
    # Isolate from the developer's own environment: drop every per-user key, then set ours.
    for key in list(os.environ):
        if get_config_scope(key) == SCOPE_USER:
            monkeypatch.delenv(key, raising=False)
    for key, value in SYSTEM_ENV.items():
        monkeypatch.setenv(key, value)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    DatabaseManager.get_instance()
    invalidate_user_config()
    yield tmp_path
    invalidate_user_config()
    DatabaseManager.reset_instance()
    Config.reset_instance()


# --------------------------------------------------------------------------
# The key requirement: no inherited model access
# --------------------------------------------------------------------------


def test_system_config_has_models_but_a_user_without_settings_has_none(env):
    system = get_config()
    assert system.llm_model_list  # the system .env has keys and channels
    assert has_usable_model(system)

    config = get_user_config("alice")

    assert config.llm_model_list == []
    assert config.litellm_model == ""
    assert config.litellm_fallback_models == []
    assert config.agent_litellm_model == ""
    assert config.openai_api_keys == []
    assert config.gemini_api_keys == []
    assert config.anthropic_api_keys == []
    assert config.deepseek_api_keys == []
    assert config.llm_channels == []
    assert config.litellm_config_path is None
    assert not has_usable_model(config)
    assert user_model_key_configured("alice") is False
    assert user_model_key_configured(PRIMARY) is True


def test_search_keys_and_endpoints_are_not_inherited(env):
    config = get_user_config("alice")

    assert config.tavily_api_keys == []
    assert config.serpapi_keys == []
    assert config.brave_api_keys == []
    assert config.searxng_base_urls == []
    assert config.searxng_public_instances_enabled is False
    assert get_config().tavily_api_keys == ["tvly-system"]


def test_data_source_credentials_inherit_but_data_semantics_follow_the_system(env):
    config = get_user_config("alice")
    system = get_config()

    assert config.tushare_token == "ts-system"
    assert config.tickflow_api_key == "tf-system"
    assert config.biying_licence == "by-system"
    assert config.mx_apikey == "mx-system"
    assert config.tickflow_kline_adjust == system.tickflow_kline_adjust == "forward"
    assert config.realtime_source_priority == system.realtime_source_priority == "tencent,efinance"


def test_realtime_priority_ignores_the_users_own_tokens(env, monkeypatch):
    monkeypatch.delenv("REALTIME_SOURCE_PRIORITY")
    Config.reset_instance()
    system = get_config()
    assert system.realtime_source_priority.startswith("tushare")  # auto-injected from the system token

    UserSettingsService().set_setting("alice", "TUSHARE_TOKEN", "ts-alice")
    UserSettingsService().set_setting("alice", "BIYING_LICENCE", "by-alice")

    config = get_user_config("alice")
    assert config.tushare_token == "ts-alice"
    assert config.realtime_source_priority == system.realtime_source_priority


def test_notification_channels_do_not_inherit_but_preferences_do(env):
    config = get_user_config("alice")

    assert config.feishu_webhook_url is None
    assert config.telegram_bot_token is None
    assert config.telegram_chat_id is None
    assert config.email_sender is None
    assert config.email_password is None
    assert not config.email_receivers
    assert not config.custom_webhook_urls
    # Push switches, report preferences and position rules fall back to the system default.
    assert config.report_language == "en"
    assert config.report_summary_only is True
    assert config.notification_min_severity == "warning"
    assert config.position_stop_loss_pct == 7.0
    assert config.position_max_holdings == 4
    assert config.daily_plan_enabled is True
    assert config.preopen_push_time == "08:30"
    assert config.agent_max_steps == 9


def test_watchlist_and_memory_bank_are_not_inherited(env):
    system = get_config()
    assert system.stock_list == ["600519", "000001"]
    assert system.hindsight_bank_id == "system-bank"

    config = get_user_config("alice")

    assert config.stock_list == []
    assert config.hindsight_bank_id == ""
    # The (non-secret) memory service address is a preference; its key is a secret.
    assert config.hindsight_base_url == "https://hindsight.system.test"
    assert config.hindsight_api_key is None


def test_system_scope_keys_always_come_from_the_system(env):
    config = get_user_config("alice")

    assert config.max_workers == 5
    assert config.webui_port == 9123
    assert config.feishu_app_id == "cli_system_app"


def test_local_cli_backends_are_forced_off_for_non_primary_users(env):
    system = get_config()
    assert system.generation_backend == "codex_cli"
    assert system.agent_backend == "codex_app_server"

    config = get_user_config("alice")

    assert config.generation_backend == "litellm"
    assert config.generation_fallback_backend == "litellm"
    assert config.agent_backend == "litellm"
    assert config.agent_generation_backend in ("auto", "litellm")
    assert get_config().generation_backend == "codex_cli"  # primary unchanged


def test_primary_user_gets_the_global_config_unchanged(env):
    assert get_user_config(PRIMARY) is get_config()


def test_user_id_is_required(env):
    with pytest.raises(ValueError):
        get_user_config("  ")


# --------------------------------------------------------------------------
# The user's own settings
# --------------------------------------------------------------------------


def test_user_settings_overlay_the_view(env):
    service = UserSettingsService()
    service.set_settings("alice", {
        "OPENAI_API_KEY": "sk-alice",
        "OPENAI_MODEL": "gpt-alice",
        "TAVILY_API_KEYS": "tvly-alice",
        "STOCK_LIST": "300750, 002594",
        "REPORT_LANGUAGE": "ko",
        "POSITION_MAX_HOLDINGS": "2",
        "TUSHARE_TOKEN": "ts-alice",
        "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/hook/alice",
    })

    config = get_user_config("alice")

    assert config.openai_api_keys == ["sk-alice"]
    assert config.llm_model_list
    assert has_usable_model(config) and user_model_key_configured("alice")
    assert "sk-system-openai" not in repr(config.llm_model_list)
    assert config.tavily_api_keys == ["tvly-alice"]
    assert config.stock_list == ["300750", "002594"]
    assert config.report_language == "ko"
    assert config.position_max_holdings == 2
    assert config.tushare_token == "ts-alice"
    assert config.feishu_webhook_url == "https://open.feishu.cn/hook/alice"
    # Another user is unaffected.
    other = get_user_config("bob")
    assert other.openai_api_keys == [] and other.stock_list == [] and other.tushare_token == "ts-system"


def test_user_defined_model_channels(env):
    service = UserSettingsService()
    service.set_settings("alice", {
        "LLM_CHANNELS": "mine",
        "LLM_MINE_PROTOCOL": "openai",
        "LLM_MINE_BASE_URL": "https://api.alice.example/v1",
        "LLM_MINE_API_KEY": "sk-alice-channel",
        "LLM_MINE_MODELS": "alice-model",
    })

    config = get_user_config("alice")

    assert [c["name"] for c in config.llm_channels] == ["mine"]
    assert config.litellm_model == "openai/alice-model"
    assert "sys-model" not in repr(config.llm_model_list)
    assert "sk-system-channel" not in repr(config.llm_model_list)
    assert has_usable_model(config)


def test_deleting_a_setting_falls_back(env):
    service = UserSettingsService()
    service.set_setting("alice", "REPORT_LANGUAGE", "ko")
    service.set_setting("alice", "OPENAI_API_KEY", "sk-alice")
    assert get_user_config("alice").report_language == "ko"

    assert service.delete_setting("alice", "REPORT_LANGUAGE") is True
    assert service.delete_setting("alice", "OPENAI_API_KEY") is True
    assert service.delete_setting("alice", "OPENAI_API_KEY") is False

    config = get_user_config("alice")
    assert config.report_language == "en"  # back to the system default
    assert config.openai_api_keys == []  # secrets never fall back to a system key


def test_view_is_read_only(env):
    config = get_user_config("alice")

    with pytest.raises(AttributeError):
        config.report_language = "zh"
    with pytest.raises(AttributeError):
        config.refresh_stock_list()
    get_config().report_language = "en"  # the global config stays mutable


def test_building_a_view_does_not_touch_os_environ(env):
    UserSettingsService().set_settings("alice", {"OPENAI_API_KEY": "sk-alice", "REPORT_LANGUAGE": "ko"})
    before = dict(os.environ)

    get_user_config("alice")
    get_user_config("bob")

    assert dict(os.environ) == before
    assert os.environ["OPENAI_API_KEY"] == "sk-system-openai"


def test_concurrent_views_are_isolated(env):
    service = UserSettingsService()
    users = [f"user{i}" for i in range(6)]
    for index, user in enumerate(users):
        service.set_settings(user, {"OPENAI_API_KEY": f"sk-{user}", "REPORT_LANGUAGE": ["zh", "en", "ko"][index % 3]})
    expected_language = {user: ["zh", "en", "ko"][index % 3] for index, user in enumerate(users)}
    barrier = threading.Barrier(len(users))
    errors = []

    def worker(user):
        try:
            barrier.wait(timeout=10)
            for _ in range(5):
                invalidate_user_config(user)
                config = get_user_config(user)
                assert config.openai_api_keys == [f"sk-{user}"], user
                assert config.report_language == expected_language[user], user
                assert config.tavily_api_keys == []
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(user,)) for user in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    assert os.environ["OPENAI_API_KEY"] == "sk-system-openai"


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


def test_view_is_cached_per_user_and_invalidated_by_that_users_writes(env):
    service = UserSettingsService()
    first = get_user_config("alice")
    bob = get_user_config("bob")

    assert get_user_config("alice") is first

    service.set_setting("alice", "REPORT_LANGUAGE", "ko")
    second = get_user_config("alice")
    assert second is not first and second.report_language == "ko"
    assert get_user_config("bob") is bob  # other users keep their cached view

    service.delete_setting("alice", "REPORT_LANGUAGE")
    assert get_user_config("alice").report_language == "en"


def test_view_notices_changes_made_by_another_process(env):
    first = get_user_config("alice")

    # A write that bypasses this process's service (another worker / script).
    UserRepository().upsert_setting("alice", "REPORT_LANGUAGE", "ko", False)

    assert get_user_config("alice") is not first
    assert get_user_config("alice").report_language == "ko"


def test_view_is_rebuilt_after_a_system_config_reload(env, monkeypatch):
    first = get_user_config("alice")
    assert first.max_workers == 5

    monkeypatch.setenv("MAX_WORKERS", "2")
    Config.reset_instance()

    reloaded = get_user_config("alice")
    assert reloaded is not first and reloaded.max_workers == 2


def test_destroyed_data_key_is_noticed_by_the_cache(env):
    service = UserSettingsService()
    service.set_setting("alice", "OPENAI_API_KEY", "sk-alice")
    assert get_user_config("alice").openai_api_keys == ["sk-alice"]

    UserSecretStore().destroy_user_key("alice")

    assert get_user_config("alice").openai_api_keys == []


# --------------------------------------------------------------------------
# Validation and scope enforcement
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", [
    "AUTH_MODE", "USER_CENTER_URL", "MAX_WORKERS", "WEBUI_PORT", "FEISHU_APP_SECRET", "FEISHU_STREAM_ENABLED",
    "FUTU_OPEND_HOST", "PYTDX_HOST", "TICKFLOW_PRIORITY", "TICKFLOW_KLINE_ADJUST", "ENABLE_REALTIME_QUOTE",
    "ENABLE_CHIP_DISTRIBUTION", "LITELLM_CONFIG", "GENERATION_BACKEND", "AGENT_BACKEND",
    "BOARD_PIPELINE_ENABLED", "NEWS_POLL_INTERVAL_MINUTES", "MARKET_REVIEW_ENABLED", "DATABASE_PATH",
    "PRIMARY_USER_ID", "USER_SECRET_KEY", "SOME_UNKNOWN_KEY", "LLM_USAGE_HMAC_SECRET",
])
def test_process_level_keys_are_rejected(env, key):
    with pytest.raises(UserSettingError) as exc:
        UserSettingsService().set_setting("alice", key, "x")

    assert exc.value.code == "scope_not_user"
    assert UserRepository().list_settings("alice") == []


@pytest.mark.parametrize("key,value", [
    ("REPORT_TYPE", "bogus"),
    ("REPORT_LANGUAGE", "xx"),
    ("ANTHROPIC_MAX_TOKENS", "abc"),
    ("REPORT_SUMMARY_ONLY", "maybe"),
    ("NOTIFICATION_MIN_SEVERITY", "loud"),
    ("REPORT_TYPE", "a\nb"),
])
def test_values_are_validated_with_the_registry_rules(env, key, value):
    with pytest.raises(UserSettingError) as exc:
        UserSettingsService().set_setting("alice", key, value)

    assert exc.value.code == "invalid_value"
    assert exc.value.issues
    assert UserRepository().list_settings("alice") == []


def test_one_invalid_item_stores_nothing(env):
    with pytest.raises(UserSettingError):
        UserSettingsService().set_settings("alice", {"REPORT_LANGUAGE": "ko", "REPORT_TYPE": "bogus"})

    assert UserRepository().list_settings("alice") == []


def test_invalid_keys_and_user_ids(env):
    service = UserSettingsService()
    for key in ("", "lower case", "1ABC", "A" * 200):
        with pytest.raises(UserSettingError):
            service.set_setting("alice", key, "x")
    with pytest.raises(UserSettingError):
        service.set_setting("", "REPORT_LANGUAGE", "en")
    with pytest.raises(UserSettingError):
        service.set_setting("alice", "REPORT_LANGUAGE", None)


def test_keys_are_normalised_to_upper_case(env):
    UserSettingsService().set_setting("alice", " report_language ", "ko")

    assert [row["key"] for row in UserSettingsService().list_settings("alice")] == ["REPORT_LANGUAGE"]


# --------------------------------------------------------------------------
# Inheritance policy
# --------------------------------------------------------------------------


def test_every_user_scope_secret_other_than_data_source_credentials_is_not_inherited(env):
    leaked = []
    for key in get_registered_field_keys():
        if get_config_scope(key) != SCOPE_USER:
            continue
        if is_secret_setting_key(key) and key not in DATA_SOURCE_CREDENTIAL_KEYS and inherits_from_system(key):
            leaked.append(key)
    assert leaked == []


def test_model_group_never_inherits(env):
    for key in ("LLM_CHANNELS", "LITELLM_MODEL", "AGENT_LITELLM_MODEL", "OPENAI_BASE_URL", "GEMINI_MODEL",
                "LLM_TEMPERATURE", "LLM_REASONING_EFFORT", "ANTHROPIC_MAX_TOKENS", "LLM_FOO_BASE_URL",
                "LLM_FOO_MODELS", "OLLAMA_API_BASE", "OPENAI_VISION_MODEL"):
        assert get_config_scope(key) == SCOPE_USER, key
        assert inherits_from_system(key) is False, key


def test_preferences_and_credentials_inherit(env):
    for key in ("TUSHARE_TOKEN", "REPORT_LANGUAGE", "REPORT_TYPE", "POSITION_STOP_MODE", "DAILY_PLAN_ENABLED",
                "PREOPEN_PUSH_ENABLED", "AGENT_MODE", "AGENT_CONTEXT_COMPRESSION_PROFILE", "MARKET_REVIEW_COLOR_SCHEME",
                "NOTIFICATION_QUIET_HOURS", "SINGLE_STOCK_NOTIFY"):
        assert inherits_from_system(key) is True, key
    for key in ("FEISHU_WEBHOOK_URL", "EMAIL_SENDER", "TELEGRAM_CHAT_ID", "NTFY_URL", "STOCK_LIST",
                "HINDSIGHT_BANK_ID", "HINDSIGHT_API_KEY", "TAVILY_API_KEYS", "SEARXNG_BASE_URLS"):
        assert inherits_from_system(key) is False, key
