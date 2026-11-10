# -*- coding: utf-8 -*-
"""Merge audit, item 1: the assistant's ``call_api`` / ``read_api_doc`` act as the session owner and nobody else.

Real internal app, real routes, real dependencies and services over a temp SQLite database; only the model is
absent (these routes never call it). Both ``AUTH_MODE`` values are covered because the identity comes from the
tool registry's ``user_id`` (the chat session owner), not from a login session or from the primary user.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import api.internal_client as internal
import src.auth as auth
from src.agent.factory import get_chat_tool_registry
from src.agent.tools import api_tools
from src.agent.tools.registry import ToolUserContextRequired
from src.repositories.user_repo import UserRepository
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    prepare_multi_user_env,
    teardown_multi_user_env,
)
from src.storage import DatabaseManager

ADMIN_2 = "uid-admin-2"
HOLDING = {"code": "000988", "shares": 777, "cost": 40.0, "buy_date": "2026-01-05", "horizon": "swing"}


@pytest.fixture(params=["local", "user_center"])
def world(request, tmp_path, monkeypatch):
    prepare_multi_user_env(tmp_path, monkeypatch)
    monkeypatch.setattr(internal, "_running_app", None)
    if request.param == "user_center":
        monkeypatch.setenv("AUTH_MODE", "user_center")
        auth.refresh_auth_state()
    DatabaseManager.get_instance()
    repo = UserRepository()
    repo.touch_app_user(PRIMARY_ID, "primary", "Primary", "admin")
    repo.touch_app_user(USER_A, "a", "A", "user")
    repo.touch_app_user(USER_B, "b", "B", "user")
    repo.touch_app_user(ADMIN_2, "admin2", "Admin Two", "admin")
    yield request.param
    monkeypatch.delenv("AUTH_MODE", raising=False)
    auth.refresh_auth_state()
    teardown_multi_user_env()


def call(user_id, method, path, **kwargs):
    return api_tools._handle_call_api(method, path, user_id=user_id, **kwargs)


def doc(user_id, **kwargs):
    return api_tools._handle_read_api_doc(user_id=user_id, **kwargs)


def test_each_user_reads_and_writes_only_their_own_data(world):
    saved = call(USER_A, "PUT", "/api/v1/plan/holdings", body={"cash": 100_000, "holdings": [HOLDING]})
    assert saved["ok"], saved
    created = call(USER_A, "POST", "/api/v1/portfolio/accounts", body={"name": "a-private-account"})
    assert created["ok"], created

    for other in (USER_B, PRIMARY_ID):
        plan = call(other, "GET", "/api/v1/plan/account")
        assert plan["ok"], plan
        assert plan["data"]["holdings"] == [] and plan["data"]["cash"] == 0
        accounts = call(other, "GET", "/api/v1/portfolio/accounts")
        assert accounts["ok"] and accounts["data"]["accounts"] == []
        assert "a-private-account" not in str(accounts) and "777" not in str(plan)

    own = call(USER_A, "GET", "/api/v1/plan/account")
    assert [h["shares"] for h in own["data"]["holdings"]] == [777]
    assert [a["name"] for a in call(USER_A, "GET", "/api/v1/portfolio/accounts")["data"]["accounts"]] == ["a-private-account"]

    # B writes through the assistant: lands on B, A is untouched
    assert call(USER_B, "PUT", "/api/v1/plan/holdings", body={"cash": 5, "holdings": []})["ok"]
    assert call(USER_A, "GET", "/api/v1/plan/account")["data"]["cash"] == 100_000
    assert call(USER_B, "GET", "/api/v1/plan/account")["data"]["cash"] == 5


def test_the_primary_user_can_still_use_the_feature(world):
    """user_center mode used to answer every route with 401 (no session on the internal app), the primary user too."""
    plan = call(PRIMARY_ID, "GET", "/api/v1/plan/account")
    assert plan["ok"] and plan["status_code"] == 200, plan
    assert call(PRIMARY_ID, "PUT", "/api/v1/plan/holdings", body={"cash": 9, "holdings": []})["ok"]
    assert call(PRIMARY_ID, "GET", "/api/v1/plan/account")["data"]["cash"] == 9
    # primary reaches the administrator-only routes that are not blocked outright
    assert call(PRIMARY_ID, "GET", "/api/v1/intelligence/sources")["ok"]
    assert call(PRIMARY_ID, "GET", "/api/v1/plan/account")["data"]["cash"] == 9


ADMIN_ONLY_CALLS = [
    ("GET", "/api/v1/intelligence/sources", None),
    ("POST", "/api/v1/intelligence/sources/fetch-enabled", None),
    ("PUT", "/api/v1/boards/thresholds", {}),
    ("POST", "/api/v1/boards/recommendations/refresh", None),
    ("POST", "/api/v1/decision-signals/outcomes/run", None),
    ("POST", "/api/v1/decision-signals/reassess", {}),
    ("POST", "/api/v1/backtest/run", {}),
    ("POST", "/api/v1/analysis/market-review", {}),
    ("POST", "/api/v1/screening/screen", {}),
]
BLOCKED_FOR_EVERYONE = [
    ("GET", "/api/v1/system/config"),
    ("PUT", "/api/v1/system/config"),
    ("POST", "/api/v1/system/scheduler/run-now"),
    ("GET", "/api/v1/usage/summary"),
    ("GET", "/api/v1/auth/status"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/me/settings"),
    ("PUT", "/api/v1/me/settings"),
    ("DELETE", "/api/v1/me/settings/LITELLM_MODEL"),
    ("GET", "/api/v1/me/notification-channels"),
    ("POST", "/api/v1/me/notification-channels"),
    ("POST", "/api/v1/me/notification-channels/1/test"),
]


def test_a_regular_user_cannot_reach_any_admin_route(world):
    for method, path, body in ADMIN_ONLY_CALLS:
        result = call(USER_B, method, path, body=body)
        assert "error" in result and "status_code" not in result, (method, path, result)
        assert "仅管理员" in result["error"], (method, path, result)
    # a user-center administrator who is not the primary user may use them (their role comes from app_user)
    assert call(ADMIN_2, "GET", "/api/v1/intelligence/sources")["ok"]


def test_admin_routes_are_derived_from_the_route_dependencies(world):
    from api.deps import require_admin
    from fastapi.routing import APIRoute

    declared = {
        (method, route.path_format)
        for route in internal.get_internal_app().routes if isinstance(route, APIRoute)
        for method in route.methods
        if any(dep.call is require_admin for dep in _walk(route.dependant))
    }
    assert declared and ("PUT", "/api/v1/boards/thresholds") in declared
    for method, template in declared:
        assert internal.route_requires_admin(method, template)
        assert internal.hidden_from(method, template, internal.acting_user(USER_B))
        assert not internal.hidden_from(method, template, internal.acting_user(PRIMARY_ID))
    assert not internal.route_requires_admin("GET", "/api/v1/plan/account")


def _walk(dependant):
    for sub in dependant.dependencies:
        yield sub
        yield from _walk(sub)


def test_credentials_and_process_settings_are_blocked_for_everyone(world):
    for user in (USER_B, ADMIN_2, PRIMARY_ID):
        for method, path in BLOCKED_FOR_EVERYONE:
            result = call(user, method, path, body={} if method in ("POST", "PUT") else None)
            assert "error" in result and "status_code" not in result, (user, method, path, result)
            assert "不能由助手调用" in result["error"] or "没有接口" in result["error"], result


@pytest.mark.parametrize("path", [
    "/api/v1/stocks/../system/config",
    "/api/v1/stocks/watchlist/../../system/config",
    "/api/v1/stocks/%2e%2e/system/config",
    "/api/v1/stocks/%2E%2E/system/config",
    "/api/v1/stocks/..%2fsystem/config",
    "/api/v1/stocks%2f..%2fsystem%2fconfig",
    "/api/v1/%73ystem/config",
    "/api/v1/./system/config",
    "/api/v1//system/config",
    "/api/v1/stocks/x\\..\\system/config",
    "/api/v1/stocks/x#/../../system/config",
    "/api/v1/stocks/%00/../system/config",
])
def test_path_tricks_are_rejected_before_any_route_runs(world, path, monkeypatch):
    ran = []
    monkeypatch.setattr(internal, "call", lambda *a, **k: ran.append(a) or (200, "application/json", "{}"))
    result = api_tools._handle_call_api("GET", path, user_id=PRIMARY_ID)
    assert "error" in result and "status_code" not in result, result
    assert ran == []
    detail = api_tools._handle_read_api_doc(endpoint=f"GET {path}", user_id=PRIMARY_ID)
    assert "error" in detail and "endpoint" not in detail


def test_the_client_refuses_blocked_routes_even_when_called_directly(world):
    with pytest.raises(internal.CallRefused):
        internal.call("GET", "/api/v1/system/config", user_id=PRIMARY_ID)
    with pytest.raises(internal.CallRefused):
        internal.call("PUT", "/api/v1/boards/thresholds", user_id=USER_B, body={})
    with pytest.raises(internal.ActingUserRequired):
        internal.call("GET", "/api/v1/plan/account", user_id=None)
    with pytest.raises(internal.ActingUserRequired):
        internal.call("GET", "/api/v1/plan/account", user_id="")


def test_percent_encoded_text_that_is_not_ascii_still_works(world):
    # Chinese board names / stock names are legitimate path text and must keep working
    result = call(USER_B, "GET", "/api/v1/stocks/%E5%B9%B3%E5%AE%89%E9%93%B6%E8%A1%8C/events")
    assert "status_code" in result  # reached the route (whatever it answers), not refused as a bad path


def test_unknown_role_or_unknown_user_is_never_an_administrator(world):
    UserRepository().touch_app_user("uid-odd-role", "odd", "Odd", "superuser")
    for uid in ("uid-odd-role", "uid-never-logged-in"):
        user = internal.acting_user(uid)
        assert user.is_admin is False
        assert "仅管理员" in call(uid, "PUT", "/api/v1/boards/thresholds", body={})["error"]
    assert internal.acting_user(ADMIN_2).is_admin is True
    assert internal.acting_user(PRIMARY_ID).is_admin is True


def test_the_directory_hides_admin_groups_and_routes_from_regular_users(world):
    def groups(user):
        return {g["group"]: g["endpoints"] for g in doc(user)["groups"]}

    regular = groups(USER_B)
    assert {"SystemConfig", "Usage", "Auth"}.isdisjoint(regular)
    assert "Intelligence" in regular and "DailyPlan" in regular
    assert regular["Intelligence"] == 1  # only GET /intelligence/items; the source routes are admin-only

    primary = groups(PRIMARY_ID)
    assert {"SystemConfig", "Usage", "Auth"}.isdisjoint(primary)  # blocked for everyone, administrators included
    assert primary["Intelligence"] > regular["Intelligence"]

    lines_b = doc(USER_B, group="Intelligence")["endpoints"]
    assert not any("/intelligence/sources" in line for line in lines_b)
    assert any("/intelligence/items" in line for line in lines_b)
    lines_primary = doc(PRIMARY_ID, group="Intelligence")["endpoints"]
    assert any("/intelligence/sources" in line for line in lines_primary)

    thresholds_b = doc(USER_B, group="Boards")["endpoints"]
    assert not any(line.startswith("PUT /api/v1/boards/thresholds") for line in thresholds_b)
    assert any(line.startswith("GET /api/v1/boards/thresholds") for line in thresholds_b)
    assert any(line.startswith("PUT /api/v1/boards/thresholds") for line in doc(PRIMARY_ID, group="Boards")["endpoints"])

    for hidden in ("SystemConfig", "Usage", "Auth"):
        assert "error" in doc(PRIMARY_ID, group=hidden)
    assert "error" in doc(USER_B, endpoint="PUT /api/v1/boards/thresholds")
    assert "error" in doc(PRIMARY_ID, endpoint="GET /api/v1/system/config")
    assert doc(PRIMARY_ID, endpoint="PUT /api/v1/boards/thresholds")["endpoint"] == "PUT /api/v1/boards/thresholds"
    # the path-prefix form of the group argument must not leak either
    assert "error" in doc(USER_B, group="/system") and "error" in doc(PRIMARY_ID, group="/me/settings")


def test_the_registry_supplies_the_owner_and_discards_model_supplied_ids(world):
    registry = get_chat_tool_registry()
    for name in ("call_api", "read_api_doc"):
        assert registry.resolve(name).user_scoped is True
    with pytest.raises(ToolUserContextRequired):
        registry.execute("call_api", method="GET", path="/api/v1/plan/account")
    with pytest.raises(ToolUserContextRequired):
        registry.execute("read_api_doc")

    assert call(USER_A, "PUT", "/api/v1/plan/holdings", body={"cash": 42, "holdings": []})["ok"]
    # model-supplied user_id inside the arguments is dropped; the session owner wins
    result = registry.execute("call_api", user_id=USER_B, method="GET", path="/api/v1/plan/account")
    assert result["ok"] and result["data"]["cash"] == 0
    result = registry.execute("call_api", user_id=USER_A, method="GET", path="/api/v1/plan/account")
    assert result["data"]["cash"] == 42
    assert "SystemConfig" not in str(registry.execute("read_api_doc", user_id=USER_B))


def test_user_id_is_not_a_model_visible_parameter(world):
    registry = get_chat_tool_registry()
    assert registry.resolve("call_api").to_public_descriptor()["parameters"]["properties"].keys() == {
        "method", "path", "query", "body",
    }, "user_id must never be a model-visible parameter"
