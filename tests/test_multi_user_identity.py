# -*- coding: utf-8 -*-
"""Multi-user identity: ``CurrentUser``, ``require_admin``, ``app_user`` and ``GET /api/v1/me``.

The user-center HTTP call is always mocked; nothing here touches the network.
"""

import os
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

import src.auth as auth
import src.user_center_auth as uc
from api import deps
from api.app import create_app
from api.deps import CurrentUser, get_current_user, require_admin
from api.middlewares import auth as auth_middleware
from src.config import Config, get_primary_user_id
from src.core.config_registry import SCOPE_USER, get_config_scope
from src.repositories.user_repo import UserRepository
from src.storage import AppUser, DatabaseManager

SESSION_ID = "b2" * 32
PUBLIC_ORIGIN = "https://stock.example.test"
CSRF = "csrf-token-value"
COOKIE = {"Cookie": f"user_session={SESSION_ID}"}
WRITE_HEADERS = {**COOKIE, "Origin": PUBLIC_ORIGIN, "X-CSRF-Token": CSRF}
PRIMARY = "uid-admin"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


def session_response(role="admin", user_id=PRIMARY, handle="darren", display_name="Darren"):
    return FakeResponse(200, {
        "user": {"userId": user_id, "handle": handle, "displayName": display_name, "role": role},
        "csrfToken": CSRF,
    })


def _base_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PRIMARY_USER_ID", PRIMARY)
    monkeypatch.setattr(auth, "_is_auth_enabled_from_env", lambda: False)
    monkeypatch.setattr(uc.time, "sleep", lambda _s: None)
    for key in list(os.environ):
        if get_config_scope(key) == SCOPE_USER:
            monkeypatch.delenv(key, raising=False)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    auth_middleware._app_user_last_touch.clear()


def _teardown():
    auth.refresh_auth_state()
    DatabaseManager.reset_instance()
    Config.reset_instance()
    auth_middleware._app_user_last_touch.clear()


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    auth.refresh_auth_state()
    yield TestClient(create_app(static_dir=tmp_path / "static"), base_url="http://testserver")
    _teardown()


@pytest.fixture
def uc_client(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTH_MODE", "user_center")
    monkeypatch.setenv("PUBLIC_ORIGIN", PUBLIC_ORIGIN)
    monkeypatch.setenv("USER_CENTER_URL", "https://user.example.test")
    monkeypatch.setenv("USER_CENTER_REQUIRED_ROLE", "user")  # let non-admin users in, so require_admin is what blocks
    auth.refresh_auth_state()
    http = MagicMock(return_value=session_response())
    monkeypatch.setattr(uc, "_http_get", http)
    client = TestClient(create_app(static_dir=tmp_path / "static"), base_url="http://testserver")
    client.http = http
    yield client
    _teardown()


# Endpoints that must only be reachable by an administrator (process-level / shared maintenance).
ADMIN_ONLY_ENDPOINTS = [
    ("GET", "/api/v1/system/scheduler/status"),
    ("POST", "/api/v1/system/scheduler/run-now"),
    ("GET", "/api/v1/system/config"),
    ("PUT", "/api/v1/system/config"),
    ("GET", "/api/v1/system/config/schema"),
    ("GET", "/api/v1/system/config/export"),
    ("POST", "/api/v1/system/config/import"),
    ("POST", "/api/v1/system/config/validate"),
    ("GET", "/api/v1/system/config/setup/status"),
    ("GET", "/api/v1/system/config/generation-backends/status"),
    ("POST", "/api/v1/system/config/llm/test-channel"),
    ("POST", "/api/v1/system/config/notification/test-channel"),
    ("POST", "/api/v1/system/config/llm/discover-models"),
    ("POST", "/api/v1/system/feishu/register"),
    ("GET", "/api/v1/usage/summary"),
    ("GET", "/api/v1/usage/dashboard"),
    ("POST", "/api/v1/intelligence/sources"),
    ("GET", "/api/v1/intelligence/sources"),
    ("GET", "/api/v1/intelligence/sources/templates"),
    ("POST", "/api/v1/intelligence/sources/defaults"),
    ("POST", "/api/v1/intelligence/sources/test"),
    ("POST", "/api/v1/intelligence/sources/fetch-enabled"),
    ("POST", "/api/v1/intelligence/sources/1/fetch"),
    ("PUT", "/api/v1/boards/thresholds"),
    ("POST", "/api/v1/boards/recommendations/refresh"),
    ("POST", "/api/v1/decision-signals/outcomes/run"),
    ("POST", "/api/v1/backtest/run"),
    ("POST", "/api/v1/analysis/market-review"),
]


def _dependency_calls(dependant):
    calls = []
    for sub in dependant.dependencies:
        calls.append(sub.call)
        calls.extend(_dependency_calls(sub))
    return calls


def test_admin_guard_is_declared_on_every_listed_endpoint(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    try:
        app = create_app(static_dir=tmp_path / "static")
        routes = {
            (method, route.path): route
            for route in app.routes if isinstance(route, APIRoute)
            for method in route.methods
        }
        missing = []
        for method, path in ADMIN_ONLY_ENDPOINTS:
            template = path
            route = next(
                (r for (m, p), r in routes.items() if m == method and p.replace("{source_id}", "1") == template),
                None,
            )
            assert route is not None, (method, path)
            if require_admin not in _dependency_calls(route.dependant):
                missing.append((method, path))
        assert missing == []
    finally:
        _teardown()


# --------------------------------------------------------------------------
# local mode: the primary user, admin, behaviour unchanged
# --------------------------------------------------------------------------


def test_local_mode_is_the_primary_admin(local_client):
    body = local_client.get("/api/v1/me").json()

    assert body == {
        "user_id": PRIMARY,
        "handle": PRIMARY,
        "display_name": PRIMARY,
        "role": "admin",
        "is_admin": True,
        "auth_mode": "local",
        "model_key_configured": False,
    }


def test_local_mode_primary_user_defaults_to_local(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.delenv("PRIMARY_USER_ID")
    Config.reset_instance()
    try:
        assert get_primary_user_id() == "local"
    finally:
        _teardown()


def test_local_mode_me_reports_a_configured_model(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    Config.reset_instance()
    auth.refresh_auth_state()
    try:
        client = TestClient(create_app(static_dir=tmp_path / "static"), base_url="http://testserver")
        assert client.get("/api/v1/me").json()["model_key_configured"] is True
    finally:
        _teardown()


def test_local_mode_admin_endpoints_stay_reachable(local_client):
    for path in ("/api/v1/usage/summary", "/api/v1/system/config/schema", "/api/v1/system/scheduler/status"):
        resp = local_client.get(path)
        assert resp.status_code == 200, (path, resp.text)


def test_local_mode_records_no_app_user(local_client):
    local_client.get("/api/v1/me")

    assert UserRepository().list_app_users() == []


# --------------------------------------------------------------------------
# user_center mode
# --------------------------------------------------------------------------


def test_user_center_admin_session_is_an_admin(uc_client):
    body = uc_client.get("/api/v1/me", headers=COOKIE).json()

    assert body["user_id"] == PRIMARY
    assert body["handle"] == "darren" and body["display_name"] == "Darren"
    assert body["role"] == "admin" and body["is_admin"] is True
    assert body["auth_mode"] == "user_center"
    assert uc_client.get("/api/v1/usage/summary", headers=COOKIE).status_code == 200


def test_user_center_regular_user_is_not_an_admin(uc_client):
    uc_client.http.return_value = session_response(role="user", user_id="uid-bob", handle="bob", display_name="Bob")

    body = uc_client.get("/api/v1/me", headers=COOKIE).json()

    assert body["user_id"] == "uid-bob"
    assert body["role"] == "user" and body["is_admin"] is False
    # A regular user with no model key of their own has none, even if the system has one.
    assert body["model_key_configured"] is False


@pytest.mark.parametrize("method,path", ADMIN_ONLY_ENDPOINTS)
def test_user_center_regular_user_is_refused_on_admin_endpoints(uc_client, method, path):
    uc_client.http.return_value = session_response(role="user", user_id="uid-bob")

    resp = uc_client.request(method, path, headers=WRITE_HEADERS, json={})

    assert resp.status_code == 403, (method, path, resp.status_code, resp.text)
    assert resp.json() == {"error": "ADMIN_REQUIRED", "message": "Administrator role required"}


def test_user_center_default_required_role_still_blocks_regular_users_at_the_middleware(uc_client, monkeypatch):
    monkeypatch.setenv("USER_CENTER_REQUIRED_ROLE", "admin")
    uc_client.http.return_value = session_response(role="user", user_id="uid-bob")

    resp = uc_client.get("/api/v1/me", headers=COOKIE)

    assert resp.status_code == 403
    assert resp.json()["error"] == "ADMIN_REQUIRED"


def test_current_user_without_a_verified_session_is_401_in_user_center_mode(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTH_MODE", "user_center")
    auth.refresh_auth_state()
    try:
        request = MagicMock()
        request.state = MagicMock(spec=[])  # no user_center_session attribute
        with pytest.raises(Exception) as exc:
            get_current_user(request)
        assert exc.value.status_code == 401
        assert exc.value.detail["error"] == "AUTH_REQUIRED"
    finally:
        _teardown()


def test_require_admin_dependency():
    admin = CurrentUser("u1", "h", "H", "admin", True)
    user = CurrentUser("u2", "h", "H", "user", False)

    assert require_admin(admin) is admin
    with pytest.raises(Exception) as exc:
        require_admin(user)
    assert exc.value.status_code == 403
    assert deps.CurrentUser is CurrentUser


# --------------------------------------------------------------------------
# app_user
# --------------------------------------------------------------------------


def _users():
    with DatabaseManager.get_instance().get_session() as session:
        return {row.user_id: row for row in session.execute(select(AppUser)).scalars().all()}


def test_verified_user_is_recorded_in_app_user(uc_client):
    uc_client.get("/api/v1/me", headers=COOKIE)

    row = _users()[PRIMARY]
    assert (row.handle, row.display_name, row.role) == ("darren", "Darren", "admin")
    assert row.first_seen_at is not None and row.last_seen_at is not None


def test_app_user_write_is_throttled_per_user(uc_client, monkeypatch):
    calls = []
    original = UserRepository.touch_app_user
    monkeypatch.setattr(
        UserRepository, "touch_app_user",
        lambda self, *args: (calls.append(args[0]), original(self, *args))[1],
    )

    for _ in range(3):
        uc_client.get("/api/v1/me", headers=COOKIE)
    assert calls == [PRIMARY]

    uc_client.http.return_value = session_response(role="user", user_id="uid-bob", handle="bob")
    uc_client.get("/api/v1/me", headers=COOKIE)
    uc_client.get("/api/v1/me", headers=COOKIE)
    assert calls == [PRIMARY, "uid-bob"]

    # After the interval the same user is written again.
    monkeypatch.setattr(auth_middleware, "APP_USER_TOUCH_INTERVAL_SEC", 0.0)
    uc_client.get("/api/v1/me", headers=COOKIE)
    assert calls == [PRIMARY, "uid-bob", "uid-bob"]


def test_a_failed_app_user_write_never_fails_the_request(uc_client, monkeypatch, caplog):
    def boom(self, *args):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(UserRepository, "touch_app_user", boom)

    with caplog.at_level("WARNING"):
        resp = uc_client.get("/api/v1/me", headers=COOKIE)

    assert resp.status_code == 200
    assert "Failed to record app_user" in caplog.text


def test_requests_that_are_not_authenticated_do_not_write_app_user(uc_client):
    uc_client.http.return_value = FakeResponse(401, {})

    assert uc_client.get("/api/v1/me", headers=COOKIE).status_code == 401
    assert uc_client.get("/api/v1/health").status_code == 200

    assert _users() == {}


def test_touch_app_user_preserves_first_seen_and_refreshes_the_snapshot(tmp_path, monkeypatch):
    _base_env(tmp_path, monkeypatch)
    try:
        repo = UserRepository()
        repo.touch_app_user("uid-1", "old", "Old Name", "user")
        first = _users()["uid-1"]
        first_seen, last_seen = first.first_seen_at, first.last_seen_at

        repo.touch_app_user("uid-1", "new", "New Name", "admin")

        row = _users()["uid-1"]
        assert (row.handle, row.display_name, row.role) == ("new", "New Name", "admin")
        assert row.first_seen_at == first_seen
        assert row.last_seen_at >= last_seen
        assert [u["user_id"] for u in repo.list_app_users()] == ["uid-1"]
    finally:
        _teardown()
