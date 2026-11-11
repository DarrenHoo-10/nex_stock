# -*- coding: utf-8 -*-
"""Tests for AUTH_MODE=user_center (NexorAI user-center authentication).

The user-center HTTP call (``src.user_center_auth._http_get``) is always mocked;
no test performs a real network request.
"""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

# Keep this test runnable when optional LLM runtime deps are not installed.
try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from fastapi.testclient import TestClient

import src.auth as auth
import src.user_center_auth as uc
from api.app import create_app
from api.v1.endpoints import system_config as system_config_endpoint
from src.config import Config
from src.storage import DatabaseManager

SESSION_ID = "a1" * 32  # 64 hex chars
PUBLIC_ORIGIN = "https://stock.example.test"
USER_CENTER_URL = "https://user.example.test"
CSRF = "csrf-token-value"
COOKIE = {"user_session": SESSION_ID}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def session_response(role="admin", csrf=CSRF, user_id="u1", handle="darren", display_name="Darren"):
    return FakeResponse(
        200,
        {
            "user": {"userId": user_id, "handle": handle, "displayName": display_name, "role": role},
            "csrfToken": csrf,
        },
    )


@pytest.fixture
def uc_env(tmp_path, monkeypatch):
    """user_center mode with an isolated (empty) env file and fresh auth caches."""
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUTH_MODE", "user_center")
    monkeypatch.setenv("PUBLIC_ORIGIN", PUBLIC_ORIGIN)
    monkeypatch.setenv("USER_CENTER_URL", USER_CENTER_URL)
    monkeypatch.delenv("USER_CENTER_REQUIRED_ROLE", raising=False)
    monkeypatch.delenv("ADMIN_AUTH_ENABLED", raising=False)
    monkeypatch.setattr(auth, "_is_auth_enabled_from_env", lambda: False)
    monkeypatch.setattr(uc.time, "sleep", lambda _seconds: None)
    Config.reset_instance()
    # The auth middleware records the user in app_user (this test's tmp database).
    DatabaseManager.reset_instance()
    auth.refresh_auth_state()
    yield tmp_path
    auth.refresh_auth_state()
    DatabaseManager.reset_instance()
    Config.reset_instance()


def build_app_with_probes(tmp_path: Path, *, with_frontend: bool = False):
    from fastapi import Request

    static_dir = tmp_path / "static"
    static_dir.mkdir(exist_ok=True)
    if with_frontend:
        (static_dir / "index.html").write_text("<html><body>spa</body></html>", encoding="utf-8")
    app = create_app(static_dir=static_dir)

    @app.get("/api/v1/_probe")
    async def probe_get(request: Request):
        session = getattr(request.state, "user_center_session", None)
        return {"ok": True, "user": session.username if session else None}

    @app.post("/api/v1/_probe")
    async def probe_post():
        return {"ok": True}

    @app.get("/api/_other_probe")
    async def other_probe():
        return {"ok": True}

    return app


@pytest.fixture
def client(uc_env):
    return TestClient(build_app_with_probes(uc_env), base_url="http://testserver")


@pytest.fixture
def uc_http(monkeypatch):
    """Patch the single user-center HTTP call; tests set ``.responses`` / ``.side_effect``."""
    mock = MagicMock(return_value=session_response())
    monkeypatch.setattr(uc, "_http_get", mock)
    return mock


def cookie_headers(cookies):
    if not cookies:
        return {}
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}


def get(client: TestClient, path: str, cookies=COOKIE, **kwargs):
    return client.get(path, headers=cookie_headers(cookies), follow_redirects=False, **kwargs)


# --------------------------------------------------------------------------
# Verification of the user_session cookie
# --------------------------------------------------------------------------


def test_valid_admin_session_reaches_the_route(client, uc_http):
    resp = get(client, "/api/v1/_probe")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "user": "Darren"}

    uc_http.assert_called_once()
    url, headers, timeout = uc_http.call_args.args
    assert url == f"{USER_CENTER_URL}/v1/session"
    assert headers["Cookie"] == f"user_session={SESSION_ID}"
    assert timeout == uc.REQUEST_TIMEOUT_SEC == 3.0


def test_private_route_verifies_original_hostname_and_sends_original_host(uc_env, tmp_path, monkeypatch):
    from tests.test_multi_user_p3a_hardening import TlsServer, _make_certificate

    host = "identity.example.test"
    cert, key = _make_certificate(tmp_path, [host])
    server = TlsServer(cert, key, bind="127.0.0.1")
    server.start()
    try:
        monkeypatch.setenv("USER_CENTER_CONNECT_IP", "127.0.0.1")
        for name in (host, "wrong.example.test"):
            monkeypatch.setattr(uc._get_http_session(name), "verify", cert)
        response = uc._http_get(f"https://{host}:{server.port}/v1/session", {}, 3)
        assert response.status_code == 200
        assert server.sni == [host]
        assert f"Host: {host}:{server.port}" in server.requests[0]
        with pytest.raises(requests.exceptions.SSLError):
            uc._http_get(f"https://wrong.example.test:{server.port}/v1/session", {}, 3)
        assert len(server.requests) == 1
    finally:
        server.close()


def test_only_the_session_cookie_is_forwarded_to_the_user_center(client, uc_http):
    resp = client.get(
        "/api/v1/_probe",
        headers={"Cookie": f"theme=dark; user_session={SESSION_ID}; dsa_session=abc"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert uc_http.call_args.args[1]["Cookie"] == f"user_session={SESSION_ID}"


def test_non_admin_is_forbidden(client, uc_http):
    uc_http.return_value = session_response(role="user")
    resp = get(client, "/api/v1/_probe")
    assert resp.status_code == 403
    assert resp.json()["error"] == "ADMIN_REQUIRED"


def test_missing_cookie_is_401_without_calling_the_user_center(client, uc_http):
    resp = get(client, "/api/v1/_probe", cookies=None)
    assert resp.status_code == 401
    assert resp.json()["error"] == "AUTH_REQUIRED"
    uc_http.assert_not_called()


@pytest.mark.parametrize(
    "cookie_header",
    [
        "user_session=short",
        "user_session=" + "g" * 64,  # right length, not hex
        "user_session=" + "A1" * 32,  # upper-case hex is not what the user center issues
        f"user_session={SESSION_ID}; user_session={SESSION_ID}",  # duplicate cookies are ambiguous
        "dsa_session=" + SESSION_ID,  # wrong cookie name
    ],
)
def test_malformed_or_ambiguous_cookie_is_401_without_calling_the_user_center(client, uc_http, cookie_header):
    resp = client.get("/api/v1/_probe", headers={"Cookie": cookie_header}, follow_redirects=False)
    assert resp.status_code == 401
    uc_http.assert_not_called()


def test_user_center_401_means_not_logged_in(client, uc_http):
    uc_http.return_value = FakeResponse(401, {"error": "unauthorized"})
    resp = get(client, "/api/v1/_probe")
    assert resp.status_code == 401
    assert resp.json()["error"] == "AUTH_REQUIRED"


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("down"),
        requests.Timeout("slow"),
        FakeResponse(500, {}),
        FakeResponse(403, {}),
        FakeResponse(200, None, raise_json=True),
        FakeResponse(200, {"user": {"role": "admin"}}),  # no csrfToken
        FakeResponse(200, {"csrfToken": "x"}),  # no user
        FakeResponse(200, ["unexpected"]),
    ],
)
def test_user_center_failure_is_503(client, uc_http, failure):
    if isinstance(failure, Exception):
        uc_http.side_effect = failure
    else:
        uc_http.return_value = failure
    resp = get(client, "/api/v1/_probe")
    assert resp.status_code == 503
    assert resp.json()["error"] == "USER_CENTER_UNAVAILABLE"


@pytest.mark.parametrize("status", [200, 401])
def test_initial_read_timeout_retries_verification_and_obeys_result(client, uc_http, status):
    response = session_response() if status == 200 else FakeResponse(401, {})
    uc_http.side_effect = [requests.ReadTimeout(), response]
    assert get(client, "/api/v1/_probe").status_code == status
    assert uc_http.call_count == 2


def test_persistent_timeout_fails_closed_after_one_retry(client, uc_http):
    uc_http.side_effect = requests.ReadTimeout()
    response = get(client, "/api/v1/_probe")
    assert response.status_code == 503
    assert response.json()["error"] == "USER_CENTER_UNAVAILABLE"
    assert uc_http.call_count == 2


def test_429_is_retried_then_succeeds(client, uc_http):
    uc_http.side_effect = [
        FakeResponse(429, {}, headers={"Retry-After": "1"}),
        FakeResponse(429, {}),
        session_response(),
    ]
    resp = get(client, "/api/v1/_probe")
    assert resp.status_code == 200
    assert uc_http.call_count == 3


def test_429_retries_are_bounded(client, uc_http):
    uc_http.return_value = FakeResponse(429, {})
    resp = get(client, "/api/v1/_probe")
    assert resp.status_code == 503
    assert uc_http.call_count == uc.MAX_RATE_LIMIT_RETRIES + 1


def test_verification_results_are_never_cached(client, uc_http):
    uc_http.side_effect = [
        session_response(role="admin"),
        session_response(role="user"),  # role revoked between requests
        FakeResponse(401, {}),  # logged out between requests
        session_response(role="admin"),
    ]
    statuses = [get(client, "/api/v1/_probe").status_code for _ in range(4)]
    assert statuses == [200, 403, 401, 200]
    assert uc_http.call_count == 4


def test_user_center_calls_are_limited_to_two_concurrent(monkeypatch):
    settings = uc.UserCenterSettings(base_url=USER_CENTER_URL, public_origin=PUBLIC_ORIGIN, required_role="admin")
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def slow_get(url, headers, timeout):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return session_response()

    monkeypatch.setattr(uc, "_http_get", slow_get)
    threads = [threading.Thread(target=uc.fetch_session, args=(SESSION_ID, settings)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert uc.MAX_CONCURRENT_REQUESTS == 2
    assert state["peak"] == 2


# --------------------------------------------------------------------------
# Origin + CSRF on non-GET requests
# --------------------------------------------------------------------------


def post(client: TestClient, headers=None):
    return client.post(
        "/api/v1/_probe",
        headers={**cookie_headers(COOKIE), **(headers or {})},
        follow_redirects=False,
    )


def test_post_with_matching_origin_and_csrf_is_allowed(client, uc_http):
    resp = post(client, {"Origin": PUBLIC_ORIGIN, "X-CSRF-Token": CSRF})
    assert resp.status_code == 200


def test_post_without_origin_is_rejected(client, uc_http):
    resp = post(client, {"X-CSRF-Token": CSRF})
    assert resp.status_code == 403
    assert resp.json()["error"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example.test", "http://stock.example.test", "https://stock.example.test.evil.test", "null"],
)
def test_post_with_foreign_origin_is_rejected(client, uc_http, origin):
    resp = post(client, {"Origin": origin, "X-CSRF-Token": CSRF})
    assert resp.status_code == 403
    assert resp.json()["error"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.parametrize("headers", [{}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": ""}])
def test_post_without_valid_csrf_is_rejected(client, uc_http, headers):
    resp = post(client, {"Origin": PUBLIC_ORIGIN, **headers})
    assert resp.status_code == 403
    assert resp.json()["error"] == "CSRF_INVALID"


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_other_write_methods_also_require_csrf(client, uc_http, method):
    resp = client.request(
        method,
        "/api/v1/_probe",
        headers={**cookie_headers(COOKIE), "Origin": PUBLIC_ORIGIN},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "CSRF_INVALID"


def test_non_admin_post_is_403_admin_required_not_csrf(client, uc_http):
    uc_http.return_value = session_response(role="user")
    resp = post(client, {"Origin": PUBLIC_ORIGIN, "X-CSRF-Token": CSRF})
    assert resp.status_code == 403
    assert resp.json()["error"] == "ADMIN_REQUIRED"


def test_unauthenticated_post_is_401(client, uc_http):
    resp = client.post("/api/v1/_probe", headers={"Origin": PUBLIC_ORIGIN, "X-CSRF-Token": CSRF})
    assert resp.status_code == 401
    uc_http.assert_not_called()


# --------------------------------------------------------------------------
# Protected surface and exemptions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/_probe",
        "/api/v1/history",
        "/api/v1/portfolio/accounts",
        "/api/v1/system/config",
        "/api/v1/boards/overview",
        "/api/_other_probe",
        "/api/unknown",
        "/docs",
        "/redoc",
        "/openapi.json",
    ],
)
def test_protected_routes_require_a_session(client, uc_http, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 401
    assert resp.json()["error"] == "AUTH_REQUIRED"
    uc_http.assert_not_called()


@pytest.mark.parametrize("path", ["/health", "/api/health", "/api/v1/health"])
def test_health_checks_are_exempt(client, uc_http, path):
    resp = client.get(path)
    assert resp.status_code == 200
    uc_http.assert_not_called()


def test_session_login_logout_endpoints_are_exempt(client, uc_http):
    uc_http.return_value = FakeResponse(401, {})
    assert client.get("/api/session").status_code == 200
    assert client.get("/api/login", follow_redirects=False).status_code == 302
    assert client.get("/api/logout", follow_redirects=False).status_code == 302


def test_spa_static_files_are_public_but_api_fallback_is_not(uc_env, uc_http):
    spa_client = TestClient(build_app_with_probes(uc_env, with_frontend=True), base_url="http://testserver")
    assert spa_client.get("/").status_code == 200
    assert "spa" in spa_client.get("/some/spa/route").text
    resp = spa_client.get("/api/does-not-exist", follow_redirects=False)
    assert resp.status_code == 401
    uc_http.assert_not_called()


def test_options_preflight_is_not_blocked(client, uc_http):
    resp = client.request(
        "OPTIONS",
        "/api/v1/_probe",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert resp.status_code != 401
    uc_http.assert_not_called()


# --------------------------------------------------------------------------
# /api/session, /api/login, /api/logout
# --------------------------------------------------------------------------


EXPECTED_LOGIN_URL = f"{USER_CENTER_URL}/login?return=https%3A%2F%2Fstock.example.test%2F"
EXPECTED_LOGOUT_URL = f"{USER_CENTER_URL}/logout?return=https%3A%2F%2Fstock.example.test%2F"


def test_session_endpoint_for_admin(client, uc_http):
    resp = get(client, "/api/session")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "authenticated": True,
        "authorized": True,
        "username": "Darren",
        "role": "admin",
        "requiredRole": "admin",
        "csrfToken": CSRF,
        "loginUrl": EXPECTED_LOGIN_URL,
        "logoutUrl": EXPECTED_LOGOUT_URL,
    }


def test_session_endpoint_logged_out(client, uc_http):
    resp = get(client, "/api/session", cookies=None)
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is False
    assert body["authorized"] is False
    assert body["username"] is None and body["role"] is None and body["csrfToken"] is None
    assert body["loginUrl"] == EXPECTED_LOGIN_URL
    uc_http.assert_not_called()


def test_session_endpoint_for_non_admin_hides_csrf_token(client, uc_http):
    uc_http.return_value = session_response(role="user")
    body = get(client, "/api/session").json()
    assert body["authenticated"] is True
    assert body["authorized"] is False
    assert body["role"] == "user"
    assert body["csrfToken"] is None
    assert body["logoutUrl"] == EXPECTED_LOGOUT_URL


def test_session_endpoint_user_center_down_is_503(client, uc_http):
    uc_http.side_effect = requests.ConnectionError("down")
    resp = get(client, "/api/session")
    assert resp.status_code == 503
    assert resp.json()["error"] == "USER_CENTER_UNAVAILABLE"


def test_login_and_logout_redirect_to_the_user_center(client, uc_http):
    login = client.get("/api/login", follow_redirects=False)
    assert login.status_code == 302
    assert login.headers["location"] == EXPECTED_LOGIN_URL
    logout = client.get("/api/logout", follow_redirects=False)
    assert logout.status_code == 302
    assert logout.headers["location"] == EXPECTED_LOGOUT_URL
    uc_http.assert_not_called()


# --------------------------------------------------------------------------
# Local password endpoints are disabled in user_center mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/auth/login", {"password": "secret1"}),
        ("post", "/api/v1/auth/logout", None),
        ("post", "/api/v1/auth/settings", {"authEnabled": False}),
        (
            "post",
            "/api/v1/auth/change-password",
            {"currentPassword": "a", "newPassword": "bbbbbb", "newPasswordConfirm": "bbbbbb"},
        ),
    ],
)
def test_local_password_endpoints_return_a_clear_error(client, uc_http, method, path, body):
    resp = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert resp.status_code == 409
    assert resp.json()["error"] == "auth_mode_user_center"
    assert not (Path(auth._get_data_dir()) / ".admin_password_hash").exists()
    uc_http.assert_not_called()


def test_status_endpoint_reports_the_mode_without_calling_the_user_center(client, uc_http):
    resp = client.get("/api/v1/auth/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authMode"] == "user_center"
    assert body["authEnabled"] is True
    assert body["loggedIn"] is False
    uc_http.assert_not_called()


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_missing_public_origin_fails_closed(uc_env, uc_http, monkeypatch):
    monkeypatch.delenv("PUBLIC_ORIGIN", raising=False)
    auth.refresh_auth_state()
    client = TestClient(build_app_with_probes(uc_env), base_url="http://testserver")
    resp = get(client, "/api/v1/_probe")
    assert resp.status_code == 503
    assert resp.json()["error"] == "AUTH_MISCONFIGURED"
    assert client.get("/api/login", follow_redirects=False).status_code == 503
    uc_http.assert_not_called()


def test_public_origin_with_a_path_is_rejected(uc_env, uc_http, monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://stock.example.test/app")
    client = TestClient(build_app_with_probes(uc_env), base_url="http://testserver")
    assert get(client, "/api/v1/_probe").json()["error"] == "AUTH_MISCONFIGURED"


def test_public_origin_trailing_slash_is_normalised(uc_env, uc_http, monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN", PUBLIC_ORIGIN + "/")
    client = TestClient(build_app_with_probes(uc_env), base_url="http://testserver")
    assert post(client, {"Origin": PUBLIC_ORIGIN, "X-CSRF-Token": CSRF}).status_code == 200


def test_user_center_url_defaults_to_the_nexorai_user_center(uc_env, uc_http, monkeypatch):
    monkeypatch.delenv("USER_CENTER_URL", raising=False)
    client = TestClient(build_app_with_probes(uc_env), base_url="http://testserver")
    get(client, "/api/v1/_probe")
    assert uc_http.call_args.args[0] == "https://user.nexorai.com.cn/v1/session"


def test_required_role_user_also_admits_admins(uc_env, uc_http, monkeypatch):
    monkeypatch.setenv("USER_CENTER_REQUIRED_ROLE", "user")
    client = TestClient(build_app_with_probes(uc_env), base_url="http://testserver")
    uc_http.return_value = session_response(role="user")
    assert get(client, "/api/v1/_probe").status_code == 200
    uc_http.return_value = session_response(role="admin")
    assert get(client, "/api/v1/_probe").status_code == 200
    uc_http.return_value = session_response(role="guest")
    assert get(client, "/api/v1/_probe").status_code == 403


def test_invalid_required_role_fails_closed(uc_env, uc_http, monkeypatch):
    monkeypatch.setenv("USER_CENTER_REQUIRED_ROLE", "root")
    client = TestClient(build_app_with_probes(uc_env), base_url="http://testserver")
    assert get(client, "/api/v1/_probe").json()["error"] == "AUTH_MISCONFIGURED"


def test_unknown_auth_mode_fails_closed(uc_env, uc_http, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "usercenter")
    auth.refresh_auth_state()
    assert auth.get_auth_mode() == auth.AUTH_MODE_USER_CENTER
    client = TestClient(build_app_with_probes(uc_env), base_url="http://testserver")
    assert client.get("/api/v1/_probe").status_code == 401


def test_env_file_value_wins_over_process_env(uc_env, monkeypatch):
    (uc_env / ".env").write_text("AUTH_MODE=local\n", encoding="utf-8")
    auth.refresh_auth_state()
    assert auth.get_auth_mode() == auth.AUTH_MODE_LOCAL


# --------------------------------------------------------------------------
# System-config backup gate (previously tied to the local session cookie)
# --------------------------------------------------------------------------


def test_env_backup_gate_accepts_a_middleware_verified_session(uc_env):
    request = SimpleNamespace(
        state=SimpleNamespace(user_center_session=object()),
        cookies={},
    )
    system_config_endpoint._allow_env_backup_access(request)


def test_env_backup_gate_rejects_requests_that_bypassed_the_middleware(uc_env):
    request = SimpleNamespace(state=SimpleNamespace(), cookies={})
    with pytest.raises(system_config_endpoint.EnvBackupAccessDenied) as exc_info:
        system_config_endpoint._allow_env_backup_access(request)
    assert exc_info.value.status_code == 401


# --------------------------------------------------------------------------
# Local mode is unchanged
# --------------------------------------------------------------------------


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("PUBLIC_ORIGIN", raising=False)
    monkeypatch.setattr(auth, "_is_auth_enabled_from_env", lambda: False)
    Config.reset_instance()
    auth.refresh_auth_state()
    yield tmp_path
    auth.refresh_auth_state()
    Config.reset_instance()


def test_default_mode_is_local_and_open_when_password_auth_is_off(local_env, monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(uc, "_http_get", mock)
    assert auth.get_auth_mode() == auth.AUTH_MODE_LOCAL
    client = TestClient(build_app_with_probes(local_env), base_url="http://testserver")
    assert client.get("/api/v1/_probe").status_code == 200
    assert client.post("/api/v1/_probe").status_code == 200  # no Origin/CSRF enforcement in local mode
    mock.assert_not_called()


def test_local_mode_password_auth_still_uses_the_local_session(local_env, monkeypatch):
    monkeypatch.setattr(auth, "_is_auth_enabled_from_env", lambda: True)
    auth.refresh_auth_state()
    mock = MagicMock()
    monkeypatch.setattr(uc, "_http_get", mock)
    client = TestClient(build_app_with_probes(local_env), base_url="http://testserver")
    resp = client.get("/api/v1/_probe")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized", "message": "Login required"}
    # Non-v1 routes keep their previous (unprotected) behaviour in local mode.
    assert client.get("/api/_other_probe").status_code == 200
    mock.assert_not_called()


def test_local_mode_session_endpoints_are_404(local_env):
    client = TestClient(build_app_with_probes(local_env), base_url="http://testserver")
    for path in ("/api/session", "/api/login", "/api/logout"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 404
        assert resp.json()["error"] == "user_center_disabled"


def test_local_mode_status_reports_auth_mode(local_env):
    client = TestClient(build_app_with_probes(local_env), base_url="http://testserver")
    body = client.get("/api/v1/auth/status").json()
    assert body["authMode"] == "local"
    assert body["authEnabled"] is False
    assert body["setupState"] == "no_password"
