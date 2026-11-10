# -*- coding: utf-8 -*-
"""Multi-user P2b-1: a non-primary user's model / search calls use only that user's own keys.

The process environment is filled with provider keys, a channel and a search key like a real server
(``set_system_provider_env``). LiteLLM is patched at its module boundary (``completion`` / ``acompletion`` /
``Router``, here and in the two modules that import ``Router`` by name): the tests assert it is never reached
when a user has no model of their own, and that it only ever sees the user's key and endpoint otherwise. No
network, no real model.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.llm.credentials_guard import ModelKeyRequiredError
from src.services.user_config_service import get_user_config, has_usable_model, require_usable_model
from tests.multi_user_helpers import (
    use_real_litellm,
    PRIMARY_ID,
    SYSTEM_PROVIDER_ENV,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    set_system_provider_env,
    stub_dns,
    teardown_multi_user_env,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _response(text="OK", prompt=11, completion=5):
    message = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion),
        model="openai/mine-model",
    )


class Boundary:
    """Everything that would reach LiteLLM, recorded."""

    def __init__(self, text="OK"):
        self.text = text
        self.completion_calls = []
        self.router_model_lists = []
        self.router_calls = []
        self.async_calls = []

    @property
    def touched(self) -> bool:
        return bool(self.completion_calls or self.router_model_lists or self.router_calls or self.async_calls)

    def seen(self) -> str:
        return repr((self.completion_calls, self.router_model_lists, self.router_calls, self.async_calls))

    def completion(self, **kwargs):
        self.completion_calls.append(kwargs)
        return _response(self.text)

    async def acompletion(self, **kwargs):
        self.async_calls.append(kwargs)
        return _response(self.text)

    def router_class(self):
        boundary = self

        class FakeRouter:
            def __init__(self, model_list=None, **kwargs):
                boundary.router_model_lists.append(model_list)

            def completion(self, **kwargs):
                boundary.router_calls.append(kwargs)
                return _response(boundary.text)

        return FakeRouter


@pytest.fixture
def mu(tmp_path, monkeypatch):
    use_real_litellm(monkeypatch)
    app = build_multi_user_app(tmp_path, monkeypatch)
    set_system_provider_env(monkeypatch)
    stub_dns(monkeypatch)
    boundary = Boundary()
    router = boundary.router_class()
    monkeypatch.setattr("litellm.completion", boundary.completion)
    # raising=False: tests/litellm_stub.py (installed by earlier test modules) has no acompletion.
    monkeypatch.setattr("litellm.acompletion", boundary.acompletion, raising=False)
    monkeypatch.setattr("litellm.Router", router)
    monkeypatch.setattr("src.analyzer.Router", router)
    monkeypatch.setattr("src.agent.llm_adapter.Router", router)
    app.boundary = boundary
    yield app
    teardown_multi_user_env()


def _assert_system_keys_never_reach_litellm(boundary):
    seen = boundary.seen()
    for name, value in SYSTEM_PROVIDER_ENV.items():
        if name.startswith("LLM_SYSCH_BASE") or name in {"LLM_CHANNELS", "LLM_SYSCH_PROTOCOL", "LLM_SYSCH_MODELS", "LITELLM_MODEL"}:
            continue
        assert value not in seen, name
    assert "sys.llm.example" not in seen and "sys-model" not in seen


def _is_model_key_required(response):
    return response.status_code == 400 and response.json()["error"] == "model_key_required"


# ===================================================================== no key of their own -> refused


def test_system_environment_really_holds_provider_keys(mu):
    from src.config import get_config

    system = get_config()
    assert has_usable_model(system) and system.llm_model_list
    assert not has_usable_model(get_user_config(USER_B))


def test_analysis_submit_is_refused_without_an_own_model(mu):
    client = mu.as_user(USER_B)
    with patch("api.v1.endpoints.analysis.get_task_queue") as queue:
        asynchronous = client.post("/api/v1/analysis/analyze", json={"stock_code": "600519", "async_mode": True})
        synchronous = client.post("/api/v1/analysis/analyze", json={"stock_code": "600519", "async_mode": False})

    assert _is_model_key_required(asynchronous), asynchronous.text
    assert _is_model_key_required(synchronous), synchronous.text
    queue.assert_not_called()
    assert not mu.boundary.touched


def test_agent_entry_points_are_refused_without_an_own_model(mu):
    client = mu.as_user(USER_B)

    responses = {
        "chat": client.post("/api/v1/agent/chat", json={"message": "hello"}),
        "stream": client.post("/api/v1/agent/chat/stream", json={"message": "hello"}),
        "research": client.post("/api/v1/agent/research", json={"question": "why"}),
        "compact": client.post("/api/v1/agent/chat/sessions/s1/compact"),
    }

    for name, response in responses.items():
        assert _is_model_key_required(response), (name, response.status_code, response.text)
    assert not mu.boundary.touched


def test_plan_run_is_refused_without_an_own_model_but_existing_plans_stay_viewable(mu):
    client = mu.as_user(USER_B)

    assert _is_model_key_required(client.post("/api/v1/plan/runs"))
    assert client.get("/api/v1/plan/account").status_code == 200
    assert client.get("/api/v1/plan/runs/latest").status_code == 200
    assert not mu.boundary.touched


def test_image_extraction_is_refused_without_an_own_model(mu):
    response = mu.as_user(USER_B).post(
        "/api/v1/stocks/extract-from-image", files={"file": ("shot.png", PNG, "image/png")},
    )

    assert _is_model_key_required(response), response.text
    assert not mu.boundary.touched


def test_position_analysis_is_refused_without_an_own_model(mu):
    client = mu.as_user(USER_B)
    account = client.post("/api/v1/portfolio/accounts", json={"name": "B", "market": "cn", "base_currency": "CNY"}).json()
    trade = client.post("/api/v1/portfolio/trades", json={
        "account_id": account["id"], "symbol": "600519", "trade_date": "2026-01-02", "side": "buy",
        "quantity": 10, "price": 100,
    })
    assert trade.status_code == 200, trade.text
    queue = MagicMock()

    with patch("api.v1.endpoints.portfolio.get_task_queue", return_value=queue), patch(
        "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price", return_value=(None, None),
    ):
        response = client.post("/api/v1/portfolio/positions/600519/analysis", json={"account_id": account["id"]})

    assert _is_model_key_required(response), response.text
    queue.submit_tasks_batch.assert_not_called()


def test_model_test_is_refused_without_an_own_model(mu):
    response = mu.as_user(USER_B).post("/api/v1/me/settings/test-model")

    assert _is_model_key_required(response)
    assert not mu.boundary.touched


def test_viewing_existing_data_still_works_without_a_model(mu):
    client = mu.as_user(USER_B)

    for path in ("/api/v1/me", "/api/v1/me/settings", "/api/v1/agent/chat/sessions", "/api/v1/plan/account",
                 "/api/v1/stocks/watchlist", "/api/v1/portfolio/accounts", "/api/v1/agent/models",
                 "/api/v1/agent/status", "/api/v1/me/usage"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/v1/me").json()["model_key_configured"] is False
    # the model list shown to a personal account never reveals the system's models
    models = client.get("/api/v1/agent/models").json()
    assert models["models"] == [] and "sys-model" not in json.dumps(models)


def test_a_user_with_only_a_search_key_or_other_prefs_is_still_refused(mu):
    from src.services.user_settings_service import UserSettingsService

    UserSettingsService().set_settings(USER_B, {"TAVILY_API_KEYS": "tvly-bob-search-key", "REPORT_LANGUAGE": "ko"})

    assert _is_model_key_required(mu.as_user(USER_B).post("/api/v1/agent/chat", json={"message": "hi"}))
    assert not mu.boundary.touched


def test_note_proposals_need_no_model_and_are_not_gated(mu):
    """Storing a proposal calls no model (the Agent proposes during chat; the API only stores): keep it open."""
    response = mu.as_user(USER_B).get("/api/v1/agent/notes/proposals")
    assert response.status_code == 200
    assert not mu.boundary.touched


# ===================================================================== primary user unchanged


def test_primary_user_passes_the_gate_on_the_system_config(mu):
    from src.config import get_config

    assert get_user_config(PRIMARY_ID) is get_config()
    assert require_usable_model(get_config()) is get_config()


def test_primary_user_analysis_submit_is_unchanged(mu):
    queue = MagicMock()
    queue.submit_tasks_batch.return_value = (
        [SimpleNamespace(task_id="t1", trace_id="t1", stock_code="600519", analysis_phase="auto", asset_type=None)], [],
    )
    with patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
        response = mu.as_user(PRIMARY_ID).post(
            "/api/v1/analysis/analyze", json={"stock_code": "600519", "async_mode": True},
        )

    assert response.status_code == 202, response.text
    kwargs = queue.submit_tasks_batch.call_args.kwargs
    assert "user_id" not in kwargs and kwargs["notify"] is True  # same call as before multi-user


def test_primary_user_model_test_uses_the_system_channel(mu):
    response = mu.as_user(PRIMARY_ID).post("/api/v1/me/settings/test-model")

    assert response.status_code == 200 and response.json()["success"] is True
    call = mu.boundary.completion_calls[0]
    assert call["api_key"] == "sk-SYSTEM-channel-key" and call["api_base"] == "https://sys.llm.example/v1"


def test_primary_user_agent_search_service_is_the_shared_singleton(mu):
    from src.agent.tools.search_tools import _acting_search_service
    from src.search_service import get_search_service

    assert _acting_search_service(PRIMARY_ID) is get_search_service()
    assert _acting_search_service(None) is get_search_service()
    assert get_search_service().is_available  # the system TAVILY key


# ===================================================================== with their own key: only theirs reaches LiteLLM


def test_model_test_sends_the_users_key_and_endpoint_only(mu):
    own = give_user_model(USER_B, key="sk-bob-own-key-0001")

    response = mu.as_user(USER_B).post("/api/v1/me/settings/test-model")

    assert response.status_code == 200 and response.json()["success"] is True
    assert len(mu.boundary.completion_calls) == 1
    call = mu.boundary.completion_calls[0]
    assert (call["api_key"], call["api_base"], call["model"]) == (own["api_key"], own["api_base"], own["model"])
    _assert_system_keys_never_reach_litellm(mu.boundary)
    assert own["api_key"] not in response.text


def test_model_test_failure_is_reported_without_leaking_the_key(mu):
    give_user_model(USER_B, key="sk-bob-own-key-0001")

    def boom(**kwargs):
        raise RuntimeError("401 invalid api key sk-bob-own-key-0001 for https://llm.mine.example/v1")

    with patch("litellm.completion", boom):
        body = mu.as_user(USER_B).post("/api/v1/me/settings/test-model").json()

    assert body["success"] is False and body["error_code"] == "model_call_failed"
    assert "sk-bob-own-key-0001" not in json.dumps(body)


def test_analyzer_router_holds_only_the_users_deployments_and_usage_is_recorded_for_them(mu):
    from src.analyzer import GeminiAnalyzer
    from src.storage import LLMUsage

    own = give_user_model(USER_B, key="sk-bob-own-key-0001")
    view = get_user_config(USER_B)

    text = GeminiAnalyzer(config=view).generate_text_with_models("ping", call_type="light_task")

    assert text == "OK"
    assert mu.boundary.router_calls and not mu.boundary.completion_calls
    deployments = [entry["litellm_params"] for models in mu.boundary.router_model_lists for entry in models]
    assert deployments and {d["api_key"] for d in deployments} == {own["api_key"]}
    assert {d["api_base"] for d in deployments} == {own["api_base"]}
    _assert_system_keys_never_reach_litellm(mu.boundary)
    with mu.db.get_session() as session:
        rows = session.query(LLMUsage).all()
    assert [(row.user_id, row.call_type) for row in rows] == [(USER_B, "light_task")]


def test_system_jobs_and_the_primary_user_record_usage_under_the_primary_user(mu):
    from src.analyzer import GeminiAnalyzer
    from src.config import get_config
    from src.storage import LLMUsage

    GeminiAnalyzer(config=get_config()).generate_text_with_models("ping", call_type="light_task")

    with mu.db.get_session() as session:
        assert [row.user_id for row in session.query(LLMUsage).all()] == [PRIMARY_ID]


def test_analyzer_never_falls_back_to_provider_variables_for_a_personal_account(mu):
    """A fallback model without a key of its own would make LiteLLM read GROQ_API_KEY & co. from the environment."""
    from src.analyzer import GeminiAnalyzer

    give_user_model(USER_B)
    view = get_user_config(USER_B)
    analyzer = GeminiAnalyzer(config=view)

    with pytest.raises(ModelKeyRequiredError):
        analyzer._dispatch_litellm_completion(
            "groq/llama-3.3-70b", {"model": "groq/llama-3.3-70b", "messages": []},
            config=view, use_channel_router=False, router_model_names=set(),
        )
    assert not (mu.boundary.completion_calls or mu.boundary.router_calls or mu.boundary.async_calls)
    # the same direct call is allowed for the primary user, whose configuration is the environment
    from src.config import get_config

    system_analyzer = GeminiAnalyzer(config=get_config())
    system_analyzer._dispatch_litellm_completion(
        "groq/llama-3.3-70b", {"model": "groq/llama-3.3-70b", "messages": []},
        config=get_config(), use_channel_router=False, router_model_names=set(),
    )
    assert len(mu.boundary.completion_calls) == 1


def test_agent_adapter_uses_the_users_deployments_and_records_usage_for_them(mu):
    from src.agent.llm_adapter import LLMToolAdapter
    from src.agent.runner import run_agent_loop
    from src.agent.tools.registry import ToolRegistry
    from src.storage import LLMUsage

    own = give_user_model(USER_B, key="sk-bob-own-key-0001")
    adapter = LLMToolAdapter(get_user_config(USER_B))

    result = run_agent_loop(
        messages=[{"role": "user", "content": "hi"}], tool_registry=ToolRegistry(), llm_adapter=adapter,
        max_steps=2, user_id=USER_B,
    )

    assert result.success, result.error
    deployments = [entry["litellm_params"] for models in mu.boundary.router_model_lists for entry in models]
    assert {d["api_key"] for d in deployments} == {own["api_key"]}
    _assert_system_keys_never_reach_litellm(mu.boundary)
    with mu.db.get_session() as session:
        assert [row.user_id for row in session.query(LLMUsage).all()] == [USER_B]


def test_agent_adapter_never_falls_back_to_provider_variables_for_a_personal_account(mu):
    from src.agent.llm_adapter import LLMToolAdapter

    give_user_model(USER_B)
    adapter = LLMToolAdapter(get_user_config(USER_B))

    with pytest.raises(ModelKeyRequiredError):
        adapter._call_litellm_model([{"role": "user", "content": "hi"}], [], "groq/llama-3.3-70b")
    assert not (mu.boundary.completion_calls or mu.boundary.router_calls or mu.boundary.async_calls)


def test_vision_uses_the_users_key_and_refuses_a_user_without_one(mu):
    from src.services.image_stock_extractor import extract_stock_codes_from_image

    mu.boundary.text = '[{"code":"600519","name":"贵州茅台","confidence":"high"}]'
    own = give_user_model(USER_B, key="sk-bob-own-key-0001")

    items, _raw = extract_stock_codes_from_image(PNG, "image/png", config=get_user_config(USER_B))

    assert items == [("600519", "贵州茅台", "high")]
    call = mu.boundary.completion_calls[0]
    assert (call["api_key"], call["api_base"]) == (own["api_key"], own["api_base"])
    _assert_system_keys_never_reach_litellm(mu.boundary)

    mu.boundary.completion_calls.clear()
    with pytest.raises(ModelKeyRequiredError):
        extract_stock_codes_from_image(PNG, "image/png", config=get_user_config(USER_A))
    assert not mu.boundary.touched


def test_image_extraction_endpoint_runs_on_the_users_key(mu):
    mu.boundary.text = '[{"code":"600519","name":"贵州茅台","confidence":"high"}]'
    own = give_user_model(USER_B, key="sk-bob-own-key-0001")

    response = mu.as_user(USER_B).post(
        "/api/v1/stocks/extract-from-image", files={"file": ("shot.png", PNG, "image/png")},
    )

    assert response.status_code == 200 and response.json()["codes"] == ["600519"], response.text
    assert mu.boundary.completion_calls[0]["api_key"] == own["api_key"]
    _assert_system_keys_never_reach_litellm(mu.boundary)


def test_daily_plan_model_comes_from_the_users_view_and_is_off_without_one(mu):
    from src.services.daily_plan_service import DailyPlanService

    service = DailyPlanService(user_id=USER_B)
    assert service.config.user_view_id == USER_B and service._llm() is None  # rule plans only, never the system model

    own = give_user_model(USER_B, key="sk-bob-own-key-0001")
    llm = DailyPlanService(user_id=USER_B)._llm()
    assert llm is not None
    assert llm("prompt", "system") == "OK"
    deployments = [entry["litellm_params"] for models in mu.boundary.router_model_lists for entry in models]
    assert {d["api_key"] for d in deployments} == {own["api_key"]}
    _assert_system_keys_never_reach_litellm(mu.boundary)
    # the plan rules come from the user's view as well
    from src.services.user_settings_service import UserSettingsService

    UserSettingsService().set_settings(USER_B, {"POSITION_MAX_HOLDINGS": "2"})
    assert DailyPlanService(user_id=USER_B).config.position_max_holdings == 2


# ===================================================================== search keys


def test_search_uses_the_users_keys_and_none_means_no_realtime_search(mu):
    from src.agent.tools.search_tools import _acting_search_service
    from src.search_service import get_search_service
    from src.services.user_settings_service import UserSettingsService

    assert get_search_service().is_available  # the system service has the system Tavily key
    without = _acting_search_service(USER_B)
    assert without is not get_search_service() and without.is_available is False

    UserSettingsService().set_settings(USER_B, {"TAVILY_API_KEYS": "tvly-bob-search-key"})
    with_key = _acting_search_service(USER_B)
    assert with_key.is_available and with_key is _acting_search_service(USER_B)  # cached per view
    keys = {key for provider in with_key._providers for key in getattr(provider, "_api_keys", [])}
    assert "tvly-bob-search-key" in keys and "tvly-SYSTEM-search-key" not in keys
    assert _acting_search_service(USER_A).is_available is False  # other users unaffected


def test_search_tool_answers_no_engine_for_a_user_without_search_keys(mu):
    from src.agent.tools.search_tools import _handle_search_stock_news

    result = _handle_search_stock_news("600519", "贵州茅台", user_id=USER_B)

    assert "No search engine available" in result["error"]


def test_unsafe_searxng_instance_is_dropped_at_call_time(mu, monkeypatch):
    from src.search_service import searxng_urls_for_config
    from src.services.user_settings_service import UserSettingsService

    stub_dns(monkeypatch, {"searx.bob.example": "93.184.216.34"})
    UserSettingsService().set_settings(USER_B, {"SEARXNG_BASE_URLS": "https://searx.bob.example"})
    view = get_user_config(USER_B)
    assert searxng_urls_for_config(view) == ["https://searx.bob.example"]

    stub_dns(monkeypatch, {"searx.bob.example": "10.0.0.7"})  # rebound after saving
    assert searxng_urls_for_config(view) == []


def test_pipeline_builds_its_search_service_from_the_requesters_view(mu, monkeypatch):
    """Real pipeline over the user's view: no search keys -> realtime search is simply unavailable (no error)."""
    import src.config
    import src.core.pipeline
    from src.core.pipeline import StockAnalysisPipeline

    # If an earlier module first imported src.core.pipeline while src.config.get_config was patched
    # (tests/test_analysis_api_contract.py does), the pipeline keeps that mock bound; use the real one.
    monkeypatch.setattr(src.core.pipeline, "get_config", src.config.get_config)
    give_user_model(USER_B)
    personal = StockAnalysisPipeline(config=get_user_config(USER_B), user_id=USER_B)
    system = StockAnalysisPipeline()

    assert personal.user_id == USER_B and system.user_id == PRIMARY_ID
    assert personal.search_service is not None and personal.search_service.is_available is False
    assert system.search_service.is_available is True  # the system Tavily key


# ===================================================================== task, service and pipeline carry the user


def test_task_queue_records_the_requester_and_the_worker_uses_it(mu):
    from src.services.task_queue import AnalysisTaskQueue

    queue = AnalysisTaskQueue(max_workers=1)
    submitted = []
    queue._executor = SimpleNamespace(submit=lambda *a, **k: submitted.append(a) or MagicMock(), shutdown=lambda **k: None)
    try:
        accepted, _dups = queue.submit_tasks_batch(["600519"], user_id=USER_B)
        primary, _ = queue.submit_tasks_batch(["000001"])
        assert accepted[0].user_id == USER_B and primary[0].user_id is None
        assert "user_id" not in accepted[0].to_dict()  # never serialized to clients

        service = MagicMock()
        service.analyze_stock.return_value = {"stock_name": "x"}
        with patch("src.services.analysis_service.AnalysisService", return_value=service):
            queue._execute_task(accepted[0].task_id, "600519", "detailed", False, False)
            queue._execute_task(primary[0].task_id, "000001", "detailed", False, True)
        assert service.analyze_stock.call_args_list[0].kwargs["user_id"] == USER_B
        assert "user_id" not in service.analyze_stock.call_args_list[1].kwargs
    finally:
        AnalysisTaskQueue._instance = None


def test_analysis_service_runs_a_personal_account_on_its_own_view(mu):
    from src.services.analysis_service import AnalysisService

    pipelines = []

    class SpyPipeline:
        def __init__(self, **kwargs):
            pipelines.append(kwargs)

        def process_single_stock(self, **kwargs):
            pipelines[-1]["process"] = kwargs
            return None

    with patch("src.core.pipeline.StockAnalysisPipeline", SpyPipeline):
        with pytest.raises(ModelKeyRequiredError):
            AnalysisService().analyze_stock("600519", user_id=USER_B)
        assert pipelines == []  # refused before a pipeline (and its analyzer / search service) exists

        give_user_model(USER_B)
        AnalysisService().analyze_stock("600519", user_id=USER_B, send_notification=True, report_language="en")
        AnalysisService().analyze_stock("600519")

    personal, system = pipelines
    assert personal["config"].user_view_id == USER_B and personal["user_id"] == USER_B
    assert personal["process"]["single_stock_notify"] is False  # no system push channel for a personal account
    assert personal["config"].report_language == "en"
    from src.config import get_config

    assert system["config"] is get_config() and "user_id" not in system and system["process"]["single_stock_notify"] is True


def test_pipeline_refuses_a_user_that_does_not_match_its_config(mu):
    from src.config import get_config
    from src.core.pipeline import StockAnalysisPipeline

    give_user_model(USER_B)
    with pytest.raises(ValueError):
        StockAnalysisPipeline(config=get_config(), user_id=USER_B)  # system keys for a personal account
    with pytest.raises(ValueError):
        StockAnalysisPipeline(config=get_user_config(USER_B), user_id=USER_A)  # someone else's view


def test_agent_mode_analysis_builds_the_executor_for_the_requesting_user(mu):
    from src.core.pipeline import StockAnalysisPipeline

    give_user_model(USER_B)
    pipeline = object.__new__(StockAnalysisPipeline)
    pipeline.config = get_user_config(USER_B)
    pipeline.user_id = USER_B
    pipeline.analysis_skills = None
    recorded = {}

    def fake_build(config, skills=None, user_id=None):
        recorded.update(config=config, user_id=user_id)
        raise RuntimeError("stop after building the executor")

    with patch("src.agent.factory.build_agent_executor", fake_build):
        pipeline._analyze_with_agent("600519", MagicMock(value="detailed"), "q1", "贵州茅台", None, None)

    assert recorded["user_id"] == USER_B and recorded["config"].user_view_id == USER_B


def test_agent_mode_analysis_reads_the_requesters_portfolio_not_the_primarys(mu):
    """build_agent_executor(view, user_id) -> real tool registry: get_portfolio_snapshot answers for user B."""
    from src.agent.factory import build_agent_executor
    from src.agent.llm_adapter import LLMResponse, ToolCall
    from src.services.portfolio_service import PortfolioService

    PortfolioService(user_id=PRIMARY_ID).create_account(name="primary-acct", broker=None, market="cn", base_currency="CNY")
    PortfolioService(user_id=USER_B).create_account(name="b-acct", broker=None, market="cn", base_currency="CNY")
    give_user_model(USER_B)

    class ScriptedAdapter:
        def __init__(self, config=None):
            self._config = config
            self.calls = 0

        def call_with_tools(self, messages, tools, provider=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    tool_calls=[ToolCall(id="c1", name="get_portfolio_snapshot", arguments={"include_risk": False})],
                    provider="fake", model="fake",
                )
            return LLMResponse(content="done", provider="fake", model="fake")

    with patch("src.agent.llm_adapter.LLMToolAdapter", ScriptedAdapter):
        executor = build_agent_executor(get_user_config(USER_B), None, user_id=USER_B)
        result = executor.run("analyse 600519", {"stock_code": "600519"})

    assert [entry["tool"] for entry in result.tool_calls_log] == ["get_portfolio_snapshot"]
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert tool_messages
    rendered = json.dumps(tool_messages, ensure_ascii=False, default=str)
    assert "b-acct" in rendered and "primary-acct" not in rendered


def test_generation_backend_of_a_personal_account_is_litellm_even_when_the_system_uses_a_local_cli(mu, monkeypatch):
    from src.analyzer import GeminiAnalyzer
    from src.agent.agent_backend import resolve_agent_backend_id
    from src.agent.llm_adapter import LLMToolAdapter
    from src.config import Config

    monkeypatch.setenv("GENERATION_BACKEND", "codex_cli")
    monkeypatch.setenv("AGENT_BACKEND", "codex_app_server")
    Config.reset_instance()
    from src.services.user_config_service import invalidate_user_config

    invalidate_user_config()
    give_user_model(USER_B)
    view = get_user_config(USER_B)

    assert GeminiAnalyzer(config=view)._resolve_generation_backend_config()[0] == "litellm"
    assert resolve_agent_backend_id(view) == "litellm"
    assert LLMToolAdapter(view)._generation_backend_id == "litellm"


# --- keyless providers (Ollama) must point at the user's own public endpoint -------------------

class _UserView:
    """Minimal stand-in for a per-user Config view (``user_view_id`` is declared on the class)."""

    user_view_id = ""  # Config declares it on the class (a property); config_user_id checks that

    def __init__(self, deployments):
        self.user_view_id = "bob"
        self.llm_model_list = deployments
        self.openai_base_url = ""


@pytest.mark.parametrize("params", [
    {"model": "ollama/llama3"},
    {"model": "ollama_chat/qwen2", "api_base": ""},
])
def test_keyless_deployment_without_own_endpoint_is_refused(monkeypatch, params):
    from src.llm.credentials_guard import validate_user_deployments

    stub_dns(monkeypatch)
    with pytest.raises(ModelKeyRequiredError):
        validate_user_deployments(_UserView([{"model_name": "m", "litellm_params": params}]))


def test_keyless_deployment_pointing_at_the_server_itself_is_refused(monkeypatch):
    from src.llm.credentials_guard import validate_user_deployments
    from src.utils.url_safety import UnsafeUrlError

    stub_dns(monkeypatch)
    for base in ("http://localhost:11434", "https://127.0.0.1:11434", "https://10.0.0.5/v1"):
        view = _UserView([{"model_name": "m", "litellm_params": {"model": "ollama/llama3", "api_base": base}}])
        with pytest.raises(UnsafeUrlError):
            validate_user_deployments(view)


def test_keyless_deployment_with_own_public_endpoint_is_allowed(monkeypatch):
    from src.llm.credentials_guard import require_explicit_credentials, validate_user_deployments

    stub_dns(monkeypatch)
    params = {"model": "ollama/llama3", "api_base": "https://ollama.mine.example"}
    view = _UserView([{"model_name": "m", "litellm_params": params}])
    validate_user_deployments(view)
    require_explicit_credentials(view, dict(params), model=params["model"])


def test_direct_keyless_call_without_own_endpoint_is_refused(monkeypatch):
    from src.llm.credentials_guard import require_explicit_credentials

    stub_dns(monkeypatch)
    with pytest.raises(ModelKeyRequiredError):
        require_explicit_credentials(_UserView([]), {"model": "ollama/llama3"}, model="ollama/llama3")
