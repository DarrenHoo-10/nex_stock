# -*- coding: utf-8 -*-
"""Assistant controls: per-request model / reasoning effort, context info and manual compaction."""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from api.v1.endpoints import agent  # noqa: E402
from src.agent.chat_context import (  # noqa: E402
    SUMMARY_USER_PREFIX,
    build_visible_chat_history,
    compact_chat_history,
    describe_chat_context,
)
from src.config import Config, get_agent_reasoning_effort, get_effective_agent_primary_model  # noqa: E402
from src.services.agent_model_service import (  # noqa: E402
    AgentChatOverrideError,
    apply_agent_chat_overrides,
    list_agent_selectable_models,
)
from src.storage import ChatSessionStateConflict, DatabaseManager  # noqa: E402


from api.deps import CurrentUser  # noqa: E402

_PRIMARY_USER = CurrentUser(user_id="local", handle="local", display_name="local", role="admin", is_admin=True)


def _model_config(**overrides) -> Config:
    config = Config(
        litellm_model="primary-alias",
        litellm_fallback_models=["fallback-alias"],
        llm_model_list=[
            {"model_name": "primary-alias", "litellm_params": {"model": "openai/primary-wire", "api_key": "k1"}},
            {"model_name": "fallback-alias", "litellm_params": {"model": "openai/fallback-wire", "api_key": "k2"}},
        ],
        llm_channels=[],
        litellm_config_path="config/litellm.yaml",
        llm_models_source="litellm_config",
        openai_base_url=None,
        llm_reasoning_effort="high",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _history_config(*, enabled: bool = False, protected: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        agent_context_compression_enabled=enabled,
        agent_context_compression_profile="balanced",
        agent_context_compression_trigger_tokens=12000,
        agent_context_protected_turns=protected,
        llm_model_list=[],
        agent_litellm_model="openai/test-model",
        litellm_model="openai/test-model",
        litellm_fallback_models=[],
    )


def _reset_db() -> DatabaseManager:
    DatabaseManager.reset_instance()
    Config.reset_instance()
    return DatabaseManager(db_url="sqlite:///:memory:")


def _add_turns(db: DatabaseManager, session_id: str, turns: int) -> None:
    for index in range(1, turns + 1):
        db.save_conversation_message(session_id, "user", f"u{index}")
        db.save_conversation_message(session_id, "assistant", f"a{index}")


def _summary_adapter(content: str = "## 会话摘要\n手动摘要") -> MagicMock:
    adapter = MagicMock()
    adapter.call_text.return_value = SimpleNamespace(content=content, provider="openai", model="openai/test-model", usage={})
    return adapter


async def _inline_to_thread(func, *args, **kwargs):
    # In-memory SQLite connections are bound to the creating thread.
    return func(*args, **kwargs)


def teardown_function() -> None:
    DatabaseManager.reset_instance()
    Config.reset_instance()


# ---------------------------------------------------------------- overrides

def test_selectable_models_use_router_aliases() -> None:
    assert list_agent_selectable_models(_model_config()) == ["primary-alias", "fallback-alias"]


def test_no_override_returns_shared_config() -> None:
    config = _model_config()
    assert apply_agent_chat_overrides(config) is config
    assert apply_agent_chat_overrides(config, model=" ", reasoning_effort="") is config


def test_model_and_effort_override_copy_without_touching_shared_config() -> None:
    config = _model_config()

    overridden = apply_agent_chat_overrides(config, model="fallback-alias", reasoning_effort="LOW")

    assert overridden is not config
    assert get_effective_agent_primary_model(overridden) == "fallback-alias"
    assert get_agent_reasoning_effort(overridden) == "low"
    assert get_effective_agent_primary_model(config) == "primary-alias"
    assert get_agent_reasoning_effort(config) == "high"


def test_effort_none_sends_no_reasoning_effort_even_when_llm_effort_is_set() -> None:
    overridden = apply_agent_chat_overrides(_model_config(), reasoning_effort="none")
    assert get_agent_reasoning_effort(overridden) == ""


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [({"model": "openai/not-configured"}, "model_not_available"), ({"reasoning_effort": "turbo"}, "invalid_reasoning_effort")],
)
def test_unknown_model_or_effort_is_rejected(kwargs, code) -> None:
    with pytest.raises(AgentChatOverrideError) as exc_info:
        apply_agent_chat_overrides(_model_config(), **kwargs)
    assert exc_info.value.code == code


def test_endpoint_override_maps_errors_to_400_and_rejects_codex() -> None:
    config = _model_config()
    with pytest.raises(HTTPException) as bad_model:
        agent._apply_request_overrides(agent.ChatRequest(message="hi", model="nope"), config, "litellm")
    assert bad_model.value.status_code == 400
    assert bad_model.value.detail["error"] == "model_not_available"

    with pytest.raises(HTTPException) as codex:
        agent._apply_request_overrides(agent.ChatRequest(message="hi", reasoning_effort="low"), config, "codex_app_server")
    assert codex.value.status_code == 400

    plain = agent.ChatRequest(message="hi")
    assert agent._apply_request_overrides(plain, config, "codex_app_server") is config


def test_models_endpoint_reports_route_model_and_defaults() -> None:
    config = _model_config()
    with patch.object(agent, "get_config", return_value=config), patch(
        "src.agent.agent_backend.resolve_agent_backend_id", return_value="litellm",
    ):
        response = asyncio.run(agent.get_agent_models())

    assert [item.route_model for item in response.models] == ["primary-alias", "fallback-alias"]
    assert response.default_reasoning_effort == "high"
    assert "xhigh" in response.reasoning_effort_options


# ---------------------------------------------------------------- history / compaction

def test_disabled_compression_uses_stored_summary_plus_newer_messages() -> None:
    db = _reset_db()
    session_id = "chat-manual-summary"
    _add_turns(db, session_id, 3)
    adapter = _summary_adapter()

    result = compact_chat_history(session_id, adapter, _history_config(enabled=False, protected=1))
    history = build_visible_chat_history(session_id, MagicMock(), _history_config(enabled=False))

    assert result.status == "compacted"
    assert result.summarized_message_count == 4
    assert history[0]["content"].startswith(SUMMARY_USER_PREFIX)
    assert [msg["content"] for msg in history[1:]] == ["u3", "a3"]


def test_disabled_compression_without_summary_keeps_recent_20() -> None:
    db = _reset_db()
    session_id = "chat-plain"
    _add_turns(db, session_id, 12)

    history = build_visible_chat_history(session_id, MagicMock(), _history_config(enabled=False))

    assert len(history) == 20
    assert history[0]["content"] == "u3"


def test_compact_nothing_to_compact_does_not_call_model() -> None:
    db = _reset_db()
    session_id = "chat-short"
    _add_turns(db, session_id, 1)
    adapter = _summary_adapter()

    result = compact_chat_history(session_id, adapter, _history_config(protected=1))

    assert result.status == "nothing_to_compact"
    adapter.call_text.assert_not_called()
    assert db.get_conversation_summary(session_id) is None


def test_compact_failure_leaves_session_untouched() -> None:
    db = _reset_db()
    session_id = "chat-fail"
    _add_turns(db, session_id, 3)

    result = compact_chat_history(session_id, _summary_adapter(content=""), _history_config(protected=1))

    assert result.status == "failed"
    assert db.get_conversation_summary(session_id) is None


def test_compact_lost_race_raises_conflict() -> None:
    db = _reset_db()
    session_id = "chat-race"
    _add_turns(db, session_id, 3)

    with patch.object(db, "upsert_conversation_summary", return_value=False):
        with pytest.raises(ChatSessionStateConflict):
            compact_chat_history(session_id, _summary_adapter(), _history_config(protected=1))


def test_second_compaction_only_folds_new_messages() -> None:
    db = _reset_db()
    session_id = "chat-incremental"
    _add_turns(db, session_id, 3)
    adapter = _summary_adapter()
    compact_chat_history(session_id, adapter, _history_config(protected=1))
    _add_turns(db, session_id, 1)  # u1/a1 again as turn 4 contents; ids are what matter

    result = compact_chat_history(session_id, adapter, _history_config(protected=1))

    assert result.status == "compacted"
    assert result.summarized_message_count == 2
    second_request = adapter.call_text.call_args_list[-1].args[0][1]["content"]
    assert "已有滚动摘要" in second_request


def test_describe_context_reports_summary_and_compactable_counts() -> None:
    db = _reset_db()
    session_id = "chat-describe"
    _add_turns(db, session_id, 3)

    before = describe_chat_context(session_id, _history_config(protected=1))
    compact_chat_history(session_id, _summary_adapter(), _history_config(protected=1))
    after = describe_chat_context(session_id, _history_config(protected=1))

    assert before.message_count == 6
    assert before.history_message_count == 6
    assert before.compactable_message_count == 4
    assert before.summary is None
    assert after.history_message_count == 2
    assert after.compactable_message_count == 0
    assert after.summary["covered_message_count"] == 4
    assert after.history_tokens > 0


def test_context_endpoint_for_unknown_session_is_empty() -> None:
    _reset_db()
    with patch.object(agent, "get_config", return_value=_history_config()), patch.object(
        agent, "resolve_model_context_window", return_value=None,
    ), patch.object(agent.asyncio, "to_thread", _inline_to_thread):
        response = asyncio.run(agent.get_chat_session_context(
            "missing-session", session_service=agent.AgentChatSessionService(), user=_PRIMARY_USER,
        ))

    assert response.message_count == 0
    assert response.history_tokens == 0
    assert response.summary is None
    assert response.model == "openai/test-model"


def test_compact_endpoint_returns_502_when_summary_fails() -> None:
    db = _reset_db()
    session_id = "chat-endpoint-fail"
    _add_turns(db, session_id, 3)
    config = _history_config(protected=1)

    with patch.object(agent, "get_config", return_value=config), patch.object(
        agent, "_select_agent_chat_backend", return_value="litellm",
    ), patch("src.agent.llm_adapter.LLMToolAdapter", return_value=_summary_adapter(content="")), patch.object(
        agent.asyncio, "to_thread", _inline_to_thread,
    ):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(agent.compact_chat_session(
                session_id, session_service=agent.AgentChatSessionService(), user=_PRIMARY_USER,
            ))

    assert exc_info.value.status_code == 502
    assert db.get_conversation_summary(session_id) is None


def test_compact_endpoint_success_returns_fresh_context() -> None:
    db = _reset_db()
    session_id = "chat-endpoint-ok"
    _add_turns(db, session_id, 3)
    config = _history_config(protected=1)

    with patch.object(agent, "get_config", return_value=config), patch.object(
        agent, "_select_agent_chat_backend", return_value="litellm",
    ), patch.object(agent, "resolve_model_context_window", return_value=128000), patch(
        "src.agent.llm_adapter.LLMToolAdapter", return_value=_summary_adapter(),
    ), patch.object(agent.asyncio, "to_thread", _inline_to_thread):
        response = asyncio.run(agent.compact_chat_session(
                session_id, session_service=agent.AgentChatSessionService(), user=_PRIMARY_USER,
            ))

    assert response.status == "compacted"
    assert response.summarized_message_count == 4
    assert response.context.summary.covered_message_count == 4
    assert response.context.context_window == 128000


# ---------------------------------------------------------------- endpoint wiring

def _capture_prepare(captured):
    def fake_prepare(request, config, session_id, session_service, user_id):
        captured.append(config)
        raise RuntimeError("stop after prepare")
    return fake_prepare


def test_stream_endpoint_prepares_turn_with_overridden_config() -> None:
    config = _model_config()
    captured = []

    async def run():
        with patch.object(agent, "get_config", return_value=config), patch.object(
            agent, "_select_agent_chat_backend", return_value="litellm",
        ), patch.object(agent, "_prepare_chat_request", _capture_prepare(captured)):
            response = await agent.agent_chat_stream(
                agent.ChatRequest(message="hi", model="fallback-alias", reasoning_effort="none"),
                session_service=MagicMock(),
                user=_PRIMARY_USER,
            )
            return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(run())

    assert '"request_not_accepted"' in "".join(chunks)
    assert get_effective_agent_primary_model(captured[0]) == "fallback-alias"
    assert get_agent_reasoning_effort(captured[0]) == ""
    assert get_effective_agent_primary_model(config) == "primary-alias"


def test_chat_endpoint_without_overrides_uses_shared_config() -> None:
    config = _model_config()
    captured = []
    with patch.object(agent, "get_config", return_value=config), patch.object(
        agent, "_select_agent_chat_backend", return_value="litellm",
    ), patch.object(agent, "_prepare_chat_request", _capture_prepare(captured)):
        with pytest.raises(HTTPException):
            asyncio.run(agent.agent_chat(
                agent.ChatRequest(message="hi"), session_service=MagicMock(), user=_PRIMARY_USER,
            ))

    assert captured == [config]
