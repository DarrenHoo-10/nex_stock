# -*- coding: utf-8 -*-
"""Multi-user P2a: Agent Chat sessions belong to their creator (no network, no LLM).

Sessions, messages and summaries are reachable only through a session the acting user owns; another
user's session id behaves like a missing one: read / delete / context / compact give exactly the missing-id
response (never a 404 that would reveal the id exists); continuing it is the endpoint's session_not_found.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute

from api.deps import get_current_user
from api.v1.endpoints import agent as agent_endpoint
from api.v1.endpoints import assistant_notes as notes_endpoint
from src.services.agent_chat_session_service import AgentChatSessionService
from src.storage import ChatSessionNotFound
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    stub_dns,
    teardown_multi_user_env,
)

NOT_FOUND = {"error": "session_not_found", "message": "会话不存在"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    stub_dns(monkeypatch)
    for user in (USER_A, USER_B):  # chatting needs the user's own model (400 model_key_required otherwise)
        give_user_model(user)
    yield built
    teardown_multi_user_env()


def _seed_session(db, session_id: str, user_id: str, text: str = "hello") -> int:
    db.ensure_chat_session_generation(session_id, user_id=user_id)
    return db.save_conversation_user_turn(session_id, text)


def _chat_config():
    return SimpleNamespace(agent_backend="auto", is_agent_available=lambda: True, report_language="zh")


def _fake_executor():
    from src.agent.agent_backend import AgentRunResult
    from src.agent.chat_executor import AgentChatExecutor

    backend = MagicMock()
    backend.backend_id = "litellm"
    backend.runtime_owns_loop = False
    backend.run.return_value = AgentRunResult(
        success=True, final_answer="reply", backend="litellm", messages=[],
    )
    return MagicMock(wraps=AgentChatExecutor(
        backend=backend, config=_chat_config(), context_llm_adapter=None,
    )), backend


# ---------------------------------------------------------------- list / read / delete


def test_session_list_contains_only_own_sessions(app):
    _seed_session(app.db, "sess-a", USER_A, "A question")
    _seed_session(app.db, "sess-b", USER_B, "B question")
    _seed_session(app.db, "sess-p", PRIMARY_ID, "primary question")
    # A pre-multi-user session that only has messages (no state row) belongs to the primary user.
    app.db.save_conversation_message("legacy-msg-only", "user", "legacy question")

    listed = {
        user: {s["session_id"] for s in app.as_user(user).get("/api/v1/agent/chat/sessions").json()["sessions"]}
        for user in (USER_A, USER_B, PRIMARY_ID)
    }

    assert listed[USER_A] == {"sess-a"}
    assert listed[USER_B] == {"sess-b"}
    assert listed[PRIMARY_ID] == {"sess-p", "legacy-msg-only"}


def test_session_list_prefix_filter_never_widens_to_other_users(app):
    _seed_session(app.db, "shared-prefix:b", USER_B)
    _seed_session(app.db, "shared-prefix:a", USER_A)

    response = app.as_user(USER_A).get("/api/v1/agent/chat/sessions", params={"user_id": "shared-prefix"})

    assert [s["session_id"] for s in response.json()["sessions"]] == ["shared-prefix:a"]


def _without_session_id(payload):
    return {key: value for key, value in payload.items() if key not in ("session_id",)}


def test_foreign_session_reads_exactly_like_a_missing_one_and_own_are_readable(app):
    _seed_session(app.db, "sess-b", USER_B, "B secret question")
    app.db.save_conversation_message("sess-b", "assistant", "B secret answer")

    foreign = app.as_user(USER_A).get("/api/v1/agent/chat/sessions/sess-b")
    missing = app.as_user(USER_A).get("/api/v1/agent/chat/sessions/no-such-session")
    own = app.as_user(USER_B).get("/api/v1/agent/chat/sessions/sess-b")

    # no existence signal: same status and body (apart from the echoed id), nothing of B's content
    assert foreign.status_code == missing.status_code == 200
    assert foreign.json()["session_id"] == "sess-b" and missing.json()["session_id"] == "no-such-session"
    assert _without_session_id(foreign.json()) == _without_session_id(missing.json())
    assert foreign.json()["messages"] == [] and "secret" not in foreign.text
    assert own.status_code == 200
    assert [m["content"] for m in own.json()["messages"]] == ["B secret question", "B secret answer"]


def test_legacy_message_only_session_belongs_to_primary_user(app):
    app.db.save_conversation_message("legacy-msg-only", "user", "legacy question")

    other = app.as_user(USER_A).get("/api/v1/agent/chat/sessions/legacy-msg-only")
    assert other.status_code == 200 and other.json()["messages"] == []
    primary = app.as_user(PRIMARY_ID).get("/api/v1/agent/chat/sessions/legacy-msg-only")
    assert primary.status_code == 200
    assert primary.json()["messages"][0]["content"] == "legacy question"


def test_delete_foreign_session_answers_like_a_missing_one_and_leaves_it_intact(app):
    _seed_session(app.db, "sess-b", USER_B, "keep me")

    foreign = app.as_user(USER_A).delete("/api/v1/agent/chat/sessions/sess-b")
    missing = app.as_user(USER_A).delete("/api/v1/agent/chat/sessions/no-such-session")

    assert foreign.status_code == missing.status_code == 200
    assert foreign.json() == missing.json() == {"deleted": 0}
    assert app.db.get_chat_session_owner("sess-b") == USER_B
    assert len(app.db.get_conversation_messages("sess-b")) == 1

    own = app.as_user(USER_B).delete("/api/v1/agent/chat/sessions/sess-b")
    assert own.status_code == 200 and own.json() == {"deleted": 1}
    assert app.db.get_conversation_messages("sess-b") == []


def test_context_and_compact_of_foreign_session_answer_like_a_missing_one(app):
    _seed_session(app.db, "sess-b", USER_B)
    app.db.save_conversation_message("sess-b", "assistant", "B answer")
    client = app.as_user(USER_A)

    def ask(session_id):
        context = client.get(f"/api/v1/agent/chat/sessions/{session_id}/context")
        with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
            "api.v1.endpoints.agent._select_agent_chat_backend", return_value="litellm",
        ), patch("api.v1.endpoints.agent._apply_request_overrides", return_value=_chat_config()), patch(
            "src.agent.llm_adapter.LLMToolAdapter",
        ):
            compact = client.post(f"/api/v1/agent/chat/sessions/{session_id}/compact", json={})
        return context, compact

    foreign_context, foreign_compact = ask("sess-b")
    missing_context, missing_compact = ask("no-such-session")

    assert foreign_context.status_code == missing_context.status_code == 200, foreign_context.text
    assert foreign_context.json()["message_count"] == 0
    assert _without_session_id(foreign_context.json()) == _without_session_id(missing_context.json())
    assert foreign_compact.status_code == missing_compact.status_code == 200, foreign_compact.text
    assert foreign_compact.json()["status"] == "nothing_to_compact"
    foreign_body, missing_body = foreign_compact.json(), missing_compact.json()
    if foreign_compact.status_code == 200:
        foreign_body["context"] = _without_session_id(foreign_body["context"])
        missing_body["context"] = _without_session_id(missing_body["context"])
    assert foreign_body == missing_body
    assert len(app.db.get_conversation_messages("sess-b")) == 2  # nothing of B's was read or changed


def test_summary_is_not_reachable_through_a_foreign_session(app):
    user_msg = _seed_session(app.db, "sess-b", USER_B)
    assistant_msg = app.db.save_conversation_message("sess-b", "assistant", "answer")
    app.db.upsert_conversation_summary("sess-b", "B PRIVATE SUMMARY", assistant_msg, 2, 10)
    assert app.db.get_conversation_summary("sess-b")["summary"] == "B PRIVATE SUMMARY"
    assert user_msg

    service = AgentChatSessionService(app.db)
    with pytest.raises(ChatSessionNotFound):
        service.get_session_detail("sess-b", 100, USER_A)
    with pytest.raises(ChatSessionNotFound):
        service.prepare_session_turn(_chat_config(), "sess-b", "hi", user_id=USER_A)

    assert service.get_session_detail("sess-b", 100, USER_B).messages


# ---------------------------------------------------------------- continue / create


def test_continuing_a_foreign_session_is_404_and_does_not_touch_it(app):
    _seed_session(app.db, "sess-b", USER_B, "B question")

    with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
        "api.v1.endpoints.agent._build_executor",
    ) as build_executor:
        response = app.as_user(USER_A).post(
            "/api/v1/agent/chat", json={"message": "hijack", "session_id": "sess-b"},
        )

    assert response.status_code == 404
    assert response.json()== NOT_FOUND
    build_executor.assert_not_called()
    assert app.db.get_chat_session_owner("sess-b") == USER_B
    assert [m["content"] for m in app.db.get_conversation_messages("sess-b")] == ["B question"]


def test_stream_continuing_a_foreign_session_emits_not_found_error(app):
    _seed_session(app.db, "sess-b", USER_B, "B question")

    async def run():
        with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
            "api.v1.endpoints.agent._build_executor",
        ) as build_executor:
            response = await agent_endpoint.agent_chat_stream(
                agent_endpoint.ChatRequest(message="hijack", session_id="sess-b"),
                session_service=AgentChatSessionService(app.db),
                user=SimpleNamespace(user_id=USER_A),
            )
            chunks = [chunk async for chunk in response.body_iterator]
            build_executor.assert_not_called()
            return chunks

    events = [json.loads(c.removeprefix("data: ")) for c in asyncio.run(run()) if c.startswith("data: ")]

    assert events[0]["type"] == "error" and events[0]["error_code"] == "session_not_found"
    assert app.db.get_chat_session_owner("sess-b") == USER_B
    assert len(app.db.get_conversation_messages("sess-b")) == 1


def test_new_session_is_owned_by_its_creator_and_only_they_can_continue(app):
    executor, backend = _fake_executor()

    def chat(user, session_id):
        with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
            "api.v1.endpoints.agent._build_executor", return_value=executor,
        ):
            return app.as_user(user).post("/api/v1/agent/chat", json={"message": "hi", "session_id": session_id})

    created = chat(USER_A, "sess-new")
    assert created.status_code == 200, created.text
    assert app.db.get_chat_session_owner("sess-new") == USER_A

    assert chat(USER_A, "sess-new").status_code == 200
    assert chat(USER_B, "sess-new").status_code == 404
    assert chat(PRIMARY_ID, "sess-new").status_code == 404
    assert app.db.get_chat_session_owner("sess-new") == USER_A
    assert backend.run.call_count == 2


def test_chat_turn_carries_the_session_owner_to_the_backend(app):
    executor, backend = _fake_executor()
    with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
        "api.v1.endpoints.agent._build_executor", return_value=executor,
    ):
        assert app.as_user(USER_A).post("/api/v1/agent/chat", json={"message": "hi", "session_id": "s-a"}).status_code == 200
        assert app.as_user(PRIMARY_ID).post("/api/v1/agent/chat", json={"message": "hi", "session_id": "s-p"}).status_code == 200

    assert [call.args[0].user_id for call in backend.run.call_args_list] == [USER_A, PRIMARY_ID]


# ---------------------------------------------------------------- stream cancel / notification push


def test_stream_cancel_is_scoped_to_the_requesting_user(app):
    import threading

    event = threading.Event()
    with agent_endpoint._ACTIVE_CODEX_STREAMS_LOCK:
        agent_endpoint._ACTIVE_CODEX_STREAMS[(USER_B, "req-1")] = event
    try:
        foreign = app.as_user(USER_A).post("/api/v1/agent/chat/stream/req-1/cancel")
        assert foreign.status_code == 404 and not event.is_set()
        own = app.as_user(USER_B).post("/api/v1/agent/chat/stream/req-1/cancel")
        assert own.status_code == 200 and event.is_set()
    finally:
        with agent_endpoint._ACTIVE_CODEX_STREAMS_LOCK:
            agent_endpoint._ACTIVE_CODEX_STREAMS.pop((USER_B, "req-1"), None)


def test_chat_send_never_pushes_other_users_content_to_primary_channels(app):
    with patch("src.notification.NotificationService") as notification:
        other = app.as_user(USER_A).post("/api/v1/agent/chat/send", json={"content": "A text"})
        assert other.json()["success"] is False and other.json()["error"] == "no_channels"
        notification.assert_not_called()

        notification.return_value.send.return_value = True
        primary = app.as_user(PRIMARY_ID).post("/api/v1/agent/chat/send", json={"content": "P text"})
        assert primary.json() == {"success": True}
        notification.return_value.send.assert_called_once_with("P text")


# ---------------------------------------------------------------- structure


def _depends_on_current_user(dependant) -> bool:
    return any(
        dep.call is get_current_user or _depends_on_current_user(dep)
        for dep in dependant.dependencies
    )


def test_every_session_and_notes_route_depends_on_current_user():
    session_paths = {
        ("POST", "/chat"), ("POST", "/chat/stream"), ("GET", "/chat/sessions"),
        ("GET", "/chat/sessions/{session_id}"), ("DELETE", "/chat/sessions/{session_id}"),
        ("GET", "/chat/sessions/{session_id}/context"), ("POST", "/chat/sessions/{session_id}/compact"),
        ("POST", "/chat/send"), ("POST", "/chat/stream/{request_id}/cancel"),
    }
    found = set()
    for route in agent_endpoint.router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (method, route.path) in session_paths:
                found.add((method, route.path))
                assert _depends_on_current_user(route.dependant), f"{method} {route.path}"
    assert found == session_paths

    notes_routes = [r for r in notes_endpoint.router.routes if isinstance(r, APIRoute)]
    assert notes_routes
    for route in notes_routes:
        assert _depends_on_current_user(route.dependant), f"{sorted(route.methods)} {route.path}"


# ---------------------------------------------------------------- bot session-id namespaces


BOT_IDS = ["telegram_12345", "feishu_ou_x", "feishu:oc_chat", "dingtalk_u1:chat", "discord_9:ask_600519_x", "Feishu_ou_x"]


@pytest.mark.parametrize("session_id", BOT_IDS)
def test_non_primary_user_cannot_create_a_bot_prefixed_session(app, session_id):
    executor, backend = _fake_executor()
    with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
        "api.v1.endpoints.agent._build_executor", return_value=executor,
    ):
        response = app.as_user(USER_B).post("/api/v1/agent/chat", json={"message": "hi", "session_id": session_id})

    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"
    backend.run.assert_not_called()
    assert app.db.get_chat_session_owner(session_id) is None  # nothing was pre-created for the bot to trip over


def test_non_primary_stream_rejects_bot_prefixed_session_before_streaming(app):
    async def run():
        with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
            "api.v1.endpoints.agent._build_executor",
        ) as build_executor:
            with pytest.raises(agent_endpoint.HTTPException) as caught:
                await agent_endpoint.agent_chat_stream(
                    agent_endpoint.ChatRequest(message="hi", session_id="telegram_12345"),
                    session_service=AgentChatSessionService(app.db),
                    user=SimpleNamespace(user_id=USER_B),
                )
            build_executor.assert_not_called()
            return caught.value

    error = asyncio.run(run())
    assert error.status_code == 400 and error.detail["error"] == "validation_error"
    assert app.db.get_chat_session_owner("telegram_12345") is None


def test_primary_user_and_ordinary_ids_keep_working(app):
    executor, _ = _fake_executor()

    def chat(user, session_id):
        with patch("api.v1.endpoints.agent.get_config", return_value=_chat_config()), patch(
            "api.v1.endpoints.agent._build_executor", return_value=executor,
        ):
            return app.as_user(user).post("/api/v1/agent/chat", json={"message": "hi", "session_id": session_id})

    assert chat(PRIMARY_ID, "feishu:oc_chat").status_code == 200  # the bots act as the primary user
    assert chat(USER_B, "my-feishu-notes").status_code == 200  # only the bot namespaces are reserved
    assert chat(USER_B, "feishu").status_code == 200
    assert chat(USER_B, None).status_code == 200


def test_reserved_prefixes_cover_every_platform_the_bots_use():
    from bot.models import Platform
    from bot.platforms.discord import DiscordPlatform
    from bot.platforms.dingtalk import DingtalkPlatform
    from src.agent.conversation import BOT_SESSION_PLATFORMS, is_bot_session_id
    from src.services import feishu_binding_service

    platforms = {p.value for p in Platform if p is not Platform.UNKNOWN}
    platforms |= {DiscordPlatform.platform_name.fget(object.__new__(DiscordPlatform)),
                  DingtalkPlatform.platform_name.fget(object.__new__(DingtalkPlatform))}
    assert platforms <= set(BOT_SESSION_PLATFORMS)
    assert feishu_binding_service.SESSION_PREFIX in BOT_SESSION_PLATFORMS
    assert is_bot_session_id(feishu_binding_service.chat_session_id("oc_1"))
    assert is_bot_session_id(feishu_binding_service.thread_session_id("oc_1", "omt_1"))
    assert not is_bot_session_id(None) and not is_bot_session_id("") and not is_bot_session_id("plain-session")
