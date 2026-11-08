# -*- coding: utf-8 -*-
"""
User-center session endpoints (AUTH_MODE=user_center).

* GET /api/session  -> who am I (200 always; ``authenticated=false`` when logged out)
* GET /api/login    -> 302 to the user-center login page, returning to PUBLIC_ORIGIN
* GET /api/logout   -> 302 to the user-center logout page, returning to PUBLIC_ORIGIN

In ``local`` mode these routes answer 404 so the SPA can tell the modes apart.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from api.middlewares.auth import auth_error_response
from src.auth import is_user_center_mode
from src.user_center_auth import (
    UserCenterAuthError,
    build_login_url,
    build_logout_url,
    load_settings,
    resolve_session,
    session_payload,
)

router = APIRouter(tags=["UserCenterAuth"])

_NO_STORE = {"Cache-Control": "no-store"}


def _not_user_center_mode() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "user_center_disabled",
            "message": "AUTH_MODE is not user_center; this endpoint is unavailable",
        },
    )


@router.get(
    "/api/session",
    summary="Current user-center session",
    description=(
        "Verifies the user_session cookie with the user center and returns "
        "{authenticated, authorized, username, role, requiredRole, csrfToken, loginUrl, logoutUrl}. "
        "csrfToken is only present for sessions allowed to write; send it as X-CSRF-Token on non-GET requests."
    ),
)
async def get_session(request: Request):
    if not is_user_center_mode():
        return _not_user_center_mode()
    settings = load_settings()
    try:
        session = await run_in_threadpool(resolve_session, request.headers.getlist("cookie"), settings)
    except UserCenterAuthError as exc:
        return auth_error_response(exc)
    return JSONResponse(content=session_payload(session, settings), headers=_NO_STORE)


def _redirect(request: Request, builder) -> RedirectResponse | JSONResponse:
    if not is_user_center_mode():
        return _not_user_center_mode()
    settings = load_settings()
    problems = settings.problems()
    if problems:
        return auth_error_response(UserCenterAuthError(503, "AUTH_MISCONFIGURED", "; ".join(problems)))
    return RedirectResponse(builder(settings), status_code=302, headers=_NO_STORE)


@router.get("/api/login", summary="Redirect to the user-center login page")
async def login_redirect(request: Request):
    return _redirect(request, build_login_url)


@router.get("/api/logout", summary="Redirect to the user-center logout page")
async def logout_redirect(request: Request):
    return _redirect(request, build_logout_url)
