# -*- coding: utf-8 -*-
"""Assistant API access: read_api_doc (progressive OpenAPI) and call_api (in-process /api/v1 calls)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import api.internal_client as internal
from src.agent.tools import api_tools
from src.config import get_primary_user_id


def _call(*args, **kwargs):
    """``call_api`` as the primary user (the registry supplies ``user_id``; local mode, no user table involved)."""
    return api_tools._handle_call_api(*args, user_id=get_primary_user_id(), **kwargs)


def _doc(*args, **kwargs):
    return api_tools._handle_read_api_doc(*args, user_id=get_primary_user_id(), **kwargs)


class _Item(BaseModel):
    name: str
    qty: int = 1


def _fake_app() -> FastAPI:
    app = FastAPI()
    calls = app.state.calls = []

    @app.get("/api/v1/items/latest", tags=["Items"], summary="Latest item")
    def latest():
        return {"id": "latest"}

    @app.get("/api/v1/items/{item_id}", tags=["Items"], summary="One item")
    def one(item_id: str, verbose: bool = False):
        return {"id": item_id, "verbose": verbose}

    @app.post("/api/v1/items", tags=["Items"], summary="Create item")
    def create(item: _Item):
        calls.append(item.model_dump())
        return {"created": item.model_dump()}

    @app.get("/api/v1/big", tags=["Misc"], summary="Big text")
    def big():
        return PlainTextResponse("x" * (api_tools._RESULT_MAX_CHARS + 10))

    @app.post("/api/v1/agent/chat", tags=["Agent"], summary="Agent Chat")
    def chat():  # pragma: no cover - must never be reached
        calls.append("chat")
        return {}

    return app


@pytest.fixture
def fake_api(monkeypatch):
    app = _fake_app()
    monkeypatch.setattr(internal, "_internal_app", app)
    monkeypatch.setattr(internal, "_running_app", None)
    monkeypatch.setattr(internal, "_route_index", None)
    yield app
    internal._route_index = None


def test_call_api_dispatches_in_process_with_path_query_and_body(fake_api):
    one = _call("GET", "/items/42", query={"verbose": True})
    assert one == {"endpoint": "GET /api/v1/items/{item_id}", "status_code": 200, "ok": True,
                   "data": {"id": "42", "verbose": True}}
    assert _call("get", "/api/v1/items/latest")["endpoint"] == "GET /api/v1/items/latest"
    assert _call("GET", "/api/v1/items/7?verbose=true")["data"]["verbose"] is True
    created = _call("POST", "/api/v1/items", body={"name": "a", "qty": 2})
    assert created["ok"] and fake_api.state.calls == [{"name": "a", "qty": 2}]


def test_call_api_reports_errors_without_raising(fake_api):
    invalid = _call("POST", "/api/v1/items", body={"qty": 2})
    assert invalid["status_code"] == 422 and not invalid["ok"]
    assert "没有接口" in _call("GET", "/api/v1/nope")["error"]
    assert "method" in _call("TRACE", "/api/v1/items/1")["error"]
    assert "query" in _call("GET", "/api/v1/items/1", query="verbose=1")["error"]
    big = _call("GET", "/api/v1/big")
    assert big["truncated"] and len(big["text"]) == api_tools._RESULT_MAX_CHARS


def test_call_api_refuses_excluded_routes(fake_api):
    result = _call("POST", "/api/v1/agent/chat", body={"message": "hi"})
    assert "不能由助手调用" in result["error"] and fake_api.state.calls == []


def test_read_api_doc_discloses_directory_group_then_endpoint(fake_api):
    directory = _doc()
    assert "read_api_doc" in directory["guide"]
    assert {g["group"]: g["endpoints"] for g in directory["groups"]} == {"Agent": 1, "Items": 3, "Misc": 1}
    group = _doc(group="items")
    assert "GET /api/v1/items/{item_id} — One item" in group["endpoints"]
    assert any("不可调用" in line for line in _doc(group="Agent")["endpoints"])
    assert "groups" in _doc(group="nothing")

    detail = _doc(endpoint="POST /api/v1/items")
    assert detail["request_body"]["schema"] == {"name": "string — Name", "qty?": "integer — Qty"}
    by_path = _doc(endpoint="/api/v1/items/42")
    assert by_path["endpoint"] == "GET /api/v1/items/{item_id}"
    assert {p["name"] for p in by_path["parameters"]} == {"item_id", "verbose"}
    assert _doc(endpoint="GET /items/latest")["endpoint"] == "GET /api/v1/items/latest"
    assert _doc(endpoint="POST /api/v1/agent/chat")["callable"] is False


def test_in_process_calls_share_the_serving_app_state(fake_api):
    serving = FastAPI()
    serving.state.marker = "serving"
    internal.register_running_app(serving)
    try:
        assert internal.get_internal_app().state.marker == "serving"
    finally:
        internal.register_running_app(None)


def test_real_api_document_covers_plan_watchlist_and_excluded_routes_exist():
    spec = internal.get_openapi_spec()
    for method, path in internal.EXCLUDED_ROUTES:
        assert method.lower() in spec["paths"][path], (method, path)
    plan = _doc(group="DailyPlan")["endpoints"]
    assert any(line.startswith("GET /api/v1/plan/account") for line in plan)
    watch = _doc(endpoint="GET /api/v1/stocks/watchlist")
    assert watch["endpoint"] == "GET /api/v1/stocks/watchlist" and "response" in watch
    assert len(str(_doc())) < api_tools._DOC_MAX_CHARS


def test_only_the_chat_registry_gets_api_tools():
    from src.agent.factory import get_chat_tool_registry, get_tool_registry

    shared = get_tool_registry()
    chat = get_chat_tool_registry()
    assert shared.resolve("call_api") is None and shared.resolve("read_api_doc") is None
    assert chat.resolve("call_api") is not None and chat.resolve("read_api_doc") is not None
    assert set(shared.list_names()) < set(chat.list_names())
    assert get_chat_tool_registry() is chat


def test_prompt_points_to_the_api_document_only_with_api_tools():
    from src.agent import executor
    from src.agent.stock_scope import StockScopeResolution

    def prompt(api_access):
        with patch.object(executor, "build_agent_chat_context_bundle") as bundle:
            bundle.return_value = SimpleNamespace(context_messages=[])
            return executor.prepare_agent_chat(
                message="我现在的自选股有哪些", session_id="s1", context={}, config=SimpleNamespace(),
                context_llm_adapter=MagicMock(), skill_instructions="", default_skill_policy="",
                use_legacy_default_prompt=False, use_codex_prompt=False, include_provider_trace=True,
                scope_resolution=StockScopeResolution({}, None), api_access=api_access,
            ).system_prompt

    assert "read_api_doc" in prompt(True) and "call_api" in prompt(True)
    assert "read_api_doc" not in prompt(False)
