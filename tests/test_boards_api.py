# -*- coding: utf-8 -*-
"""API tests for /api/v1/boards (services are faked, no network / LLM)."""

from __future__ import annotations

import json
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.deps import get_board_anomaly_service, get_system_config_service
from api.middlewares.error_handler import add_error_handlers
from api.v1.endpoints import boards as boards_endpoint
from src.services.system_config_service import ConfigConflictError, ConfigValidationError


def _board_row(**overrides):
    row = {
        "id": "industry:BK0448",
        "name": "消费电子",
        "board_type": "industry",
        "state": "first_launch",
        "tags": ["mainline", "supported"],
        "change_pct": 3.42,
        "change_5d_pct": 6.1,
        "excess_pct": 2.8,
        "amount_ratio": 1.8,
        "breadth_pct": 86.5,
        "limit_up_count": None,
        "broken_rate_pct": None,
        "reason": "放量上涨，上涨家数占比高",
    }
    row.update(overrides)
    return row


def _threshold(**overrides):
    item = {
        "key": "amount_ratio_mild",
        "label": "温和放量",
        "value": 1.5,
        "default": 1.5,
        "display": "成交额 ≥ 5 日均值 1.5 倍",
        "unit": "倍",
        "evidence": "medium",
        "note": None,
        "overridden": False,
    }
    item.update(overrides)
    return item


class FakeBoardService:
    def __init__(self):
        self.calls = []
        self.thresholds = [_threshold()]
        self.validation_errors = []
        self.detail_result = "default"

    def list_anomalies(self, trade_date, board_type, state):
        self.calls.append(("list_anomalies", trade_date, board_type, state))
        return {
            "trade_date": "2026-11-06",
            "market": {
                "index_name": "中证全指",
                "index_change_pct": 0.35,
                "up_count": 3120,
                "down_count": 1850,
                "limit_up_count": 62,
                "limit_down_count": 4,
                "turnover_yi": 11230.5,
                "broad_rally": False,
                "note": None,
            },
            "boards": [_board_row(), _board_row(id="concept:BK1001", name="AI 算力", board_type="concept", state="divergence", tags=["single_stock"], limit_up_count=5, broken_rate_pct=22.5)],
            "history_note": "板块历史积累 3 天，部分判定不可用",
        }

    def get_board_detail(self, board_id, trade_date):
        self.calls.append(("get_board_detail", board_id, trade_date))
        if self.detail_result is None:
            return None
        return {
            **_board_row(id=board_id),
            "verdict_text": "首次放量启动，有 3 只个股涨停承接",
            "drivers": "原因未明：未检索到明确催化",
            "driver_kind": "unknown",
            "metrics": [{"label": "成交额倍数", "value": "1.8 倍", "hint": "相对 5 日均值"}],
            "excess_series": [{"trade_date": "2026-11-06", "excess_pct": 2.8}, {"trade_date": "2026-11-05", "excess_pct": None}],
            "representatives": [{"code": "002475", "name": "立讯精密", "role": "leader", "close": 40.2, "change_pct": 5.1}],
            "evidence": ["连续 2 日跑赢基准"],
            "can_pick": True,
            "data_gaps": [],
        }

    def get_threshold_definitions(self):
        self.calls.append(("get_threshold_definitions",))
        return list(self.thresholds)

    def validate_threshold_overrides(self, overrides):
        self.calls.append(("validate_threshold_overrides", dict(overrides)))
        return list(self.validation_errors)


class FakeConfigService:
    def __init__(self, fake_board_service=None, raises=None):
        self.updates = []
        self.raises = raises
        self.board_service = fake_board_service

    def get_config(self, include_schema=True, mask_token="******"):
        assert include_schema is False
        return {"config_version": "ver-1", "items": []}

    def update(self, config_version, items, mask_token="******", reload_now=True):
        if self.raises is not None:
            raise self.raises
        self.updates.append({"config_version": config_version, "items": list(items), "reload_now": reload_now})
        if self.board_service is not None:
            # emulate the runtime picking up the new override
            overrides = json.loads(items[0]["value"]) if items[0]["value"] else {}
            self.board_service.thresholds = [
                _threshold(value=overrides.get("amount_ratio_mild", 1.5), overridden="amount_ratio_mild" in overrides)
            ]
        return {"updated_keys": [item["key"] for item in items]}


@pytest.fixture
def service():
    return FakeBoardService()


@pytest.fixture
def config_service(service):
    return FakeConfigService(service)


@pytest.fixture
def client(service, config_service):
    app = FastAPI()
    app.include_router(boards_endpoint.router, prefix="/api/v1/boards")
    add_error_handlers(app)
    app.dependency_overrides[get_board_anomaly_service] = lambda: service
    app.dependency_overrides[get_system_config_service] = lambda: config_service
    return TestClient(app)


def test_list_anomalies_shape_and_params(client, service):
    response = client.get("/api/v1/boards/anomalies?trade_date=2026-11-06&board_type=industry&state=first_launch")
    assert response.status_code == 200
    body = response.json()
    assert body["trade_date"] == "2026-11-06"
    assert body["market"]["index_name"] == "中证全指"
    assert body["market"]["broad_rally"] is False
    assert body["history_note"] == "板块历史积累 3 天，部分判定不可用"
    assert [row["id"] for row in body["boards"]] == ["industry:BK0448", "concept:BK1001"]
    assert body["boards"][0]["limit_up_count"] is None
    assert body["boards"][1]["broken_rate_pct"] == 22.5
    assert service.calls == [("list_anomalies", date(2026, 11, 6), "industry", "first_launch")]


def test_list_anomalies_defaults_to_none_filters(client, service):
    assert client.get("/api/v1/boards/anomalies").status_code == 200
    assert service.calls == [("list_anomalies", None, None, None)]


@pytest.mark.parametrize(
    "query",
    ["board_type=bogus", "state=bogus", "trade_date=not-a-date"],
)
def test_list_anomalies_rejects_bad_query(client, query):
    response = client.get(f"/api/v1/boards/anomalies?{query}")
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_board_detail_shape(client, service):
    response = client.get("/api/v1/boards/industry:BK0448?trade_date=2026-11-06")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "industry:BK0448"
    assert body["state"] == "first_launch"
    assert body["driver_kind"] == "unknown"
    assert body["drivers"].startswith("原因未明")
    assert body["metrics"][0] == {"label": "成交额倍数", "value": "1.8 倍", "hint": "相对 5 日均值"}
    assert body["excess_series"][1]["excess_pct"] is None
    assert body["representatives"][0]["role"] == "leader"
    assert body["can_pick"] is True
    assert service.calls[-1] == ("get_board_detail", "industry:BK0448", date(2026, 11, 6))


def test_board_detail_not_found(client, service):
    service.detail_result = None
    response = client.get("/api/v1/boards/industry:UNKNOWN")
    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "industry:UNKNOWN" in body["message"]


def test_thresholds_route_is_not_captured_by_board_id(client, service):
    response = client.get("/api/v1/boards/thresholds")
    assert response.status_code == 200
    assert response.json() == {"items": [_threshold()]}
    assert ("get_board_detail", "thresholds", None) not in service.calls


def test_thresholds_declared_before_board_id():
    paths = [route.path for route in boards_endpoint.router.routes]
    assert paths.index("/thresholds") < paths.index("/{board_id}")


def test_put_thresholds_persists_single_key_and_returns_definitions(client, service, config_service):
    response = client.put("/api/v1/boards/thresholds", json={"overrides": {"amount_ratio_mild": 2.0}})
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["value"] == 2.0
    assert body["items"][0]["overridden"] is True
    assert len(config_service.updates) == 1
    update = config_service.updates[0]
    assert update["config_version"] == "ver-1"
    assert len(update["items"]) == 1
    assert update["items"][0]["key"] == "BOARD_ANOMALY_THRESHOLDS"
    assert json.loads(update["items"][0]["value"]) == {"amount_ratio_mild": 2.0}
    assert ("validate_threshold_overrides", {"amount_ratio_mild": 2.0}) in service.calls


def test_put_thresholds_empty_overrides_resets_to_default(client, config_service):
    response = client.put("/api/v1/boards/thresholds", json={"overrides": {}})
    assert response.status_code == 200
    assert config_service.updates[0]["items"] == [{"key": "BOARD_ANOMALY_THRESHOLDS", "value": ""}]
    assert response.json()["items"][0]["overridden"] is False


def test_put_thresholds_validation_errors_block_write(client, service, config_service):
    service.validation_errors = ["amount_ratio_mild 必须大于 1", "未知阈值 foo"]
    response = client.put("/api/v1/boards/thresholds", json={"overrides": {"amount_ratio_mild": 0.5, "foo": 1}})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "validation_error"
    assert body["message"] == "amount_ratio_mild 必须大于 1；未知阈值 foo"
    assert body["detail"] == ["amount_ratio_mild 必须大于 1", "未知阈值 foo"]
    assert config_service.updates == []


def test_put_thresholds_requires_overrides_field(client, config_service):
    response = client.put("/api/v1/boards/thresholds", json={})
    assert response.status_code == 422
    assert config_service.updates == []


def test_put_thresholds_config_conflict_is_409(client, service):
    conflict_client_service = FakeConfigService(raises=ConfigConflictError(current_version="ver-2"))
    client.app.dependency_overrides[get_system_config_service] = lambda: conflict_client_service
    response = client.put("/api/v1/boards/thresholds", json={"overrides": {"amount_ratio_mild": 2.0}})
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "config_version_conflict"
    assert body["detail"] == {"current_config_version": "ver-2"}


def test_put_thresholds_config_validation_error_is_400(client):
    issues = [{"key": "BOARD_ANOMALY_THRESHOLDS", "code": "invalid", "message": "bad", "severity": "error"}]
    failing = FakeConfigService(raises=ConfigValidationError(issues))
    client.app.dependency_overrides[get_system_config_service] = lambda: failing
    response = client.put("/api/v1/boards/thresholds", json={"overrides": {"amount_ratio_mild": 2.0}})
    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"
    assert response.json()["detail"] == issues


def test_unexpected_service_error_is_500(client, service):
    def boom(*_args):
        raise RuntimeError("db down")

    service.list_anomalies = boom
    response = client.get("/api/v1/boards/anomalies")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"


def test_service_unavailable_is_503_when_module_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "src.services.board_anomaly_service", None)
    app = FastAPI()
    app.include_router(boards_endpoint.router, prefix="/api/v1/boards")
    add_error_handlers(app)
    client = TestClient(app)
    for method, path in [
        ("get", "/api/v1/boards/anomalies"),
        ("get", "/api/v1/boards/thresholds"),
        ("get", "/api/v1/boards/industry:BK0448"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code == 503, path
        assert response.json() == {"error": "daily_plan_unavailable", "message": "次日计划服务未就绪"}
    response = client.put("/api/v1/boards/thresholds", json={"overrides": {}})
    assert response.status_code == 503


def test_router_is_mounted_in_v1_router():
    from api.v1.router import router as v1_router

    app = FastAPI()
    app.include_router(v1_router, prefix="/api/v1")
    paths = set(app.openapi()["paths"])
    assert "/api/v1/boards/anomalies" in paths
    assert "/api/v1/boards/thresholds" in paths
    assert "/api/v1/boards/{board_id}" in paths
