# -*- coding: utf-8 -*-
"""Scan-to-create Feishu bot (OAuth device authorization, the flow botmux uses).

1. ``start``: ask ``accounts.feishu.cn`` for a device code + verification URL; the
   web page renders the URL as a QR code.
2. The user scans it in Feishu; Feishu creates a PersonalAgent bot app for them.
3. ``poll``: once authorized the registration endpoint returns the new app's
   ``client_id`` / ``client_secret`` (and the scanner's ``open_id``). They are
   written into the system config here, server side, so the secret never
   reaches the browser. The scanner's DM becomes the push target, so no
   ``/bind`` is needed for the common case.

The device code stays on the server; the browser only holds an opaque session id.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

ACCOUNTS_HOSTS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
REGISTRATION_PATH = "/oauth/v1/app/registration"
REQUEST_TIMEOUT_SECONDS = 10
MIN_POLL_INTERVAL = 1
MAX_POLL_INTERVAL = 30
SLOW_DOWN_STEP = 5
MAX_SESSIONS = 8

STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_ERROR = "error"

_FINAL_STATUSES = {STATUS_SUCCESS, STATUS_DENIED, STATUS_EXPIRED, STATUS_ERROR}


class RegistrationError(Exception):
    """The registration endpoint could not be reached or rejected the request."""


class RegistrationSessionNotFound(Exception):
    """Unknown or already purged session id."""


def _post(domain: str, form: Dict[str, str]) -> Dict[str, Any]:
    url = ACCOUNTS_HOSTS[domain] + REGISTRATION_PATH
    try:
        response = requests.post(url, data=form, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RegistrationError(f"无法连接飞书账号服务：{exc.__class__.__name__}") from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise RegistrationError(f"飞书账号服务返回了无法识别的内容（HTTP {response.status_code}）")
    return body


class _Session:
    def __init__(self, device_code: str, domain: str, expires_at: float, interval: int, verification_url: str):
        self.device_code = device_code
        self.domain = domain
        self.expires_at = expires_at
        self.interval = interval
        self.verification_url = verification_url
        self.next_poll_at = 0.0
        self.result: Optional[Dict[str, Any]] = None  # final, cached outcome
        self.lock = threading.Lock()


class FeishuRegistrationService:
    """Process-wide registry of in-flight scan sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, _Session] = {}
        self._lock = threading.Lock()

    def start(self, domain: str = "feishu") -> Dict[str, Any]:
        if domain not in ACCOUNTS_HOSTS:
            domain = "feishu"
        # `init` confirms the endpoint supports client_secret apps before we create a device code.
        init = _post(domain, {"action": "init"})
        if "client_secret" not in (init.get("supported_auth_methods") or []):
            raise RegistrationError("飞书账号服务不支持扫码创建应用，请改用手动填写 App ID / Secret。")
        begin = _post(
            domain,
            {
                "action": "begin",
                "archetype": "PersonalAgent",
                "auth_method": "client_secret",
                "request_user_info": "open_id",
            },
        )
        device_code = begin.get("device_code")
        url = begin.get("verification_uri_complete") or begin.get("verification_uri")
        if not device_code or not url:
            raise RegistrationError(str(begin.get("error_description") or begin.get("error") or "创建扫码会话失败"))
        interval = min(max(int(begin.get("interval") or 5), MIN_POLL_INTERVAL), MAX_POLL_INTERVAL)
        expires_in = int(begin.get("expires_in") or 600)
        session_id = secrets.token_urlsafe(16)
        with self._lock:
            self._purge_locked()
            self._sessions[session_id] = _Session(
                device_code=device_code,
                domain=domain,
                expires_at=time.time() + expires_in,
                interval=interval,
                verification_url=url,
            )
        return {
            "session_id": session_id,
            "verification_url": url,
            "expires_in": expires_in,
            "interval": interval,
        }

    def poll(self, session_id: str) -> Dict[str, Any]:
        """Return ``{status, ...}``; on ``success`` also ``app_id``, ``app_secret``, ``open_id``, ``domain``.

        The caller consumes the credentials exactly once (``finish`` drops the session).
        """
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise RegistrationSessionNotFound(session_id)
        with session.lock:
            if session.result is not None:
                return dict(session.result)
            now = time.time()
            if now >= session.expires_at:
                return self._finalize(session, {"status": STATUS_EXPIRED})
            if now < session.next_poll_at:
                return {"status": STATUS_PENDING, "interval": session.interval}
            try:
                body = _post(session.domain, {"action": "poll", "device_code": session.device_code})
            except RegistrationError as exc:
                # transient network failure: keep the session, let the page retry
                logger.warning("[Feishu] registration poll failed: %s", exc)
                session.next_poll_at = now + session.interval
                return {"status": STATUS_PENDING, "interval": session.interval, "warning": str(exc)}
            session.next_poll_at = now + session.interval

            error = body.get("error")
            if body.get("client_id") and body.get("client_secret"):
                user_info = body.get("user_info") or {}
                brand = str(user_info.get("tenant_brand") or "").lower()
                return self._finalize(
                    session,
                    {
                        "status": STATUS_SUCCESS,
                        "app_id": str(body["client_id"]),
                        "app_secret": str(body["client_secret"]),
                        "open_id": str(user_info.get("open_id") or ""),
                        "domain": "lark" if brand == "lark" else "feishu",
                    },
                )
            if error == "slow_down":
                session.interval = min(session.interval + SLOW_DOWN_STEP, MAX_POLL_INTERVAL)
                session.next_poll_at = now + session.interval
                return {"status": STATUS_PENDING, "interval": session.interval}
            if error in (None, "authorization_pending"):
                return {"status": STATUS_PENDING, "interval": session.interval}
            if error == "access_denied":
                return self._finalize(session, {"status": STATUS_DENIED})
            if error == "expired_token":
                return self._finalize(session, {"status": STATUS_EXPIRED})
            message = str(body.get("error_description") or error)
            return self._finalize(session, {"status": STATUS_ERROR, "message": message})

    def finish(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    @staticmethod
    def _finalize(session: _Session, result: Dict[str, Any]) -> Dict[str, Any]:
        session.result = result
        return dict(result)

    def _purge_locked(self) -> None:
        now = time.time()
        for sid in [s for s, v in self._sessions.items() if now > v.expires_at + 60]:
            self._sessions.pop(sid, None)
        while len(self._sessions) >= MAX_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)))


_service: Optional[FeishuRegistrationService] = None
_service_lock = threading.Lock()


def get_registration_service() -> FeishuRegistrationService:
    global _service
    with _service_lock:
        if _service is None:
            _service = FeishuRegistrationService()
        return _service


def apply_registration(result: Dict[str, Any], config_service: Any) -> Dict[str, Any]:
    """Persist a successful scan into the config and (re)start the long connection.

    Returns ``{"restart_required": bool}``. A new app invalidates the old ``/bind``
    chat (chat ids belong to one app), so any existing binding is cleared; pushes go
    to the scanner's DM (``FEISHU_CHAT_ID`` = open_id) until a chat is bound again.
    """
    from src.config import get_config
    from src.services.feishu_binding_service import unbind_chat

    previous_app_id = str(getattr(get_config(), "feishu_app_id", "") or "").strip()
    items = [
        {"key": "FEISHU_APP_ID", "value": result["app_id"]},
        {"key": "FEISHU_APP_SECRET", "value": result["app_secret"]},
        {"key": "FEISHU_DOMAIN", "value": result["domain"]},
        {"key": "FEISHU_STREAM_ENABLED", "value": "true"},
    ]
    if result.get("open_id"):
        items.append({"key": "FEISHU_CHAT_ID", "value": result["open_id"]})
        items.append({"key": "FEISHU_RECEIVE_ID_TYPE", "value": "open_id"})
    config_service.update(
        config_version=config_service.get_config(include_schema=False)["config_version"],
        items=items,
        reload_now=True,
    )
    if previous_app_id and previous_app_id != result["app_id"]:
        unbind_chat()
    return {"restart_required": not _start_stream()}


def _start_stream() -> bool:
    """Start the long connection with the freshly saved credentials; False if it needs a restart."""
    try:
        from bot.platforms import FEISHU_SDK_AVAILABLE
        from bot.platforms.feishu_stream import get_feishu_stream_client, reset_idle_feishu_stream_client, start_feishu_stream_background

        if not FEISHU_SDK_AVAILABLE:
            return False
        client = get_feishu_stream_client()
        if client is not None and client.is_running:
            return False  # connected with the old credentials; a service restart swaps them
        reset_idle_feishu_stream_client()
        return bool(start_feishu_stream_background())
    except Exception as exc:
        logger.warning("[Feishu] starting long connection after scan failed: %s", exc)
        return False
