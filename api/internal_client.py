# -*- coding: utf-8 -*-
"""In-process access to the ``/api/v1`` routes for the assistant.

The assistant's ``read_api_doc`` / ``call_api`` tools (``src/agent/tools/api_tools.py``) use this module.
Requests are dispatched to the same route handlers the Web UI uses, in this process, without HTTP and
without the auth middleware: the assistant already runs inside the trusted backend, and it is meant to
have the same reach as the logged-in user.

* ``register_running_app`` is called by ``api.app.create_app`` so in-process calls share the serving
  app's ``state`` (runtime scheduler, system config service). Without a running server (CLI, bots) the
  dependencies in ``api/deps.py`` create those services lazily, as they do for the first request.
* A few routes cannot be called this way and are excluded (see ``EXCLUDED_ROUTES``).
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Tuple

API_PREFIX = "/api/v1"

# (METHOD, path template) -> reason. Kept to routes that cannot work in-process.
EXCLUDED_ROUTES: Dict[Tuple[str, str], str] = {
    ("POST", "/api/v1/agent/chat"): "助手不能通过接口调用自己",
    ("POST", "/api/v1/agent/chat/stream"): "助手不能通过接口调用自己",
    ("POST", "/api/v1/agent/chat/stream/{request_id}/cancel"): "助手不能通过接口调用自己",
    ("GET", "/api/v1/analysis/tasks/stream"): "SSE 事件流不会结束，改用 GET /api/v1/analysis/tasks 或任务详情接口",
}

_lock = threading.Lock()
_internal_app = None
_running_app = None


def register_running_app(app: Any) -> None:
    """Remember the serving app so in-process calls share its ``state``."""
    global _running_app
    _running_app = app


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
        if _running_app is not None and _internal_app.state is not _running_app.state:
            _internal_app.state = _running_app.state
        return _internal_app


_route_patterns: Optional[List[Tuple[str, str, "re.Pattern[str]"]]] = None


def get_openapi_spec() -> Dict[str, Any]:
    return get_internal_app().openapi()


def _patterns() -> List[Tuple[str, str, "re.Pattern[str]"]]:
    """(METHOD, template, regex) for every documented route; literal routes before parameterised ones.

    Built from the OpenAPI paths rather than ``app.routes``: FastAPI includes routers lazily, and the
    OpenAPI templates are exactly what ``read_api_doc`` shows the assistant.
    """
    global _route_patterns
    if _route_patterns is None:
        patterns = []
        for template, operations in get_openapi_spec().get("paths", {}).items():
            regex = re.sub(r"\{[^}/]+:path\}", ".+", template)
            regex = re.sub(r"\{[^}/]+\}", "[^/]+", regex)
            for method in operations:
                patterns.append((method.upper(), template, re.compile(f"^{regex}/?$")))
        patterns.sort(key=lambda item: item[1].count("{"))
        _route_patterns = patterns
    return _route_patterns


def match_route(method: str, path: str) -> Optional[str]:
    """Path template of the route that serves ``METHOD path`` (``None`` when no route matches)."""
    verb = method.upper()
    for candidate_method, template, regex in _patterns():
        if candidate_method == verb and regex.match(path):
            return template
    return None


def match_any_method(path: str) -> List[Tuple[str, str]]:
    """(METHOD, template) of every route whose path matches ``path``."""
    return [(method, template) for method, template, regex in _patterns() if regex.match(path)]


def excluded_reason(method: str, path: str) -> Optional[str]:
    template = match_route(method, path) or path
    return EXCLUDED_ROUTES.get((method.upper(), template))


def call(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Any = None,
) -> Tuple[int, str, str]:
    """Dispatch one request in-process; returns ``(status_code, content_type, text)``."""
    from starlette.testclient import TestClient

    # No ``with`` block: the app's lifespan (schedulers) is never started for these calls.
    client = TestClient(get_internal_app(), base_url="http://assistant.internal", raise_server_exceptions=False)
    kwargs: Dict[str, Any] = {"params": params or None}
    if body is not None:
        kwargs["json"] = body
    response = client.request(method.upper(), path, **kwargs)
    return response.status_code, response.headers.get("content-type", ""), response.text
