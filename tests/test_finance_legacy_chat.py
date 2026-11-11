import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.services import finance_legacy_chat as legacy
from src.services.finance_identity import FinanceIdentityStore, user_system_available


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from api import deps
    from api.v1.endpoints import agent, finance, finance_assistant
    monkeypatch.setattr(legacy, "setting", lambda key: "https://core.test" if key == "FINANCE_CORE_URL" else "")
    monkeypatch.setattr(legacy, "require_local_access", lambda *args, **kwargs: True)
    monkeypatch.setattr(legacy, "FinanceIdentityStore", lambda: FinanceIdentityStore(tmp_path / "ledger.db"))
    monkeypatch.setattr(finance, "browser_identity", lambda *args, **kwargs: {"issuer": "test", "subject": "alice"})
    monkeypatch.setattr(finance_assistant, "core_session", lambda *args, **kwargs: ("finance-bot", "private-server-token"))
    calls = []
    def core(method, path, token, *, data=None, params=None):
        assert token == "private-server-token"
        calls.append((method, path, data, params))
        if path == "finance/alias":
            if method == "POST" and data.get("modelSelection"):
                assert data["modelSelection"]["connectionId"] == "mine"
                assert data["modelSelection"]["reasoning"] == "max"
            return (202, {"botId": "finance-bot", "conversationId": "topic", "messageId": "input", "taskId": "task"}) if method == "POST" else (200, {"conversationId": "topic"})
        if path == "agents/messages":
            return 200, {"messages": [{"id": "input", "role": "user", "content": "hello", "task_id": "task"},
                                      {"id": "answer", "role": "assistant", "content": "same topic answer", "task_id": "task"}]}
        if path == "agents/tasks":
            return 200, {"tasks": [{"id": "task", "status": "succeeded"}]}
        if path == "finance/models":
            return 200, {"connections": [{"id": "mine", "enabled": True, "configured": True,
                                         "defaultModel": "mine-full", "models": ["mine-full", "mine-alt"],
                                         "options": {"mine-full": {"reasoning": ["high", "max"]}}}],
                         "selection": {"selected_connection": "mine", "selected_model": "mine-full", "selected_reasoning": "max", "model_selection_version": 4}}
        if path == "bots/model":
            pytest.fail("A legacy per-turn override must never mutate the Core default")
        if path == "finance/aliases":
            return 200, {"sessions": [{"session_id": "old-session", "title": "hello", "message_count": 2}]}
        if path == "agents/cancel":
            assert data["messageId"] == "input"
            return 200, {"accepted": True}
        return 200, {"deleted": 2}
    monkeypatch.setattr(finance_assistant, "core_call", core)
    async def connected(self):
        return False
    monkeypatch.setattr(Request, "is_disconnected", connected)
    app = FastAPI()
    app.include_router(agent.router, prefix="/api/v1/agent")
    app.dependency_overrides[deps.get_agent_chat_session_service] = lambda: SimpleNamespace()
    return TestClient(app), calls


def test_real_legacy_chat_and_sse_enter_same_managed_core_topic(managed):
    client, calls = managed
    payload = {"message": "hello", "session_id": "old-session", "request_id": "request-one"}
    response = client.post("/api/v1/agent/chat", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["content"] == "same topic answer"
    assert response.json()["session_id"] == "old-session"
    stream = client.post("/api/v1/agent/chat/stream", json={**payload, "request_id": "request-two"})
    assert stream.status_code == 200, stream.text
    assert '"type": "accepted"' in stream.text and '"type": "done"' in stream.text
    assert "private-server-token" not in response.text + stream.text
    aliases = [data for method, path, data, _ in calls if method == "POST" and path == "finance/alias"]
    assert {data["sessionId"] for data in aliases} == {"old-session"}
    assert client.get("/api/v1/agent/chat/sessions/old-session").json()["messages"][-1]["content"] == "same topic answer"
    assert client.get("/api/v1/agent/chat/sessions").json()["sessions"][0]["session_id"] == "old-session"
    assert client.post("/api/v1/agent/chat/stream/request-two/cancel").status_code == 200


def test_legacy_opt_out_does_not_contact_core(monkeypatch):
    monkeypatch.setattr(legacy, "setting", lambda key: "")
    assert asyncio.run(legacy.maybe_managed(None, "send")) is None


def test_old_explicit_model_uses_own_core_capabilities_and_unknown_never_falls_back(managed):
    client, calls = managed
    response = client.post("/api/v1/agent/chat", json={"message": "hello", "session_id": "model-session",
                           "request_id": "model-request", "model": "mine-full", "reasoning_effort": "max"})
    assert response.status_code == 200, response.text
    changes = [data for method, path, data, _ in calls if path == "bots/model"]
    assert changes == []
    per_turn = client.post("/api/v1/agent/chat", json={"message": "hello", "request_id": "different-model",
                           "model": "mine-alt", "reasoning_effort": "max"})
    assert per_turn.status_code == 200
    selections = [data["modelSelection"] for method, path, data, _ in calls if path == "finance/alias" and method == "POST" and "modelSelection" in data]
    assert [selection["model"] for selection in selections] == ["mine-full", "mine-alt"]
    count = len([call for call in calls if call[1] == "finance/alias"])
    rejected = client.post("/api/v1/agent/chat", json={"message": "hello", "request_id": "unknown-model",
                           "model": "someone-elses-route", "reasoning_effort": "max"})
    assert rejected.status_code == 422
    assert len([call for call in calls if call[1] == "finance/alias"]) == count


@pytest.mark.skipif(not user_system_available(), reason="Requires actual P1/P2 owner columns")
def test_history_export_actual_rows_owner_roles_context_and_message_only_primary(tmp_path, monkeypatch):
    from datetime import datetime
    from src.storage import DatabaseManager, ConversationMessage, ConversationSessionState
    from tests.multi_user_helpers import prepare_multi_user_env, teardown_multi_user_env, PRIMARY_ID
    from src.services.finance_history_export import export_messages
    prepare_multi_user_env(tmp_path, monkeypatch)
    db = DatabaseManager.get_instance()
    try:
        with db.get_session() as session:
            session.add(ConversationSessionState(session_id="a", user_id="alice", selected_skill_ids_json="null"))
            session.add(ConversationSessionState(session_id="b", user_id="bob", selected_skill_ids_json="null"))
            session.add_all([
                ConversationMessage(session_id="a", role="user", content="alice", created_at=datetime(2026, 11, 10), context_snapshot='{"code":"AAPL","api_key":"must-not-export"}'),
                ConversationMessage(session_id="a", role="tool", content="private-tool-trace"),
                ConversationMessage(session_id="b", role="assistant", content="bob"),
                ConversationMessage(session_id="feishu:old-shared", role="assistant", content="legacy privately imported"),
            ])
            session.commit()
        result = export_messages("alice", limit=1)
        assert [m["content"] for m in result["messages"]] == ["alice"]
        assert result["messages"][0]["sourceContext"] == {"code": "AAPL"}
        assert "feishu" not in result["messages"][0]
        assert result["hasMore"] is False
        assert export_messages("alice", result["nextCursor"])["messages"] == []
        assert [m["content"] for m in export_messages("bob")["messages"]] == ["bob"]
        assert [m["content"] for m in export_messages(PRIMARY_ID)["messages"]] == ["legacy privately imported"]
        original_id = result["messages"][0]["id"]
        with db.get_session() as session:
            session.get(ConversationMessage, original_id).content = "edited alice"
            session.commit()
        edited = export_messages("alice", result["nextCursor"])
        assert edited["messages"][0]["id"] == original_id
        assert edited["messages"][0]["content"] == "edited alice"
        with db.get_session() as session:
            session.add(ConversationMessage(session_id="a", role="assistant", content="later follow-up"))
            session.commit()
        appended = export_messages("alice", edited["nextCursor"])
        assert [m["content"] for m in appended["messages"]] == ["later follow-up"]
        with db.get_session() as session:
            session.delete(session.get(ConversationMessage, original_id))
            session.commit()
        deleted = export_messages("alice", appended["nextCursor"])
        assert deleted["messages"] == [{"id": original_id, "sessionId": "a", "cursor": deleted["nextCursor"], "deleted": True}]
        assert all(m.get("content") != "edited alice" for m in export_messages("alice")["messages"])
        with db.get_session() as session:
            session.get(ConversationSessionState, "a").user_id = "bob"
            session.commit()
        moved = export_messages("alice", deleted["nextCursor"])
        assert all(m["deleted"] is True for m in moved["messages"])
        assert "later follow-up" in [m.get("content") for m in export_messages("bob")["messages"]]
    finally:
        teardown_multi_user_env()


@pytest.mark.skipif(not user_system_available(), reason="Requires P2 ownership migration")
def test_local_history_journal_upgrades_to_owner_aware_triggers_without_primary_leak(tmp_path, monkeypatch):
    from src import storage
    from tests.multi_user_helpers import prepare_multi_user_env, teardown_multi_user_env, PRIMARY_ID
    from src.services.finance_history_export import export_messages
    prepare_multi_user_env(tmp_path, monkeypatch)
    db = storage.DatabaseManager.get_instance()
    actual_state = storage.ConversationSessionState
    try:
        with db.get_session() as session:
            session.add(actual_state(session_id="after-upgrade", user_id="alice", selected_skill_ids_json="null"))
            session.add(storage.ConversationMessage(session_id="after-upgrade", role="user", content="alice source"))
            session.commit()
        # Simulate the pre-P2 code view while using the same physical DB. Its
        # initial journal has primary-only trigger definitions.
        monkeypatch.setattr(storage, "ConversationSessionState", SimpleNamespace())
        previous = export_messages(PRIMARY_ID)
        assert previous["messages"][0]["content"] == "alice source"
        monkeypatch.setattr(storage, "ConversationSessionState", actual_state)
        assert export_messages("alice")["messages"][0]["content"] == "alice source"
        tombstones = export_messages(PRIMARY_ID, previous["nextCursor"])["messages"]
        assert tombstones and all(message.get("deleted") for message in tombstones)
        with db.get_session() as session:
            session.add(storage.ConversationMessage(session_id="after-upgrade", role="assistant", content="new owned reply"))
            session.commit()
        assert any(message.get("content") == "new owned reply" for message in export_messages("alice")["messages"])
        assert all(message.get("deleted") for message in export_messages(PRIMARY_ID)["messages"])
    finally:
        teardown_multi_user_env()
