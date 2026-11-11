# -*- coding: utf-8 -*-
"""Multi-user P2b-2: private reports and their decision signals are visible to their requester only (offline).

A report produced with personal context (portfolio / position-aware analysis, an Agent run that read the
portfolio snapshot) is private to its requester; shared reports stay visible to everybody with requester
attribution. Every history route, the analysis status / flow routes, the decision-signal routes and the places
that feed reports into prompts or notifications must treat another user's private report / signal as missing.
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

from src.analyzer import AnalysisResult
from src.enums import ReportType
from src.storage import AnalysisHistory, DecisionSignalRecord
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    teardown_multi_user_env,
)

HISTORY = "/api/v1/history"
SIGNALS = "/api/v1/decision-signals"


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    yield built
    teardown_multi_user_env()


def _result(code: str, name: str) -> AnalysisResult:
    return AnalysisResult(
        code=code, name=name, sentiment_score=70, trend_prediction="看多", operation_advice="买入",
        analysis_summary=f"{name} summary",
    )


def _save(db, query_id: str, code: str, *, requester=None, private=False, **extra) -> int:
    record_id = db.save_analysis_history(
        result=_result(code, f"name-{code}"), query_id=query_id, report_type="simple", news_content="news",
        context_snapshot=None, save_snapshot=False, requester_user_id=requester, is_private=private, **extra,
    )
    assert record_id > 0
    return record_id


@pytest.fixture
def reports(app):
    """shared report by A, private report by the primary user, private report by A."""
    return SimpleNamespace(
        shared=_save(app.db, "q-shared", "600519", requester=USER_A),
        primary_private=_save(app.db, "q-primary-private", "000001", requester=PRIMARY_ID, private=True),
        a_private=_save(app.db, "q-a-private", "000002", requester=USER_A, private=True),
    )


def _ids(response):
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


# ===================================================================== requester / private marker are recorded


def test_save_records_requester_privacy_and_reuse_columns(app):
    record_id = _save(
        app.db, "q-cols", "600519", requester=USER_A, private=True, model_channel_fingerprint="fp1",
        report_trade_date="2026-11-06", report_language="zh",
    )
    default_id = _save(app.db, "q-default", "600519")

    with app.db.get_session() as session:
        row = session.get(AnalysisHistory, record_id)
        default_row = session.get(AnalysisHistory, default_id)
        assert (row.requester_user_id, row.is_private, row.model_channel_fingerprint) == (USER_A, True, "fp1")
        assert (row.report_trade_date, row.report_language) == ("2026-11-06", "zh")
        # no requester given (CLI / system): the primary user, shared
        assert default_row.requester_user_id == PRIMARY_ID and default_row.is_private is False


def _ownership_pipeline(user_id, portfolio_context=None):
    from tests.test_pipeline_market_phase_context import _make_pipeline

    pipeline = _make_pipeline(agent_mode=False)
    pipeline.user_id = user_id
    pipeline.portfolio_context = portfolio_context
    pipeline.db.save_analysis_history.return_value = 77
    return pipeline


def test_pipeline_saves_the_api_requester_with_every_report(app):
    pipeline = _ownership_pipeline(USER_A)
    with patch("src.core.pipeline.extract_and_persist_from_analysis_result") as extract:
        result = pipeline.analyze_stock("600519", ReportType.SIMPLE, "q-1")

    assert result is not None
    kwargs = pipeline.db.save_analysis_history.call_args.kwargs
    assert kwargs["requester_user_id"] == USER_A
    assert kwargs["is_private"] is False
    assert kwargs["report_language"] == "zh" and kwargs["report_trade_date"]
    assert "owner_user_id" not in extract.call_args.kwargs  # a shared report's signals are shared


def test_position_aware_analysis_is_private_and_its_signal_is_owned_by_the_requester(app):
    context = {"symbol": "600519", "quantity": 100, "owner_user_id": USER_A}
    pipeline = _ownership_pipeline(USER_A, portfolio_context=context)
    with patch("src.core.pipeline.extract_and_persist_from_analysis_result") as extract:
        pipeline.analyze_stock("600519", ReportType.SIMPLE, "q-2")

    kwargs = pipeline.db.save_analysis_history.call_args.kwargs
    assert kwargs["requester_user_id"] == USER_A and kwargs["is_private"] is True
    assert extract.call_args.kwargs["owner_user_id"] == USER_A


def test_primary_position_analysis_is_private_to_the_primary_user(app):
    pipeline = _ownership_pipeline(PRIMARY_ID, portfolio_context={"symbol": "600519", "quantity": 100})
    with patch("src.core.pipeline.extract_and_persist_from_analysis_result") as extract:
        pipeline.analyze_stock("600519", ReportType.SIMPLE, "q-3")

    assert pipeline.db.save_analysis_history.call_args.kwargs["is_private"] is True
    assert extract.call_args.kwargs["owner_user_id"] == PRIMARY_ID


def test_agent_run_that_read_the_portfolio_snapshot_makes_the_report_private(app):
    pipeline = _ownership_pipeline(USER_A)
    result = _result("600519", "x")
    portfolio_run = SimpleNamespace(tool_calls_log=[{"tool": "get_stock_info"}, {"tool": "get_portfolio_snapshot"}])
    search_only_run = SimpleNamespace(tool_calls_log=[{"tool": "search_stock_news"}, {"tool": "get_daily_history"}])

    private = pipeline._history_ownership_kwargs(result, code="600519", agent_result=portfolio_run)
    shared = pipeline._history_ownership_kwargs(result, code="600519", agent_result=search_only_run)

    assert private["is_private"] is True and private["requester_user_id"] == USER_A
    assert shared["is_private"] is False  # user-scoped search tools read no personal data


@pytest.mark.parametrize("method,path,private", [
    ("GET", "/api/v1/portfolio/accounts", True),
    ("GET", "/stocks/watchlist", True),
    ("GET", "/api/v1/history/1", True),
    ("POST", "/api/v1/screening/screen", True),
    ("GET", "/api/v1/unknown/data", True),
    ("GET", "/api/v1/stocks/AAPL/quote", False),
    ("GET", "/api/v1/stocks/600519/history?limit=5", False),
])
def test_call_api_classification_persists_report_owner_and_visibility(app, method, path, private):
    pipeline = _ownership_pipeline(USER_A)
    result = _result("600519", "API evidence")
    run = SimpleNamespace(tool_calls_log=[{"tool": "call_api", "arguments": {"method": method, "path": path}}])
    ownership = pipeline._history_ownership_kwargs(result, code="600519", agent_result=run)
    assert ownership["is_private"] is private
    report_id = app.db.save_analysis_history(result, query_id="api-evidence", report_type="simple",
                                             news_content=None, **ownership)
    assert report_id > 0
    with app.db.get_session() as session:
        row = session.get(AnalysisHistory, report_id)
        assert row.requester_user_id == USER_A and row.is_private is private
    assert (app.db.get_analysis_history_by_id(report_id, viewer_user_id=USER_B) is None) is private


@pytest.mark.parametrize("arguments", [None, {}, "not-json", {"method":"GET"},
                                        {"method":"GET","path":"http://[broken"}])
def test_ambiguous_call_api_trace_is_private(arguments):
    from src.agent.tools.registry import tool_calls_read_personal_data
    assert tool_calls_read_personal_data([{"tool":"call_api", "arguments_summary":arguments}])


def test_personal_data_tool_names_are_real_user_scoped_tools():
    from src.agent.tools.data_tools import get_portfolio_snapshot_tool
    from src.agent.tools.registry import PERSONAL_DATA_TOOL_NAMES

    assert PERSONAL_DATA_TOOL_NAMES == {get_portfolio_snapshot_tool.name}
    assert get_portfolio_snapshot_tool.user_scoped is True


# ===================================================================== history routes


def test_list_shows_shared_plus_own_private_and_mine_only_filters(app, reports):
    assert _ids(app.as_user(USER_B).get(HISTORY)) == {reports.shared}
    assert _ids(app.as_user(USER_A).get(HISTORY)) == {reports.shared, reports.a_private}
    assert _ids(app.as_user(PRIMARY_ID).get(HISTORY)) == {reports.shared, reports.primary_private}

    assert _ids(app.as_user(USER_A).get(HISTORY, params={"mine_only": True})) == {reports.shared, reports.a_private}
    assert _ids(app.as_user(PRIMARY_ID).get(HISTORY, params={"mine_only": True})) == {reports.primary_private}
    assert _ids(app.as_user(USER_B).get(HISTORY, params={"mine_only": True})) == set()


def test_list_total_and_stock_filter_respect_visibility(app, reports):
    body = app.as_user(USER_B).get(HISTORY).json()
    assert body["total"] == 1
    assert _ids(app.as_user(USER_B).get(HISTORY, params={"stock_code": "000001"})) == set()


def test_shared_reports_carry_requester_attribution(app, reports):
    from src.repositories.user_repo import UserRepository

    UserRepository().touch_app_user(USER_A, "alice", "Alice", "user")
    item = next(i for i in app.as_user(USER_B).get(HISTORY).json()["items"] if i["id"] == reports.shared)
    assert item["requester_user_id"] == USER_A and item["requester_name"] == "Alice"
    assert item["is_mine"] is False and item["is_private"] is False

    mine = next(i for i in app.as_user(USER_A).get(HISTORY).json()["items"] if i["id"] == reports.a_private)
    assert mine["is_mine"] is True and mine["is_private"] is True

    detail = app.as_user(USER_B).get(f"{HISTORY}/{reports.shared}").json()["meta"]
    assert detail["requester_user_id"] == USER_A and detail["is_mine"] is False


def test_detail_by_id_and_by_query_id_treat_a_foreign_private_report_as_missing(app, reports):
    client = app.as_user(USER_B)
    missing = client.get(f"{HISTORY}/999999")

    for key in (reports.primary_private, "q-primary-private", reports.a_private, "q-a-private"):
        response = client.get(f"{HISTORY}/{key}")
        assert response.status_code == 404, key
        assert response.json()["error"] == missing.json()["error"] == "not_found"
    assert client.get(f"{HISTORY}/{reports.shared}").status_code == 200
    # the owner and nobody else
    assert app.as_user(PRIMARY_ID).get(f"{HISTORY}/{reports.primary_private}").status_code == 200
    assert app.as_user(USER_A).get(f"{HISTORY}/q-a-private").status_code == 200
    assert app.as_user(USER_A).get(f"{HISTORY}/{reports.primary_private}").status_code == 404


@pytest.mark.parametrize("suffix", ["diagnostics", "flow", "news", "markdown", "share-image-html"])
def test_every_report_subresource_treats_a_foreign_private_report_as_missing(app, reports, suffix):
    foreign = app.as_user(USER_B).get(f"{HISTORY}/{reports.primary_private}/{suffix}")
    missing = app.as_user(USER_B).get(f"{HISTORY}/999999/{suffix}")

    assert foreign.status_code == missing.status_code, (suffix, foreign.text)
    if suffix != "news":  # news of a missing report is an empty list, which must be what a foreign one gives too
        assert foreign.status_code == 404
    else:
        assert foreign.json() == missing.json()
    owner = app.as_user(PRIMARY_ID).get(f"{HISTORY}/{reports.primary_private}/{suffix}")
    assert owner.status_code != 404, (suffix, owner.text)


def test_stock_bar_hides_foreign_private_stocks(app, reports):
    def codes(user):
        response = app.as_user(user).get(f"{HISTORY}/stocks")
        assert response.status_code == 200, response.text
        return {item["stock_code"] for item in response.json()["items"]}

    assert codes(USER_B) == {"600519"}
    assert codes(USER_A) == {"600519", "000002"}
    assert codes(PRIMARY_ID) == {"600519", "000001"}


def test_delete_only_touches_reports_the_user_may_see_and_owns(app, reports):
    # B: a foreign private report and a shared report of someone else are both untouched
    response = app.as_user(USER_B).request(
        "DELETE", HISTORY, json={"record_ids": [reports.primary_private, reports.shared]},
    )
    assert response.status_code == 200 and response.json()["deleted"] == 0
    assert app.as_user(USER_B).delete(f"{HISTORY}/by-code/000001").json()["deleted"] == 0
    assert app.as_user(USER_B).delete(f"{HISTORY}/by-code/600519").json()["deleted"] == 0
    # A may not delete the primary user's private report, but may delete the report A requested
    assert app.as_user(USER_A).request("DELETE", HISTORY, json={"record_ids": [reports.primary_private]}).json()["deleted"] == 0
    assert app.as_user(USER_A).request("DELETE", HISTORY, json={"record_ids": [reports.shared]}).json()["deleted"] == 1
    with app.db.get_session() as session:
        assert session.get(AnalysisHistory, reports.primary_private) is not None
        assert session.get(AnalysisHistory, reports.shared) is None


def test_admin_deletes_shared_reports_of_others_but_not_their_private_ones(app, reports):
    admin = app.as_user(PRIMARY_ID)
    assert admin.request("DELETE", HISTORY, json={"record_ids": [reports.a_private]}).json()["deleted"] == 0
    assert admin.request("DELETE", HISTORY, json={"record_ids": [reports.shared]}).json()["deleted"] == 1


def test_analysis_status_and_flow_fall_back_to_visible_history_only(app, reports):
    status = "/api/v1/analysis/status/"
    flow = "/api/v1/analysis/tasks/{}/flow"
    foreign = app.as_user(USER_B).get(status + "q-primary-private")
    missing = app.as_user(USER_B).get(status + "q-nope")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"] == missing.json()["error"]
    assert app.as_user(USER_B).get(flow.format("q-primary-private")).status_code == 404
    assert app.as_user(PRIMARY_ID).get(status + "q-primary-private").status_code == 200
    assert app.as_user(USER_B).get(status + "q-shared").status_code == 200


def test_profile_comparison_alert_and_memory_readers_only_see_what_their_user_may(app, reports):
    from src.agent.memory import AgentMemory
    from src.services.history_comparison_service import get_signal_changes
    from src.services.stock_profile_service import StockProfileService

    # the profile of a stock reads history as the profile's user
    assert StockProfileService(user_id=USER_B)._history_service().viewer_user_id == USER_B
    assert StockProfileService()._history_service().viewer_user_id == PRIMARY_ID

    # push-notification history comparison renders for the primary user: shared + the primary's private only
    assert len(get_signal_changes("000001")) == 1
    assert get_signal_changes("000002") == []

    # agent memory feeds a prompt whose report may be shared: it reads shared reports only, nobody's private ones
    memory = AgentMemory(enabled=True)
    assert len(memory.get_stock_history("600519")) == 1
    assert memory.get_stock_history("000001") == [] and memory.get_stock_history("000002") == []


# ===================================================================== decision signals


def _signal(code="SH600519", **extra):
    payload = {
        "stock_code": code, "stock_name": "n", "market": "cn", "source_type": "analysis", "trigger_source": "api",
        "action": "buy", "confidence": 0.7, "score": 80, "horizon": "3d", "reason": "position-aware reason",
        "risk_summary": "holding context",
    }
    payload.update(extra)
    return payload


@pytest.fixture
def signals(app, reports):
    from src.services.decision_signal_service import DecisionSignalService

    service = DecisionSignalService()
    shared = service.create_signal(_signal("SH600519", reason="shared reason"))["item"]["id"]
    primary_private = service.create_signal(
        _signal("SH000001", owner_user_id=PRIMARY_ID, source_report_id=reports.primary_private)
    )["item"]["id"]
    a_private = service.create_signal(
        _signal("SH000002", owner_user_id=USER_A, source_report_id=reports.a_private)
    )["item"]["id"]
    return SimpleNamespace(shared=shared, primary_private=primary_private, a_private=a_private)


def _signal_ids(response):
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


def test_signal_list_shows_shared_plus_own_private_signals(app, signals):
    assert _signal_ids(app.as_user(USER_B).get(SIGNALS)) == {signals.shared}
    assert _signal_ids(app.as_user(USER_A).get(SIGNALS)) == {signals.shared, signals.a_private}
    # the primary user's position-aware signals are private too: no longer a shared surface
    assert _signal_ids(app.as_user(PRIMARY_ID).get(SIGNALS)) == {signals.shared, signals.primary_private}
    assert app.as_user(USER_B).get(SIGNALS).json()["total"] == 1


def test_private_signal_reads_are_missing_for_everybody_else(app, signals):
    client = app.as_user(USER_B)
    missing = client.get(f"{SIGNALS}/999999")
    for signal_id in (signals.primary_private, signals.a_private):
        for suffix in ("", "/outcomes", "/feedback"):
            response = client.get(f"{SIGNALS}/{signal_id}{suffix}")
            assert response.status_code == 404, (signal_id, suffix, response.text)
            assert response.json() == {**missing.json(), "message": response.json()["message"]}
    assert client.get(f"{SIGNALS}/{signals.shared}").status_code == 200
    assert app.as_user(PRIMARY_ID).get(f"{SIGNALS}/{signals.primary_private}").status_code == 200
    assert app.as_user(USER_A).get(f"{SIGNALS}/{signals.primary_private}").status_code == 404


def test_latest_active_review_and_stats_exclude_foreign_private_signals(app, signals):
    client = app.as_user(USER_B)
    assert client.get(f"{SIGNALS}/latest/SH000001").json()["items"] == []
    assert client.get(f"{SIGNALS}/latest/SH000002").json()["items"] == []
    assert len(client.get(f"{SIGNALS}/latest/SH600519").json()["items"]) == 1
    assert len(app.as_user(PRIMARY_ID).get(f"{SIGNALS}/latest/SH000001").json()["items"]) == 1

    # outcome rows of a private signal are invisible to others as well
    from src.repositories.decision_signal_outcome_repo import DecisionSignalOutcomeRepository

    repo = DecisionSignalOutcomeRepository()
    for signal_id in (signals.primary_private, signals.shared):
        repo.upsert_outcome({
            "signal_id": signal_id, "horizon": "3d", "engine_version": "v1", "eval_status": "completed",
            "outcome": "hit", "action": "buy", "market": "cn", "holding_state": "holding",
        })
    listed = app.as_user(USER_B).get(f"{SIGNALS}/outcomes", params={"engine_version": "v1"}).json()
    assert {item["signal_id"] for item in listed["items"]} == {signals.shared}
    owner_view = app.as_user(PRIMARY_ID).get(f"{SIGNALS}/outcomes", params={"engine_version": "v1"}).json()
    assert {item["signal_id"] for item in owner_view["items"]} == {signals.shared, signals.primary_private}
    stats = app.as_user(USER_B).get(f"{SIGNALS}/outcomes/stats", params={"engine_version": "v1", "statuses": ["active"]})
    assert stats.status_code == 200 and stats.json()["total"] == 1


def test_feedback_status_and_reassess_cannot_reach_a_foreign_private_signal(app, reports, signals):
    other_admin = app.as_user(PRIMARY_ID)
    # a non-primary administrator is just another user: the primary user's private signal does not exist for them
    from api.deps import get_current_user
    from tests.multi_user_helpers import make_user

    app.client.app.dependency_overrides[get_current_user] = lambda: make_user(USER_A, is_admin=True)
    admin_a = app.client
    assert admin_a.put(f"{SIGNALS}/{signals.primary_private}/feedback", json={"feedback_value": "useful"}).status_code == 404
    assert admin_a.patch(f"{SIGNALS}/{signals.primary_private}/status", json={"status": "invalidated"}).status_code == 404
    assert admin_a.get(f"{SIGNALS}/{signals.primary_private}/feedback").status_code == 404
    reassess = admin_a.post(
        f"{SIGNALS}/reassess", json={"source_report_id": reports.primary_private, "decision_profile": "balanced"},
    )
    assert reassess.status_code == 404 and reassess.json()["error"] == "source_report_not_found"
    assert admin_a.put(f"{SIGNALS}/{signals.a_private}/feedback", json={"feedback_value": "useful"}).status_code == 200
    assert other_admin is not None


def test_a_private_signal_never_supersedes_shared_signals_or_other_users_signals(app, signals):
    from src.services.decision_signal_service import DecisionSignalService

    service = DecisionSignalService()
    service.create_signal(_signal("SH600519", action="sell", owner_user_id=USER_A, trigger_source="alice"))
    service.create_signal(_signal("SH600519", action="sell", owner_user_id=USER_B, trigger_source="bob"))

    shared = service.get_signal(signals.shared)
    assert shared["status"] == "active"  # another user's private "sell" did not invalidate the shared "buy"
    # ... and a shared opposing signal supersedes shared ones only
    new_shared = service.create_signal(_signal("SH600519", action="sell", trigger_source="system"))["item"]
    assert service.get_signal(signals.shared)["status"] == "invalidated"
    with app.db.get_session() as session:
        alice = session.query(DecisionSignalRecord).filter_by(trigger_source="alice").one()
        assert alice.status == "active" and new_shared["id"] != alice.id


def test_extractor_stores_the_owner_of_a_private_report_and_leaves_shared_ones_unowned(app):
    from src.services.decision_signal_extractor import extract_and_persist_from_analysis_result

    kwargs = dict(trace_id="t", query_source="api", report_type="simple", profile_source="auto_default")
    private = extract_and_persist_from_analysis_result(
        _result("600519", "n"), source_report_id=11, owner_user_id=USER_A,
        portfolio_context={"quantity": 100, "owner_user_id": USER_A}, **kwargs,
    )
    shared = extract_and_persist_from_analysis_result(_result("000858", "n"), source_report_id=12, **kwargs)

    with app.db.get_session() as session:
        assert session.get(DecisionSignalRecord, private["item"]["id"]).owner_user_id == USER_A
        assert session.get(DecisionSignalRecord, shared["item"]["id"]).owner_user_id is None
    # the P2a rule stays: another user's holding is never written into a signal's metadata
    assert private["item"]["metadata"]["holding_state"] == "unknown"


def test_lazy_backfill_of_a_private_report_creates_a_signal_owned_by_its_requester(app, reports):
    from src.services.decision_signal_service import DecisionSignalService

    service = DecisionSignalService()
    seen_by_other = service.list_signals(
        source_type="analysis", source_report_id=reports.primary_private, viewer_user_id=USER_B,
    )
    assert seen_by_other["total"] == 0
    with app.db.get_session() as session:
        # somebody who may not see the report cannot even trigger the extraction
        assert session.query(DecisionSignalRecord).filter_by(source_report_id=reports.primary_private).count() == 0
    owner = service.list_signals(
        source_type="analysis", source_report_id=reports.primary_private, viewer_user_id=PRIMARY_ID,
    )
    assert owner["total"] == 1
    with app.db.get_session() as session:
        created = session.query(DecisionSignalRecord).filter_by(source_report_id=reports.primary_private).all()
        assert [row.owner_user_id for row in created] == [PRIMARY_ID]
    assert service.list_signals(source_type="analysis", source_report_id=reports.primary_private,
                                viewer_user_id=USER_B)["total"] == 0


def test_agent_memory_decision_review_reads_only_visible_signals(app, signals):
    from src.repositories.decision_signal_outcome_repo import DecisionSignalOutcomeRepository
    from src.services.decision_signal_outcome_service import (
        DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
        DecisionSignalOutcomeService,
    )

    DecisionSignalOutcomeRepository().upsert_outcome({
        "signal_id": signals.primary_private, "horizon": "3d", "engine_version": DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
        "eval_status": "completed", "outcome": "hit", "action": "buy", "market": "cn", "holding_state": "holding",
    })
    service = DecisionSignalOutcomeService()
    assert service.get_stock_review("SH000001", viewer_user_id=USER_B)["sample_size"] == 0
    assert service.get_stock_review("SH000001", viewer_user_id=PRIMARY_ID)["sample_size"] == 1


def test_alert_signals_of_a_personal_rule_belong_to_its_user_and_are_never_the_shared_summary(app):
    from src.config import get_config
    from src.services.alert_worker import AlertWorker, RuntimeAlertRule
    from src.services.decision_signal_service import DecisionSignalService

    service = DecisionSignalService()
    worker = AlertWorker(
        config_provider=lambda: get_config(), service=MagicMock(), decision_signal_service=service, notifier=MagicMock(),
    )

    def payload(_self, runtime_rule, result, *, stock_code, market):
        owner = runtime_rule.user_id or "primary"
        return _signal("SH600519", source_type="alert", action="alert", trigger_source="alert",
                       trace_id=f"alert-rule-{owner}", reason=f"threshold of {owner}")

    def rule_of(user_id):
        # the owner lives on ``RuntimeAlertRule.user_id`` (the inner rule object - PriceAlert ... - has none)
        return RuntimeAlertRule(key=f"k-{user_id}", rule=SimpleNamespace(), source="db", user_id=user_id)

    with patch.object(AlertWorker, "_symbol_identity_for_decision_signal", return_value=("SH600519", "cn")), \
            patch.object(AlertWorker, "_alert_decision_signal_payload", payload):
        by_a = worker._resolve_decision_signal_summary(rule_of(USER_A), {})
        by_primary = worker._resolve_decision_signal_summary(rule_of(None), {})
        by_b = worker._resolve_decision_signal_summary(rule_of(USER_B), {})

    # A's signal is A's alone: neither the primary user's rule nor B's picked it up as "the latest signal"
    assert by_primary["id"] != by_a["id"] and by_b["id"] != by_a["id"]
    assert by_b["id"] == by_primary["id"]  # ... while the primary user's (shared) alert signal is visible to everyone
    with app.db.get_session() as session:
        owners = {row.id: row.owner_user_id for row in session.query(DecisionSignalRecord).all()}
    assert owners[by_a["id"]] == USER_A
    assert owners[by_primary["id"]] is None  # the primary user's alert signals keep their existing (shared) behaviour


def test_portfolio_risk_reads_decision_signals_as_its_own_user(app):
    from src.services.portfolio_risk_service import PortfolioRiskService

    seen = []

    class Recorder:
        def list_signals(self, **kwargs):
            seen.append(kwargs)
            return {"items": [], "total": 0}

    risk = PortfolioRiskService(user_id=USER_B, decision_signal_service=Recorder())
    with patch.object(risk, "_held_position_identities", return_value=[{"market": "cn", "signal_stock_code": "SH000001"}]):
        risk._build_decision_signal_risk({})

    assert seen and all(call["viewer_user_id"] == USER_B for call in seen)
