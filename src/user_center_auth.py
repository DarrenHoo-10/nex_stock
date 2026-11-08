# -*- coding: utf-8 -*-
"""
NexorAI user-center authentication (``AUTH_MODE=user_center``).

The user center (nex_user) owns login and holds an opaque server-side session.
The browser carries its ``user_session`` cookie (scoped to the parent domain) to
this service; every protected request is verified by forwarding that cookie to
``GET {USER_CENTER_URL}/v1/session``:

* 200 ``{"user": {"userId", "handle", "displayName", "role", ...}, "csrfToken": "..."}``
* 401 -> not logged in

Behaviour (mirrors the nex_asset reference implementation):

* exactly one ``user_session=<64 hex>`` cookie is accepted, anything else is "no session";
* 3 s timeout, at most 2 concurrent user-center calls, bounded retry on 429;
* a completed verification result is never cached, so logout and role changes
  apply on the very next request;
* 401 AUTH_REQUIRED / 403 ADMIN_REQUIRED / 503 USER_CENTER_UNAVAILABLE;
* non-GET requests must carry ``Origin == PUBLIC_ORIGIN`` and ``X-CSRF-Token``
  equal to the session's ``csrfToken``.
"""

from __future__ import annotations

import hmac
import logging
import re
import threading
import time
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote, urlsplit

import requests

from src.auth import read_auth_setting

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "user_session"
CSRF_HEADER_NAME = "X-CSRF-Token"
DEFAULT_USER_CENTER_URL = "https://user.nexorai.com.cn"
DEFAULT_REQUIRED_ROLE = "admin"

REQUEST_TIMEOUT_SEC = 3.0
MAX_CONCURRENT_REQUESTS = 2
SEMAPHORE_WAIT_SEC = 5.0
MAX_RATE_LIMIT_RETRIES = 2
RATE_LIMIT_BACKOFF_SEC = 0.2
RATE_LIMIT_BACKOFF_CAP_SEC = 2.0

_SESSION_COOKIE_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Higher rank satisfies a lower required role (admin can do everything a user can).
_ROLE_RANK = {"user": 1, "admin": 2}

_http_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
_http_session: Optional[requests.Session] = None
_http_session_lock = threading.Lock()


class UserCenterAuthError(Exception):
    """An authentication / authorization failure with an HTTP status and stable code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UserCenterSettings:
    """Resolved user-center settings (re-read per call so config reloads apply)."""

    base_url: str
    public_origin: str
    required_role: str

    def problems(self) -> List[str]:
        issues: List[str] = []
        if not self.public_origin:
            issues.append("PUBLIC_ORIGIN is required when AUTH_MODE=user_center")
        if self.required_role not in _ROLE_RANK:
            issues.append(
                f"USER_CENTER_REQUIRED_ROLE must be one of {sorted(_ROLE_RANK)}, got {self.required_role!r}"
            )
        if not _is_http_base(self.base_url):
            issues.append("USER_CENTER_URL must be an absolute http(s) URL")
        return issues


@dataclass(frozen=True)
class UserSession:
    """A verified user-center session."""

    user_id: str
    handle: str
    display_name: str
    role: str
    csrf_token: str

    @property
    def username(self) -> str:
        return self.display_name or self.handle or self.user_id


def _is_http_base(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def normalize_origin(value: str) -> str:
    """Normalise ``scheme://host[:port]``; return '' when the value is not a bare origin."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    if parts.path or parts.query or parts.fragment:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def load_settings() -> UserCenterSettings:
    """Read user-center settings from the active config (file first, then env)."""
    base_url = (read_auth_setting("USER_CENTER_URL") or DEFAULT_USER_CENTER_URL).rstrip("/")
    public_origin = normalize_origin(read_auth_setting("PUBLIC_ORIGIN"))
    required_role = (read_auth_setting("USER_CENTER_REQUIRED_ROLE") or DEFAULT_REQUIRED_ROLE).lower()
    return UserCenterSettings(
        base_url=base_url,
        public_origin=public_origin,
        required_role=required_role,
    )


def build_login_url(settings: UserCenterSettings) -> str:
    return f"{settings.base_url}/login?return={quote(settings.public_origin + '/', safe='')}"


def build_logout_url(settings: UserCenterSettings) -> str:
    return f"{settings.base_url}/logout?return={quote(settings.public_origin + '/', safe='')}"


def extract_session_cookie(cookie_headers: Sequence[str]) -> Optional[str]:
    """Return the session id only when exactly one well-formed ``user_session`` cookie exists."""
    found: List[str] = []
    for header in cookie_headers:
        for part in (header or "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == SESSION_COOKIE_NAME:
                found.append(value.strip())
    if len(found) != 1:
        return None
    return found[0] if _SESSION_COOKIE_RE.match(found[0]) else None


def _get_http_session() -> requests.Session:
    """Shared keep-alive session; never stores cookies (they are passed explicitly per call)."""
    global _http_session
    with _http_session_lock:
        if _http_session is None:
            session = requests.Session()
            session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=1,
                pool_maxsize=MAX_CONCURRENT_REQUESTS,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            _http_session = session
        return _http_session


def _http_get(url: str, headers: Mapping[str, str], timeout: float) -> requests.Response:
    """Single HTTP call to the user center (patched in tests; never follows redirects)."""
    return _get_http_session().get(url, headers=dict(headers), timeout=timeout, allow_redirects=False)


def _unavailable(message: str) -> UserCenterAuthError:
    return UserCenterAuthError(503, "USER_CENTER_UNAVAILABLE", message)


def _parse_session_payload(payload: Any) -> UserSession:
    if not isinstance(payload, dict):
        raise _unavailable("User center returned an unexpected session payload")
    user = payload.get("user")
    csrf = payload.get("csrfToken")
    if not isinstance(user, dict) or not isinstance(csrf, str) or not csrf:
        raise _unavailable("User center returned an unexpected session payload")
    role = user.get("role")
    if not isinstance(role, str):
        raise _unavailable("User center returned an unexpected session payload")
    return UserSession(
        user_id=str(user.get("userId") or ""),
        handle=str(user.get("handle") or ""),
        display_name=str(user.get("displayName") or ""),
        role=role.lower(),
        csrf_token=csrf,
    )


def fetch_session(session_id: str, settings: UserCenterSettings) -> Optional[UserSession]:
    """Verify one session id with the user center.

    Returns ``None`` when the user center says the session is not logged in (401).
    Raises ``UserCenterAuthError(503)`` when it cannot be verified.
    Blocking: call from a worker thread, not directly from the event loop.
    """
    url = f"{settings.base_url}/v1/session"
    headers = {"Cookie": f"{SESSION_COOKIE_NAME}={session_id}", "Accept": "application/json"}

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        if not _http_semaphore.acquire(timeout=SEMAPHORE_WAIT_SEC):
            raise _unavailable("User center verification is saturated; retry shortly")
        try:
            response = _http_get(url, headers, REQUEST_TIMEOUT_SEC)
        except requests.RequestException as exc:
            logger.warning("User center session check failed: %s", type(exc).__name__)
            raise _unavailable("User center is unreachable") from exc
        finally:
            _http_semaphore.release()

        status = response.status_code
        if status == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise _unavailable("User center returned a non-JSON session response") from exc
            return _parse_session_payload(payload)
        if status == 401:
            return None
        if status == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
            delay = RATE_LIMIT_BACKOFF_SEC * (attempt + 1)
            retry_after = (response.headers or {}).get("Retry-After", "")
            if retry_after.strip().isdigit():
                delay = max(delay, float(retry_after.strip()))
            time.sleep(min(delay, RATE_LIMIT_BACKOFF_CAP_SEC))
            continue
        logger.warning("User center session check returned unexpected status %s", status)
        raise _unavailable(f"User center returned unexpected status {status}")
    raise _unavailable("User center is rate limiting session checks")  # pragma: no cover


def role_satisfies(role: str, required_role: str) -> bool:
    return _ROLE_RANK.get(role, 0) >= _ROLE_RANK.get(required_role, 99)


def forbidden_error(settings: UserCenterSettings) -> UserCenterAuthError:
    if settings.required_role == "admin":
        return UserCenterAuthError(403, "ADMIN_REQUIRED", "Administrator role required")
    return UserCenterAuthError(403, "ROLE_REQUIRED", f"Role '{settings.required_role}' required")


def resolve_session(
    cookie_headers: Sequence[str],
    settings: Optional[UserCenterSettings] = None,
) -> Optional[UserSession]:
    """Verify the request cookies. ``None`` means not logged in.

    Raises ``UserCenterAuthError(503)`` when the deployment is misconfigured or the
    user center cannot be reached. Role is *not* enforced here.
    """
    settings = settings or load_settings()
    problems = settings.problems()
    if problems:
        raise UserCenterAuthError(503, "AUTH_MISCONFIGURED", "; ".join(problems))
    session_id = extract_session_cookie(cookie_headers)
    if session_id is None:
        return None
    return fetch_session(session_id, settings)


def check_write_request(
    method: str,
    origin: Optional[str],
    csrf_header: Optional[str],
    session: UserSession,
    settings: UserCenterSettings,
) -> None:
    """Enforce Origin + CSRF token on state-changing requests."""
    if method.upper() in _SAFE_METHODS:
        return
    if not origin or normalize_origin(origin) != settings.public_origin:
        raise UserCenterAuthError(403, "ORIGIN_NOT_ALLOWED", "Request origin is not allowed")
    if not csrf_header or not hmac.compare_digest(csrf_header.encode("utf-8"), session.csrf_token.encode("utf-8")):
        raise UserCenterAuthError(403, "CSRF_INVALID", "Missing or invalid CSRF token")


def authorize_request(
    method: str,
    headers: Any,
    settings: Optional[UserCenterSettings] = None,
) -> UserSession:
    """Full gate for a protected request: session -> role -> origin/CSRF.

    ``headers`` is a Starlette ``Headers`` (needs ``getlist`` and ``get``).
    Returns the verified admin session or raises ``UserCenterAuthError``.
    Blocking (see ``fetch_session``).
    """
    settings = settings or load_settings()
    session = resolve_session(headers.getlist("cookie"), settings)
    if session is None:
        raise UserCenterAuthError(401, "AUTH_REQUIRED", "Login required")
    if not role_satisfies(session.role, settings.required_role):
        raise forbidden_error(settings)
    check_write_request(method, headers.get("origin"), headers.get(CSRF_HEADER_NAME), session, settings)
    return session


def session_payload(session: Optional[UserSession], settings: UserCenterSettings) -> Dict[str, Any]:
    """Shape returned by ``GET /api/session``."""
    authorized = session is not None and role_satisfies(session.role, settings.required_role)
    return {
        "authenticated": session is not None,
        "authorized": authorized,
        "username": session.username if session else None,
        "role": session.role if session else None,
        "requiredRole": settings.required_role,
        # The CSRF token is only handed to a session that may actually write.
        "csrfToken": session.csrf_token if authorized and session else None,
        "loginUrl": build_login_url(settings),
        "logoutUrl": build_logout_url(settings),
    }
