# -*- coding: utf-8 -*-
"""API tests for /api/v1/plan (service is faked, no network / LLM)."""

from __future__ import annotations

import sys
import types
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.deps import get_daily_plan_service
from api.middlewares.error_handler import add_error_handlers
from api.v1.endpoints import daily_plan as plan_endpoint


class FakeRunConflictError(Exception):
    pass


class FakeValidationError(ValueError):
    pass


@pytest.fixture
def fake_service_module(monkeypatch):
    """Provide the exception classes the endpoint resolves lazily."""
    module = types.ModuleType("src.services.daily_plan_service")
    module.DailyPlanRunConflictError = FakeRunConflictError
    module.DailyPlanValidationError = FakeValidationError
    monkeypatch.setitem(sys.modules, "src.services.daily_plan_service", module)
    return module


def _stop(price=42.3):
    return {
        "price": price,
        "mode": "fixed",
        "formula": "47.00 × 0.90",
        "tech_support": 44.8,
        "warning": "止损会在支撑之前触发",
    }


def _sizing(**overrides):
    data = {
        "status": "ok",
        "message": None,
        "price": 47.0,
        "lot": 100,
        "by_single": 2127.7,
        "by_total": 2127.7,
        "by_cash": 2127.7,
        "raw_shares": 2127.7,
        "shares": 2100,
        "amount": 98700.0,
        "capital_pct": 98.7,
        "post_position_pct": 98.7,
        "stop": _stop(),
        "max_loss": 9870.0,
        "limit_down_price": 42.3,
        "limit_down_loss": 9870.0,
        "steps": ["可用资金 100000 元", "47.00 元可买 2127 股，取整到 2100 股"],
    }
    data.update(overrides)
    return data


def _account():
    return {
        "cash": 1184.0,
        "market_value": 98816.0,
        "total_capital": 100000.0,
        "position_pct": 98.8,
        "holdings": [
            {
                "code": "002475",
                "shares": 2400,
                "cost": 40.2,
                "buy_date": "2026-11-03",
                "horizon": "swing",
                "name": "立讯精密",
                "board_market": "main",
                "is_st": False,
                "close": 41.17,
                "change_pct": 1.2,
                "market_value": 98808.0,
                "pnl": 2328.0,
                "pnl_pct": 2.41,
                "sellable": True,
                "sellable_note": None,
            }
        ],
        "rules": {
            "stop_loss_pct": 10.0,
            "max_total_pct": 100.0,
            "max_holdings": 1,
            "max_single_pct": 100.0,
            "max_single_pct_derived": True,
            "stop_mode": "fixed",
        },
        "updated_at": "2026-11-07T09:30:00+08:00",
    }


def _stock_plan():
    buy_plan = {
        "verdict": "buy",
        "headline": "回踩 47.00 附近买入 2100 股",
        "reason": None,
        "entry_price": 47.0,
        "entry_zone": [46.8, 47.2],
        "breakout_price": 49.5,
        "invalid_price": 45.9,
        "take_profit_price": 52.0,
        "conditions": [
            {
                "if_text": "开盘回踩 47.00",
                "then_text": "买入 2100 股",
                "action": "buy",
                "shares": 2100,
                "price": 47.0,
                "outside_limit": False,
            }
        ],
        "sizing": _sizing(),
        "stop": _stop(),
    }
    return {
        "code": "000988",
        "name": "华工科技",
        "board_market": "main",
        "is_st": False,
        "held": False,
        "close": 48.1,
        "change_pct": 2.3,
        "verdict": "buy",
        "headline": "回踩 47.00 附近买入 2100 股",
        "status": "ready",
        "failure_reason": None,
        "trade_date": "2026-11-06",
        "next_trade_date": "2026-11-09",
        "situation": "放量突破平台，回踩不破可买",
        "horizons": {
            "short": buy_plan,
            "swing": {"verdict": "skip", "headline": "波段不给计划", "reason": "板块持续性不足"},
        },
        "default_horizon": "short",
        "limit_pct": 10.0,
        "next_limit_up": 52.91,
        "next_limit_down": 43.29,
        "levels": [
            {"kind": "limit_up", "price": 52.91, "label": "涨停"},
            {"kind": "entry", "price": 47.0, "label": "买入价"},
            {"kind": "stop", "price": 42.3, "label": "止损"},
        ],
        "switch": {
            "decision": "keep",
            "summary": "暂不换股",
            "candidate": {"code": "002475", "name": "立讯精密", "reason": "持仓中", "verdict": "hold"},
            "conditions": [{"text": "候选明显更强", "met": False}, {"text": "持仓转弱", "met": None}],
            "costs": ["T+1 无法当日回转"],
        },
        "risks": ["板块高位分歧"],
        "data_gaps": [],
        "model": {"model": "auto/test-model", "fallback_from": None, "fallback_reason": None},
    }


def _plan_run(status="running", **overrides):
    data = {
        "id": "run-20261107-001",
        "trade_date": "2026-11-06",
        "status": status,
        "trigger": "manual",
        "started_at": "2026-11-07T20:00:00+08:00",
        "finished_at": None,
        "duration_sec": None,
        "steps": [
            {"key": "boards", "label": "板块异动", "status": "succeeded", "finished_at": "2026-11-07T20:00:30+08:00", "detail": None},
            {"key": "plans", "label": "个股计划", "status": "running", "finished_at": None, "detail": "2/3"},
        ],
        "stock_total": 3,
        "stock_succeeded": 2,
        "stock_failed": 1,
        "failures": [{"code": "300750", "name": "宁德时代", "reason": "行情缺失"}],
        "push": {"channel": "feishu", "status": "pending", "sent_at": None, "error": None, "configured": True},
        "push_preview_markdown": "# 次日计划",
        "model_calls": [{"step": "plans", "calls": 3, "model": "auto/test-model", "note": None}],
        "data_gaps": [],
    }
    data.update(overrides)
    return data


class FakePlanService:
    def __init__(self):
        self.calls = []
        self.stock_plan = _stock_plan()
        self.run = _plan_run()
        self.latest = _plan_run(status="succeeded")
        self.raises = None

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises

    def get_account(self):
        self._maybe_raise()
        return _account()

    def set_holdings_snapshot(self, cash, holdings):
        self.calls.append(("set_holdings_snapshot", cash, holdings))
        self._maybe_raise()
        return _account()

    def list_stock_plans(self, trade_date):
        self.calls.append(("list_stock_plans", trade_date))
        self._maybe_raise()
        return {
            "trade_date": "2026-11-06",
            "next_trade_date": "2026-11-09",
            "items": [
                {"code": "002475", "name": "立讯精密", "board_market": "main", "is_st": False, "held": True, "close": 41.17, "change_pct": 1.2, "verdict": "hold", "headline": "持有，跌破 37.00 清仓", "status": "ready", "failure_reason": None},
                {"code": "000988", "name": "华工科技", "board_market": "main", "is_st": False, "held": False, "close": 48.1, "change_pct": 2.3, "verdict": "buy", "headline": "回踩 47.00 买入 2100 股", "status": "ready", "failure_reason": None},
                {"code": "300750", "name": "宁德时代", "board_market": "chinext", "is_st": False, "held": False, "close": None, "change_pct": None, "verdict": None, "headline": "", "status": "failed", "failure_reason": "行情缺失"},
            ],
        }

    def get_stock_plan(self, code, trade_date):
        self.calls.append(("get_stock_plan", code, trade_date))
        self._maybe_raise()
        return self.stock_plan

    def preview_sizing(self, code, price, horizon):
        self.calls.append(("preview_sizing", code, price, horizon))
        self._maybe_raise()
        return _sizing()

    def get_latest_run(self):
        self._maybe_raise()
        return self.latest

    def get_run(self, run_id):
        self.calls.append(("get_run", run_id))
        self._maybe_raise()
        return self.run

    def trigger_run(self):
        self._maybe_raise()
        return self.run


@pytest.fixture
def service():
    return FakePlanService()


@pytest.fixture
def client(service):
    app = FastAPI()
    app.include_router(plan_endpoint.router, prefix="/api/v1/plan")
    add_error_handlers(app)
    app.dependency_overrides[get_daily_plan_service] = lambda: service
    return TestClient(app)


# ---------------------------------------------------------------- happy paths


def test_get_account_shape(client):
    response = client.get("/api/v1/plan/account")
    assert response.status_code == 200
    body = response.json()
    assert body["total_capital"] == 100000.0
    assert body["rules"]["max_single_pct_derived"] is True
    assert body["rules"]["stop_mode"] == "fixed"
    holding = body["holdings"][0]
    assert (holding["code"], holding["shares"], holding["cost"]) == ("002475", 2400, 40.2)
    assert holding["sellable"] is True and holding["horizon"] == "swing"


def test_put_holdings_passes_plain_dicts_to_service(client, service):
    payload = {
        "cash": 1184,
        "holdings": [{"code": "002475", "shares": 2400, "cost": 40.2, "buy_date": "2026-11-03", "horizon": "swing"}],
    }
    response = client.put("/api/v1/plan/holdings", json=payload)
    assert response.status_code == 200
    assert response.json()["holdings"][0]["name"] == "立讯精密"
    name, cash, holdings = service.calls[-1]
    assert name == "set_holdings_snapshot"
    assert cash == 1184
    assert holdings == [{"code": "002475", "shares": 2400, "cost": 40.2, "buy_date": "2026-11-03", "horizon": "swing"}]


def test_put_holdings_allows_clearing_everything(client, service):
    response = client.put("/api/v1/plan/holdings", json={"cash": 0, "holdings": []})
    assert response.status_code == 200
    assert service.calls[-1] == ("set_holdings_snapshot", 0, [])


def test_list_stock_plans_shape(client, service):
    response = client.get("/api/v1/plan/stocks?trade_date=2026-11-06")
    assert response.status_code == 200
    body = response.json()
    assert body["next_trade_date"] == "2026-11-09"
    assert [item["code"] for item in body["items"]] == ["002475", "000988", "300750"]
    assert body["items"][2]["status"] == "failed"
    assert body["items"][2]["verdict"] is None
    assert service.calls[-1] == ("list_stock_plans", date(2026, 11, 6))


def test_get_stock_plan_shape(client, service):
    response = client.get("/api/v1/plan/stocks/000988")
    assert response.status_code == 200
    body = response.json()
    assert service.calls[-1] == ("get_stock_plan", "000988", None)
    short = body["horizons"]["short"]
    assert short["verdict"] == "buy"
    assert short["entry_zone"] == [46.8, 47.2]
    assert short["sizing"]["shares"] == 2100
    assert short["sizing"]["amount"] == 98700.0
    assert short["sizing"]["stop"]["price"] == 42.3
    assert short["sizing"]["max_loss"] == 9870.0
    assert short["conditions"][0]["shares"] == 2100
    assert body["horizons"]["swing"]["reason"] == "板块持续性不足"
    assert body["horizons"]["swing"]["sizing"] is None
    assert body["switch"]["decision"] == "keep"
    assert body["switch"]["conditions"][1]["met"] is None
    assert body["model"]["model"] == "auto/test-model"
    assert {level["kind"] for level in body["levels"]} == {"limit_up", "entry", "stop"}


def test_get_stock_plan_allows_null_horizon(client, service):
    service.stock_plan["horizons"]["swing"] = None
    response = client.get("/api/v1/plan/stocks/000988?trade_date=2026-11-06")
    assert response.status_code == 200
    assert response.json()["horizons"]["swing"] is None
    assert service.calls[-1] == ("get_stock_plan", "000988", date(2026, 11, 6))


def test_preview_sizing(client, service):
    response = client.post("/api/v1/plan/sizing", json={"code": "000988", "price": 47.0, "horizon": "short"})
    assert response.status_code == 200
    body = response.json()
    assert body["shares"] == 2100
    assert body["amount"] == 98700.0
    assert body["stop"]["price"] == 42.3
    assert body["max_loss"] == 9870.0
    assert body["status"] == "ok"
    assert service.calls[-1] == ("preview_sizing", "000988", 47.0, "short")


def test_latest_run_returns_run(client):
    response = client.get("/api/v1/plan/runs/latest")
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["status"] == "succeeded"
    assert run["push"]["channel"] == "feishu"
    assert run["steps"][0]["key"] == "boards"


def test_latest_run_null_when_no_runs(client, service):
    service.latest = None
    response = client.get("/api/v1/plan/runs/latest")
    assert response.status_code == 200
    assert response.json() == {"run": None}


def test_latest_route_is_not_captured_by_run_id(client, service):
    client.get("/api/v1/plan/runs/latest")
    assert not any(call[0] == "get_run" for call in service.calls)
    paths = [route.path for route in plan_endpoint.router.routes]
    assert paths.index("/runs/latest") < paths.index("/runs/{run_id}")


def test_get_run_by_id(client, service):
    response = client.get("/api/v1/plan/runs/run-20261107-001")
    assert response.status_code == 200
    assert response.json()["id"] == "run-20261107-001"
    assert service.calls[-1] == ("get_run", "run-20261107-001")


def test_trigger_run_returns_202(client):
    response = client.post("/api/v1/plan/runs")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "running"
    assert body["trigger"] == "manual"
    assert body["failures"] == [{"code": "300750", "name": "宁德时代", "reason": "行情缺失"}]


# ---------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    "payload",
    [
        {"cash": 100, "holdings": [{"code": "24750", "shares": 100, "cost": 10}]},
        {"cash": 100, "holdings": [{"code": "ABC123", "shares": 100, "cost": 10}]},
        {"cash": 100, "holdings": [{"code": "002475", "shares": 0, "cost": 10}]},
        {"cash": 100, "holdings": [{"code": "002475", "shares": 100.5, "cost": 10}]},
        {"cash": 100, "holdings": [{"code": "002475", "shares": 100, "cost": 0}]},
        {"cash": -1, "holdings": []},
        {"cash": 100, "holdings": [{"code": "002475", "shares": 100, "cost": 10, "buy_date": "11/03"}]},
        {"cash": 100, "holdings": [{"code": "002475", "shares": 100, "cost": 10, "horizon": "long"}]},
        {"cash": 100, "holdings": [{"code": f"{i:06d}", "shares": 100, "cost": 10} for i in range(51)]},
    ],
)
def test_put_holdings_rejects_invalid_input(client, service, payload):
    response = client.put("/api/v1/plan/holdings", json=payload)
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert not any(call[0] == "set_holdings_snapshot" for call in service.calls)


def test_put_holdings_accepts_fifty_rows(client):
    payload = {"cash": 0, "holdings": [{"code": f"{i:06d}", "shares": 100, "cost": 10} for i in range(50)]}
    assert client.put("/api/v1/plan/holdings", json=payload).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "00098", "price": 47.0, "horizon": "short"},
        {"code": "000988", "price": 0, "horizon": "short"},
        {"code": "000988", "price": -3, "horizon": "short"},
        {"code": "000988", "price": 47.0, "horizon": "long"},
        {"code": "000988", "price": 47.0},
    ],
)
def test_sizing_rejects_invalid_input(client, service, payload):
    response = client.post("/api/v1/plan/sizing", json=payload)
    assert response.status_code == 422
    assert not any(call[0] == "preview_sizing" for call in service.calls)


def test_stock_code_path_must_be_six_digits(client, service):
    assert client.get("/api/v1/plan/stocks/AAPL").status_code == 422
    assert not any(call[0] == "get_stock_plan" for call in service.calls)


def test_trade_date_query_must_be_a_date(client):
    assert client.get("/api/v1/plan/stocks?trade_date=yesterday").status_code == 422


# ---------------------------------------------------------------- error mapping


def test_service_validation_error_is_400(client, service, fake_service_module):
    service.raises = FakeValidationError("持仓代码重复：002475")
    response = client.put("/api/v1/plan/holdings", json={"cash": 1, "holdings": []})
    assert response.status_code == 400
    assert response.json() == {"error": "validation_error", "message": "持仓代码重复：002475"}


def test_sizing_validation_error_is_400(client, service, fake_service_module):
    service.raises = FakeValidationError("该股票不在自选或持仓中")
    response = client.post("/api/v1/plan/sizing", json={"code": "000988", "price": 47.0, "horizon": "short"})
    assert response.status_code == 400
    assert response.json()["message"] == "该股票不在自选或持仓中"


def test_stock_plan_not_found_is_404(client, service):
    service.stock_plan = None
    response = client.get("/api/v1/plan/stocks/600000")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "600000" in body["message"]


def test_run_not_found_is_404(client, service):
    service.run = None
    response = client.get("/api/v1/plan/runs/missing")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_trigger_run_conflict_is_409(client, service, fake_service_module):
    service.raises = FakeRunConflictError("running")
    response = client.post("/api/v1/plan/runs")
    assert response.status_code == 409
    assert response.json()["error"] == "daily_plan_run_conflict"


def test_unexpected_error_is_500(client, service, fake_service_module):
    service.raises = RuntimeError("db down")
    response = client.get("/api/v1/plan/account")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


def test_unexpected_error_is_500_when_exception_module_missing(client, service, monkeypatch):
    monkeypatch.setitem(sys.modules, "src.services.daily_plan_service", None)
    service.raises = RuntimeError("db down")
    response = client.post("/api/v1/plan/runs")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


def test_malformed_service_payload_is_500(client, service):
    service.stock_plan = {"code": "000988"}
    response = client.get("/api/v1/plan/stocks/000988")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


def test_service_unavailable_is_503_when_module_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "src.services.daily_plan_service", None)
    app = FastAPI()
    app.include_router(plan_endpoint.router, prefix="/api/v1/plan")
    add_error_handlers(app)
    client = TestClient(app)
    expected = {"error": "daily_plan_unavailable", "message": "次日计划服务未就绪"}
    requests = [
        client.get("/api/v1/plan/account"),
        client.put("/api/v1/plan/holdings", json={"cash": 0, "holdings": []}),
        client.get("/api/v1/plan/stocks"),
        client.get("/api/v1/plan/stocks/000988"),
        client.post("/api/v1/plan/sizing", json={"code": "000988", "price": 47.0, "horizon": "short"}),
        client.get("/api/v1/plan/runs/latest"),
        client.get("/api/v1/plan/runs/abc"),
        client.post("/api/v1/plan/runs"),
    ]
    for response in requests:
        assert response.status_code == 503
        assert response.json() == expected


def test_router_is_mounted_in_v1_router():
    from api.v1.router import router as v1_router

    app = FastAPI()
    app.include_router(v1_router, prefix="/api/v1")
    paths = set(app.openapi()["paths"])
    for path in (
        "/api/v1/plan/account",
        "/api/v1/plan/holdings",
        "/api/v1/plan/stocks",
        "/api/v1/plan/stocks/{code}",
        "/api/v1/plan/sizing",
        "/api/v1/plan/runs/latest",
        "/api/v1/plan/runs/{run_id}",
        "/api/v1/plan/runs",
    ):
        assert path in paths
