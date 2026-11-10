# -*- coding: utf-8 -*-
"""In-process access to the ``/api/v1`` routes for the assistant.

The assistant's ``read_api_doc`` / ``call_api`` tools (``src/agent/tools/api_tools.py``) use this module.
Requests are dispatched to the same route handlers the Web UI uses, in this process, without HTTP and
without the auth middleware: the assistant already runs inside the trusted backend, but it has the reach
of exactly one user, the owner of the chat session (docs/design/multi-user.md section 23).

* ``register_running_app`` is called by ``api.app.create_app`` so in-process calls share the serving
  app's ``state`` (runtime scheduler, system config service). Without a running server (CLI, bots) the
  dependencies in ``api/deps.py`` create those services lazily, as they do for the first request.
* ``acting_user`` builds the ``CurrentUser`` that ``api.deps.get_current_user`` would resolve for the
  session owner; the internal app answers ``get_current_user`` with it (per request, see ``_ActingAs``), so
  every route dependency, ``require_admin`` included, runs for real.
* Some routes are never callable (``EXCLUDED_ROUTES`` cannot work in-process, ``BLOCKED_PREFIXES`` are
  credentials / process settings), and routes behind ``require_admin`` are closed to non-administrators.
  The policy is checked against the route Starlette would execute for the path, and ``safe_route_path``
  rejects paths that could mean something else to the router than to the policy check.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from fastapi import Request
from starlette.routing import Match

API_PREFIX = "/api/v1"

# (METHOD, path template) -> reason. Kept to routes that cannot work in-process.
EXCLUDED_ROUTES: Dict[Tuple[str, str], str] = {
    ("POST", "/api/v1/agent/chat"): "助手不能通过接口调用自己",
    ("POST", "/api/v1/agent/chat/stream"): "助手不能通过接口调用自己",
    ("POST", "/api/v1/agent/chat/stream/{request_id}/cancel"): "助手不能通过接口调用自己",
    ("GET", "/api/v1/analysis/tasks/stream"): "SSE 事件流不会结束，改用 GET /api/v1/analysis/tasks 或任务详情接口",
}

# Path template prefix -> reason. Never callable by the assistant, for any user (administrators included):
# process settings, global usage, authentication, personal secrets and push channels (a prompt-injected
# assistant must not read or rewrite credentials).
BLOCKED_PREFIXES: Dict[str, str] = {
    "/api/v1/system": "系统设置不开放给助手，请在设置页修改",
    "/api/v1/usage": "全站用量不开放给助手",
    "/api/v1/auth": "登录与认证接口不开放给助手",
    "/api/v1/me/settings": "个人设置（含密钥）不开放给助手，请在设置页修改",
    "/api/v1/me/notification-channels": "推送渠道（含 webhook 与 Token）不开放给助手，请在设置页修改",
}
ADMIN_ONLY_REASON = "仅管理员可用"

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

_lock = threading.Lock()
_internal_app = None
_running_app = None
_route_index: Optional[Tuple[Any, Dict[Tuple[str, str], Any]]] = None


class CallRefused(Exception):
    """The assistant may not run this call. ``code``: ``bad_path`` / ``no_route`` / ``blocked``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ActingUserRequired(PermissionError):
    """An in-process call was attempted without the acting user (fail closed, never the primary user)."""


def register_running_app(app: Any) -> None:
    """Remember the serving app so in-process calls share its ``state``."""
    global _running_app
    _running_app = app


# ------------------------------------------------------------------
# identity
# ------------------------------------------------------------------

def _assistant_current_user(request: Request) -> Any:
    """``get_current_user`` for the internal app: the user ``call`` is acting as."""
    from api.v1.errors import api_error

    user = getattr(request.state, "assistant_user", None)
    if user is None:
        raise api_error(401, "AUTH_REQUIRED", "Login required")
    return user


class _ActingAs:
    """ASGI wrapper that attaches the acting user to every request's ``state``; nothing the model sends can set it."""

    def __init__(self, app: Any, user: Any):
        self._app = app
        self._user = user

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope.setdefault("state", {})["assistant_user"] = self._user
        await self._app(scope, receive, send)


def _install_identity(app: Any) -> None:
    from api.deps import get_current_user

    app.dependency_overrides.setdefault(get_current_user, _assistant_current_user)


def acting_user(user_id: Optional[str]) -> Any:
    """The ``CurrentUser`` ``api.deps.get_current_user`` would resolve for ``user_id``.

    * ``AUTH_MODE=local``: the primary user is the administrator (what ``get_current_user`` returns there);
    * ``AUTH_MODE=user_center``: the role stored in ``app_user`` (refreshed at every login). The primary user
      without a row (CLI / bot owner who never logged in on the Web) is the administrator; anyone else without a
      row, or with an unknown role, is a regular user. Never an administrator by default.
    """
    from api.deps import CurrentUser
    from src.auth import is_user_center_mode
    from src.config import get_primary_user_id

    uid = str(user_id or "").strip()
    if not uid:
        raise ActingUserRequired("call_api / read_api_doc need the acting user; none was provided")
    is_primary = uid == get_primary_user_id()
    if is_primary and not is_user_center_mode():
        return CurrentUser(user_id=uid, handle=uid, display_name=uid, role="admin", is_admin=True)
    row = None
    try:
        from src.repositories.user_repo import UserRepository

        row = UserRepository().get_app_user(uid)
    except Exception:  # noqa: BLE001 - an unreadable user table means "regular user", not "administrator"
        row = None
    handle = (row or {}).get("handle") or uid
    display_name = (row or {}).get("display_name") or handle
    if is_primary and (row is None or not is_user_center_mode()):
        return CurrentUser(user_id=uid, handle=handle, display_name=display_name, role="admin", is_admin=True)
    role = str((row or {}).get("role") or "user")
    return CurrentUser(user_id=uid, handle=handle, display_name=display_name, role=role, is_admin=role == "admin")


# ------------------------------------------------------------------
# app and routes
# ------------------------------------------------------------------

def get_internal_app() -> Any:
    """FastAPI app with only the ``/api/v1`` routes and the shared error handlers (built once)."""
    global _internal_app
    with _lock:
        if _internal_app is None:
            from fastapi import FastAPI

            from api.middlewares.error_handler import add_error_handlers
            from api.v1.router import router as api_v1_router

            app = FastAPI(title="Daily Stock Analysis API (assistant)", version="1.0.0")
            app.include_router(api_v1_router, prefix=API_PREFIX)
            add_error_handlers(app)
            _internal_app = app
        # No auth middleware here: identity is the owner of the assistant session, never the primary user.
        _install_identity(_internal_app)
        if _running_app is not None and _internal_app.state is not _running_app.state:
            _internal_app.state = _running_app.state
        return _internal_app


def get_openapi_spec() -> Dict[str, Any]:
    return get_internal_app().openapi()


def _routes_by_template() -> Dict[Tuple[str, str], Any]:
    """(METHOD, path template) -> route, in registration order (the first one is the one that executes)."""
    global _route_index
    app = get_internal_app()
    if _route_index is None or _route_index[0] is not app:
        index: Dict[Tuple[str, str], Any] = {}
        for route in app.routes:
            template = getattr(route, "path_format", None)
            for method in getattr(route, "methods", None) or ():
                index.setdefault((method.upper(), template), route)
        _route_index = (app, index)
    return _route_index[1]


def _route_for(method: str, path: str) -> Optional[Any]:
    """The route Starlette executes for ``METHOD path``: the first full match in registration order."""
    scope = {
        "type": "http", "method": method.upper(), "path": unquote(path), "root_path": "",
        "headers": [], "query_string": b"", "path_params": {},
    }
    for route in get_internal_app().routes:
        match, _child = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def match_route(method: str, path: str) -> Optional[str]:
    """Path template of the route that serves ``METHOD path`` (``None`` when no route matches)."""
    text = path.rstrip("/") if len(path) > 1 else path
    route = _route_for(method, text)
    return getattr(route, "path_format", None) if route is not None else None


def match_any_method(path: str) -> List[Tuple[str, str]]:
    """(METHOD, template) of every route whose path matches ``path``."""
    found = []
    for method in _METHODS:
        template = match_route(method, path)
        if template is not None:
            found.append((method, template))
    return found


# ------------------------------------------------------------------
# policy
# ------------------------------------------------------------------

def route_requires_admin(method: str, template: str) -> bool:
    """Whether the route (or its router) depends on ``require_admin``: derived from the dependencies, not a path list."""
    from api.deps import require_admin

    route = _routes_by_template().get((method.upper(), template))
    stack = [getattr(route, "dependant", None)]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        for sub in node.dependencies:
            if sub.call is require_admin:
                return True
            stack.append(sub)
    return False


def blocked_prefix_reason(template: str) -> Optional[str]:
    for prefix, reason in BLOCKED_PREFIXES.items():
        if template == prefix or template.startswith(prefix + "/"):
            return reason
    return None


def hidden_from(method: str, template: str, user: Any) -> bool:
    """Routes ``read_api_doc`` does not show to ``user``: admin-only routes for non-administrators."""
    return route_requires_admin(method, template) and not getattr(user, "is_admin", False)


def excluded_reason(method: str, path: str, user: Any = None) -> Optional[str]:
    """Why ``user`` cannot call ``METHOD path`` (a concrete path); ``None`` when they can."""
    return template_reason(method, match_route(method, path) or path, user)


def template_reason(method: str, template: str, user: Any = None) -> Optional[str]:
    """Why ``user`` cannot call the route ``METHOD template``; ``None`` when they can."""
    reason = blocked_prefix_reason(template)
    if reason:
        return reason
    if user is not None and hidden_from(method, template, user):
        return ADMIN_ONLY_REASON
    return EXCLUDED_ROUTES.get((method.upper(), template))


_UNSAFE_CHARS = re.compile(r"[\x00-\x20\x7f\\#?]")
# A percent escape is allowed only for bytes >= 0x80 (UTF-8 text); %2e %2f %5c ... would change the route.
_ASCII_ESCAPE = re.compile(r"%(?![89a-fA-F][0-9a-fA-F])")


def safe_route_path(path: str) -> str:
    """Validate the path the model supplied and return it without a trailing slash; ``CallRefused`` otherwise.

    The policy is checked against the template this path matches, so the path must mean the same thing to the
    policy check and to the router: no ``.`` / ``..`` segments (httpx would collapse them), no encoded ASCII
    (``%2f`` ``%2e`` ``%5c``), no empty segments, no fragment / backslash / whitespace / control characters.
    """
    text = str(path or "")
    if _UNSAFE_CHARS.search(text) or _ASCII_ESCAPE.search(text):
        raise CallRefused("bad_path", "路径里不能有编码的 / . \\、空白、# 或 ?（查询参数请放在 query 里）")
    for form in (text, unquote(text)):
        if "//" in form or any(segment in (".", "..") for segment in form.split("/")):
            raise CallRefused("bad_path", "路径里不能有 . .. 段或连续的 /")
    return text.rstrip("/") if len(text) > 1 else text


def authorize(method: str, path: str, user: Any) -> Tuple[str, str]:
    """Check one call for ``user``; returns ``(clean_path, route_template)`` or raises ``CallRefused``."""
    clean = safe_route_path(path)
    template = match_route(method, clean)
    if template is None:
        raise CallRefused("no_route", f"没有接口 {method.upper()} {clean}")
    reason = excluded_reason(method, clean, user)
    if reason:
        raise CallRefused("blocked", f"{method.upper()} {template} 不能由助手调用：{reason}")
    return clean, template


def call(
    method: str,
    path: str,
    *,
    user_id: Optional[str],
    params: Optional[Dict[str, Any]] = None,
    body: Any = None,
) -> Tuple[int, str, str]:
    """Dispatch one request in-process as ``user_id``; returns ``(status_code, content_type, text)``.

    The route policy is enforced here too, so no caller can reach a route the assistant may not use.
    """
    from starlette.testclient import TestClient

    user = acting_user(user_id)
    clean, _template = authorize(method, path, user)
    # No ``with`` block: the app's lifespan (schedulers) is never started for these calls.
    client = TestClient(
        _ActingAs(get_internal_app(), user), base_url="http://assistant.internal", raise_server_exceptions=False,
    )
    kwargs: Dict[str, Any] = {"params": params or None}
    if body is not None:
        kwargs["json"] = body
    response = client.request(method.upper(), clean, **kwargs)
    return response.status_code, response.headers.get("content-type", ""), response.text
