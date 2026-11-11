# -*- coding: utf-8 -*-
"""
Auth middleware.

* ``AUTH_MODE=local`` (default): protect /api/v1/* with the local admin password
  session when ADMIN_AUTH_ENABLED is on.
* ``AUTH_MODE=user_center``: protect every /api/* route (plus the OpenAPI docs)
  by verifying the user-center ``user_session`` cookie, admin role, and
  Origin/CSRF on writes. See ``src/user_center_auth.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import COOKIE_NAME, is_auth_enabled, is_user_center_mode, verify_session
from src.user_center_auth import UserCenterAuthError, UserSession, authorize_request

logger = logging.getLogger(__name__)

# ``app_user.last_seen_at`` is refreshed at most once per user per this many seconds.
APP_USER_TOUCH_INTERVAL_SEC = 60.0
_app_user_last_touch: Dict[str, float] = {}
_app_user_touch_lock = threading.Lock()

EXEMPT_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/status",
    "/api/health",
    "/api/v1/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})

# user_center mode: only health checks and the session / login / logout
# endpoints are reachable without a verified admin session.
USER_CENTER_EXEMPT_PATHS = frozenset({
    "/api/health",
    "/api/v1/health",
    "/health",
    "/api/session",
    "/api/login",
    "/api/logout",
})
# Every /api/v1/auth/* handler refuses to act in user_center mode (status only
# reports the mode), so they are safe to leave reachable and return a clear error.
USER_CENTER_EXEMPT_PREFIXES = ("/api/v1/auth/",)
# Non-/api paths that must also be protected (they describe the whole API).
USER_CENTER_PROTECTED_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})


def _path_exempt(path: str) -> bool:
    """Check if path is exempt from auth."""
    normalized = path.rstrip("/") or "/"
    return normalized in EXEMPT_PATHS


def _user_center_requires_auth(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    if normalized in USER_CENTER_EXEMPT_PATHS:
        return False
    if any((normalized + "/").startswith(prefix) for prefix in USER_CENTER_EXEMPT_PREFIXES):
        return False
    if normalized == "/api" or normalized.startswith("/api/"):
        return True
    return normalized in USER_CENTER_PROTECTED_PATHS or normalized.startswith("/docs/")


def _should_touch_app_user(user_id: str) -> bool:
    """In-memory throttle: True at most once per user per ``APP_USER_TOUCH_INTERVAL_SEC``."""
    now = time.monotonic()
    with _app_user_touch_lock:
        last = _app_user_last_touch.get(user_id)
        if last is not None and now - last < APP_USER_TOUCH_INTERVAL_SEC:
            return False
        _app_user_last_touch[user_id] = now
        return True


def _write_app_user(session: UserSession) -> None:
    """Upsert ``app_user`` (runs in a worker thread); a failed write is logged, never raised."""
    try:
        from src.repositories.user_repo import UserRepository

        UserRepository().touch_app_user(session.user_id, session.handle, session.display_name, session.role)
    except Exception as exc:  # noqa: BLE001 - recording the user must never fail a request
        logger.warning("Failed to record app_user %s: %s", session.user_id, exc)


async def _record_app_user(session: UserSession) -> None:
    """Throttled ``app_user`` upsert in a worker thread (off the event loop); never raises."""
    try:
        if session.user_id and _should_touch_app_user(session.user_id):
            await run_in_threadpool(_write_app_user, session)
    except Exception as exc:  # noqa: BLE001 - recording the user must never fail a request
        logger.warning("Failed to record app_user %s: %s", session.user_id, exc)


def auth_error_response(exc: UserCenterAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a valid session for protected routes according to AUTH_MODE."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ):
        path = request.url.path

        # These exact endpoints enforce paired-service credentials and delegated
        # user identity themselves. A service Bearer never bypasses other routes.
        from api.v1.endpoints.finance import SERVICE_PATHS
        if path in SERVICE_PATHS:
            return await call_next(request)

        if is_user_center_mode():
            # CORS preflights carry no cookies; CORSMiddleware answers them.
            if request.method == "OPTIONS" or not _user_center_requires_auth(path):
                return await call_next(request)
            try:
                session = await run_in_threadpool(authorize_request, request.method, request.headers)
            except UserCenterAuthError as exc:
                return auth_error_response(exc)
            request.state.user_center_session = session
            await _record_app_user(session)
            from src.services.finance_identity import session_scope
            with session_scope(session):
                return await call_next(request)

        if not is_auth_enabled():
            return await call_next(request)

        if _path_exempt(path):
            return await call_next(request)

        if not path.startswith("/api/v1/"):
            return await call_next(request)

        cookie_val = request.cookies.get(COOKIE_NAME)
        if not cookie_val or not verify_session(cookie_val):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Login required",
                },
            )

        return await call_next(request)


def add_auth_middleware(app):
    """Add auth middleware to protect API routes.

    The middleware is always registered; whether auth is enforced is determined
    at request time (AUTH_MODE / is_auth_enabled()) so the decision stays
    consistent across any runtime configuration reload.
    """
    app.add_middleware(AuthMiddleware)
