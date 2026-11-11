"""Stock browser facade for the same account's NexBot finance topics."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import Future
from threading import Lock
from urllib.parse import urlsplit

import requests
from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from api.v1.endpoints.finance import browser_identity, cookie
from src.core.backend_http import BackendRouteError, backend_request
from src.services.finance_identity import FinanceIdentityStore, configuration, fail, setting

router = APIRouter()
_sessions = {}
_exchanges = {}
_session_lock = Lock()


def core_url():
    url = setting("FINANCE_CORE_URL").rstrip("/")
    parts = urlsplit(url)
    loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
            or parts.path or not (parts.scheme == "https" or loopback and parts.scheme == "http")):
        fail(503, "FINANCE_CORE_URL_INVALID")
    return url


def core_call(method, path, token, *, data=None, params=None):
    try:
        response = backend_request(
            method, core_url() + "/api/" + path, connect_ip=setting("FINANCE_CORE_CONNECT_IP"),
            pool_size=8, headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            json=data, params=params, timeout=(5, 35),
        )
    except BackendRouteError:
        fail(503, "FINANCE_CORE_ROUTE_INVALID")
    except requests.RequestException:
        fail(503, "FINANCE_CORE_UNAVAILABLE")
    if response.is_redirect:
        fail(502, "FINANCE_CORE_INVALID_RESPONSE")
    try:
        result = response.json()
    except ValueError:
        fail(502, "FINANCE_CORE_INVALID_RESPONSE")
    if response.status_code == 401 and token.startswith("stock_"):
        with _session_lock:
            for key in [key for key, entry in _sessions.items() if entry[2] == token]:
                del _sessions[key]
        # The Stock browser session has already been authenticated. A Core lease
        # expiring must not sign the user out of Stock or silently replay a write.
        return 409, {"error": "FINANCE_SESSION_CHANGED", "message": "金融连接已变化，请刷新话题后继续"}
    return response.status_code, result


@router.get("/status")
def status(request: Request):
    if not setting("FINANCE_CORE_URL"):
        return {"managed": False, "bound": False}
    owner = browser_identity(request)
    return {"managed": True, "bound": FinanceIdentityStore().bound(owner)}


@router.post("/connect")
def connect(request: Request):
    owner = browser_identity(request, True)
    FinanceIdentityStore().ensure_bound(owner, cookie(request))
    return {"managed": True, "bound": True}


def core_session(request: Request, *, write: bool):
    owner = browser_identity(request, write)
    config = configuration()
    cookie_value = cookie(request)
    key = (core_url(), owner["issuer"], owner["subject"], owner["instanceId"], owner["coreId"],
           hashlib.sha256((config["SERVICE_TOKEN"] + "\x00" + cookie_value).encode()).digest())
    with _session_lock:
        cutoff = time.time() * 1000 + 5000
        for expired in [k for k, entry in _sessions.items() if entry[0] <= cutoff]:
            del _sessions[expired]
        if key in _sessions:
            return _sessions[key][1:]
        pending = _exchanges.get(key)
        if pending is None:
            pending = _exchanges[key] = Future()
            leader = True
        else:
            leader = False
    if not leader:
        return pending.result()
    try:
        code = FinanceIdentityStore().exchange(owner, cookie_value)["code"]
        status_code, result = core_call("POST", "finance/exchange", config["SERVICE_TOKEN"],
                                        data={"code": code, "instanceId": config["INSTANCE_ID"]})
        if (status_code != 200 or not isinstance(result, dict) or not result.get("token")
                or not result.get("botId") or not isinstance(result.get("expiresAt"), int)):
            fail(status_code if 400 <= status_code < 600 else 502, "FINANCE_CORE_EXCHANGE_FAILED")
        resolved = result["botId"], result["token"]
        with _session_lock:
            _sessions[key] = (result["expiresAt"], *resolved)
        pending.set_result(resolved)
        return resolved
    except Exception as exc:
        pending.set_exception(exc)
        raise
    finally:
        with _session_lock:
            _exchanges.pop(key, None)


@router.get("/feishu-settings")
def feishu_settings():
    # Same authenticated browser middleware as the other read-only BFF routes.
    # This route also works before finance pairing; no service secret is returned.
    from src.services.finance_feishu_settings import settings_state
    return settings_state()


@router.get("/{resource}")
def fetch_resource(resource: str, request: Request, conversation_id: str | None = Query(None, alias="conversationId"),
                   before: int | None = None, after: int | None = None):
    routes = {"topics": "agents/topics", "messages": "agents/messages", "tasks": "agents/tasks", "models": "finance/models"}
    if resource not in routes:
        fail(404, "FINANCE_RESOURCE_NOT_FOUND")
    if resource in {"messages", "tasks"} and not conversation_id:
        fail(400, "FINANCE_TOPIC_REQUIRED")
    bot_id, token = core_session(request, write=False)
    params = {k: v for k, v in {"bot": bot_id, "conversationId": conversation_id, "before": before, "after": after}.items()
              if v is not None}
    status_code, result = core_call("GET", routes[resource], token, params=params)
    return JSONResponse(content=result, status_code=status_code)


@router.post("/{action}")
def perform_action(action: str, request: Request, data: dict = Body(...)):
    routes = {"input": "agents/input", "read": "agents/read", "cancel": "agents/cancel", "edit": "agents/edit",
              "retry": "agents/retry", "delete": "agents/delete-topic", "model": "bots/model"}
    if action not in routes:
        fail(404, "FINANCE_ACTION_NOT_FOUND")
    if action == "input" and not data.get("conversationId") and data.get("newConversation") is not True:
        fail(400, "FINANCE_TOPIC_REQUIRED")
    if action in {"read", "cancel", "edit", "retry", "delete"} and not data.get("conversationId"):
        fail(400, "FINANCE_TOPIC_REQUIRED")
    bot_id, token = core_session(request, write=True)
    payload = {**data, "botId": bot_id}
    if action == "model":
        payload["id"] = bot_id
    if action == "input":
        payload.pop("audience", None)
        payload.pop("sourceContext", None)
        if data.get("newConversation"):
            payload["audience"] = "private"
            context = data.get("sourceContext") if isinstance(data.get("sourceContext"), dict) else {}
            payload["sourceContext"] = {**{key: context[key] for key in ("kind", "id", "code", "name", "tradeDate", "asOf")
                                          if isinstance(context.get(key), str)}, "source": "nex_stock"}
    status_code, result = core_call("POST", routes[action], token, data=payload)
    return JSONResponse(content=result, status_code=status_code)
