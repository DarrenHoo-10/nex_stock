# -*- coding: utf-8 -*-
"""Multi-user P2a: the next-day plan (account, holdings, runs, stored plans) is per user.

Runs the real app, router, ``get_daily_plan_service`` dependency, service and repository over a temp SQLite
database; only the acting user is switched (``tests/multi_user_helpers.py``). No network, no model call (the test
config has no analysis model, so plans are rule plans).
"""

from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from sqlalchemy import select

from api.deps import get_current_user
from api.v1.endpoints import daily_plan as plan_endpoint
from src.repositories.daily_plan_repo import DailyPlanRepository
from src.services import daily_plan_service as service_module
from src.services.daily_plan_schedule import run_scheduled_daily_plan
from src.services.daily_plan_service import DailyPlanService
from src.services.watchlist_service import WatchlistService
from src.storage import DailyPlanAccount, DailyPlanHolding, DailyPlanRun, DailyPlanStock, MarketStockDaily
from tests.board_fixtures import END_DATE, weekdays
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    prepare_multi_user_env,
    stub_dns,
    teardown_multi_user_env,
)

CODE = "000988"


def _seed_market(db, code: str = CODE, last_close: float = 48.35, days: int = 30) -> None:
    closes = [round(38.0 + (last_close - 38.0) * i / (days - 1), 2) for i in range(days)]
    with db.session_scope() as session:
        for index, (day, close) in enumerate(zip(weekdays(END_DATE, days), closes)):
            prev = closes[index - 1] if index else close
            session.add(MarketStockDaily(
                trade_date=day, code=code, name="华工科技", exchange_board="main", is_st=False,
                is_suspended=False, prev_close=prev, close=close, pct_chg=round((close / prev - 1) * 100, 2),
                amount=5e8, turnover_rate=2.0, limit_state="none",
            ))


@pytest.fixture
def mu(tmp_path, monkeypatch):
    service_module._ACTIVE.clear()
    # Runs stay "running" until the test finishes them: no background thread, no real work.
    monkeypatch.setattr(DailyPlanService, "_spawn_thread", staticmethod(lambda target: None))
    app = build_multi_user_app(tmp_path, monkeypatch)
    stub_dns(monkeypatch)
    for user in (USER_A, USER_B):  # POST /plan/runs needs the caller's own model (400 model_key_required otherwise)
        give_user_model(user)
    _seed_market(app.db)
    yield app
    service_module._ACTIVE.clear()
    teardown_multi_user_env()


def _put(client, cash, holdings):
    response = client.put("/api/v1/plan/holdings", json={"cash": cash, "holdings": holdings})
    assert response.status_code == 200, response.text
    return response.json()


def _holding(code=CODE, shares=100, cost=40.0):
    return {"code": code, "shares": shares, "cost": cost, "buy_date": "2026-01-05", "horizon": "swing"}


# ---------------------------------------------------------------------------- account / holdings
def test_account_and_holdings_are_private_to_each_user(mu):
    _put(mu.as_user(USER_A), 100_000, [_holding(shares=100, cost=40.0)])

    seen_by_b = mu.as_user(USER_B).get("/api/v1/plan/account").json()
    assert seen_by_b["cash"] == 0 and seen_by_b["holdings"] == [] and seen_by_b["updated_at"] is None
    assert mu.as_user(PRIMARY_ID).get("/api/v1/plan/account").json()["holdings"] == []

    # B saving a snapshot replaces only B's rows (same code, different numbers) and leaves A untouched
    _put(mu.as_user(USER_B), 5_000, [_holding(shares=300, cost=45.0)])
    account_a = mu.as_user(USER_A).get("/api/v1/plan/account").json()
    account_b = mu.as_user(USER_B).get("/api/v1/plan/account").json()
    assert account_a["cash"] == 100_000 and [(h["shares"], h["cost"]) for h in account_a["holdings"]] == [(100, 40.0)]
    assert account_b["cash"] == 5_000 and [(h["shares"], h["cost"]) for h in account_b["holdings"]] == [(300, 45.0)]

    # clearing B's holdings (the only delete path) does not delete A's
    _put(mu.as_user(USER_B), 0, [])
    assert len(mu.as_user(USER_A).get("/api/v1/plan/account").json()["holdings"]) == 1

    with mu.db.get_session() as session:
        accounts = session.execute(select(DailyPlanAccount.user_id)).scalars().all()
        holdings = session.execute(select(DailyPlanHolding.user_id, DailyPlanHolding.shares)).all()
    assert sorted(accounts) == [USER_A, USER_B]
    assert holdings == [(USER_A, 100)]


def test_sizing_preview_uses_the_callers_own_account(mu):
    _put(mu.as_user(USER_A), 100_000, [])
    _put(mu.as_user(USER_B), 0, [])
    body = {"code": CODE, "price": 47.0, "horizon": "short"}

    sizing_a = mu.as_user(USER_A).post("/api/v1/plan/sizing", json=body).json()
    sizing_b = mu.as_user(USER_B).post("/api/v1/plan/sizing", json=body).json()

    assert sizing_a["status"] == "ok" and sizing_a["shares"] > 0
    assert sizing_b["shares"] == 0


# ---------------------------------------------------------------------------- watchlist -> universe
def test_universe_is_holdings_plus_the_callers_own_watchlist(tmp_path, monkeypatch):
    service_module._ACTIVE.clear()
    app = build_multi_user_app(tmp_path, monkeypatch, stock_list="600519")
    try:
        _seed_market(app.db, "600519", 1500.0)
        _seed_market(app.db, CODE)
        # primary: STOCK_LIST from .env; user A: user_watchlist rows
        assert app.as_user(PRIMARY_ID).post("/api/v1/stocks/watchlist/add", json={"stock_code": "000001"}).status_code == 200
        assert app.as_user(USER_A).post("/api/v1/stocks/watchlist/add", json={"stock_code": CODE}).status_code == 200

        primary_codes = [i["code"] for i in app.as_user(PRIMARY_ID).get("/api/v1/plan/stocks").json()["items"]]
        a_codes = [i["code"] for i in app.as_user(USER_A).get("/api/v1/plan/stocks").json()["items"]]
        b_codes = [i["code"] for i in app.as_user(USER_B).get("/api/v1/plan/stocks").json()["items"]]
        assert primary_codes == ["600519", "000001"]
        assert a_codes == [CODE]
        assert b_codes == []

        # a held code is listed first and only for its owner
        _put(app.as_user(USER_B), 10_000, [_holding("600519", 100, 1400.0)])
        assert [i["code"] for i in app.as_user(USER_B).get("/api/v1/plan/stocks").json()["items"]] == ["600519"]
        assert "600519" not in [i["code"] for i in app.as_user(USER_A).get("/api/v1/plan/stocks").json()["items"]]
    finally:
        service_module._ACTIVE.clear()
        teardown_multi_user_env()


# ---------------------------------------------------------------------------- stored stock plans
def test_stock_plans_are_stored_and_read_per_user(mu):
    _put(mu.as_user(USER_A), 100_000, [_holding(shares=100, cost=40.0)])
    _put(mu.as_user(USER_B), 50_000, [_holding(shares=300, cost=45.0)])

    plan_a = mu.as_user(USER_A).get(f"/api/v1/plan/stocks/{CODE}")
    plan_b = mu.as_user(USER_B).get(f"/api/v1/plan/stocks/{CODE}")
    assert plan_a.status_code == 200 and plan_b.status_code == 200
    assert plan_a.json()["held"] is True and plan_b.json()["held"] is True

    with mu.db.get_session() as session:
        rows = session.execute(select(DailyPlanStock.user_id, DailyPlanStock.code, DailyPlanStock.fingerprint)).all()
    # A code can have several plan rows per day (the newest wins, e.g. after a model refine); every row
    # belongs to its own user and carries that user's holding fingerprint.
    assert sorted(set(rows)) == [(USER_A, CODE, "held:100:40.0000:2026-01-05:swing"),
                                 (USER_B, CODE, "held:300:45.0000:2026-01-05:swing")]

    # the stored plan of A is not visible to a user who neither holds nor watches the code
    stranger = mu.as_user(PRIMARY_ID).get(f"/api/v1/plan/stocks/{CODE}")
    assert stranger.status_code == 404 and stranger.json()["error"] == "not_found"
    listing = mu.as_user(USER_A).get("/api/v1/plan/stocks").json()["items"]
    assert [(i["code"], i["status"]) for i in listing] == [(CODE, "ready")]
    assert mu.as_user(PRIMARY_ID).get("/api/v1/plan/stocks").json()["items"] == []

    # the repository itself never crosses users either
    repo = DailyPlanRepository(mu.db)
    latest = repo.latest_stock_plans(USER_A, [CODE], END_DATE)
    assert latest[CODE]["fingerprint"] == "held:100:40.0000:2026-01-05:swing"
    assert repo.latest_stock_plans("someone-else", [CODE], END_DATE) == {}


# ---------------------------------------------------------------------------- runs
def test_runs_are_private_and_the_run_lock_is_per_user(mu):
    run_a = mu.as_user(USER_A).post("/api/v1/plan/runs")
    assert run_a.status_code == 202
    run_a_id = run_a.json()["id"]

    # another user is not blocked by A's running run ...
    run_b = mu.as_user(USER_B).post("/api/v1/plan/runs")
    assert run_b.status_code == 202
    # ... while A cannot start a second one
    again = mu.as_user(USER_A).post("/api/v1/plan/runs")
    assert again.status_code == 409 and again.json()["error"] == "daily_plan_run_conflict"

    # run ids of other users are 404 (same body as an unknown id), latest only shows the caller's own run
    foreign = mu.as_user(USER_B).get(f"/api/v1/plan/runs/{run_a_id}")
    unknown = mu.as_user(USER_B).get("/api/v1/plan/runs/plan-20990101-deadbeef")
    assert foreign.status_code == 404 and unknown.status_code == 404
    assert foreign.json()["error"] == unknown.json()["error"] == "not_found"
    assert foreign.json()["message"].replace(run_a_id, "X") == unknown.json()["message"].replace(
        "plan-20990101-deadbeef", "X")
    assert mu.as_user(USER_B).get("/api/v1/plan/runs/latest").json()["run"]["id"] == run_b.json()["id"]
    assert mu.as_user(PRIMARY_ID).get("/api/v1/plan/runs/latest").json()["run"] is None
    assert mu.as_user(USER_A).get(f"/api/v1/plan/runs/{run_a_id}").json()["id"] == run_a_id

    # B polling its own state does not mark A's still-running run as interrupted (and vice versa)
    assert mu.as_user(USER_A).get("/api/v1/plan/runs/latest").json()["run"]["status"] == "running"

    with mu.db.get_session() as session:
        owners = dict(session.execute(select(DailyPlanRun.run_id, DailyPlanRun.user_id)).all())
    assert owners == {run_a_id: USER_A, run_b.json()["id"]: USER_B}


def test_dead_process_run_of_one_user_is_failed_only_for_that_user(mu):
    run_a = mu.as_user(USER_A).post("/api/v1/plan/runs").json()["id"]
    run_b = mu.as_user(USER_B).post("/api/v1/plan/runs").json()["id"]
    service_module._ACTIVE.pop(USER_A)  # A's process is gone, B's is alive

    assert mu.as_user(USER_A).get(f"/api/v1/plan/runs/{run_a}").json()["status"] == "failed"
    assert mu.as_user(USER_B).get(f"/api/v1/plan/runs/{run_b}").json()["status"] == "running"


# ---------------------------------------------------------------------------- routes / identity
def _depends_on(dependant, target) -> bool:
    return any(dep.call is target or _depends_on(dep, target) for dep in dependant.dependencies)


def test_every_plan_route_resolves_the_acting_user():
    routes = [route for route in plan_endpoint.router.routes if hasattr(route, "dependant")]
    assert len(routes) >= 8
    for route in routes:
        assert _depends_on(route.dependant, get_current_user), route.path
        assert "user_id" not in {param.name for param in route.dependant.query_params + route.dependant.path_params}


def test_clients_cannot_choose_the_user(mu):
    _put(mu.as_user(USER_A), 1_000, [_holding()])
    response = mu.as_user(USER_B).get("/api/v1/plan/account", params={"user_id": USER_A})
    assert response.json()["holdings"] == [] and response.json()["cash"] == 0
    response = mu.as_user(USER_B).put(
        "/api/v1/plan/holdings", json={"cash": 1, "holdings": [], "user_id": USER_A})
    assert response.status_code == 200
    assert mu.as_user(USER_A).get("/api/v1/plan/account").json()["cash"] == 1_000


# ---------------------------------------------------------------------------- service level
def test_service_binds_the_primary_user_by_default(tmp_path, monkeypatch):
    prepare_multi_user_env(tmp_path, monkeypatch)
    try:
        assert DailyPlanService().user_id == PRIMARY_ID
        assert DailyPlanService(user_id=USER_A).user_id == USER_A
    finally:
        teardown_multi_user_env()


def test_non_primary_universe_ignores_the_env_stock_list(tmp_path, monkeypatch):
    prepare_multi_user_env(tmp_path, monkeypatch)
    try:
        from src.storage import DatabaseManager

        db = DatabaseManager.get_instance()
        config = SimpleNamespace(stock_list=["600519"], max_workers=1, litellm_model="")  # primary's STOCK_LIST
        primary = DailyPlanService(db_manager=db, config=config, user_id=PRIMARY_ID, calendar_fn=None)
        user = DailyPlanService(db_manager=db, config=config, user_id=USER_A, calendar_fn=None)
        assert primary._watchlist() == (["600519"], [])
        assert user._watchlist() == ([], [])
        WatchlistService(db_manager=db).add(USER_A, "000001")
        assert user._watchlist() == (["000001"], [])
        assert primary._watchlist() == (["600519"], [])
    finally:
        teardown_multi_user_env()


def test_scheduled_run_belongs_to_the_primary_user(tmp_path, monkeypatch):
    """P2: the scheduler runs the plan for the primary user only, with the primary's watchlist and account."""
    service_module._ACTIVE.clear()
    app = build_multi_user_app(tmp_path, monkeypatch, stock_list=CODE)
    try:
        _seed_market(app.db)
        _seed_market(app.db, "600519", 1500.0)
        _put(app.as_user(USER_A), 100_000, [_holding("600519", 100, 1400.0)])  # another user's holding
        cfg = SimpleNamespace(daily_plan_enabled=True, schedule_time="18:00", schedule_times=["18:00"])

        result = run_scheduled_daily_plan(lambda: cfg, today=date(2026, 11, 6), is_trading_day=lambda d: True)

        assert result["status"] in ("succeeded", "partial"), result
        with app.db.get_session() as session:
            runs = session.execute(select(DailyPlanRun.user_id, DailyPlanRun.trigger)).all()
            plans = session.execute(select(DailyPlanStock.user_id, DailyPlanStock.code)).all()
        assert runs == [(PRIMARY_ID, "schedule")]
        assert plans == [(PRIMARY_ID, CODE)]
        assert app.as_user(USER_A).get("/api/v1/plan/runs/latest").json()["run"] is None
    finally:
        service_module._ACTIVE.clear()
        teardown_multi_user_env()


def _plan_service(app, user_id, pushed):
    """Real service over the temp database; only the model, the clock and the Feishu sender are doubles."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    config = SimpleNamespace(
        litellm_model="", max_workers=1, stock_list=[], daily_plan_enabled=True, position_stop_loss_pct=10.0,
        position_max_total_pct=100.0, position_max_holdings=5, position_max_single_pct=None,
        position_stop_mode="fixed", schedule_time="16:00", schedule_times=["16:00"],
    )
    anomalies = SimpleNamespace(repo=SimpleNamespace(latest_completed_run=lambda trade_date=None: None))
    return DailyPlanService(
        db_manager=app.db, config=config, spawn=lambda fn: fn(), pusher=pushed.append,
        push_configured=lambda: True,  # the (primary user's) Feishu channel exists
        anomaly_service=anomalies, calendar_fn=None,
        now_fn=lambda: datetime(2026, 11, 6, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")), workers=1, user_id=user_id,
    )


def test_non_primary_run_never_pushes_through_the_global_feishu_channel(tmp_path, monkeypatch):
    """A non-primary user's plan (their holdings) must not reach the server's Feishu channel: the push step is
    skipped with its own reason and the pusher is never called; the primary user's push is unchanged."""
    service_module._ACTIVE.clear()
    app = build_multi_user_app(tmp_path, monkeypatch)
    try:
        _seed_market(app.db)
        pushed = []
        for user_id in (USER_A, PRIMARY_ID):
            service = _plan_service(app, user_id, pushed)
            service.set_holdings_snapshot(100_000.0, [_holding()])
            before = len(pushed)

            run = service.trigger_run(wait=True)

            push_step = {step["key"]: step for step in run["steps"]}["push"]
            if user_id == USER_A:
                assert len(pushed) == before == 0
                assert run["push"]["status"] == "skipped" and run["push"]["configured"] is False
                assert run["push"]["sent_at"] is None and run["push"]["error"] is None
                assert push_step["status"] == "skipped" and "个人推送渠道" in push_step["detail"]
                assert run["push_preview_markdown"]  # the plan is still readable in the web page
                assert run["stock_succeeded"] == 1
            else:
                assert len(pushed) == before + 1
                assert run["push"]["status"] == "sent" and push_step["status"] == "succeeded"
    finally:
        service_module._ACTIVE.clear()
        teardown_multi_user_env()


def test_non_primary_run_record_does_not_claim_push_configured_at_start(tmp_path, monkeypatch):
    service_module._ACTIVE.clear()
    app = build_multi_user_app(tmp_path, monkeypatch)
    try:
        _seed_market(app.db)
        pushed = []
        service = _plan_service(app, USER_B, pushed)
        service.set_holdings_snapshot(100_000.0, [_holding()])
        run = service.trigger_run(wait=True)
        assert run["push"]["configured"] is False and pushed == []
        assert _plan_service(app, USER_B, pushed)._is_push_configured() is False
        assert _plan_service(app, PRIMARY_ID, pushed)._is_push_configured() is True
    finally:
        service_module._ACTIVE.clear()
        teardown_multi_user_env()
