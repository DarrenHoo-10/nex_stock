# -*- coding: utf-8 -*-
"""Multi-user P2b-2: shared report reuse and the model channel fingerprint (offline, LiteLLM never reached).

A report is reused when the stock, trading date, model channel (provider + Base URL + model name), effective
strategy set, report language and report type all match and the stored report is shared. A reuse hit calls no model
and needs no key; a miss without a key is refused with ``model_key_required``; ``force_regenerate`` always generates.
(The strategy rules have their own module: tests/test_multi_user_reuse_strategy_metadata.py.)
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.agent.factory import compute_strategy_fingerprint
from src.analyzer import AnalysisResult
from src.core.trading_calendar import MarketPhase, get_effective_trading_date
from src.enums import ReportType
from src.llm.channel_fingerprint import (
    compute_model_channel_fingerprint,
    config_model_channel_fingerprint,
    normalize_base_url,
)
from src.services.user_config_service import get_user_config
from src.services.user_settings_service import UserSettingsService
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
from tests.test_multi_user_p2b_model_keys import Boundary

ANALYZE = "/api/v1/analysis/analyze"
SYSTEM_BASE = "https://sys.llm.example/v1"
USER_C = "uid-user-c"  # a user who declared the same channel but has no key
REPORT_TYPE = ReportType.from_str("detailed").value


def _is_model_key_required(response):
    return response.status_code == 400 and response.json()["error"] == "model_key_required"


# ===================================================================== fingerprint


def _deployment(model, base, key="k"):
    params = {"model": model, "api_key": key}
    if base:
        params["api_base"] = base
    return {"model_name": model, "litellm_params": params}


def test_fingerprint_is_the_same_channel_regardless_of_key_and_order():
    first = compute_model_channel_fingerprint("openai/m", [_deployment("openai/m", SYSTEM_BASE, "key-1")])
    second = compute_model_channel_fingerprint(
        "openai/m", [_deployment("openai/other", "https://x/v1"), _deployment("openai/m", SYSTEM_BASE + "/", "key-2")]
    )
    assert first == second and len(first) == 32
    assert "key" not in first


def test_fingerprint_differs_by_base_url_provider_and_model_name():
    base = compute_model_channel_fingerprint("openai/m", [_deployment("openai/m", SYSTEM_BASE)])
    other_base = compute_model_channel_fingerprint("openai/m", [_deployment("openai/m", "https://unofficial.example/v1")])
    other_model = compute_model_channel_fingerprint("openai/n", [_deployment("openai/n", SYSTEM_BASE)])
    other_provider = compute_model_channel_fingerprint("deepseek/m", [_deployment("deepseek/m", SYSTEM_BASE)])
    no_base = compute_model_channel_fingerprint("openai/m", [_deployment("openai/m", None)])

    assert len({base, other_base, other_model, other_provider, no_base}) == 5
    assert compute_model_channel_fingerprint("", []) is None


def test_base_url_normalisation_ignores_case_default_port_and_trailing_slash():
    assert normalize_base_url("HTTPS://Sys.LLM.example:443/v1/") == normalize_base_url("https://sys.llm.example/v1")
    assert normalize_base_url("https://a.example/v1") != normalize_base_url("https://a.example/v2")
    assert normalize_base_url("") == "" and normalize_base_url(None) == ""


def test_a_declared_but_keyless_channel_gets_the_same_fingerprint_as_the_keyed_one():
    keyed = compute_model_channel_fingerprint("openai/m", [_deployment("openai/m", SYSTEM_BASE)])
    declared = compute_model_channel_fingerprint("openai/m", [], {"openai": SYSTEM_BASE})
    assert keyed == declared
    assert declared != compute_model_channel_fingerprint("openai/m", [], {"openai": "https://other.example/v1"})


def test_local_cli_generation_backends_have_no_fingerprint():
    config = SimpleNamespace(generation_backend="codex_cli", litellm_model="openai/m", llm_model_list=[])
    from src.llm.backend_registry import LOCAL_CLI_GENERATION_BACKEND_IDS

    config.generation_backend = sorted(LOCAL_CLI_GENERATION_BACKEND_IDS)[0]
    assert config_model_channel_fingerprint(config) is None


# ===================================================================== reuse over the API


@pytest.fixture
def reuse(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    set_system_provider_env(monkeypatch)
    stub_dns(monkeypatch)
    boundary = Boundary()
    monkeypatch.setattr("litellm.completion", boundary.completion)
    monkeypatch.setattr("litellm.Router", boundary.router_class())
    monkeypatch.setattr("src.analyzer.Router", boundary.router_class())
    # market closed: the in-session guard is covered by its own test (the real answer depends on the wall clock)
    monkeypatch.setattr("src.services.report_reuse_service.infer_market_phase", lambda *a, **k: MarketPhase.POSTMARKET)

    from src.config import get_config

    app.system_fingerprint = config_model_channel_fingerprint(get_config())
    app.default_strategy = compute_strategy_fingerprint(get_config())  # a plain request: no skills, no AGENT_SKILLS
    app.boundary = boundary
    app.trade_date = get_effective_trading_date("cn").isoformat()
    give_user_model(USER_B, model="sys-model", base_url=SYSTEM_BASE)  # same channel as the system's, own key
    UserSettingsService().set_settings(USER_C, {"LITELLM_MODEL": "openai/sys-model", "OPENAI_BASE_URL": SYSTEM_BASE})
    yield app
    teardown_multi_user_env()


_DEFAULT = object()


def _save_report(app, *, query_id="q-reuse-src", code="600519", fingerprint=None, private=False, report_type=REPORT_TYPE,
                 language="zh", trade_date=None, requester=USER_A, strategy=_DEFAULT):
    result = AnalysisResult(
        code=code, name="Moutai", sentiment_score=70, trend_prediction="up", operation_advice="buy",
        analysis_summary="the shared report", model_used="openai/sys-model", report_language=language,
    )
    return app.db.save_analysis_history(
        result=result, query_id=query_id, report_type=report_type, news_content="n", context_snapshot=None,
        save_snapshot=False, requester_user_id=requester, is_private=private,
        model_channel_fingerprint=fingerprint or app.system_fingerprint,
        report_trade_date=trade_date or app.trade_date, report_language=language,
        skill_fingerprint=app.default_strategy if strategy is _DEFAULT else strategy,
    )


def _fake_pipeline(generated):
    class FakePipeline:
        def __init__(self, *args, **kwargs):
            generated.append(kwargs)

        def process_single_stock(self, code, **kwargs):
            return AnalysisResult(
                code=code, name="Moutai", sentiment_score=60, trend_prediction="flat", operation_advice="hold",
                analysis_summary="fresh report", model_used="openai/mine-model",
            )

    return patch("src.core.pipeline.StockAnalysisPipeline", FakePipeline)


def _sync(app, user, **body):
    payload = {"stock_code": "600519", "async_mode": False, **body}
    return app.as_user(user).post(ANALYZE, json=payload)


def test_keyless_user_gets_the_existing_report_without_a_model_call(reuse):
    record_id = _save_report(reuse)
    assert not get_user_config(USER_C).llm_model_list  # really no key

    generated = []
    with _fake_pipeline(generated):
        response = _sync(reuse, USER_C)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reused"] is True and body["reused_report_id"] == record_id
    assert body["query_id"] == "q-reuse-src" and body["report"]["summary"]["analysis_summary"] == "the shared report"
    assert generated == [] and not reuse.boundary.touched  # no pipeline, no LiteLLM


def test_user_with_a_key_on_the_same_channel_reuses_too_and_nothing_is_billed(reuse):
    _save_report(reuse)
    generated = []
    with _fake_pipeline(generated):
        response = _sync(reuse, USER_B)

    assert response.status_code == 200 and response.json()["reused"] is True
    assert generated == [] and not reuse.boundary.touched


def test_async_reuse_completes_the_task_from_the_existing_report(reuse):
    from src.services.task_queue import AnalysisTaskQueue, get_task_queue

    _save_report(reuse)
    original = AnalysisTaskQueue._instance
    AnalysisTaskQueue._instance = None
    try:
        queue = get_task_queue()
        generated = []
        with _fake_pipeline(generated), patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
            accepted = reuse.as_user(USER_C).post(ANALYZE, json={"stock_code": "600519", "async_mode": True})
            assert accepted.status_code == 202, accepted.text
            task_id = accepted.json()["task_id"]
            queue._futures[task_id].result(timeout=20)
            status = reuse.as_user(USER_C).get(f"/api/v1/analysis/status/{task_id}")
        assert status.status_code == 200
        result = status.json()["result"]
        assert status.json()["status"] == "completed" and result["reused"] is True
        assert generated == [] and not reuse.boundary.touched
    finally:
        queue.shutdown()
        AnalysisTaskQueue._instance = original


def test_a_miss_without_a_key_is_still_refused(reuse):
    _save_report(reuse)  # a report of another stock exists, not of this one
    generated = []
    with _fake_pipeline(generated):
        sync = _sync(reuse, USER_C, stock_code="000858")
        asynchronous = reuse.as_user(USER_C).post(ANALYZE, json={"stock_code": "000858", "async_mode": True})
        mixed = reuse.as_user(USER_C).post(
            ANALYZE, json={"stock_codes": ["600519", "000858"], "async_mode": True},
        )

    assert _is_model_key_required(sync) and _is_model_key_required(asynchronous) and _is_model_key_required(mixed)
    assert generated == [] and not reuse.boundary.touched


@pytest.mark.parametrize("mismatch", [
    {"fingerprint": "f" * 32},                      # another model channel (e.g. another Base URL)
    {"report_type": ReportType.from_str("simple").value},
    {"language": "en"},
    {"trade_date": "2000-01-03"},
    {"private": True},                               # produced with personal context: never handed to anybody else
    {"code": "000858"},
    {"strategy": "e" * 32},                         # produced with another strategy set
    {"strategy": None},                             # written before the strategy fingerprint existed: never direct
], ids=["channel", "report-type", "language", "trade-date", "private", "stock", "strategy", "legacy-null-strategy"])
def test_any_difference_means_no_reuse(reuse, mismatch):
    _save_report(reuse, **mismatch)

    with _fake_pipeline([]):
        assert _is_model_key_required(_sync(reuse, USER_C))


def test_a_different_base_url_is_a_different_channel(reuse):
    UserSettingsService().set_settings(USER_C, {"OPENAI_BASE_URL": "https://unofficial.example/v1"})
    _save_report(reuse)

    with _fake_pipeline([]):
        assert _is_model_key_required(_sync(reuse, USER_C))


def test_a_report_in_another_language_is_reused_only_for_that_language(reuse):
    _save_report(reuse, language="en")
    with _fake_pipeline([]):
        assert _is_model_key_required(_sync(reuse, USER_C))
        english = _sync(reuse, USER_C, report_language="en")
    assert english.status_code == 200 and english.json()["reused"] is True


@pytest.mark.parametrize("extra", [
    {"force_regenerate": True},
    {"force_refresh": True},
    {"analysis_phase": "intraday"},
], ids=["force_regenerate", "force_refresh", "phase-override"])
def test_force_regenerate_and_non_plain_requests_bypass_reuse_and_need_a_key(reuse, extra):
    _save_report(reuse)
    generated = []
    with _fake_pipeline(generated):
        assert _is_model_key_required(_sync(reuse, USER_C, **extra))
        with_key = _sync(reuse, USER_B, **extra)

    assert with_key.status_code == 200, with_key.text
    assert "reused" not in with_key.json() or not with_key.json()["reused"]
    assert len(generated) == 1  # generated by the user who has a key, on that user's own view
    assert generated[0]["user_id"] == USER_B


def test_nothing_is_reused_while_the_market_is_in_session(reuse, monkeypatch):
    _save_report(reuse)
    monkeypatch.setattr("src.services.report_reuse_service.infer_market_phase", lambda *a, **k: MarketPhase.INTRADAY)

    with _fake_pipeline([]):
        assert _is_model_key_required(_sync(reuse, USER_C))


def test_a_reuse_miss_with_a_key_generates_and_a_stored_report_is_never_changed(reuse):
    record_id = _save_report(reuse, fingerprint="f" * 32)
    generated = []
    with _fake_pipeline(generated):
        response = _sync(reuse, USER_B)

    assert response.status_code == 200 and not response.json().get("reused")
    assert len(generated) == 1
    with reuse.db.get_session() as session:
        from src.storage import AnalysisHistory

        assert session.get(AnalysisHistory, record_id).analysis_summary == "the shared report"


def test_primary_user_requests_that_push_a_notification_always_generate(reuse):
    """The primary user's default request asks for a push; a reused report has nothing new to push, so it generates."""
    _save_report(reuse)
    generated = []
    with _fake_pipeline(generated):
        default = _sync(reuse, PRIMARY_ID)  # notify defaults to true
        quiet = _sync(reuse, PRIMARY_ID, notify=False)

    assert default.status_code == 200 and not default.json().get("reused")
    assert len(generated) == 1
    assert quiet.status_code == 200 and quiet.json()["reused"] is True


def test_report_language_defaults_follow_the_requesters_view(reuse):
    UserSettingsService().set_settings(USER_C, {"REPORT_LANGUAGE": "en"})
    _save_report(reuse, language="en")

    with _fake_pipeline([]):
        response = _sync(reuse, USER_C)  # no explicit language: the user's own REPORT_LANGUAGE decides
    assert response.status_code == 200 and response.json()["reused"] is True


def test_new_reports_are_saved_with_the_fingerprint_of_the_model_that_answered(reuse):
    from tests.test_pipeline_market_phase_context import _make_pipeline

    pipeline = _make_pipeline(agent_mode=False)
    pipeline.config = get_user_config(USER_B)
    pipeline.user_id = USER_B
    result = SimpleNamespace(model_used="openai/sys-model", report_language="zh")

    saved = pipeline._history_ownership_kwargs(result, code="600519")

    assert saved["model_channel_fingerprint"] == reuse.system_fingerprint  # same channel as the system's
    assert saved["requester_user_id"] == USER_B and saved["report_trade_date"] == reuse.trade_date
    assert saved["skill_fingerprint"] == reuse.default_strategy  # the effective strategy set it ran with
    other = pipeline._history_ownership_kwargs(SimpleNamespace(model_used="openai/mine-model"), code="600519")
    assert other["model_channel_fingerprint"] != saved["model_channel_fingerprint"]
