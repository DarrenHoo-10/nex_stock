# -*- coding: utf-8 -*-
"""Multi-user P2b-1: personal settings API (/api/v1/me/settings), SSRF guard on saved URLs, own usage.

Real app, router, services and SQLite over a temp directory; DNS is stubbed and no model is ever called.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.services.user_config_service import get_user_config, has_usable_model
from src.services.user_settings_service import MASK_TOKEN
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    set_system_provider_env,
    stub_dns,
    teardown_multi_user_env,
)

SETTINGS = "/api/v1/me/settings"


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    set_system_provider_env(monkeypatch)
    monkeypatch.setenv("REPORT_LANGUAGE", "en")
    monkeypatch.setenv("TUSHARE_TOKEN", "ts-system-token")
    from src.config import Config

    Config.reset_instance()
    built.dns = stub_dns(monkeypatch)
    yield built
    teardown_multi_user_env()


def _fields(payload):
    return {item["key"]: item for group in payload["groups"] for item in group["fields"]}


def _put(client, **values):
    return client.put(SETTINGS, json={"items": [{"key": key, "value": value} for key, value in values.items()]})


# ------------------------------------------------------------------ read model


def test_groups_follow_the_design_and_cover_every_user_scope_key(app):
    from src.core.config_registry import SCOPE_USER, get_config_scope, get_registered_field_keys
    from src.services.me_settings_service import MANAGED_ELSEWHERE_KEYS
    from src.services.user_settings_service import LEGACY_CHANNEL_KEYS

    payload = app.as_user(USER_A).get(SETTINGS).json()

    assert [group["id"] for group in payload["groups"]] == [
        "model", "search", "data_source", "agent", "push", "position", "report",
    ]
    shown = set(_fields(payload))
    expected = {
        key for key in get_registered_field_keys()
        if get_config_scope(key) == SCOPE_USER and key not in MANAGED_ELSEWHERE_KEYS | LEGACY_CHANNEL_KEYS
    }
    assert expected <= shown
    assert not shown & LEGACY_CHANNEL_KEYS  # P3a: per-channel .env keys are no longer personal settings
    assert not any(get_config_scope(key) != SCOPE_USER for key in shown)  # no system-scope key leaks into the page
    by_group = {group["id"]: {f["key"] for f in group["fields"]} for group in payload["groups"]}
    assert {"LITELLM_MODEL", "OPENAI_API_KEY", "LLM_CHANNELS"} <= by_group["model"]
    assert {"TAVILY_API_KEYS", "SEARXNG_BASE_URLS"} <= by_group["search"]
    assert "TUSHARE_TOKEN" in by_group["data_source"]
    assert {"AGENT_MAX_STEPS", "HINDSIGHT_BASE_URL"} <= by_group["agent"]
    assert {"NOTIFICATION_QUIET_HOURS", "PREOPEN_PUSH_TIME"} <= by_group["push"]
    assert "POSITION_STOP_LOSS_PCT" in by_group["position"]
    assert "REPORT_LANGUAGE" in by_group["report"]


def test_value_source_distinguishes_user_system_default_and_unset(app):
    client = app.as_user(USER_A)
    assert _put(client, POSITION_STOP_LOSS_PCT="9", OPENAI_API_KEY="sk-alice-secret-value").status_code == 200

    fields = _fields(client.get(SETTINGS).json())

    own = fields["POSITION_STOP_LOSS_PCT"]
    assert (own["source"], own["value"], own["inherits"], own["configured"], own["editable"]) == ("user", "9", True, True, True)
    inherited = fields["REPORT_LANGUAGE"]
    # an inherited system default is reported as source / configured only, never as its value
    assert (inherited["source"], inherited["value"], inherited["inherits"], inherited["configured"]) == (
        "system_default", "", True, True,
    )
    token = fields["TUSHARE_TOKEN"]
    assert (token["source"], token["value"], token["is_secret"], token["inherits"], token["configured"]) == (
        "system_default", "", True, True, True,
    )
    # model / search keys never inherit, even though the system has them
    for key in ("LITELLM_MODEL", "LLM_CHANNELS", "TAVILY_API_KEYS", "GEMINI_API_KEY"):
        assert (fields[key]["source"], fields[key]["value"], fields[key]["inherits"]) == ("unset", "", False), key
    assert fields["OPENAI_API_KEY"]["source"] == "user" and fields["OPENAI_API_KEY"]["value"] == MASK_TOKEN


def test_responses_never_contain_secret_plaintext(app):
    client = app.as_user(USER_A)
    secret = "sk-alice-super-secret-123456"
    response = _put(client, OPENAI_API_KEY=secret, TAVILY_API_KEYS="tvly-alice-search-key")

    assert response.status_code == 200
    body = response.text + client.get(SETTINGS).text
    for plaintext in (secret, "tvly-alice-search-key", "ts-system-token", "sk-SYSTEM-openai-key", "tvly-SYSTEM-search-key"):
        assert plaintext not in body, plaintext


def test_primary_user_gets_read_only_system_values_and_cannot_write(app):
    client = app.as_user(PRIMARY_ID)

    payload = client.get(SETTINGS).json()
    fields = _fields(payload)
    assert payload["is_primary"] is True and payload["managed_in_system_settings"] is True
    assert all(field["editable"] is False for field in fields.values())
    assert fields["LITELLM_MODEL"]["value"] == "openai/sys-model" and fields["LITELLM_MODEL"]["source"] == "user"
    assert fields["OPENAI_API_KEY"]["value"] == MASK_TOKEN
    assert "sk-SYSTEM-openai-key" not in client.get(SETTINGS).text

    rejected = _put(client, REPORT_LANGUAGE="zh")
    assert rejected.status_code == 403 and rejected.json()["error"] == "primary_user_uses_env"
    assert client.delete(f"{SETTINGS}/REPORT_LANGUAGE").json()["error"] == "primary_user_uses_env"
    # nothing was written for the primary user
    from src.repositories.user_repo import UserRepository

    assert UserRepository().list_settings(PRIMARY_ID) == []


# ------------------------------------------------------------------ write semantics


def test_empty_or_masked_secret_keeps_the_stored_value(app):
    client = app.as_user(USER_A)
    _put(client, OPENAI_API_KEY="sk-alice-original-key-1")
    from src.services.user_settings_service import UserSettingsService

    for kept in ("", MASK_TOKEN):
        assert _put(client, OPENAI_API_KEY=kept, REPORT_LANGUAGE="zh").status_code == 200
        assert UserSettingsService().get_plain_values(USER_A)["OPENAI_API_KEY"] == "sk-alice-original-key-1"
    assert UserSettingsService().get_plain_values(USER_A)["REPORT_LANGUAGE"] == "zh"

    _put(client, OPENAI_API_KEY="sk-alice-rotated-key-22")
    assert UserSettingsService().get_plain_values(USER_A)["OPENAI_API_KEY"] == "sk-alice-rotated-key-22"


def test_delete_resets_to_the_default(app):
    client = app.as_user(USER_A)
    _put(client, REPORT_LANGUAGE="ko", OPENAI_API_KEY="sk-alice-key-123456")

    after = client.delete(f"{SETTINGS}/REPORT_LANGUAGE")
    assert after.status_code == 200
    field = _fields(after.json())["REPORT_LANGUAGE"]
    assert (field["source"], field["value"], field["configured"]) == ("system_default", "", True)
    assert get_user_config(USER_A).report_language == "en"

    after = client.delete(f"{SETTINGS}/openai_api_key")  # key is case-insensitive
    assert _fields(after.json())["OPENAI_API_KEY"]["source"] == "unset"
    assert get_user_config(USER_A).openai_api_keys == []
    # deleting something never set is fine
    assert client.delete(f"{SETTINGS}/REPORT_LANGUAGE").status_code == 200


def test_system_scope_keys_are_rejected_and_nothing_is_stored(app):
    client = app.as_user(USER_A)

    for key in ("MAX_WORKERS", "WEBUI_PORT", "AUTH_MODE", "TICKFLOW_KLINE_ADJUST", "LITELLM_CONFIG", "PRIMARY_USER_ID"):
        response = _put(client, REPORT_LANGUAGE="zh", **{key: "1"})
        assert response.status_code == 400 and response.json()["error"] == "scope_not_user", key
        assert client.delete(f"{SETTINGS}/{key}").json()["error"] == "scope_not_user", key
    from src.repositories.user_repo import UserRepository

    assert UserRepository().list_settings(USER_A) == []  # the valid REPORT_LANGUAGE in the same request is not stored either
    assert _put(client, STOCK_LIST="600519").json()["error"] == "managed_elsewhere"


def test_values_are_validated_with_the_registry_rules(app):
    client = app.as_user(USER_A)

    bad = _put(client, AGENT_MAX_STEPS="not-a-number")
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_value"
    assert bad.json()["detail"]["issues"][0]["key"] == "AGENT_MAX_STEPS"
    assert _put(client, AGENT_MAX_STEPS="0").status_code == 400  # below the registry minimum
    assert _put(client, LLM_TEMPERATURE="9").status_code == 400
    assert _put(client, REPORT_LANGUAGE="klingon").status_code == 400
    assert _put(client, PREOPEN_PUSH_TIME="25:99").status_code == 400
    # a rejected secret never echoes what was submitted
    secret = _put(client, TAVILY_API_KEYS="line1\nline2-secret")
    assert secret.status_code == 400 and "line2-secret" not in secret.text


def test_saving_invalidates_the_cached_config_view(app):
    client = app.as_user(USER_A)
    assert client.get("/api/v1/me").json()["model_key_configured"] is False
    before = get_user_config(USER_A)
    assert not has_usable_model(before)

    give_user_model(USER_A)  # same path as PUT: UserSettingsService.set_settings
    after = get_user_config(USER_A)
    assert after is not before and has_usable_model(after)
    assert client.get("/api/v1/me").json()["model_key_configured"] is True

    _put(client, POSITION_STOP_LOSS_PCT="11")
    assert get_user_config(USER_A).position_stop_loss_pct == 11
    client.delete(f"{SETTINGS}/POSITION_STOP_LOSS_PCT")
    assert get_user_config(USER_A).position_stop_loss_pct != 11


def test_users_cannot_see_or_change_each_others_settings(app):
    _put(app.as_user(USER_A), REPORT_LANGUAGE="ko", TAVILY_API_KEYS="tvly-alice-only-key")

    fields_b = _fields(app.as_user(USER_B).get(SETTINGS).json())
    assert fields_b["REPORT_LANGUAGE"]["source"] == "system_default"
    assert fields_b["TAVILY_API_KEYS"]["source"] == "unset"
    assert get_user_config(USER_B).tavily_api_keys == []


# ------------------------------------------------------------------ SSRF guard on saved URLs


@pytest.mark.parametrize("url", [
    "http://api.example.com/v1",              # not https
    "https://127.0.0.1/v1",
    "https://10.0.0.5/v1",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/v1",
    "https://localhost/v1",
    "https://2130706433/v1",                   # numeric alias of 127.0.0.1
    "https://user:pw@api.example.com/v1",
])
@pytest.mark.parametrize("key", [
    # (the per-channel webhook keys are no longer personal settings: see test_multi_user_p3a_hardening legacy-key test)
    "OPENAI_BASE_URL", "LLM_MINE_BASE_URL", "OLLAMA_API_BASE", "SEARXNG_BASE_URLS", "HINDSIGHT_BASE_URL",
    "ANSPIRE_LLM_BASE_URL",
])
def test_unsafe_urls_are_rejected_for_every_endpoint_setting(app, key, url):
    response = _put(app.as_user(USER_A), **{key: url})

    assert response.status_code == 400, (key, url, response.text)
    body = response.json()
    assert body["error"] == "unsafe_url"
    assert url not in response.text  # the submitted value is not echoed
    from src.repositories.user_repo import UserRepository

    assert UserRepository().list_settings(USER_A) == []


def test_hostname_resolving_to_a_private_address_is_rejected(app, monkeypatch):
    stub_dns(monkeypatch, {"sneaky.example.com": "10.1.2.3", "v6.example.com": ["93.184.216.34", "::1"]})
    client = app.as_user(USER_A)

    assert _put(client, OPENAI_BASE_URL="https://sneaky.example.com/v1").json()["error"] == "unsafe_url"
    assert _put(client, OPENAI_BASE_URL="https://v6.example.com/v1").json()["error"] == "unsafe_url"
    assert _put(client, OPENAI_BASE_URL="https://fine.example.com/v1").status_code == 200


def test_every_url_in_a_list_setting_is_checked(app):
    client = app.as_user(USER_A)

    assert _put(client, SEARXNG_BASE_URLS="https://s1.example.com, https://10.0.0.9").json()["error"] == "unsafe_url"
    assert _put(client, SEARXNG_BASE_URLS="https://s1.example.com,http://s2.example.com").json()["error"] == "unsafe_url"
    assert _put(client, SEARXNG_BASE_URLS="https://s1.example.com, https://s2.example.com").status_code == 200


def test_saved_url_is_checked_again_at_call_time(app, monkeypatch):
    """DNS rebinding: a host that was public when saved and later resolves to the LAN is refused before LiteLLM."""
    stub_dns(monkeypatch, {"llm.mine.example": "93.184.216.34"})
    give_user_model(USER_A)
    client = app.as_user(USER_A)
    litellm_called = MagicMock()

    stub_dns(monkeypatch, {"llm.mine.example": "192.168.1.20"})  # rebound
    with patch("litellm.completion", litellm_called), patch("litellm.Router", litellm_called):
        response = client.post(f"{SETTINGS}/test-model")

    assert response.status_code == 400 and response.json()["error"] == "unsafe_url"
    litellm_called.assert_not_called()


# ------------------------------------------------------------------ own usage


def _usage_row(db, user_id, model, tokens):
    db.record_llm_usage("analysis", model, tokens, 1, tokens + 1, stock_code="600519", user_id=user_id)


def test_me_usage_returns_only_the_callers_own_rows_and_global_usage_stays_admin_only(app):
    _usage_row(app.db, USER_A, "model-a", 100)
    _usage_row(app.db, USER_A, "model-a", 50)
    _usage_row(app.db, USER_B, "model-b", 7000)
    _usage_row(app.db, None, "system-model", 900)  # system job / primary user: recorded under the primary user

    mine = app.as_user(USER_A).get("/api/v1/me/usage", params={"period": "all"}).json()
    assert mine["total_calls"] == 2 and mine["total_prompt_tokens"] == 150
    assert {call["model"] for call in mine["recent_calls"]} == {"model-a"}
    assert [row["model"] for row in mine["by_model"]] == ["model-a"]

    theirs = app.as_user(USER_B).get("/api/v1/me/usage", params={"period": "all"}).json()
    assert theirs["total_calls"] == 1 and theirs["recent_calls"][0]["model"] == "model-b"

    primary = app.as_user(PRIMARY_ID).get("/api/v1/me/usage", params={"period": "all"}).json()
    assert {call["model"] for call in primary["recent_calls"]} == {"system-model"}

    everyone = app.as_user(PRIMARY_ID).get("/api/v1/usage/dashboard", params={"period": "all"}).json()
    assert everyone["total_calls"] == 4
    # shape of /me/usage is the shape of /usage/dashboard
    assert set(mine) == set(everyone)
    # the global views are administrator-only (the test switch makes only the primary user an admin)
    assert app.as_user(USER_A).get("/api/v1/usage/dashboard").status_code == 403
    assert app.as_user(USER_A).get("/api/v1/usage/summary").status_code == 403


def test_usage_period_filter_applies_to_own_rows(app):
    _usage_row(app.db, USER_A, "model-a", 10)
    today = app.as_user(USER_A).get("/api/v1/me/usage", params={"period": "today"}).json()
    assert today["period"] == "today" and today["total_calls"] == 1
    assert datetime.fromisoformat(today["recent_calls"][0]["called_at"]).date() == datetime.now().date()


def test_every_user_scope_key_that_holds_an_address_goes_through_the_ssrf_guard():
    """New endpoint-like keys are covered by name; this fails when one is added with a name the rule misses."""
    from src.services.me_settings_service import editable_setting_keys
    from src.services.user_settings_service import is_url_setting_key

    address_like = [
        key for key in editable_setting_keys()
        if "URL" in key or key.endswith(("_BASE", "_API_BASE")) or "WEBHOOK" in key and not key.endswith(("_SECRET", "_KEYWORD", "_TEMPLATE", "_TOKEN"))
    ]
    assert {"OPENAI_BASE_URL", "OLLAMA_API_BASE", "SEARXNG_BASE_URLS", "HINDSIGHT_BASE_URL"} <= set(address_like)
    # the per-channel webhook keys are not personal settings any more (P3a), but would still be name-matched by the guard
    assert not set(address_like) & {"FEISHU_WEBHOOK_URL", "CUSTOM_WEBHOOK_URLS", "NTFY_URL", "GOTIFY_URL", "ASTRBOT_URL"}
    assert all(is_url_setting_key(key) for key in ("FEISHU_WEBHOOK_URL", "CUSTOM_WEBHOOK_URLS", "NTFY_URL"))
    assert [key for key in address_like if not is_url_setting_key(key)] == []
    # dynamic per-channel keys are covered too
    assert is_url_setting_key("LLM_ANYTHING_BASE_URL")
    # ...while ordinary settings are not
    assert not any(is_url_setting_key(key) for key in ("LITELLM_MODEL", "REPORT_LANGUAGE", "OPENAI_API_KEY", "FEISHU_WEBHOOK_SECRET"))
