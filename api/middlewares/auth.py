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
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import COOKIE_NAME, is_auth_enabled, is_user_center_mode, verify_session
from src.user_center_auth import UserCenterAuthError, authorize_request

logger = logging.getLogger(__name__)

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

        if is_user_center_mode():
            # CORS preflights carry no cookies; CORSMiddleware answers them.
            if request.method == "OPTIONS" or not _user_center_requires_auth(path):
                return await call_next(request)
            try:
                session = await run_in_threadpool(authorize_request, request.method, request.headers)
            except UserCenterAuthError as exc:
                return auth_error_response(exc)
            request.state.user_center_session = session
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
