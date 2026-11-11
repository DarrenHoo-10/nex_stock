"""Offline cross-service security/replay tests, with real ASGI dispatch and ledger."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.services import finance_identity as ident
from api.v1.endpoints import finance


@pytest.fixture
def store(tmp_path, monkeypatch):
    values = {"FINANCE_SERVICE_TOKEN": "a" * 40, "FINANCE_INSTANCE_ID": "stock-test",
              "FINANCE_CORE_ID": "core-test", "FINANCE_LOCAL_ISSUER": "native:test",
              "FINANCE_LOCAL_SUBJECT": "known-owner", "PUBLIC_ORIGIN": "https://stock.test"}
    monkeypatch.setattr(ident, "setting", lambda key: values.get(key, ""))
    monkeypatch.setattr(finance, "setting", lambda key: values.get(key, ""))
    monkeypatch.setattr(ident, "is_user_center_mode", lambda: False)
    monkeypatch.setattr(finance, "is_user_center_mode", lambda: False)
    result = ident.FinanceIdentityStore(tmp_path / "ledger.db")
    monkeypatch.setattr(finance, "FinanceIdentityStore", lambda: result)
    return result


def owner():
    return ident.identity(None)


def test_single_use_exchange_and_revocation(store):
    grant = store.create(owner(), "")
    code = store.exchange(owner())["code"]
    redeemed = store.redeem(code, "core-test")
    assert redeemed["delegationToken"] == grant["delegationToken"]
    with pytest.raises(HTTPException) as exc:
        store.redeem(code, "core-test")
    assert exc.value.status_code == 401
    store.revoke(owner(), grant["delegationId"])
    with pytest.raises(HTTPException):
        store.verify(grant["delegationToken"])
    with pytest.raises(HTTPException) as exc:
        store.exchange(owner())
    assert exc.value.status_code == 409


def test_durable_binding_renews_expired_lease_but_old_token_stays_dead(store):
    grant = store.create(owner(), "")
    with store.db() as db:
        db.execute("UPDATE grants SET expires=0")
    with pytest.raises(HTTPException):
        store.verify(grant["delegationToken"])
    code = store.exchange(owner())["code"]
    renewed = store.redeem(code, "core-test")
    assert renewed["delegationId"] == grant["delegationId"]
    assert renewed["delegationToken"] != grant["delegationToken"]
    with pytest.raises(HTTPException):
        store.verify(grant["delegationToken"])


def test_wrong_core_and_owner_cannot_consume_or_revoke(store):
    grant = store.create(owner(), "")
    code = store.exchange(owner())["code"]
    with pytest.raises(HTTPException):
        store.redeem(code, "other")
    assert store.redeem(code, "core-test")["subject"] == "known-owner"
    with pytest.raises(HTTPException):
        store.revoke({**owner(), "subject": "intruder"}, grant["delegationId"])


def test_credential_rotation_requires_explicit_rebind_and_no_old_grant_fallback(store, monkeypatch):
    original = store.create(owner(), "")
    first_setting = ident.setting
    monkeypatch.setattr(ident, "setting", lambda key: "b" * 40 if key == "FINANCE_SERVICE_TOKEN" else first_setting(key))
    with pytest.raises(HTTPException) as exc:
        store.exchange(owner())
    assert exc.value.status_code == 409
    rebound = store.create(owner(), "")
    assert rebound["delegationId"] != original["delegationId"]
    store.revoke(owner(), rebound["delegationId"])
    with pytest.raises(HTTPException) as exc:
        store.exchange(owner())
    assert exc.value.status_code == 409


def test_ciphertext_does_not_contain_cookie_or_bearer(store):
    grant = store.create(owner(), "user_session=private-cookie")
    data = store.path.read_bytes()
    assert b"private-cookie" not in data
    assert grant["delegationToken"].encode() not in data


def test_user_session_is_reverified_and_subject_checked(store, monkeypatch):
    from src import user_center_auth
    monkeypatch.setattr(ident, "is_user_center_mode", lambda: True)
    settings = SimpleNamespace(base_url="https://identity.test", required_role="user")
    monkeypatch.setattr(user_center_auth, "load_settings", lambda: settings)
    session = SimpleNamespace(user_id="alice", role="user")
    monkeypatch.setattr(user_center_auth, "resolve_session", lambda cookies: session)
    grant = store.create(ident.identity(session), "user_session=test")
    assert store.verify(grant["delegationToken"])[1].user_id == "alice"
    session.user_id = "bob"
    with pytest.raises(HTTPException) as exc:
        store.verify(grant["delegationToken"])
    assert exc.value.status_code == 403
    monkeypatch.setattr(user_center_auth, "resolve_session", lambda cookies: None)
    with pytest.raises(HTTPException) as exc:
        store.verify(grant["delegationToken"])
    assert exc.value.status_code == 401


def test_idempotency_conflict_and_unknown_outcome(store):
    calls = []
    def execute():
        calls.append(True)
        return {"created": 1}
    assert store.operation("grant", "op", {"a": 1}, execute) == {"created": 1}
    assert store.operation("grant", "op", {"a": 1}, execute) == {"created": 1}
    assert len(calls) == 1
    with pytest.raises(HTTPException):
        store.operation("grant", "op", {"a": 2}, execute)
    def crash():
        raise RuntimeError("lost response")
    with pytest.raises(RuntimeError):
        store.operation("grant", "crash", {}, crash)
    with pytest.raises(HTTPException) as exc:
        store.operation("grant", "crash", {}, execute)
    assert exc.value.detail["error"] == "FINANCE_OPERATION_OUTCOME_UNKNOWN"


def test_asgi_identity_is_per_call_and_no_global_override(store):
    app = FastAPI()
    app.add_middleware(ident.InternalIdentityMiddleware)
    @app.get("/who")
    def who(request: Request):
        return {"user": getattr(request.state, "user_center_session", SimpleNamespace(user_id=None)).user_id}
    client = TestClient(app)
    def read(user):
        with ident.session_scope(SimpleNamespace(user_id=user)):
            return client.get("/who").json()["user"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(read, ["alice", "bob"])) == ["alice", "bob"]
    assert client.get("/who").json()["user"] is None


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(finance.router, prefix="/api/v1/finance")
    return TestClient(app)


def headers(grant, audience="private"):
    return {"Authorization": "Bearer " + "a" * 40, "X-Finance-Delegation": grant["delegationToken"],
            "X-Finance-Audience": audience}


def test_service_bearer_alone_never_identifies_user(client):
    assert client.post("/api/v1/finance/delegations/verify", headers={"Authorization": "Bearer " + "a" * 40}).status_code == 401


def test_browser_binding_validates_real_cookie_csrf_and_masks_token(store, monkeypatch):
    from api.middlewares import auth
    from src import user_center_auth
    monkeypatch.setattr(auth, "is_user_center_mode", lambda: True)
    # Login telemetry is outside the identity risk path; do not touch the
    # deployment database when running this transport-auth fixture.
    monkeypatch.setattr(auth, "_write_app_user", lambda session: None, raising=False)
    monkeypatch.setattr(ident, "is_user_center_mode", lambda: True)
    monkeypatch.setattr(finance, "is_user_center_mode", lambda: True)
    settings = user_center_auth.UserCenterSettings("https://identity.test", "https://stock.test", "user")
    monkeypatch.setattr(user_center_auth, "load_settings", lambda: settings)
    verified = user_center_auth.UserSession("alice", "alice", "Alice", "user", "csrf-test")
    monkeypatch.setattr(user_center_auth, "fetch_session", lambda value, cfg: verified)
    app = FastAPI()
    app.include_router(finance.router, prefix="/api/v1/finance")
    app.add_middleware(auth.AuthMiddleware)
    @app.get("/api/v1/test-verified-context")
    def verified_context():
        return {"role": ident.acting_session.get().role, "user_id": ident.acting_session.get().user_id}
    client = TestClient(app)
    values = {"Cookie": "user_session=" + "a" * 64, "Origin": "https://stock.test", "X-CSRF-Token": "csrf-test"}
    assert client.post("/api/v1/finance/delegations").status_code == 401
    assert client.post("/api/v1/finance/delegations", headers={"Cookie": values["Cookie"]}).status_code == 403
    response = client.post("/api/v1/finance/delegations", headers=values)
    assert response.status_code == 200, response.text
    assert response.json()["subject"] == "alice"
    assert "delegationToken" not in response.json()
    assert "refreshToken" not in response.json()
    listed = client.get("/api/v1/finance/delegations", headers=values)
    assert "delegationToken" not in listed.text
    assert "refreshToken" not in listed.text
    assert client.get("/api/v1/test-verified-context", headers=values).json() == {"role": "user", "user_id": "alice"}


def test_concurrent_explicit_connect_reuses_binding_and_rotation_status_requires_rebind(store, monkeypatch):
    def connect(_):
        return store.ensure_bound(owner(), "")
    with ThreadPoolExecutor(max_workers=2) as pool:
        grants = list(pool.map(connect, [1, 2]))
    assert grants[0]["delegationId"] == grants[1]["delegationId"]
    assert store.bound(owner()) is True
    previous = ident.setting
    monkeypatch.setattr(ident, "setting", lambda key: "rotated" * 8 if key == "FINANCE_SERVICE_TOKEN" else previous(key))
    assert store.bound(owner()) is False
    replacement = store.ensure_bound(owner(), "")
    assert replacement["delegationId"] != grants[0]["delegationId"]
    assert store.bound(owner()) is True


def test_refresh_revalidates_actual_authority_and_preserves_operation_scope(store, monkeypatch):
    from src import user_center_auth
    monkeypatch.setattr(ident, "is_user_center_mode", lambda: True)
    settings = SimpleNamespace(base_url="https://identity.test", required_role="user")
    session = SimpleNamespace(user_id="alice", role="user")
    monkeypatch.setattr(user_center_auth, "load_settings", lambda: settings)
    monkeypatch.setattr(user_center_auth, "resolve_session", lambda cookies: session)
    grant = store.create(ident.identity(session), "user_session=verified")
    first = store.operation(grant["delegationId"], "one-write", {}, lambda: {"written": True})
    with store.db() as db:
        db.execute("UPDATE grants SET expires=0")
    renewed = store.renew(grant["delegationId"], grant["refreshToken"])
    assert renewed["delegationId"] == grant["delegationId"]
    assert renewed["refreshToken"] == grant["refreshToken"]
    assert renewed["delegationToken"] != grant["delegationToken"]
    assert store.operation(renewed["delegationId"], "one-write", {}, lambda: pytest.fail("replayed write")) == first
    assert renewed["capabilities"]["offlinePrivateRenewal"] is False
    session.role = "disabled"
    with pytest.raises(HTTPException):
        store.renew(grant["delegationId"], grant["refreshToken"])
    monkeypatch.setattr(user_center_auth, "resolve_session", lambda cookies: None)
    with pytest.raises(HTTPException):
        store.renew(grant["delegationId"], grant["refreshToken"])


def test_public_subscription_is_independent_proof_not_user_identity(client, store, monkeypatch):
    from src.services import finance_report_events as events
    grant_owner = owner()
    grant = store.create(grant_owner, "")
    monkeypatch.setattr(events, "FinanceIdentityStore", lambda: store)
    monkeypatch.setattr(events.FinanceReportEvents, "reconcile", lambda *args, **kwargs: None)
    feed = events.FinanceReportEvents(store)
    feed.record("private-before", "daily_plan", None, "private", owner="alice")
    feed.record("public-one", "market_review", "2026-11-10", "public1", owner=None, public=True)
    feed.record("private-between", "daily_plan", None, "private", owner="bob")
    feed.record("public-two", "market_review", "2026-11-10", "public2", owner=None, public=True)
    proof_headers = {"Authorization": "Bearer " + "a" * 40, "X-Finance-Subscription": grant["refreshToken"]}
    response = client.get("/api/v1/finance/reports/public-events", headers=proof_headers)
    assert response.status_code == 200, response.text
    assert [event["cursor"] for event in response.json()["events"]] == [1, 2]
    assert "private" not in response.text
    assert client.get("/api/v1/finance/reports/public-events?head=true", headers=proof_headers).json() == {"events": [], "nextCursor": 2}
    assert client.post("/api/v1/finance/delegations/verify", headers=proof_headers).status_code == 401
    store.revoke(grant_owner, grant["delegationId"])
    assert client.get("/api/v1/finance/reports/public-events", headers=proof_headers).status_code == 401


def test_public_private_fetch_denied_before_dispatch(client, store, monkeypatch):
    import api.internal_client
    grant = store.create(owner(), "")
    def unexpected(*args, **kwargs):
        pytest.fail("forbidden route reached dispatch")
    monkeypatch.setattr(api.internal_client, "call", unexpected)
    response = client.post("/api/v1/finance/tools/call", headers=headers(grant, "public"), json={
        "name": "call_api", "arguments": {"method": "GET", "path": "/api/v1/portfolio/accounts"}})
    assert response.status_code == 403


def test_personal_paid_screening_tools_reject_public_and_require_write_receipt(client, store):
    grant = store.create(owner(), "")
    request = {"name": "screening_run", "arguments": {"strategy": "balanced"}, "operationId": "owned-screen"}
    assert client.post("/api/v1/finance/tools/call", headers=headers(grant, "public"), json=request).status_code == 403
    assert client.post("/api/v1/finance/tools/call", headers=headers(grant, "public"), json={"name": "screening_task", "arguments": {}}).status_code == 403
    request.pop("operationId")
    assert client.post("/api/v1/finance/tools/call", headers=headers(grant), json=request).status_code == 422


def test_screening_task_gateway_keeps_real_private_result_owner_filter(client, store):
    import time
    from src.services.finance_user_screening import FinanceUserScreening
    grant_owner = owner()
    grant = store.create(grant_owner, "")
    FinanceUserScreening(store=store)
    with store.db() as db:
        for task_id, subject in (("own", grant_owner["subject"]), ("foreign", "another-user")):
            db.execute("""INSERT INTO finance_screening_tasks
                (task_id,issuer,subject,grant_id,user_id,request,status,result,epoch,created)
                VALUES(?,?,?,?,?,?,'completed',?,'fixture',?)""",
                       (task_id, grant_owner["issuer"], subject, grant["delegationId"], subject, "{}",
                        json.dumps({"private": subject}), time.time()))
    own = client.post("/api/v1/finance/tools/call", headers=headers(grant), json={"name": "screening_task", "arguments": {"task_id": "own"}})
    assert own.status_code == 200 and own.json()["result"]["private"] == grant_owner["subject"]
    foreign = client.post("/api/v1/finance/tools/call", headers=headers(grant), json={"name": "screening_task", "arguments": {"task_id": "foreign"}})
    assert foreign.status_code == 404
    listed = client.post("/api/v1/finance/tools/call", headers=headers(grant), json={"name": "screening_task", "arguments": {}})
    assert [task["task_id"] for task in listed.json()["tasks"]] == ["own"]


def test_paid_call_write_requires_operation_id_and_identity_args_rejected(client, store, monkeypatch):
    import api.internal_client
    grant = store.create(owner(), "")
    monkeypatch.setattr(api.internal_client, "match_route", lambda *args: "/api/v1/stocks/watchlist")
    args = {"method": "POST", "path": "/api/v1/stocks/watchlist", "body": {"code": "TEST"}}
    assert client.post("/api/v1/finance/tools/call", headers=headers(grant), json={"name": "call_api", "arguments": args}).status_code == 422
    args["user_id"] = "other"
    assert client.post("/api/v1/finance/tools/call", headers=headers(grant), json={"name": "call_api", "arguments": args}).status_code == 422


def test_shared_main_blocks_personal_and_derived_without_p2(store, monkeypatch):
    monkeypatch.setattr(finance, "is_user_center_mode", lambda: True)
    monkeypatch.setattr(finance, "user_system_available", lambda: False)
    monkeypatch.setattr(finance, "personal_data_available", lambda: False)
    monkeypatch.setattr("api.internal_client.excluded_reason", lambda *args: None)
    assert not finance.allowed("GET", "/api/v1/history/123")
    assert not finance.allowed("GET", "/api/v1/stocks/watchlist")
    assert not finance.allowed("POST", "/api/v1/screening/screen")
    assert finance.allowed("GET", "/api/v1/stocks/TEST/quote")


def test_foundation_identity_alone_does_not_enable_personal_or_paid_routes(monkeypatch):
    from api import deps
    import src
    monkeypatch.setattr(deps, "CurrentUser", object, raising=False)
    monkeypatch.setattr(src, "storage_multi_user_migration", SimpleNamespace(MULTI_USER_SCHEMA_VERSION="p1"), raising=False)
    assert not ident.personal_data_available()
    assert not ident.user_system_available()
    monkeypatch.setattr(finance, "is_user_center_mode", lambda: True)
    monkeypatch.setattr("api.internal_client.excluded_reason", lambda *args: None)
    assert not finance.allowed("GET", "/api/v1/portfolio/accounts")
    assert not finance.allowed("POST", "/api/v1/analysis/tasks")
    assert finance.allowed("GET", "/api/v1/stocks/TEST/quote")


def test_p2a_allows_verified_ledger_while_derived_and_paid_routes_wait(monkeypatch):
    monkeypatch.setattr(finance, "is_user_center_mode", lambda: True)
    monkeypatch.setattr(finance, "personal_data_available", lambda: True)
    monkeypatch.setattr(finance, "user_system_available", lambda: False)
    monkeypatch.setattr("api.internal_client.excluded_reason", lambda *args: None)
    assert finance.allowed("GET", "/api/v1/portfolio/accounts")
    assert finance.allowed("POST", "/api/v1/stocks/watchlist/add")
    assert finance.allowed("POST", "/api/v1/stocks/watchlist/remove")
    assert not finance.allowed("GET", "/api/v1/history/4")
    assert not finance.allowed("POST", "/api/v1/portfolio/positions/AAPL/analysis")
    assert not finance.allowed("GET", "/api/v1/portfolio/accounts", "public")


@pytest.mark.skipif(not ident.user_system_available(), reason="Requires integrated P1/P2 user system")
def test_integrated_real_watchlist_and_legacy_call_api_use_same_subject(client, store, tmp_path, monkeypatch):
    """Keep real CurrentUser, business parsing/service and SQLite in the risk path."""
    from tests.multi_user_helpers import prepare_multi_user_env, teardown_multi_user_env
    from src import user_center_auth
    from api import deps, internal_client
    from api.v1.router import router
    from src.agent.tools.api_tools import _handle_call_api
    prepare_multi_user_env(tmp_path, monkeypatch)
    monkeypatch.setattr(ident, "is_user_center_mode", lambda: True)
    monkeypatch.setattr(finance, "is_user_center_mode", lambda: True)
    monkeypatch.setattr(deps, "is_user_center_mode", lambda: True)
    sessions = {name: SimpleNamespace(user_id=name, role="user", handle=name, display_name=name)
                for name in ("alice", "bob")}
    monkeypatch.setattr(user_center_auth, "load_settings", lambda: SimpleNamespace(base_url="https://identity.test", required_role="user"))
    monkeypatch.setattr(user_center_auth, "resolve_session", lambda cookies: sessions.get(cookies[0]))
    internal = FastAPI()
    internal.add_middleware(ident.InternalIdentityMiddleware)
    internal.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(internal_client, "_internal_app", internal)
    monkeypatch.setattr(internal_client, "_running_app", None)
    monkeypatch.setattr(internal_client, "_route_index", None)
    grants = {name: store.create(ident.identity(session), name) for name, session in sessions.items()}
    def invoke(user, method, path, body=None, operation_id=None):
        return client.post("/api/v1/finance/tools/call", headers=headers(grants[user]), json={
            "name": "call_api", "arguments": {"method": method, "path": path, "body": body},
            "operationId": operation_id})
    try:
        added = invoke("alice", "POST", "/api/v1/stocks/watchlist/add", {"stock_code": "AAPL"}, "add-one")
        assert added.status_code == 200, added.text
        assert added.json()["data"]["stock_codes"] == ["AAPL"]
        assert invoke("bob", "GET", "/api/v1/stocks/watchlist").json()["data"]["stock_codes"] == []
        assert invoke("alice", "POST", "/api/v1/stocks/watchlist/add", {"stock_code": "AAPL"}, "add-one").json() == added.json()
        with ident.session_scope(sessions["alice"]):
            assert _handle_call_api("GET", "/api/v1/stocks/watchlist")["data"]["stock_codes"] == ["AAPL"]
            with pytest.raises(internal_client.ActingUserRequired):
                internal_client.acting_user("bob")
        with ident.session_scope(sessions["bob"]):
            assert _handle_call_api("GET", "/api/v1/stocks/watchlist")["data"]["stock_codes"] == []
        admin_path = "/api/v1/intelligence/sources"
        def catalog():
            response = client.get("/api/v1/finance/tools", headers=headers(grants["bob"]))
            assert response.status_code == 200
            return response.json()["openapi"]["paths"]
        assert admin_path not in catalog()
        assert invoke("bob", "GET", admin_path).status_code == 403
        sessions["bob"].role = "admin"  # the same delegation revalidates the current session's role
        assert "get" in catalog()[admin_path]
    finally:
        teardown_multi_user_env()
