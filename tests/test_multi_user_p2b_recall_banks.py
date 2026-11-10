# -*- coding: utf-8 -*-
"""Multi-user P2b-1: long-term recall (Hindsight) uses one memory bank per user (no network).

``requests.Session.request`` is replaced by a recorder, so the tests see exactly which bank every call targets.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from src.agent import assistant_memory
from src.agent.recall import (
    RecallQuery,
    derive_user_bank_id,
    forget_documents,
    get_recall_service,
    get_user_recall_service,
    reset_recall_service,
)
from src.agent.recall.service import user_recall_settings
from src.services.user_config_service import get_user_config
from src.services.user_settings_service import UserSettingsService
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    prepare_multi_user_env,
    stub_dns,
    teardown_multi_user_env,
)

SYSTEM_URL = "https://hindsight.sys.example"
SYSTEM_BANK = "sysbank"


class Recorder:
    def __init__(self):
        self.calls = []

    def request(self, session, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        return SimpleNamespace(status_code=200, json=lambda: {"results": []})

    def banks(self):
        banks = []
        for call in self.calls:
            marker = "/v1/default/banks/"
            if marker in call["url"]:
                banks.append(unquote(call["url"].split(marker, 1)[1].split("/", 1)[0]))
        return banks


@pytest.fixture
def env(tmp_path, monkeypatch):
    prepare_multi_user_env(tmp_path, monkeypatch)
    monkeypatch.setenv("ASSISTANT_RECALL_PROVIDER", "hindsight")
    monkeypatch.setenv("HINDSIGHT_BASE_URL", SYSTEM_URL)
    monkeypatch.setenv("HINDSIGHT_API_KEY", "hs-system-key")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", SYSTEM_BANK)
    from src.config import Config
    from src.services.user_config_service import invalidate_user_config

    Config.reset_instance()
    invalidate_user_config()
    reset_recall_service()
    stub_dns(monkeypatch)
    recorder = Recorder()
    monkeypatch.setattr("requests.Session.request", lambda session, method, url, **kw: recorder.request(session, method, url, **kw))
    yield recorder
    reset_recall_service()
    teardown_multi_user_env()


# ------------------------------------------------------------------ derivation


def test_bank_id_is_deterministic_distinct_and_url_safe():
    first = derive_user_bank_id("sysbank", "uid-user-a")

    assert first == derive_user_bank_id("sysbank", "uid-user-a")
    assert first != derive_user_bank_id("sysbank", "uid-user-b")
    assert first != derive_user_bank_id("otherbank", "uid-user-a")
    assert first.startswith("sysbank-u-") and first != "sysbank"
    odd = derive_user_bank_id("sysbank", "weird/user id?x=1&y")
    assert all(ch.isalnum() or ch in "-_" for ch in odd)
    assert derive_user_bank_id("", "u").startswith("dsa-u-")  # no system bank id configured


# ------------------------------------------------------------------ effective settings per user


def test_system_hindsight_gives_every_user_a_bank_of_their_own(env):
    a = user_recall_settings(USER_A, get_user_config(USER_A))
    b = user_recall_settings(USER_B, get_user_config(USER_B))

    for settings in (a, b):
        assert settings["assistant_recall_provider"] == "hindsight"
        assert settings["hindsight_base_url"] == SYSTEM_URL and settings["hindsight_api_key"] == "hs-system-key"
        assert settings["hindsight_bank_id"] not in ("", SYSTEM_BANK)
    assert a["hindsight_bank_id"] != b["hindsight_bank_id"]
    assert a["hindsight_bank_id"] == derive_user_bank_id(SYSTEM_BANK, USER_A)


def test_a_bank_id_typed_by_the_user_cannot_point_at_someone_elses_bank(env):
    UserSettingsService().set_settings(USER_A, {"HINDSIGHT_BANK_ID": SYSTEM_BANK})

    settings = user_recall_settings(USER_A, get_user_config(USER_A))

    assert settings["hindsight_bank_id"] == derive_user_bank_id(SYSTEM_BANK, USER_A)  # still the derived one


def test_user_with_their_own_hindsight_uses_theirs(env):
    UserSettingsService().set_settings(USER_A, {
        "HINDSIGHT_BASE_URL": "https://memory.alice.example", "HINDSIGHT_API_KEY": "hs-alice-key",
        "HINDSIGHT_BANK_ID": "alice-bank",
    })

    settings = user_recall_settings(USER_A, get_user_config(USER_A))

    assert (settings["hindsight_base_url"], settings["hindsight_api_key"], settings["hindsight_bank_id"]) == (
        "https://memory.alice.example", "hs-alice-key", "alice-bank",
    )
    # and never the system key
    assert "hs-system-key" not in repr(settings)


def test_own_endpoint_without_a_bank_gets_a_derived_one_and_unsafe_endpoint_disables_recall(env, monkeypatch):
    stub_dns(monkeypatch, {"memory.alice.example": "93.184.216.34"})
    UserSettingsService().set_settings(USER_A, {"HINDSIGHT_BASE_URL": "https://memory.alice.example"})
    own = user_recall_settings(USER_A, get_user_config(USER_A))
    assert own["hindsight_bank_id"] == derive_user_bank_id("", USER_A)

    stub_dns(monkeypatch, {"memory.alice.example": "10.0.0.4"})  # DNS rebinding after saving
    refused = user_recall_settings(USER_A, get_user_config(USER_A))
    assert refused["assistant_recall_provider"] == "none" and refused["hindsight_base_url"] == ""


def test_recall_is_disabled_when_nothing_is_configured(env, monkeypatch):
    UserSettingsService().set_settings(USER_A, {"ASSISTANT_RECALL_PROVIDER": "none"})
    assert get_user_recall_service(USER_A, get_user_config(USER_A)).enabled is False

    monkeypatch.setenv("HINDSIGHT_BASE_URL", "")
    from src.config import Config

    Config.reset_instance()
    assert get_user_recall_service(USER_B, get_user_config(USER_B)).configured is False


# ------------------------------------------------------------------ real traffic goes to the right bank


def _turn_kwargs(session, message_id, user_id):
    return dict(
        session_id=session, user_message_id=message_id, user_text="hello", assistant_text="hi there",
        memory=None, user_id=user_id,
    )


def test_retain_and_recall_target_each_users_own_bank(env):
    recorder = env
    for user in (USER_A, USER_B, PRIMARY_ID):
        assistant_memory.retain_chat_turn(**_turn_kwargs(f"sess-{user}", 1, user))
        service = assistant_memory._recall_service_for(user, get_user_config(user))
        assert service.wait_idle(5.0)
        service.recall(RecallQuery(text="what did we say", tags=[], limit=3, now=datetime(2026, 1, 5)))

    banks = recorder.banks()
    assert banks.count(SYSTEM_BANK) == 2                       # the primary user's retain + recall, unchanged
    bank_a, bank_b = derive_user_bank_id(SYSTEM_BANK, USER_A), derive_user_bank_id(SYSTEM_BANK, USER_B)
    assert banks.count(bank_a) == 2 and banks.count(bank_b) == 2
    # each user's retained document went only into their own bank
    retains = [c for c in recorder.calls if c["url"].endswith("/memories") and c["method"] == "POST"]
    by_bank = {unquote(c["url"].split("/banks/", 1)[1].split("/", 1)[0]): c["json"]["items"][0]["document_id"] for c in retains}
    assert by_bank == {SYSTEM_BANK: f"sess-{PRIMARY_ID}:1", bank_a: f"sess-{USER_A}:1", bank_b: f"sess-{USER_B}:1"}


def test_memory_injection_of_a_user_recalls_from_their_bank(env):
    recorder = env

    injection = assistant_memory.build_memory_injection(
        message="what about 600519", effective_context={"stock_code": "600519"}, user_id=USER_B,
    )

    assert injection.recall_status == "used"
    assert set(recorder.banks()) == {derive_user_bank_id(SYSTEM_BANK, USER_B)}


def test_deleting_a_users_session_forgets_in_their_bank_only(env):
    recorder = env

    forget_documents(["s1:1", "s1:2"], user_id=USER_B)
    service = get_user_recall_service(USER_B, get_user_config(USER_B))
    assert service.wait_idle(5.0)

    deletes = [c for c in recorder.calls if c["method"] == "DELETE"]
    assert len(deletes) == 2
    assert {unquote(c["url"].split("/banks/", 1)[1].split("/", 1)[0]) for c in deletes} == {derive_user_bank_id(SYSTEM_BANK, USER_B)}


def test_two_users_never_share_a_service_and_the_primary_keeps_the_global_one(env):
    service_a = get_user_recall_service(USER_A, get_user_config(USER_A))
    service_b = get_user_recall_service(USER_B, get_user_config(USER_B))

    assert service_a is not service_b
    assert service_a is get_user_recall_service(USER_A, get_user_config(USER_A))  # cached while unchanged
    assert service_a.provider._bank_id != service_b.provider._bank_id
    from src.config import get_config

    assert get_recall_service(get_config()).provider._bank_id == SYSTEM_BANK
