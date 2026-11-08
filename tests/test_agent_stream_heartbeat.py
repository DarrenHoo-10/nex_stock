# -*- coding: utf-8 -*-
"""Agent chat SSE keep-alive: long model calls must not leave the stream silent."""

import asyncio
import json
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from api.v1.endpoints import agent as agent_endpoint  # noqa: E402


class _SlowExecutor:
    def __init__(self, delay: float, release: threading.Event | None = None) -> None:
        self.delay = delay
        self.release = release

    def execute_turn(self, turn, progress_callback=None, **_kwargs):
        if self.release is not None:
            self.release.wait(self.delay)
        else:
            threading.Event().wait(self.delay)
        return SimpleNamespace(success=True, content="答案", error=None, total_steps=1, backend="litellm", error_code=None)


def _collect(executor) -> list[str]:
    request = SimpleNamespace(session_id="s1", request_id="r1")

    async def run() -> list[str]:
        with patch.object(agent_endpoint, "get_config", return_value=SimpleNamespace()), \
                patch.object(agent_endpoint, "_select_agent_chat_backend", return_value="litellm"), \
                patch.object(agent_endpoint, "_prepare_chat_request", return_value=(executor, object(), {}, None)), \
                patch.object(agent_endpoint, "_accepted_state_payload", return_value={}):
            response = await agent_endpoint.agent_chat_stream(request, session_service=object())
            return [chunk async for chunk in response.body_iterator]

    return asyncio.run(run())


def _events(chunks: list[str]) -> list[dict]:
    return [json.loads(chunk[len("data: "):]) for chunk in chunks if chunk.startswith("data: ")]


def test_stream_sends_keepalive_comments_while_the_model_is_silent() -> None:
    with patch.object(agent_endpoint, "_STREAM_HEARTBEAT_SECONDS", 0.05):
        chunks = _collect(_SlowExecutor(0.3))

    keepalives = [chunk for chunk in chunks if chunk == ": keepalive\n\n"]
    assert len(keepalives) >= 2
    events = _events(chunks)
    assert [event["type"] for event in events] == ["accepted", "done"]
    assert events[-1]["content"] == "答案"


def test_stream_still_times_out_after_the_idle_limit() -> None:
    release = threading.Event()
    with patch.object(agent_endpoint, "_STREAM_HEARTBEAT_SECONDS", 0.05), \
            patch.object(agent_endpoint, "_STREAM_IDLE_TIMEOUT_SECONDS", 0.2):
        try:
            chunks = _collect(_SlowExecutor(5.0, release))
        finally:
            release.set()

    events = _events(chunks)
    assert events[-1] == {"type": "error", "message": "分析超时"}
    assert ": keepalive\n\n" in chunks
