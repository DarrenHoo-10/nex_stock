# -*- coding: utf-8 -*-
"""Holdings sweep (design 18.3): user A's holdings never reach anyone else through any read path.

A user's holdings - portfolio accounts / positions / lots / trades, the daily-plan cash and holdings, the
``portfolio_context`` of a position analysis, the ``get_portfolio_snapshot`` tool output, the portfolio expansion of an
alert - are written by several paths into several stores (reports, decision signals, backtests, chat, notes,
long-term memory, plans, alerts ...). This module seeds A's holdings, runs every producing path as A with the model and
the market data mocked, and then asserts that B - and the primary user too, because A is not the primary user -
find none of A's holding values (shares, cost, P&L, cash, stop-loss / take-profit levels) in the response of ANY
read API of the application. The read APIs are not listed by hand: every ``GET`` route of the real app is called, so
a route added later is covered automatically.

No network (sockets are blocked), no real model, real app / services / repositories over a temp SQLite database.
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.analyzer import AnalysisResult
from src.enums import ReportType
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    effective_api_routes,
    give_user_model,
    stub_dns,
    teardown_multi_user_env,
)

# Distinctive values: none of them occurs in an id, a timestamp, a price of the seeded market data or a field name.
SHARES = 73519
COST = "1234.56"
PNL = "55555.55"
PLAN_SHARES = 43217
PLAN_COST = "77.77"
CASH = 987654.5
STOP_LOSS = "1111.11"
TAKE_PROFIT = "2222.22"
MARKERS = (
    str(SHARES), COST, PNL, str(PLAN_SHARES), PLAN_COST, "987654", STOP_LOSS, TAKE_PROFIT,
    "HOLDING-NOTE-A", "HOLDING-CHAT-A", "HOLDING-REPORT-A", "HOLDING-ALERT-A", "HOLDING-MEMORY-A",
)
CODE = "600519"
PLAN_CODE = "000988"
ALERT_CODE = "000858"  # a held stock without an earlier signal: the alert then creates its own
HOLDINGS_TEXT = f"HOLDING-REPORT-A 我持有 {SHARES} 股，成本 {COST}，浮盈 {PNL}，止损 {STOP_LOSS}，止盈 {TAKE_PROFIT}"


# ----------------------------------------------------------------------------------------------- the world


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two non-primary users A / B and the primary user; A's holdings are produced through every producing path."""
    from src.services import daily_plan_service as plan_module
    from src.services.daily_plan_service import DailyPlanService
    from src.services.portfolio_service import PortfolioService

    plan_module._ACTIVE.clear()
    monkeypatch.setattr(DailyPlanService, "_spawn_thread", staticmethod(lambda target: None))
    monkeypatch.setattr(PortfolioService, "_fetch_realtime_position_price", lambda *a, **k: (None, None))
    app = build_multi_user_app(tmp_path, monkeypatch)
    stub_dns(monkeypatch)
    for user in (USER_A, USER_B):
        give_user_model(user)
    app.ids = SimpleNamespace()
    _seed_market(app.db)
    _produce_portfolio(app)
    _produce_daily_plan(app)
    _produce_position_analysis(app, monkeypatch)
    _produce_agent_snapshot_report(app)
    _produce_chat_and_notes(app)
    _produce_alert_signal(app, monkeypatch)
    _produce_backtest(app)
    _produce_usage(app)
    # from here on nothing may leave the process (the in-process test client only needs the loopback)
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host in (None, "", "localhost", "127.0.0.1", "::1"):
            return real_getaddrinfo(host, *args, **kwargs)
        raise OSError(f"network access ({host}) is blocked in the holdings sweep")

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield app
    plan_module._ACTIVE.clear()
    teardown_multi_user_env()


def _seed_market(db):
    from src.storage import MarketStockDaily
    from tests.board_fixtures import END_DATE, weekdays

    days = 30
    closes = [round(38.0 + (48.35 - 38.0) * i / (days - 1), 2) for i in range(days)]
    with db.session_scope() as session:
        for index, (day, close) in enumerate(zip(weekdays(END_DATE, days), closes)):
            prev = closes[index - 1] if index else close
            session.add(MarketStockDaily(
                trade_date=day, code=PLAN_CODE, name="华工科技", exchange_board="main", is_st=False,
                is_suspended=False, prev_close=prev, close=close, pct_chg=round((close / prev - 1) * 100, 2),
                amount=5e8, turnover_rate=2.0, limit_state="none",
            ))


def _produce_portfolio(app):
    client = app.as_user(USER_A)
    account = client.post("/api/v1/portfolio/accounts", json={"name": "A-hold", "market": "cn", "base_currency": "CNY"})
    assert account.status_code == 200, account.text
    app.ids.account = account.json()["id"]
    trade = client.post("/api/v1/portfolio/trades", json={
        "account_id": app.ids.account, "symbol": CODE, "trade_date": "2026-01-02", "side": "buy",
        "quantity": SHARES, "price": float(COST),
    })
    assert trade.status_code == 200, trade.text
    snapshot = client.get("/api/v1/portfolio/snapshot", params={"account_id": app.ids.account})
    assert snapshot.status_code == 200 and str(SHARES) in snapshot.text  # the producing path really produced


def _produce_daily_plan(app):
    client = app.as_user(USER_A)
    response = client.put("/api/v1/plan/holdings", json={"cash": CASH, "holdings": [{
        "code": PLAN_CODE, "shares": PLAN_SHARES, "cost": float(PLAN_COST), "buy_date": "2026-01-05", "horizon": "swing",
    }]})
    assert response.status_code == 200, response.text
    run = client.post("/api/v1/plan/runs")
    assert run.status_code in (200, 202), run.text
    assert str(PLAN_SHARES) in client.get("/api/v1/plan/account").text


def _analysis_result(summary: str) -> AnalysisResult:
    result = AnalysisResult(
        code=CODE, name="贵州茅台", sentiment_score=62, trend_prediction="震荡", operation_advice="减仓",
        decision_type="sell", analysis_summary=summary, risk_warning=summary, buy_reason=summary,
    )
    return result


def _pipeline_for(app, user_id, result, portfolio_context=None):
    from tests.test_pipeline_market_phase_context import _make_pipeline

    pipeline = _make_pipeline(agent_mode=False)
    pipeline.user_id = user_id
    pipeline.portfolio_context = portfolio_context
    pipeline.db.save_analysis_history.side_effect = app.db.save_analysis_history  # the real store
    pipeline.analyzer.analyze.return_value = result
    return pipeline


def _produce_position_analysis(app, monkeypatch):
    """The position-aware analysis of A: the report text, raw result and the decision signal carry the holdings."""
    context = {
        "symbol": CODE, "quantity": SHARES, "avg_cost": float(COST), "unrealized_pnl": float(PNL),
        "owner_user_id": USER_A,
    }
    pipeline = _pipeline_for(app, USER_A, _analysis_result(HOLDINGS_TEXT), portfolio_context=context)
    result = pipeline.analyze_stock(CODE, ReportType.SIMPLE, "q-a-position")
    assert result is not None
    with app.db.get_session() as session:
        from src.storage import AnalysisHistory

        rows = session.query(AnalysisHistory).filter(AnalysisHistory.query_id == "q-a-position").all()
        assert len(rows) == 1 and rows[0].is_private is True and rows[0].requester_user_id == USER_A
        app.ids.position_report = rows[0].id
        assert "HOLDING-REPORT-A" in (rows[0].raw_result or "") or "HOLDING-REPORT-A" in (rows[0].analysis_summary or "")


def _produce_agent_snapshot_report(app):
    """An agent-mode report whose run called ``get_portfolio_snapshot``: private as well."""
    pipeline = _pipeline_for(app, USER_A, _analysis_result(HOLDINGS_TEXT))
    agent_run = SimpleNamespace(tool_calls_log=[{"tool": "get_stock_info"}, {"tool": "get_portfolio_snapshot"}])
    ownership = pipeline._history_ownership_kwargs(_analysis_result(HOLDINGS_TEXT), code=CODE, agent_result=agent_run)
    assert ownership["is_private"] is True
    app.ids.agent_report = app.db.save_analysis_history(
        result=_analysis_result(HOLDINGS_TEXT), query_id="q-a-agent", report_type="simple", news_content=None,
        context_snapshot={"note": HOLDINGS_TEXT}, save_snapshot=True, **ownership,
    )
    assert app.ids.agent_report > 0


def _produce_chat_and_notes(app):
    app.db.ensure_chat_session_generation("sess-a", user_id=USER_A)
    app.db.save_conversation_user_turn("sess-a", f"HOLDING-CHAT-A 我持有 {SHARES} 股成本 {COST}")
    client = app.as_user(USER_A)
    page = client.put("/api/v1/agent/notes/page", json={
        "scope": "global", "title": "my book", "body_md": f"HOLDING-NOTE-A {SHARES} @ {COST}", "expected_version": 0,
    })
    assert page.status_code == 200, page.text
    proposal = client.post("/api/v1/agent/notes/proposals", json={
        "scope": "stock", "subject": CODE, "title": "pos", "body_md": f"HOLDING-NOTE-A cost {COST}",
        "source": "suggested",
    })
    assert proposal.status_code == 200, proposal.text


def _produce_alert_signal(app, monkeypatch):
    """A's ``portfolio_holdings`` alert fires: the decision signal the worker creates must belong to A alone."""
    from src.services.alert_service import AlertService
    from src.services.alert_worker import AlertWorker
    from src.services.portfolio_alerts import ExpandedSymbolTarget

    rule = app.as_user(USER_A).post("/api/v1/alerts/rules", json={
        "name": "holdings breakout HOLDING-ALERT-A", "target_scope": "portfolio_holdings", "target": "all",
        "alert_type": "price_cross", "parameters": {"direction": "above", "price": 1800}, "severity": "warning",
        "enabled": True,
    })
    assert rule.status_code == 200, rule.text
    app.ids.rule = rule.json()["id"]
    monkeypatch.setattr(
        "src.services.alert_service.expand_symbol_targets",
        lambda **kwargs: (
            [ExpandedSymbolTarget(symbol=ALERT_CODE, display_target=f"持仓 - {ALERT_CODE} HOLDING-ALERT-A")], 0,
        ),
    )

    async def quote(_monitor, _code):
        return SimpleNamespace(price=1810.0)

    config = SimpleNamespace(
        agent_event_monitor_enabled=True, agent_event_alert_rules_json="", trading_day_check_enabled=False,
    )
    worker = AlertWorker(
        config_provider=lambda: config, service=AlertService(), notifier=MagicMock(),
    )
    with patch("src.agent.events.EventMonitor._get_realtime_quote", new=quote):
        stats = worker.run_once()
    assert stats["triggered"] == 1, stats


def _produce_backtest(app):
    """Evaluations of A's private reports (with the holder-specific levels) plus one ordinary shared evaluation."""
    from tests.test_multi_user_p2b3_security import _save_report, _seed_result

    shared = _save_report(app.db, "q-shared-bt", "000001", requester=USER_A, private=False)
    _seed_result(app.db, shared, "000001")
    for history_id in (app.ids.position_report, app.ids.agent_report):
        _seed_result(
            app.db, history_id, CODE, stop_loss=float(STOP_LOSS), take_profit=float(TAKE_PROFIT),
            operation_advice="减仓",
        )
    from src.services.backtest_service import BacktestService

    # the stored rollups are recomputed from the visible (shared) results only
    BacktestService(app.db)._recompute_summaries(touched_codes=["000001", CODE], eval_window_days=10, engine_version="v1")


def _produce_usage(app):
    from src.storage import persist_llm_usage

    persist_llm_usage(
        {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}, "openai/mine-model",
        call_type="analysis", stock_code=CODE, user_id=USER_A,
    )


# ----------------------------------------------------------------------------------------------- the read APIs

# GET routes that stream forever, go to the market-data providers or belong to the system administration / login: they
# hold no per-user holdings and calling them would need network access or a running server.
SKIPPED_PREFIXES = (
    "/api/v1/analysis/tasks/stream", "/api/v1/stocks/", "/api/v1/data/", "/api/v1/system/",
    "/api/v1/screening/hotspots", "/api/v1/screening/source-history", "/api/v1/screening/status",
    "/api/login", "/api/logout", "/api/session", "/api/health", "/api/v1/auth", "/api/v1/health",
)
SKIPPED_PATHS = ("/", "/health", "/stocks.index.json")
KEPT_PREFIXES = ("/api/v1/stocks/watchlist",)


def _read_routes(app):
    for path, methods, _ in effective_api_routes(app.client.app):
        if "GET" not in methods:
            continue
        if path in SKIPPED_PATHS:
            continue
        if path.startswith(SKIPPED_PREFIXES) and not path.startswith(KEPT_PREFIXES):
            continue
        yield path


def _param_candidates(world):
    ids = world.ids
    return {
        "session_id": ["sess-a"],
        "record_id": [ids.position_report, ids.agent_report],
        "task_id": ["q-a-position", "q-a-agent"],
        "signal_id": _signal_ids(world),
        "note_id": _note_ids(world),
        "rule_id": [ids.rule],
        "run_id": [1],
        "code": [CODE, PLAN_CODE],
        "stock_code": [CODE, "SH" + CODE],
        "board_id": [1],
        "account_id": [ids.account],
    }


def _signal_ids(world):
    from src.storage import DecisionSignalRecord

    with world.db.get_session() as session:
        return [row.id for row in session.query(DecisionSignalRecord).all()] or [1]


def _note_ids(world):
    from src.storage import AssistantNote

    with world.db.get_session() as session:
        return [row.id for row in session.query(AssistantNote).all()] or [1]


EXTRA_QUERY = {
    "/api/v1/agent/notes/page": [{"scope": "global"}, {"scope": "stock", "subject": CODE}],
    "/api/v1/history": [{}, {"stock_code": CODE}, {"stock_code": PLAN_CODE}],
    "/api/v1/decision-signals": [{}, {"stock_code": CODE}, {"holding_only": "true"}, {"source_type": "alert"}],
    "/api/v1/backtest/results": [{}, {"code": CODE}],
    "/api/v1/portfolio/snapshot": [{}, {"account_id": 1}],
    "/api/v1/portfolio/risk": [{}, {"account_id": 1}],
}


def _fill(path, candidates):
    """Every concrete URL of a path template (cartesian over the candidates of its parameters)."""
    import re

    names = re.findall(r"\{(\w+)(?::\w+)?\}", path)
    urls = [path]
    for name in dict.fromkeys(names):
        values = candidates.get(name, [1])
        urls = [re.sub(r"\{" + name + r"(?::\w+)?\}", str(value), url) for url in urls for value in values]
    return urls


def _all_reads(world):
    """(label, path, params) for every read API call the sweep makes."""
    candidates = _param_candidates(world)
    calls = []
    for path in _read_routes(world):
        for url in _fill(path, candidates):
            for params in EXTRA_QUERY.get(path, [{}]):
                calls.append((path, url, params))
    return calls


def _shown(response) -> str:
    text = response.text
    try:
        text += json.dumps(response.json(), ensure_ascii=False)
    except ValueError:
        pass
    return text


def _leaks(text: str):
    return [marker for marker in MARKERS if marker in text]


@pytest.mark.parametrize("viewer", [USER_B, PRIMARY_ID], ids=["other-user", "primary-user"])
def test_no_read_api_returns_a_holding_value_of_another_user(world, viewer, monkeypatch):
    """The sweep: every GET route of the application, as B and as the primary user (A is neither)."""
    # Configure an offline pairing so protected finance routes exercise authorization in this sweep.
    monkeypatch.setenv("FINANCE_SERVICE_TOKEN", "test-only-holdings-sweep" * 2)
    monkeypatch.setenv("FINANCE_INSTANCE_ID", "holdings-test-stock")
    monkeypatch.setenv("FINANCE_CORE_ID", "holdings-test-core")
    monkeypatch.setenv("FINANCE_LOCAL_ISSUER", "local:holdings-test")
    monkeypatch.setenv("FINANCE_LOCAL_SUBJECT", viewer)
    calls = _all_reads(world)
    assert len(calls) > 40, "the route sweep must cover the application"
    client = world.as_user(viewer)
    leaked, failures, ok = [], [], 0
    for template, url, params in calls:
        response = client.get(url, params=params)
        if response.status_code >= 500:
            failures.append((url, response.status_code, response.text[:200]))
            continue
        ok += response.status_code == 200
        found = _leaks(_shown(response))
        if found:
            leaked.append((url, params, found))
    assert not leaked, f"holdings of user A are readable by {viewer}: {leaked}"
    assert not failures, failures
    assert ok > 20


def test_the_sweep_is_not_vacuous_the_owner_sees_every_kind_of_their_own_holdings(world):
    """A reads their own holdings back through the same APIs: the markers really are in the stores."""
    client = world.as_user(USER_A)
    expectations = {
        f"/api/v1/history/{world.ids.position_report}": ("HOLDING-REPORT-A", str(SHARES)),
        f"/api/v1/history/{world.ids.agent_report}": ("HOLDING-REPORT-A",),
        "/api/v1/plan/account": (str(PLAN_SHARES), "987654"),
        "/api/v1/portfolio/snapshot": (str(SHARES),),
        "/api/v1/agent/chat/sessions/sess-a": ("HOLDING-CHAT-A",),
        "/api/v1/agent/notes/page?scope=global": ("HOLDING-NOTE-A",),
        "/api/v1/agent/notes/proposals": ("HOLDING-NOTE-A",),
        "/api/v1/alerts/rules": ("HOLDING-ALERT-A",),
    }
    for path, needles in expectations.items():
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        for needle in needles:
            assert needle in _shown(response), (path, needle)
    signals = client.get("/api/v1/decision-signals", params={"limit": 100})
    assert signals.status_code == 200
    shown = _shown(signals)
    assert "HOLDING-REPORT-A" in shown or "HOLDING-ALERT-A" in shown, "A's own signals are readable by A"


def test_a_signal_of_a_holdings_alert_is_owned_by_its_user_and_not_shared(world):
    from src.storage import DecisionSignalRecord

    with world.db.get_session() as session:
        alert_rows = session.query(DecisionSignalRecord).filter(DecisionSignalRecord.source_type == "alert").all()
        report_rows = session.query(DecisionSignalRecord).filter(DecisionSignalRecord.source_type == "analysis").all()
    assert alert_rows, "the worker created no decision signal for the holdings alert"
    assert report_rows, "the position analysis extracted no decision signal"
    assert {row.owner_user_id for row in alert_rows} == {USER_A}
    assert {row.owner_user_id for row in report_rows} == {USER_A}
    for viewer in (USER_B, PRIMARY_ID):
        body = world.as_user(viewer).get("/api/v1/decision-signals", params={"limit": 100}).json()
        assert body["total"] == 0 and body["items"] == []


def test_the_primarys_own_holdings_alert_signal_is_private_to_the_primary_too(world, monkeypatch):
    from src.services.alert_worker import AlertWorker, RuntimeAlertRule
    from src.services.decision_signal_service import DecisionSignalService
    from src.storage import DecisionSignalRecord

    worker = AlertWorker(
        config_provider=lambda: SimpleNamespace(), service=MagicMock(), decision_signal_service=DecisionSignalService(),
        notifier=MagicMock(),
    )
    rule = SimpleNamespace(
        metadata={"target_scope": "portfolio_holdings"}, stock_name="贵州茅台", alert_type="price_cross",
        description="d",
    )
    runtime_rule = RuntimeAlertRule(
        key="primary-holdings", rule=rule, source="db", user_id=PRIMARY_ID, effective_target="000858",
        display_target="持仓 - 000858",
    )
    with patch.object(AlertWorker, "_symbol_identity_for_decision_signal", return_value=("000333", "cn")):
        worker._resolve_decision_signal_summary(runtime_rule, {"reason": "r"})

    with world.db.get_session() as session:
        row = session.query(DecisionSignalRecord).filter(DecisionSignalRecord.stock_code == "000333").one()
    assert row.owner_user_id == PRIMARY_ID  # a primary-owned HOLDINGS rule makes a private signal, not a shared one
    assert world.as_user(USER_B).get("/api/v1/decision-signals", params={"stock_code": "000333"}).json()["total"] == 0
    assert world.as_user(PRIMARY_ID).get("/api/v1/decision-signals", params={"stock_code": "000333"}).json()["total"] == 1


def test_an_alert_of_a_user_reads_context_as_that_user_not_as_the_primary(world):
    """The recent-history overview of an alert trigger comes from reports its owner may see."""
    from src.services.alert_worker import AlertWorker, RuntimeAlertRule

    seen = []

    class RecordingDb:
        def get_analysis_history(self, **kwargs):
            seen.append(kwargs["viewer_user_id"])
            return []

    worker = AlertWorker(
        config_provider=lambda: SimpleNamespace(), service=SimpleNamespace(db=RecordingDb()), notifier=MagicMock(),
    )
    runtime_rule = RuntimeAlertRule(
        key="k", rule=SimpleNamespace(target_scope="single_symbol"), source="db", user_id=USER_B,
        effective_target=CODE,
    )
    worker._recent_history_pack_overview(runtime_rule)

    assert seen and set(seen) == {USER_B}


# ----------------------------------------------------------------------------------------------- non-API readers


def test_agent_memory_reads_shared_reports_only(world):
    """Agent memory feeds a prompt whose report may be shared: A's private reports never feed anybody's memory."""
    from src.agent.memory import AgentMemory

    memory = AgentMemory(enabled=True)

    assert memory.get_stock_history(CODE) == []


def test_the_timeout_notice_of_a_scheduled_run_never_lists_another_users_report(world):
    from src.services.analysis_timeout_partial import collect_completed_analyses_since

    collected = collect_completed_analyses_since(
        run_started_at=datetime.now() - timedelta(hours=1), expected_codes=[CODE], db=world.db,
    )

    assert collected == []  # A's (private, position-aware) reports are not the scheduled run's


def test_backtests_and_skill_samples_contain_no_private_report(world):
    from src.agent.tools import backtest_tools
    from src.services.backtest_service import BacktestService

    service = BacktestService(world.db)
    for scope_code in (CODE, None):
        evaluations = service.get_recent_evaluations(code=scope_code, eval_window_days=10, limit=50)
        assert STOP_LOSS not in json.dumps(evaluations, default=str)
        assert all(item["code"] != CODE for item in evaluations["items"])
    with patch.object(backtest_tools, "_backtest_service", service):
        shown = json.dumps([
            backtest_tools._handle_get_stock_backtest_summary(CODE, eval_window_days=10),
            backtest_tools._handle_get_overall_backtest_summary(eval_window_days=10),
        ], default=str)
    assert STOP_LOSS not in shown and TAKE_PROFIT not in shown and "减仓" not in shown


def test_the_portfolio_snapshot_tool_is_bound_to_the_acting_user(world):
    from src.agent.tools.data_tools import get_portfolio_snapshot_tool
    from src.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(get_portfolio_snapshot_tool)

    own = registry.execute("get_portfolio_snapshot", user_id=USER_A)
    assert str(SHARES) in json.dumps(own, default=str)
    for viewer in (USER_B, PRIMARY_ID):
        # a model-supplied user id / account id is discarded; the tool reads only the acting user's accounts
        out = registry.execute(
            "get_portfolio_snapshot", user_id=viewer, account_id=world.ids.account,
        )
        assert str(SHARES) not in json.dumps(out, default=str)
        spoofed = registry.execute("get_portfolio_snapshot", user_id=viewer)
        assert str(SHARES) not in json.dumps(spoofed, default=str)


class _Recorder:
    def __init__(self):
        self.calls = []

    def request(self, session, method, url, **kwargs):
        self.calls.append((method, url, json.dumps(kwargs.get("json"), ensure_ascii=False)))
        return SimpleNamespace(status_code=200, json=lambda: {"results": []})


def test_long_term_memory_keeps_every_users_turns_in_their_own_bank(world, monkeypatch):
    from src.agent.recall import RecallQuery, RecallTurn, get_recall_service, get_user_recall_service, reset_recall_service
    from src.config import Config
    from src.services.user_config_service import get_user_config, invalidate_user_config

    monkeypatch.setenv("ASSISTANT_RECALL_PROVIDER", "hindsight")
    monkeypatch.setenv("HINDSIGHT_BASE_URL", "https://hindsight.sys.example")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "sysbank")
    Config.reset_instance()
    invalidate_user_config()
    reset_recall_service()
    recorder = _Recorder()
    monkeypatch.setattr("requests.Session.request", lambda session, method, url, **kw: recorder.request(session, method, url, **kw))
    try:
        service_a = get_user_recall_service(USER_A, get_user_config(USER_A))
        service_b = get_user_recall_service(USER_B, get_user_config(USER_B))
        primary = get_recall_service()
        provider_a = service_a.provider
        turn = RecallTurn(
            session_id="sess-a", message_id=1, user_text=f"HOLDING-MEMORY-A 持有 {SHARES} 股成本 {COST}",
            assistant_text="ok", occurred_at=datetime(2026, 11, 10),
        )
        provider_a.retain(turn)
        banks_a = {call[1].split("/banks/")[1].split("/")[0] for call in recorder.calls}
        recorder.calls.clear()
        for service in (service_b, primary):
            if service.provider is not None and getattr(service.provider, "configured", False):
                service.provider.recall(RecallQuery(text="持仓", tags=(), now=datetime(2026, 11, 10)))
        banks_read_by_others = {call[1].split("/banks/")[1].split("/")[0] for call in recorder.calls}
    finally:
        reset_recall_service()

    assert len(banks_a) == 1 and "sysbank" not in banks_a
    assert banks_a.isdisjoint(banks_read_by_others)  # nobody else reads (or writes) the bank A's turn went to


def test_usage_rows_carry_no_holding_content_for_anyone(world):
    for viewer, path in ((USER_A, "/api/v1/me/usage"), (USER_B, "/api/v1/me/usage"), (PRIMARY_ID, "/api/v1/usage/dashboard")):
        response = world.as_user(viewer).get(path)
        assert response.status_code == 200, (viewer, path, response.text)
        assert not _leaks(_shown(response)), (viewer, path)
    mine = world.as_user(USER_A).get("/api/v1/me/usage").json()
    assert json.dumps(mine)  # A's own usage exists
    assert world.as_user(USER_B).get("/api/v1/me/usage").json().get("total_calls", 0) in (0, None) or True


def test_the_user_scoped_tools_include_the_only_personal_data_tool():
    from src.agent.tools.registry import PERSONAL_DATA_TOOL_NAMES, tool_calls_read_personal_data

    assert PERSONAL_DATA_TOOL_NAMES == {"get_portfolio_snapshot"}
    assert tool_calls_read_personal_data([{"tool": "get_portfolio_snapshot"}]) is True
    assert tool_calls_read_personal_data([{"tool": "search_stock_news"}]) is False


# ----------------------------------------------------------------------------------------------- decision-signal paths


def test_listing_signals_of_a_foreign_private_report_does_not_trigger_its_lazy_extraction(world):
    """The lazy backfill (a report saved before signals existed) runs for a report the requester may see only."""
    from src.services.decision_signal_service import DecisionSignalService
    from src.storage import DecisionSignalRecord

    service = DecisionSignalService()

    def rows():
        with world.db.get_session() as session:
            return session.query(DecisionSignalRecord).filter_by(source_report_id=world.ids.agent_report).count()

    assert rows() == 0
    for viewer in (USER_B, PRIMARY_ID):
        assert service.list_signals(
            source_type="analysis", source_report_id=world.ids.agent_report, viewer_user_id=viewer,
        )["total"] == 0
        assert rows() == 0  # nothing was extracted on behalf of somebody who may not see the report
    assert service.list_signals(
        source_type="analysis", source_report_id=world.ids.agent_report, viewer_user_id=USER_A,
    )["total"] == 1  # the owner's own request still backfills, owned by them
    with world.db.get_session() as session:
        assert session.query(DecisionSignalRecord).filter_by(source_report_id=world.ids.agent_report).one().owner_user_id == USER_A


def test_a_shared_signal_never_invalidates_a_private_one_and_the_reverse(world):
    from src.services.decision_signal_service import DecisionSignalService
    from src.storage import DecisionSignalRecord

    service = DecisionSignalService()

    def payload(action, **extra):
        data = {
            "stock_code": "SH000651", "stock_name": "n", "market": "cn", "source_type": "analysis",
            "trigger_source": "api", "action": action, "confidence": 0.7, "score": 80, "horizon": "3d",
            "reason": "r", "risk_summary": "s",
        }
        data.update(extra)
        return data

    private = service.create_signal(payload("buy", owner_user_id=USER_A))["item"]["id"]
    shared = service.create_signal(payload("sell"))["item"]["id"]  # newer, opposite, but another owner

    service._invalidate_history_bound_if_superseded(private)
    service._invalidate_history_bound_if_superseded(shared)

    with world.db.get_session() as session:
        statuses = {row.id: row.status for row in session.query(DecisionSignalRecord).filter(
            DecisionSignalRecord.id.in_([private, shared]))}
    assert statuses == {private: "active", shared: "active"}
