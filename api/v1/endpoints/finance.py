"""Versioned finance identity exchange and authenticated business tool gateway."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.auth import is_user_center_mode
from src.services.finance_identity import (
    FinanceIdentityStore, configuration, fail, identity, service_auth,
    personal_data_available, session_scope, setting, user_system_available,
)

router = APIRouter()
SERVICE_PATHS = frozenset({
    "/api/v1/finance/session-exchanges/redeem", "/api/v1/finance/delegations/verify",
    "/api/v1/finance/tools", "/api/v1/finance/tools/call",
    "/api/v1/finance/reports/events",
    "/api/v1/finance/channels/feishu",
    "/api/v1/finance/history",
    "/api/v1/finance/delegations/renew", "/api/v1/finance/reports/public-events",
})


def browser_identity(request, write=False):
    # Main's auth middleware verifies cookies, Origin and CSRF. Resolve the actual
    # CurrentUser once present, and never accept an identity from request bodies.
    session = getattr(request.state, "user_center_session", None)
    if personal_data_available():
        from api.deps import get_current_user
        get_current_user(request)
    if write and not is_user_center_mode():
        from src.user_center_auth import normalize_origin
        origin = normalize_origin(setting("PUBLIC_ORIGIN"))
        if not origin or normalize_origin(request.headers.get("origin", "")) != origin:
            fail(403, "ORIGIN_NOT_ALLOWED")
    return identity(session)


def cookie(request):
    from src.user_center_auth import extract_session_cookie
    value = extract_session_cookie(request.headers.getlist("cookie"))
    return "user_session=" + value if value else ""


@router.post("/delegations")
def bind(request: Request):
    payload = FinanceIdentityStore().ensure_bound(browser_identity(request, True), cookie(request))
    return {k: v for k, v in payload.items() if k not in {"delegationToken", "refreshToken"}}


@router.get("/delegations")
def bindings(request: Request):
    return {"delegations": FinanceIdentityStore().list(browser_identity(request))}


@router.delete("/delegations/{delegation_id}")
def revoke(delegation_id: str, request: Request):
    FinanceIdentityStore().revoke(browser_identity(request, True), delegation_id)
    return {"revoked": True}


@router.post("/session-exchanges")
def exchange(request: Request):
    return FinanceIdentityStore().exchange(browser_identity(request, True), cookie(request))


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    coreId: str = Field(min_length=1, max_length=256)


@router.post("/session-exchanges/redeem")
def redeem(body: RedeemRequest, request: Request):
    service_auth(request.headers)
    return FinanceIdentityStore().redeem(body.code, body.coreId)


def delegated(request):
    service_auth(request.headers)
    token = request.headers.get("x-finance-delegation", "")
    if not token:
        fail(401, "FINANCE_DELEGATION_REQUIRED")
    return FinanceIdentityStore().verify(token)


@router.post("/delegations/verify")
def verify(request: Request):
    payload, _ = delegated(request)
    return {k: v for k, v in payload.items() if k not in {"delegationToken", "refreshToken"}}


class RenewalRequest(BaseModel):
    delegationId: str = Field(min_length=1, max_length=256)
    refreshToken: str = Field(min_length=1, max_length=256)


@router.post("/delegations/renew")
def renew_delegation(body: RenewalRequest, request: Request):
    service_auth(request.headers)
    return FinanceIdentityStore().renew(body.delegationId, body.refreshToken)


# Only business namespaces. Auth, system, model settings, identity exchange,
# assistant execution and arbitrary connectors cannot be reached recursively.
BUSINESS_GROUPS = frozenset({
    "stocks", "data", "boards", "screening", "analysis", "history", "portfolio",
    "plan", "alerts", "decision-signals", "backtest", "intelligence", "closing-analysis",
})
PUBLIC_PRE_USER_SYSTEM = frozenset({"data", "boards", "stocks", "screening"})


def public_route(method, path):
    """Positive public fact allowlist, enforced before a handler can fetch data."""
    from src.services.finance_data_visibility import classify_api_data
    return classify_api_data(method, path)["audience"] == "public"


def _tool_user(session):
    from api.internal_client import acting_user
    from src.config import get_primary_user_id

    if session is None and is_user_center_mode():
        fail(401, "FINANCE_LOGIN_EXPIRED")
    with session_scope(session):
        return acting_user(session.user_id if session is not None else get_primary_user_id())


def allowed(method, path, audience="private", user=None, *, template=None):
    from api.internal_client import template_reason
    parts = path.split("/")
    if len(parts) < 4 or path[:8] != "/api/v1/":
        return False
    group = parts[3]
    if group not in BUSINESS_GROUPS and not (path == "/api/v1/agent/notes" or path.startswith("/api/v1/agent/notes/")):
        return False
    if template_reason(method, template or path, user):
        return False
    if audience == "public" and not public_route(method, path):
        return False
    if is_user_center_mode() and not user_system_available():
        # P2a has real owner-scoped watchlists, ledgers and notes. Analysis,
        # aggregate views and paid jobs depend on the later ownership migration.
        owner_path = path.startswith(("/api/v1/portfolio/", "/api/v1/agent/notes/")) or path in {
            "/api/v1/stocks/watchlist", "/api/v1/stocks/watchlist/add", "/api/v1/stocks/watchlist/remove",
            "/api/v1/portfolio", "/api/v1/agent/notes"}
        owned = personal_data_available() and owner_path and not path.endswith("/analysis")
        return public_route(method, path) or owned
    return True


def discovery(audience="private", user=None):
    from api.internal_client import get_openapi_spec
    spec = get_openapi_spec()
    paths = {path: {method: op for method, op in methods.items()
                    if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                    and allowed(method.upper(), path, audience, user)} for path, methods in spec["paths"].items()}
    financial_tools = [
        {"name": "screening_run", "description": "Run an owned private strategy screening with this user's configuration, model/search credentials and costs. Requires a stable operationId; returns an owned task reference.",
         "inputSchema": {"type": "object", "properties": {"strategy": {"type": "string"},
             "market": {"type": "string", "enum": ["cn", "us"], "default": "cn"},
             "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
             "variant_seed": {"type": "string"}, "scope": {"type": "object"}},
             "required": ["strategy"], "additionalProperties": False}},
        {"name": "screening_task", "description": "Read this user's private screening tasks; task_id reads the owned task and final private result, omission lists own history.",
         "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "additionalProperties": False}},
    ] if audience == "private" and (not is_user_center_mode() or user_system_available()) else []
    return {"version": "finance.v1", "tools": financial_tools + [
        {"name": "strategy_inputs", "description": "Batch cross-security daily facts and quantitative indicators plus optional strategy YAML; no shared LLM/search credentials. Model applies the strategy using supplied evidence.",
         "inputSchema": {"type": "object", "properties": {"stock_codes": {"type": "array", "items": {"type": "string"}},
             "days": {"type": "integer", "minimum": 1, "maximum": 365}, "strategy": {"type": "string"}},
             "required": ["stock_codes"], "additionalProperties": False}},
        {"name": "read_api_doc", "description": "Discover finance business APIs and exact input/output schemas",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "call_api", "description": "Execute discovered finance API as the delegated user; writes require operationId",
         "inputSchema": {"type": "object", "properties": {
             "method": {"type": "string"}, "path": {"type": "string"},
             "query": {"type": "object"}, "body": {}}, "required": ["method", "path"]}},
    ], "openapi": {**spec, "paths": {p: m for p, m in paths.items() if m}},
        "personalDataIsolation": user_system_available(),
        "evidenceContract": {"retrievedAt": "gateway retrieval UTC; never a market observation timestamp",
                             "marketTimestamp": "Only provider timestamps in data are evidence; absence means unknown"}}


@router.get("/tools")
def tools(request: Request):
    _, session = delegated(request)
    return discovery(audience_of(request), _tool_user(session))


def audience_of(request):
    audience = request.headers.get("x-finance-audience", "private")
    if audience not in {"public", "private"}:
        fail(422, "FINANCE_INVALID_AUDIENCE")
    return audience


@router.get("/reports/events")
def report_events(request: Request, after: int = 0, head: bool = False):
    payload, _ = delegated(request)
    if after < 0:
        fail(422, "FINANCE_INVALID_CURSOR")
    from src.services.finance_report_events import FinanceReportEvents, primary_user_id
    feed = FinanceReportEvents()
    ready = not is_user_center_mode() or user_system_available()
    feed.reconcile(public_only=not ready)
    subject = payload["subject"] if is_user_center_mode() else primary_user_id()
    audience = audience_of(request) if ready else "public"
    if head:
        return feed.head(subject=subject, audience=audience)
    return feed.read(subject, after, audience)


@router.get("/reports/public-events")
def public_report_events(request: Request, after: int = Query(0, ge=0), head: bool = False):
    service_auth(request.headers)
    proof = request.headers.get("x-finance-subscription", "")
    if not proof:
        fail(401, "FINANCE_SUBSCRIPTION_REQUIRED")
    FinanceIdentityStore().subscription(proof)
    from src.services.finance_report_events import FinanceReportEvents
    feed = FinanceReportEvents()
    feed.reconcile(public_only=True)
    if head:
        return feed.head(public=True)
    return feed.read_public(after)


@router.get("/history")
def legacy_history(request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
    payload, _ = delegated(request)
    if audience_of(request) != "private":
        fail(403, "FINANCE_HISTORY_PRIVATE")
    from src.services.finance_history_export import export_messages
    from src.services.finance_report_events import primary_user_id
    subject = payload["subject"] if is_user_center_mode() else primary_user_id()
    return export_messages(subject, after, limit)


class ChannelTransition(BaseModel):
    expectedVersion: int
    owner: str
    appId: str


@router.get("/channels/feishu")
def feishu_ownership(request: Request):
    service_auth(request.headers)
    from src.services.finance_channel_ownership import channel_state
    return channel_state()


@router.post("/channels/feishu")
def transfer_feishu(body: ChannelTransition, request: Request):
    service_auth(request.headers)
    from src.services.finance_channel_ownership import transition, channel_state
    if channel_state().get("appId") != body.appId:
        fail(409, "FINANCE_FEISHU_APP_MISMATCH")
    return transition(body.expectedVersion, body.owner)


class ToolCall(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)
    operationId: str | None = Field(default=None, min_length=1, max_length=256)


@router.post("/tools/call")
def call_tool(body: ToolCall, request: Request):
    payload, session = delegated(request)
    audience = audience_of(request)
    if body.name == "read_api_doc":
        return discovery(audience, _tool_user(session))
    if body.name == "strategy_inputs":
        from src.services.finance_strategy_inputs import strategy_inputs
        with session_scope(session):
            return strategy_inputs(body.arguments, user_id=_tool_user(session).user_id)
    if body.name in {"screening_run", "screening_task"}:
        if audience != "private":
            fail(403, "FINANCE_SCREENING_PRIVATE")
        if is_user_center_mode() and not user_system_available():
            fail(503, "FINANCE_USER_SYSTEM_REQUIRED")
        if body.name == "screening_run" and not body.operationId:
            fail(422, "FINANCE_OPERATION_ID_REQUIRED")
        from src.services.finance_user_screening import FinanceUserScreening
        service = FinanceUserScreening(store=FinanceIdentityStore())
        if body.name == "screening_run":
            return {"version": "finance.v1", **service.start(body.arguments, owner=payload, session=session, operation_id=body.operationId)}
        return {"version": "finance.v1", **service.get(body.arguments, owner=payload)}
    if body.name != "call_api":
        fail(404, "FINANCE_TOOL_NOT_FOUND")
    from urllib.parse import unquote
    from api.internal_client import CallRefused, call, match_route, safe_route_path
    args = body.arguments
    method, path = str(args.get("method", "GET")).upper(), str(args.get("path", ""))
    if set(args) - {"method", "path", "query", "body"}:
        fail(422, "FINANCE_UNKNOWN_ARGUMENT")
    if unquote(path) != path:
        fail(422, "FINANCE_INVALID_PATH")
    try:
        path = safe_route_path(path)
    except CallRefused:
        fail(422, "FINANCE_INVALID_PATH")
    user = _tool_user(session)
    template = match_route(method, path)
    if not allowed(method, path, audience, user, template=template):
        fail(403, "FINANCE_API_NOT_DELEGATED")
    if template is None:
        fail(404, "FINANCE_API_NOT_FOUND")

    def execute():
        with session_scope(session):
            status, content_type, text = call(method, path, user_id=user.user_id, params=args.get("query"), body=args.get("body"))
        try:
            data = json.loads(text) if "json" in content_type else text
        except ValueError:
            data = text
        from src.services.finance_market_evidence import evidence_for
        from src.services.finance_data_visibility import classify_api_data
        visibility = classify_api_data(method, path, response=data)
        if audience == "public" and visibility["audience"] != "public":
            fail(403, "FINANCE_RESPONSE_NOT_PUBLIC")
        evidence = evidence_for(path, data)
        if path.endswith("/quote") and isinstance(data, dict):
            data = {**data, "update_time": evidence["marketTimestamp"]}
        return {"version": "finance.v1", "status_code": status, "ok": 200 <= status < 300,
                "data": data, "evidence": evidence, "data_visibility": visibility}

    if method not in {"GET", "HEAD", "OPTIONS"}:
        if not body.operationId:
            fail(422, "FINANCE_OPERATION_ID_REQUIRED")
        result = FinanceIdentityStore().operation(payload["delegationId"], body.operationId, args, execute)
    else:
        result = execute()
    # Permission/ownership errors retain their HTTP status; the connector must
    # pause on 401/403 rather than retry through a different identity.
    return JSONResponse(result, status_code=result["status_code"])
