# -*- coding: utf-8 -*-
"""Multi-user report reuse, second design (docs/design/multi-user.md section 7): strategy fingerprint + metadata reuse.

1. *Direct reuse* returns an existing shared report (no model call, no key) only when the stock, trading date, model
   channel, report language and type AND the effective strategy set match, there are no holdings, the market is
   closed and nothing forces a new report.
2. Everything else that has to be analysed again (holdings, another strategy, ...) uses the requester's own key but
   takes the *inputs* an earlier analysis of the same stock / trading date collected (quote, chip distribution,
   fundamentals, market structure, news evidence) instead of fetching them again; never its conclusions, never another
   user's private report.
3. Holdings are never shared: a holdings report is private, legacy Agent-mode reports are made private by a migration.

The model is mocked at LiteLLM's module boundary and the data providers / search at their client objects (no network,
no real model); everything between is the real application over a temp SQLite database.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from data_provider.realtime_types import ChipDistribution, RealtimeSource, UnifiedRealtimeQuote
from src import storage_multi_user_migration as migration
from src.agent.factory import analysis_uses_agent, compute_strategy_fingerprint
from src.analyzer import AnalysisResult
from src.core.trading_calendar import MarketPhase, get_effective_trading_date
from src.enums import ReportType
from src.llm.channel_fingerprint import config_model_channel_fingerprint
from src.services.analysis_metadata_reuse import (
    PART_CHIP,
    PART_FUNDAMENTAL,
    PART_MARKET_STRUCTURE,
    PART_NEWS,
    PART_REALTIME_QUOTE,
    load_reusable_metadata,
    strip_holding_fields,
)
from src.services.user_config_service import get_user_config
from src.services.user_settings_service import UserSettingsService
from src.storage import AnalysisHistory
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
USER_C = "uid-user-c"  # declared the shared channel, has no key
USER_D = "uid-user-d"
CODE = "600519"
REPORT_TYPE = ReportType.from_str("detailed").value
CONCLUSION_A = "ALPHA-CONCLUSION-buy-at-1688"
CONCLUSION_B = "BETA-CONCLUSION-hold"
NEWS_TEXT = "NEWS-EVIDENCE-TEXT-of-the-day"
STRUCTURE = {"summary": "STRUCTURE-CONTEXT-of-the-day"}


def _is_model_key_required(response):
    return response.status_code == 400 and response.json()["error"] == "model_key_required"


def _dashboard(summary: str) -> str:
    """A complete dashboard: nothing for the analyzer's integrity retry to ask the model again about."""
    return json.dumps({
        "stock_name": "Moutai",
        "sentiment_score": 66,
        "trend_prediction": "up",
        "operation_advice": "buy",
        "decision_type": "buy",
        "confidence_level": "high",
        "analysis_summary": summary,
        "dashboard": {
            "core_conclusion": {"one_sentence": summary},
            "intelligence": {"risk_alerts": ["none"]},
            "battle_plan": {"sniper_points": {"ideal_buy": "1600", "stop_loss": "1500", "take_profit": "1800"}},
            "phase_decision": {
                "phase_context": {"phase": "closed"}, "action_window": "next open", "immediate_action": "wait",
                "watch_conditions": ["x"], "next_check_time": "tomorrow", "confidence_reason": "r",
                "data_limitations": ["none"],
            },
        },
    })


def _model_calls(app):
    """Stock-analysis invocations of the model (not the stream attempt that falls back, not a market review)."""
    return [
        call for call in app.boundary.router_calls
        if not call.get("stream") and CODE in json.dumps(call.get("messages"), ensure_ascii=False, default=str)
    ]


# ===================================================================== the effective strategy set


def _skill_ids():
    from src.agent.factory import get_skill_manager

    return [skill.name for skill in get_skill_manager(None).list_skills()]


@pytest.fixture
def config_app(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    set_system_provider_env(monkeypatch)
    stub_dns(monkeypatch)
    yield app
    teardown_multi_user_env()


def _fp(config, skills=None):
    value = compute_strategy_fingerprint(config, skills)
    assert value and len(value) == 32
    return value


def test_the_same_skill_set_has_one_fingerprint_whatever_the_order_and_duplicates(config_app):
    from src.config import get_config

    one, two = "box_oscillation", "chan_theory"
    assert _fp(get_config(), [one, two]) == _fp(get_config(), [two, one, one])
    assert _fp(get_config(), [one]) != _fp(get_config(), [two])
    assert _fp(get_config(), [one]) != _fp(get_config(), [one, two])


def test_a_plain_request_is_not_the_same_strategy_as_an_explicit_skill_request(config_app):
    from src.config import get_config

    plain = _fp(get_config())
    assert plain != _fp(get_config(), ["box_oscillation"])
    # the default skill named explicitly runs through the Agent pipeline: not the same report as the classic one
    assert plain != _fp(get_config(), ["bull_trend"])
    assert analysis_uses_agent(get_config(), None) == (False, "")
    assert analysis_uses_agent(get_config(), ["bull_trend"]) == (True, "request_skills")


def test_unknown_request_skills_fall_back_to_the_default_like_the_analysis_does(config_app):
    from src.config import get_config

    # the pipeline switches to the Agent pipeline for any request skill, but resolves unknown ids to the default set
    assert _fp(get_config(), ["no_such_skill"]) == _fp(get_config(), ["no_such_skill", "also_unknown"])
    assert _fp(get_config(), ["no_such_skill"]) != _fp(get_config())


def test_the_users_own_agent_skills_are_part_of_the_effective_set(config_app):
    UserSettingsService().set_settings(USER_A, {"AGENT_SKILLS": "box_oscillation"})
    mine, theirs = get_user_config(USER_A), get_user_config(USER_B)
    assert list(mine.agent_skills) == ["box_oscillation"] and not theirs.agent_skills

    assert _fp(mine) != _fp(theirs)  # the same plain request, another effective strategy set
    assert _fp(mine) == _fp(theirs, ["box_oscillation"])  # equals what B gets by asking for that skill explicitly
    assert _fp(mine, ["chan_theory"]) == _fp(theirs, ["chan_theory"])  # request skills win over the personal ones


def test_a_changed_skill_definition_is_another_strategy(config_app, monkeypatch):
    from src.agent import factory
    from src.config import get_config

    before = _fp(get_config(), ["box_oscillation"])
    factory.get_skill_manager(get_config())  # make sure the prototype exists
    monkeypatch.setattr(factory._SKILL_MANAGER_PROTOTYPE.get("box_oscillation"), "instructions", "a new definition")
    assert _fp(get_config(), ["box_oscillation"]) != before


def test_multi_agent_routing_is_part_of_the_fingerprint_inputs(config_app):
    from src.config import get_config

    base = get_config()

    def view(**overrides):
        clone = SimpleNamespace(**{k: getattr(base, k) for k in (
            "agent_mode", "agent_skills", "agent_arch", "agent_orchestrator_mode", "agent_skill_routing", "agent_skill_dir",
        )})
        for key, value in overrides.items():
            setattr(clone, key, value)
        return clone

    single = _fp(view(agent_mode=True))
    auto_routed = _fp(view(agent_mode=True, agent_arch="multi", agent_orchestrator_mode="specialist"))
    manual = _fp(view(agent_mode=True, agent_arch="multi", agent_orchestrator_mode="specialist",
                      agent_skill_routing="manual"))
    standard = _fp(view(agent_mode=True, agent_arch="multi", agent_orchestrator_mode="standard"))
    assert len({single, auto_routed, manual, standard}) == 4
    # an explicit request skips the router: the routing mode no longer matters for it
    explicit = ["box_oscillation"]
    assert _fp(view(agent_mode=True, agent_arch="multi", agent_orchestrator_mode="specialist"), explicit) == _fp(
        view(agent_mode=True, agent_arch="multi", agent_orchestrator_mode="specialist", agent_skill_routing="manual"),
        explicit,
    )
    assert _fp(view(agent_mode=True, agent_arch="multi", agent_orchestrator_mode="specialist"), explicit) != _fp(
        view(agent_mode=True), explicit
    )  # ... but the architecture still does


def test_an_unresolvable_strategy_set_has_no_fingerprint(config_app, monkeypatch):
    from src.agent import factory
    from src.config import get_config

    monkeypatch.setattr(factory, "get_skill_manager", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken")))
    assert compute_strategy_fingerprint(get_config()) is None


# ===================================================================== the world: real application, mocked edges


@pytest.fixture
def world(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    set_system_provider_env(monkeypatch)
    stub_dns(monkeypatch)
    boundary = Boundary(_dashboard(CONCLUSION_A))
    router = boundary.router_class()

    def answer(text="OK", prompt=11, completion=5):  # the model that answers is the system's channel model
        message = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion),
            model="openai/sys-model",
        )

    monkeypatch.setattr("tests.test_multi_user_p2b_model_keys._response", answer)
    monkeypatch.setattr("litellm.completion", boundary.completion)
    monkeypatch.setattr("litellm.Router", router)
    monkeypatch.setattr("src.analyzer.Router", router)
    monkeypatch.setattr("src.agent.llm_adapter.Router", router)
    # market closed (the real answer depends on the wall clock); the in-session guards have their own tests
    closed = lambda *a, **k: MarketPhase.POSTMARKET  # noqa: E731
    monkeypatch.setattr("src.services.report_reuse_service.infer_market_phase", closed)
    monkeypatch.setattr("src.services.analysis_metadata_reuse.infer_market_phase", closed)
    import src.config
    import src.core.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "get_config", src.config.get_config)

    app.boundary = boundary
    app.trade_date = get_effective_trading_date("cn")
    app.system_fingerprint = config_model_channel_fingerprint(src.config.get_config())
    for user in (USER_A, USER_B):
        give_user_model(user, model="sys-model", base_url=SYSTEM_BASE)  # the system's channel, each with an own key
    UserSettingsService().set_settings(USER_C, {"LITELLM_MODEL": "openai/sys-model", "OPENAI_BASE_URL": SYSTEM_BASE})
    UserSettingsService().set_settings(USER_D, {"LITELLM_MODEL": "openai/sys-model", "OPENAI_BASE_URL": SYSTEM_BASE})
    assert not get_user_config(USER_C).llm_model_list

    # the data providers and the search service, as one set of recorded fakes
    fakes = SimpleNamespace(pipelines=[])
    fakes.quote = UnifiedRealtimeQuote(
        code=CODE, name="Moutai", source=RealtimeSource.TENCENT, price=1688.0, change_pct=1.2, volume_ratio=1.1,
        turnover_rate=0.4, pe_ratio=25.0, fetched_at="2026-11-09T15:30:00+08:00", provider_timestamp="2026-11-09T15:00:00+08:00",
    )
    fakes.chip = ChipDistribution(code=CODE, date="2026-11-09", profit_ratio=0.62, avg_cost=1500.0, concentration_90=0.12)
    fakes.fundamental = {
        "market": "cn", "coverage": {"boards": "ok"}, "source_chain": ["fake"], "belong_boards": [{"name": "BOARD-LIQUOR"}],
        "capital_flow": {"status": "ok", "data": {"main_net_inflow": 123456}}, "concept_boards": {"status": "ok", "data": {"top": []}},
    }
    bars = [
        {"date": app.trade_date - timedelta(days=39 - i), "open": 1600 + i, "high": 1610 + i, "low": 1590 + i,
         "close": 1605 + i, "volume": 100000 + i, "amount": 1.6e8, "pct_chg": 0.5}
        for i in range(40)
    ]
    fakes.fetcher = MagicMock()
    fakes.fetcher.get_stock_name.return_value = "Moutai"
    fakes.fetcher.get_realtime_quote.return_value = fakes.quote
    fakes.fetcher.get_chip_distribution.return_value = fakes.chip
    fakes.fetcher.get_fundamental_context.return_value = fakes.fundamental
    fakes.fetcher.get_daily_data.return_value = (pd.DataFrame(bars), "fake")
    fakes.fetcher.get_belong_boards.return_value = []
    fakes.search = MagicMock()
    fakes.search.is_available = True
    fakes.search.news_window_days = 3
    fakes.search.search_comprehensive_intel.return_value = {
        "latest_news": SimpleNamespace(success=True, results=[1, 2, 3], query="q")
    }
    fakes.search.format_intel_report.return_value = NEWS_TEXT
    fakes.structure = MagicMock()
    fakes.structure.build_context.return_value = dict(STRUCTURE)
    fakes.hotspot = MagicMock()
    fakes.hotspot.get_concept_rankings.return_value = ([], [])
    monkeypatch.setattr(app.db, "save_news_intel", MagicMock())
    app.fakes = fakes

    real_pipeline = pipeline_module.StockAnalysisPipeline

    class FakedPipeline(real_pipeline):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fetcher_manager = fakes.fetcher
            self.search_service = fakes.search
            self.market_structure_service = fakes.structure
            self.market_hotspot_service = fakes.hotspot
            self.social_sentiment_service = None
            self.notifier = MagicMock()
            fakes.pipelines.append(self)

    monkeypatch.setattr(pipeline_module, "StockAnalysisPipeline", FakedPipeline)
    yield app
    teardown_multi_user_env()


def _fetch_calls(app):
    fakes = app.fakes
    return SimpleNamespace(
        quote=fakes.fetcher.get_realtime_quote.call_count,
        chip=fakes.fetcher.get_chip_distribution.call_count,
        fundamental=fakes.fetcher.get_fundamental_context.call_count,
        daily=fakes.fetcher.get_daily_data.call_count,
        search=fakes.search.search_comprehensive_intel.call_count,
        structure=fakes.structure.build_context.call_count,
    )


def _reset_calls(app):
    for mock in (
        app.fakes.fetcher, app.fakes.search, app.fakes.structure, app.boundary.router_calls, app.boundary.completion_calls,
    ):
        if hasattr(mock, "reset_mock"):
            mock.reset_mock()
        else:
            mock.clear()
    app.boundary.router_model_lists.clear()
    app.fakes.fetcher.get_stock_name.return_value = "Moutai"
    app.fakes.fetcher.get_realtime_quote.return_value = app.fakes.quote
    app.fakes.fetcher.get_chip_distribution.return_value = app.fakes.chip
    app.fakes.fetcher.get_fundamental_context.return_value = app.fakes.fundamental
    app.fakes.search.format_intel_report.return_value = NEWS_TEXT
    app.fakes.search.search_comprehensive_intel.return_value = {
        "latest_news": SimpleNamespace(success=True, results=[1, 2, 3], query="q")
    }
    app.fakes.structure.build_context.return_value = dict(STRUCTURE)


def _analyze(app, user, **body):
    payload = {"stock_code": CODE, "async_mode": False, **body}
    return app.as_user(user).post(ANALYZE, json=payload)


def _row(app, report_id):
    with app.db.get_session() as session:
        row = session.get(AnalysisHistory, report_id)
        session.expunge(row)
        return row


def _history_ids(app):
    with app.db.get_session() as session:
        return [row.id for row in session.query(AnalysisHistory).order_by(AnalysisHistory.id)]


def _prompt_texts(app):
    return json.dumps([call.get("messages") for call in app.boundary.router_calls], ensure_ascii=False, default=str)


def _used_keys(app):
    return {
        entry["litellm_params"]["api_key"] for models in app.boundary.router_model_lists for entry in models
    }


def _first_report(app, user=USER_A, **body):
    """User A analyses the stock from scratch: the source of the inputs and of the shared report."""
    app.boundary.text = _dashboard(CONCLUSION_A)
    response = _analyze(app, user, **body)
    assert response.status_code == 200, response.text
    assert not response.json().get("reused") and not response.json().get("metadata_reused_from")
    (report_id,) = _history_ids(app)
    return report_id


# ===================================================================== (1) direct reuse: the same strategy set


def test_the_first_analysis_records_inputs_for_reuse_and_the_strategy_fingerprint(world):
    report_id = _first_report(world)

    row = _row(world, report_id)
    assert row.skill_fingerprint == compute_strategy_fingerprint(get_user_config(USER_A))
    assert row.is_private is False and row.requester_user_id == USER_A
    snapshot = json.loads(row.context_snapshot)
    assert snapshot["realtime_quote_raw"]["price"] == 1688.0 and snapshot["chip_distribution_raw"]["avg_cost"] == 1500.0
    assert snapshot["news_content"] == NEWS_TEXT and snapshot["market_structure_context"] == STRUCTURE
    assert "metadata_reuse" not in snapshot  # nothing was reused by the first analysis
    assert _fetch_calls(world).quote == 1 and _fetch_calls(world).chip == 1 and _fetch_calls(world).search == 1


def test_same_strategy_without_holdings_is_reused_directly_without_a_key_and_without_a_model(world):
    report_id = _first_report(world)
    _reset_calls(world)

    keyless = _analyze(world, USER_C)  # no model key of their own
    with_key = _analyze(world, USER_B)  # has a key: still nothing to generate

    for response in (keyless, with_key):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reused"] is True and body["reused_report_id"] == report_id
        assert not body.get("metadata_reused_from")
    assert not world.boundary.touched and world.fakes.pipelines[1:] == []  # no model call, no pipeline at all
    assert _history_ids(world) == [report_id]
    calls = _fetch_calls(world)
    assert (calls.quote, calls.chip, calls.fundamental, calls.search, calls.daily) == (0, 0, 0, 0, 0)


def test_plain_request_never_directly_gets_a_report_of_another_strategy(world):
    _first_report(world, skills=None)
    # B runs with another strategy set (the personal AGENT_SKILLS 'all' keeps the classic pipeline)
    UserSettingsService().set_settings(USER_B, {"AGENT_SKILLS": "all"})
    world.boundary.text = _dashboard(CONCLUSION_B)
    assert _analyze(world, USER_B).status_code == 200  # generated: B's report has B's strategy fingerprint
    first, second = _history_ids(world)
    assert _row(world, first).skill_fingerprint != _row(world, second).skill_fingerprint

    # C (no key) is plain: only A's report matches its effective set, never B's
    keyless = _analyze(world, USER_C)
    assert keyless.status_code == 200 and keyless.json()["reused_report_id"] == first
    # D asks for B's strategy set explicitly... which is not the same as B's personal 'all' (pipeline differs): refused
    assert _is_model_key_required(_analyze(world, USER_D, skills=["box_oscillation"]))


def test_a_personal_agent_skills_setting_decides_what_a_plain_request_can_reuse(world):
    first = _first_report(world)
    UserSettingsService().set_settings(USER_C, {"AGENT_SKILLS": "box_oscillation"})  # C: keyless, own strategy set

    # C's plain request is now a request for box_oscillation: A's default-strategy report is no match
    assert _is_model_key_required(_analyze(world, USER_C))
    # ... while a user without that setting still matches A's report
    assert _analyze(world, USER_D).json()["reused_report_id"] == first


def test_a_report_of_the_same_requested_skills_is_reused_by_a_plain_request_with_the_same_personal_skills(world):
    from src.config import get_config

    fingerprint = compute_strategy_fingerprint(get_config(), ["box_oscillation"])
    first = world.db.save_analysis_history(
        result=AnalysisResult(code=CODE, name="Moutai", sentiment_score=61, trend_prediction="up", operation_advice="buy",
                              analysis_summary=CONCLUSION_A, model_used="openai/sys-model", report_language="zh"),
        query_id="q-skills", report_type=REPORT_TYPE, news_content=None, context_snapshot=None, save_snapshot=False,
        requester_user_id=USER_A, model_channel_fingerprint=world.system_fingerprint,
        report_trade_date=world.trade_date.isoformat(), report_language="zh", skill_fingerprint=fingerprint,
    )
    UserSettingsService().set_settings(USER_C, {"AGENT_SKILLS": "box_oscillation"})

    by_personal_setting = _analyze(world, USER_C)
    by_request = _analyze(world, USER_D, skills=["box_oscillation"])

    assert by_personal_setting.status_code == 200 and by_personal_setting.json()["reused_report_id"] == first
    assert by_request.status_code == 200 and by_request.json()["reused_report_id"] == first
    assert _is_model_key_required(_analyze(world, USER_D, skills=["chan_theory"]))


def test_legacy_reports_without_a_strategy_fingerprint_are_never_reused_directly(world):
    _first_report(world)
    with world.db.get_session() as session:
        session.query(AnalysisHistory).update({AnalysisHistory.skill_fingerprint: None})
        session.commit()

    assert _is_model_key_required(_analyze(world, USER_C))
    assert world.db.find_reusable_analysis(
        code_candidates=[CODE], report_type=REPORT_TYPE, report_trade_date=world.trade_date.isoformat(),
        model_channel_fingerprint=world.system_fingerprint, report_language="zh", skill_fingerprint="",
    ) is None


def test_a_keyless_user_gets_a_direct_hit_but_any_other_case_needs_their_own_key(world):
    _first_report(world)
    assert _analyze(world, USER_C).status_code == 200  # direct hit
    assert _is_model_key_required(_analyze(world, USER_C, skills=["box_oscillation"]))  # another strategy: re-analysis
    assert _is_model_key_required(_analyze(world, USER_C, force_regenerate=True))
    assert _is_model_key_required(_analyze(world, USER_C, report_language="en"))
    mixed = world.as_user(USER_C).post(ANALYZE, json={"stock_codes": [CODE, "000858"], "async_mode": True})
    assert _is_model_key_required(mixed)  # one miss refuses the whole batch


# ===================================================================== (2) another strategy / channel: metadata reuse


def test_another_strategy_reanalyses_with_the_key_of_the_requester_and_the_inputs_of_the_earlier_analysis(world):
    source_id = _first_report(world)
    UserSettingsService().set_settings(USER_B, {"AGENT_SKILLS": "all"})
    _reset_calls(world)
    world.boundary.text = _dashboard(CONCLUSION_B)

    response = _analyze(world, USER_B)

    assert response.status_code == 200, response.text
    body = response.json()
    assert not body.get("reused") and body["metadata_reused_from"] == source_id
    assert body["report"]["summary"]["analysis_summary"] == CONCLUSION_B
    # the reused inputs were not fetched again: quote, chip distribution, fundamentals, market structure, news search
    calls = _fetch_calls(world)
    assert (calls.quote, calls.chip, calls.fundamental, calls.search, calls.structure, calls.daily) == (0, 0, 0, 0, 0, 0)
    # one model call, with B's own key only
    assert len(_model_calls(world)) == 1 and not world.boundary.completion_calls
    assert _used_keys(world) == {"sk-uid-user-b-own-key"}
    # the prompt carries the reused data but nothing of A's conclusions
    prompt = _prompt_texts(world)
    assert NEWS_TEXT in prompt and "1688" in prompt
    assert CONCLUSION_A not in prompt and "buy-at-1688" not in prompt
    # the new report is B's own, shared, with B's strategy and a snapshot that records what was reused
    _, new_id = _history_ids(world)
    row = _row(world, new_id)
    assert row.requester_user_id == USER_B and row.is_private is False
    assert row.skill_fingerprint == compute_strategy_fingerprint(get_user_config(USER_B))
    assert row.skill_fingerprint != _row(world, source_id).skill_fingerprint
    block = json.loads(row.context_snapshot)["metadata_reuse"]
    assert block["source_report_id"] == source_id and block["realtime_quote"] == "reused"
    assert set(block["reused_parts"]) == {PART_REALTIME_QUOTE, PART_CHIP, PART_FUNDAMENTAL, PART_MARKET_STRUCTURE, PART_NEWS}
    assert all(block["reused_parts"].values())  # the original capture times are kept


def test_another_model_channel_or_language_also_reuses_the_inputs(world):
    source_id = _first_report(world)
    _reset_calls(world)

    english = _analyze(world, USER_B, report_language="en")

    assert english.status_code == 200 and english.json()["metadata_reused_from"] == source_id
    calls = _fetch_calls(world)
    assert (calls.quote, calls.chip, calls.fundamental, calls.search) == (0, 0, 0, 0)
    assert len(_model_calls(world)) == 1


def test_force_regenerate_and_force_refresh_fetch_everything_again(world):
    _first_report(world)
    for flag in ({"force_regenerate": True}, {"force_refresh": True}):
        _reset_calls(world)
        response = _analyze(world, USER_B, **flag)
        assert response.status_code == 200 and not response.json().get("metadata_reused_from"), flag
        calls = _fetch_calls(world)
        assert (calls.quote, calls.chip, calls.fundamental, calls.search) == (1, 1, 1, 1), flag


def test_without_a_source_the_analysis_fetches_normally(world):
    response = _analyze(world, USER_B)

    assert response.status_code == 200 and not response.json().get("metadata_reused_from")
    calls = _fetch_calls(world)
    assert (calls.quote, calls.chip, calls.fundamental, calls.search) == (1, 1, 1, 1)


def test_the_inputs_of_another_trading_date_or_stock_are_not_reused(world):
    _first_report(world)
    with world.db.get_session() as session:
        session.query(AnalysisHistory).update({AnalysisHistory.report_trade_date: "2000-01-03"})
        session.commit()
    _reset_calls(world)

    response = _analyze(world, USER_B)
    assert response.status_code == 200 and not response.json().get("metadata_reused_from")
    assert _fetch_calls(world).quote == 1
    _reset_calls(world)
    _analyze(world, USER_B, stock_code="000858")
    assert _fetch_calls(world).chip == 1


def test_in_session_the_quote_is_refreshed_and_everything_else_is_reused(world, monkeypatch):
    source_id = _first_report(world)
    _reset_calls(world)
    live = UnifiedRealtimeQuote(code=CODE, name="Moutai", source=RealtimeSource.TENCENT, price=1777.0, change_pct=3.0)
    world.fakes.fetcher.get_realtime_quote.return_value = live
    monkeypatch.setattr("src.services.analysis_metadata_reuse.infer_market_phase", lambda *a, **k: MarketPhase.INTRADAY)

    response = _analyze(world, USER_B, report_language="en")

    assert response.status_code == 200 and response.json()["metadata_reused_from"] == source_id
    calls = _fetch_calls(world)
    assert calls.quote == 1  # refreshed
    assert (calls.chip, calls.fundamental, calls.search, calls.structure) == (0, 0, 0, 0)  # reused
    assert "1777" in _prompt_texts(world) and "1688" not in _prompt_texts(world)
    _, new_id = _history_ids(world)
    block = json.loads(_row(world, new_id).context_snapshot)["metadata_reuse"]
    assert block["realtime_quote"] == "fetched" and PART_REALTIME_QUOTE not in block["reused_parts"]


def test_a_quote_the_source_captured_in_session_is_not_reused_after_the_close(world, monkeypatch):
    _first_report(world)
    _reset_calls(world)
    # the phase of the source's creation time is "in session"; the phase now is closed
    seen = []

    def phase(market, current_time=None):
        seen.append(current_time)
        return MarketPhase.INTRADAY if current_time is not None else MarketPhase.POSTMARKET

    monkeypatch.setattr("src.services.analysis_metadata_reuse.infer_market_phase", phase)
    response = _analyze(world, USER_B, report_language="en")

    assert response.status_code == 200 and response.json()["metadata_reused_from"]
    calls = _fetch_calls(world)
    assert calls.quote == 1 and (calls.chip, calls.fundamental, calls.search) == (0, 0, 0)


# ===================================================================== (3) holdings are never shared


def _position_context(user_id):
    return {
        "symbol": CODE, "quantity": 73519, "avg_cost": 1234.56, "total_cost": 90762000.0, "owner_user_id": user_id,
        "unrealized_pnl_base": 55555.55, "account_id": 1, "account_name": "HOLDINGS-ACCOUNT-A",
    }


def _holdings_analysis(app, user, **kwargs):
    from src.services.analysis_service import AnalysisService

    app.boundary.text = _dashboard(CONCLUSION_B)
    return AnalysisService().analyze_stock(
        CODE, report_type="detailed", user_id=user, allow_reuse=True, reuse_metadata=True,
        portfolio_context=_position_context(user), query_source="portfolio", **kwargs,
    )


def test_a_holdings_request_never_reuses_a_report_directly_and_its_report_is_private(world):
    source_id = _first_report(world)
    _reset_calls(world)

    result = _holdings_analysis(world, USER_B)

    assert result and not result.get("reused")  # a matching shared report existed: still analysed
    assert result["metadata_reused_from"] == source_id  # ... on the same day's inputs
    assert len(_model_calls(world)) == 1 and _used_keys(world) == {"sk-uid-user-b-own-key"}
    calls = _fetch_calls(world)
    assert (calls.quote, calls.chip, calls.fundamental, calls.search) == (0, 0, 0, 0)
    new_id = _history_ids(world)[-1]
    row = _row(world, new_id)
    assert row.is_private is True and row.requester_user_id == USER_B  # the report is B's alone
    snapshot = row.context_snapshot or ""
    assert "73519" not in snapshot and "HOLDINGS-ACCOUNT-A" not in snapshot
    assert '"portfolio_context":' not in snapshot  # (the overview only names the data block, it holds no value)


def test_a_private_holdings_report_is_neither_reused_directly_nor_a_metadata_source_for_anybody_else(world):
    _holdings_analysis(world, USER_B)  # the only report of the day is B's private holdings report
    private_id = _history_ids(world)[-1]
    assert _row(world, private_id).is_private is True
    _reset_calls(world)

    # nobody else gets it directly ...
    assert _is_model_key_required(_analyze(world, USER_C))
    assert _is_model_key_required(_analyze(world, USER_D))
    # ... nor its collected inputs
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_A) is None
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=PRIMARY_ID) is None
    response = _analyze(world, USER_A)
    assert response.status_code == 200 and not response.json().get("metadata_reused_from")
    assert _fetch_calls(world).quote == 1  # A fetched everything itself
    # B's own later request may build on B's own private report (market data only)
    own = load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_B)
    assert own is not None and own.source_report_id in (private_id, _history_ids(world)[-1])


def test_the_primary_user_who_asks_for_a_push_generates_but_still_reuses_the_inputs(world):
    source_id = _first_report(world)
    _reset_calls(world)
    from src.services.analysis_service import AnalysisService

    # what the API passes for the primary user's default request: no direct reuse (notify), metadata reuse on
    result = AnalysisService().analyze_stock(CODE, report_type="detailed", allow_reuse=False, reuse_metadata=True,
                                             send_notification=False)

    assert result and not result.get("reused") and result["metadata_reused_from"] == source_id
    assert _fetch_calls(world).quote == 0 and len(_model_calls(world)) == 1


def test_api_primary_push_request_generates_and_a_quiet_one_reuses_directly(world):
    source_id = _first_report(world)
    _reset_calls(world)

    pushed = _analyze(world, PRIMARY_ID)  # notify defaults to true
    assert pushed.status_code == 200 and not pushed.json().get("reused")
    assert len(_model_calls(world)) == 1
    # the primary user's strategy is the system's: its report is a different one from A's? same strategy -> quiet reuse
    quiet = _analyze(world, PRIMARY_ID, notify=False)
    assert quiet.status_code == 200 and quiet.json().get("reused") is True
    assert quiet.json()["reused_report_id"] in _history_ids(world) and source_id in _history_ids(world)


# ===================================================================== metadata: whitelist, stripping, visibility


def _store(app, *, requester=USER_A, private=False, snapshot=None, trade_date=None, created=None, code=CODE):
    result = AnalysisResult(
        code=code, name="Moutai", sentiment_score=80, trend_prediction="up", operation_advice="buy",
        analysis_summary=CONCLUSION_A, model_used="openai/sys-model", report_language="zh",
    )
    report_id = app.db.save_analysis_history(
        result=result, query_id=f"q-{requester}-{private}-{datetime.now().timestamp()}", report_type=REPORT_TYPE,
        news_content="x", context_snapshot=snapshot, save_snapshot=snapshot is not None, requester_user_id=requester,
        is_private=private, report_trade_date=trade_date or date.today().isoformat(), report_language="zh",
        model_channel_fingerprint="f" * 32, skill_fingerprint="e" * 32,
    )
    if created is not None:
        with app.db.get_session() as session:
            session.get(AnalysisHistory, report_id).created_at = created
            session.commit()
    return report_id


@pytest.fixture
def store(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    closed = lambda *a, **k: MarketPhase.POSTMARKET  # noqa: E731
    monkeypatch.setattr("src.services.analysis_metadata_reuse.infer_market_phase", closed)
    app.trade_date = get_effective_trading_date("cn").isoformat()
    yield app
    teardown_multi_user_env()


def _snapshot(**overrides):
    base = {
        "enhanced_context": {
            "fundamental_context": {"market": "cn", "belong_boards": [{"name": "X"}]},
            "trend_analysis": {"signal_reasons": [CONCLUSION_A]},
            "today": {"close": 1.0},
        },
        "news_content": NEWS_TEXT,
        "news_result_count": 3,
        "news_evidence_present": True,
        "realtime_quote_raw": {"code": CODE, "name": "Moutai", "source": "tencent", "price": 10.5, "fetched_at": "t"},
        "chip_distribution_raw": {"code": CODE, "date": "d", "source": "x", "profit_ratio": 0.5, "avg_cost": 9.0},
        "market_structure_context": dict(STRUCTURE),
        "analysis_context_pack_overview": {"conclusion": CONCLUSION_A},
        "diagnostics": {"trace": CONCLUSION_A},
    }
    base.update(overrides)
    return base


def test_only_inputs_are_lifted_from_a_source_never_conclusions(store):
    source_id = _store(store, snapshot=_snapshot(), trade_date=store.trade_date)

    metadata = load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_B)

    assert metadata.source_report_id == source_id
    assert set(metadata.parts) == {PART_REALTIME_QUOTE, PART_CHIP, PART_FUNDAMENTAL, PART_MARKET_STRUCTURE, PART_NEWS}
    assert metadata.parts[PART_REALTIME_QUOTE].price == 10.5 and metadata.parts[PART_CHIP].avg_cost == 9.0
    assert metadata.news_result_count == 3 and metadata.news_evidence_present is True
    assert CONCLUSION_A not in repr(metadata.parts)  # trend_analysis, overview, diagnostics, advice: not taken


def test_holdings_fields_are_stripped_from_the_reused_inputs_even_of_a_shared_report(store):
    dirty = _snapshot(
        enhanced_context={"fundamental_context": {
            "market": "cn", "portfolio_context": {"quantity": 73519}, "boards": {"holdings": [{"qty": 5}], "ok": 1},
            "deep": [{"positions": [1], "kept": "yes"}],
        }},
        market_structure_context={"portfolio": {"cash": 1}, "summary": "kept"},
        realtime_quote_raw={"code": CODE, "name": "Moutai", "source": "tencent", "price": 10.5, "holdings": 3},
    )
    _store(store, snapshot=dirty, trade_date=store.trade_date)

    metadata = load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_B)

    text = json.dumps({k: v for k, v in metadata.parts.items() if isinstance(v, dict)})
    for forbidden in ("73519", "portfolio_context", "holdings", "positions", "portfolio"):
        assert forbidden not in text
    assert "kept" in text and metadata.parts[PART_FUNDAMENTAL]["boards"] == {"ok": 1}
    assert strip_holding_fields({"a": {"portfolio_context": 1, "b": [{"holdings": 2, "c": 3}]}}) == {"a": {"b": [{"c": 3}]}}
    assert strip_holding_fields({"avg_cost": 9.0}) == {"avg_cost": 9.0}  # chip cost is market data, not a position


def test_the_sources_are_shared_reports_and_the_requesters_own_but_never_anybody_elses_private_ones(store):
    others_private = _store(store, requester=USER_A, private=True, snapshot=_snapshot(), trade_date=store.trade_date)
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_B) is None
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=PRIMARY_ID) is None
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_A).source_report_id == others_private

    shared = _store(store, requester=USER_D, snapshot=_snapshot(), trade_date=store.trade_date)
    # B: only the shared one; A: the newest of shared + own private
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_B).source_report_id == shared
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_A).source_report_id == shared  # newer than A's own


def test_a_report_without_a_snapshot_or_with_an_unreadable_one_is_no_source(store):
    _store(store, snapshot=None, trade_date=store.trade_date)
    with store.db.get_session() as session:
        broken = _store(store, snapshot=_snapshot(), trade_date=store.trade_date)
        session.get(AnalysisHistory, broken).context_snapshot = "{not json"
        session.commit()
    assert load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_B) is None


def test_chains_keep_the_original_capture_time(store):
    first = _store(store, snapshot=_snapshot(), trade_date=store.trade_date, created=datetime(2026, 11, 9, 15, 5))
    second_snapshot = _snapshot(metadata_reuse={
        "source_report_id": first, "reused_parts": {PART_NEWS: "2026-11-09T15:05:00"}, "realtime_quote": "fetched",
    })
    _store(store, snapshot=second_snapshot, trade_date=store.trade_date, created=datetime(2026, 11, 9, 18, 30))

    metadata = load_reusable_metadata(stock_code=CODE, viewer_user_id=USER_B)

    assert metadata.captured_at[PART_NEWS] == "2026-11-09T15:05:00"  # news: collected by the first analysis
    assert metadata.captured_at[PART_CHIP].startswith("2026-11-09T18:30")  # others: collected by the second


# ===================================================================== agent pipeline: the same inputs are injected


def test_agent_mode_injects_the_reused_news_and_neither_searches_nor_loads_intelligence_again():
    from src.core.pipeline import StockAnalysisPipeline
    from src.services.analysis_metadata_reuse import ReusedMetadata
    from tests.test_pipeline_market_phase_context import _make_pipeline

    pipeline = _make_pipeline(agent_mode=True)
    pipeline.config.agent_mode = True
    pipeline.config.agent_arch = "single"
    pipeline.db.save_news_intel = MagicMock()
    pipeline.search_service.is_available = True
    pipeline._load_persisted_intelligence_context = MagicMock(side_effect=AssertionError("must not load again"))
    pipeline.reused_metadata = ReusedMetadata(
        source_report_id=7, trade_date="2026-11-09", refresh_realtime=False, parts={PART_NEWS: NEWS_TEXT},
        captured_at={PART_NEWS: "2026-11-09T15:05:00"}, news_result_count=3, news_evidence_present=True,
    )
    captured = {}

    class Executor:
        tool_registry = None

        def run(self, message, context=None):
            captured["context"] = context
            return SimpleNamespace(
                success=True, error="", provider="fake", model="openai/m", dashboard={"sentiment_score": 50},
                tool_calls_log=[], runtime_facts=None,
            )

    with patch("src.agent.factory.build_agent_executor", lambda *a, **k: Executor()):
        result = StockAnalysisPipeline._analyze_with_agent(
            pipeline, CODE, ReportType.from_str("detailed"), "q1", "Moutai", None, None, {"market": "cn"}, None,
        )

    assert result is not None
    assert captured["context"]["news_context"] == NEWS_TEXT
    pipeline.search_service.search_stock_news.assert_not_called()
    pipeline._load_persisted_intelligence_context.assert_not_called()
    assert PART_NEWS in pipeline.reused_metadata.used


# ===================================================================== the migration


NOW = "2026-11-01 10:00:00.000000"
PRIMARY = "uid-admin"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from src.config import Config
    from src.storage import DatabaseManager

    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stock.db"))
    monkeypatch.setenv("PRIMARY_USER_ID", PRIMARY)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    yield tmp_path
    DatabaseManager.reset_instance()
    Config.reset_instance()


def _insert_history(conn, report_id, raw_result, *, trade_date=None, requester=PRIMARY, private=0):
    conn.execute(
        "INSERT INTO analysis_history (id, query_id, code, name, report_type, requester_user_id, raw_result, "
        "report_trade_date, is_private, created_at) VALUES (?, ?, '600519', 'n', 'detailed', ?, ?, ?, ?, ?)",
        (report_id, f"q{report_id}", requester, raw_result, trade_date, private, NOW),
    )


def _before_p2b4(env):
    """A database as it was before this migration: no skill_fingerprint column, neither version recorded."""
    from src.storage import DatabaseManager

    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    with sqlite3.connect(db_path) as conn:
        for index in conn.execute('PRAGMA index_list("analysis_history")').fetchall():
            columns = [info[2] for info in conn.execute(f'PRAGMA index_info("{index[1]}")')]
            if "skill_fingerprint" in columns and not index[1].startswith("sqlite_autoindex"):
                conn.execute(f'DROP INDEX "{index[1]}"')
        conn.execute("ALTER TABLE analysis_history DROP COLUMN skill_fingerprint")
        for version in (migration.MULTI_USER_P2B4_SCHEMA_VERSION, migration.MULTI_USER_P2B4_AGENT_PRIVACY_VERSION):
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
        _insert_history(conn, 1, json.dumps({"data_sources": "agent:litellm", "x": 1}))                      # legacy Agent
        _insert_history(conn, 2, json.dumps({"data_sources": "litellm,tushare"}))                           # legacy classic
        _insert_history(conn, 3, json.dumps({"data_sources": "agent:litellm"}), trade_date="2026-11-02")     # tool calls were checked
        _insert_history(conn, 4, "not json but says agent:", requester="uid-user")                           # unreadable Agent
        _insert_history(conn, 5, json.dumps({"data_sources": "agent:litellm"}), private=1)                    # already private
        _insert_history(conn, 6, json.dumps({"note": "an agent: mention in free text"}))                     # not Agent mode
        conn.execute(
            "INSERT INTO decision_signals (id, stock_code, market, source_type, source_report_id, trigger_source, action, "
            "plan_quality, status) VALUES (1, '600519', 'cn', 'analysis', 1, 'api', 'buy', 'unknown', 'active')"
        )
        conn.execute(
            "INSERT INTO backtest_results (id, analysis_history_id, code, eval_window_days, engine_version, eval_status) "
            "VALUES (1, 1, '600519', 10, 'v1', 'completed')"
        )
        conn.execute(
            "INSERT INTO backtest_results (id, analysis_history_id, code, eval_window_days, engine_version, eval_status) "
            "VALUES (2, 2, '600519', 10, 'v1', 'completed')"
        )
    return db_path


def _state(db_path):
    with sqlite3.connect(db_path) as conn:
        private = {row[0] for row in conn.execute("SELECT id FROM analysis_history WHERE is_private = 1")}
        owners = dict(conn.execute("SELECT id, owner_user_id FROM decision_signals"))
        backtests = {row[0] for row in conn.execute("SELECT id FROM backtest_results")}
    return private, owners, backtests


def test_legacy_agent_reports_that_may_have_used_holdings_become_private(env):
    from src.storage import DatabaseManager

    db_path = _before_p2b4(env)

    DatabaseManager(db_url=f"sqlite:///{db_path}")

    private, owners, backtests = _state(db_path)
    # 1: Agent mode, no tool-call marker -> undeterminable -> private; 4: unreadable but Agent; 5: already private
    # 2: classic pipeline (no personal-data tool exists there); 3: the tool calls were checked at save time; 6: not Agent
    assert private == {1, 4, 5}
    assert owners == {1: PRIMARY}  # the signal of a now-private report is owned by the report's requester
    assert backtests == {2}  # derived data of a private report is purged again
    with sqlite3.connect(db_path) as conn:
        assert "skill_fingerprint" in [row[1] for row in conn.execute('PRAGMA table_info("analysis_history")')]
        assert conn.execute("SELECT COUNT(*) FROM analysis_history WHERE skill_fingerprint IS NOT NULL").fetchone() == (0,)
        recorded = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        requesters = dict(conn.execute("SELECT id, requester_user_id FROM analysis_history"))
    assert {migration.MULTI_USER_P2B4_SCHEMA_VERSION, migration.MULTI_USER_P2B4_AGENT_PRIVACY_VERSION} <= recorded
    assert requesters[1] == PRIMARY and requesters[4] == "uid-user"  # private to whoever requested it


def test_the_p2b4_migration_is_backed_up_first_idempotent_and_recorded_once(env):
    from src.storage import DatabaseManager

    db_path = _before_p2b4(env)
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    backups = sorted(db_path.parent.glob("stock.db.pre-multi-user-*.bak"))
    assert backups
    with sqlite3.connect(backups[-1]) as conn:  # the restore point still has the pre-migration schema
        assert "skill_fingerprint" not in [row[1] for row in conn.execute('PRAGMA table_info("analysis_history")')]
        assert conn.execute("SELECT is_private FROM analysis_history WHERE id = 1").fetchone() == (0,)
    first = _state(db_path)

    DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert _state(db_path) == first
    assert sorted(db_path.parent.glob("stock.db.pre-multi-user-*.bak")) == backups  # nothing new to protect
    with sqlite3.connect(db_path) as conn:
        for version in (migration.MULTI_USER_P2B4_SCHEMA_VERSION, migration.MULTI_USER_P2B4_AGENT_PRIVACY_VERSION):
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (version,)).fetchone() == (1,)


def test_a_failing_p2b4_backfill_rolls_back_and_refuses_startup(env):
    from src.storage import DatabaseManager

    db_path = _before_p2b4(env)
    with pytest.MonkeyPatch.context() as patch_:
        patch_.setattr(migration, "_backfill_legacy_agent_privacy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            DatabaseManager(db_url=f"sqlite:///{db_path}")

    private, _owners, _bt = _state(db_path)
    assert private == {5}  # nothing was marked
    with sqlite3.connect(db_path) as conn:
        assert "skill_fingerprint" not in [row[1] for row in conn.execute('PRAGMA table_info("analysis_history")')]
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2B4_SCHEMA_VERSION,)
        ).fetchone() == (0,)


def test_a_fresh_database_has_the_column_and_both_records_without_a_backup(env):
    from src.storage import DatabaseManager

    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert not list(db_path.parent.glob("stock.db.pre-multi-user-*.bak"))
    with sqlite3.connect(db_path) as conn:
        assert "skill_fingerprint" in [row[1] for row in conn.execute('PRAGMA table_info("analysis_history")')]
        recorded = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    assert {migration.MULTI_USER_P2B4_SCHEMA_VERSION, migration.MULTI_USER_P2B4_AGENT_PRIVACY_VERSION} <= recorded
