# -*- coding: utf-8 -*-
"""Shared doubles for the multi-user P3a tests (personal push channels, wired pushes, per-user schedules).

``FakeHTTP`` replaces ``requests.adapters.HTTPAdapter.send`` - the lowest layer of ``requests`` - so the real
``Session.send`` (and with it the SSRF / no-redirect hook of ``src/utils/safe_requests.py``), the real senders and the real
services all run; nothing ever leaves the process. Every outbound request is recorded.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from requests.structures import CaseInsensitiveDict

from tests.multi_user_helpers import PRIMARY_ID, USER_A, USER_B  # noqa: F401  (re-exported for the P3a tests)

# Valid channel configurations (public hosts; call ``stub_dns`` first).
FEISHU = {"webhook_url": "https://open.feishu.example.com/hook/aaa111", "secret": "feishu-sign-secret", "keyword": "股票"}
DINGTALK = {"webhook_url": "https://oapi.dingtalk.example.com/robot/send?access_token=tok", "secret": "SECdingtalk"}
WECOM = {"webhook_url": "https://qyapi.wecom.example.com/cgi-bin/webhook/send?key=abc"}
TELEGRAM = {"bot_token": "123456789:AAFakeTokenFakeTokenFakeToken_123", "chat_id": "-1001234567890"}
PUSHPLUS = {"token": "abcdef0123456789"}
SERVERCHAN3 = {"sendkey": "SCT123456TabcdefABCDEF"}
EMAIL = {
    "smtp_host": "smtp.mail.example.com", "smtp_port": 587, "username": "login@mail.example.com",
    "password": "smtp-password", "sender": "me@mail.example.com", "receivers": ["a@x.example.com", "b@x.example.com"],
}
CUSTOM = {
    "webhook_url": "https://hooks.example.org/in/xyz", "bearer_token": "bearer-secret",
    "body_template": '{"text": $content_json}',
}
CONFIGS = {
    "feishu": FEISHU, "dingtalk": DINGTALK, "wecom": WECOM, "telegram": TELEGRAM, "pushplus": PUSHPLUS,
    "serverchan3": SERVERCHAN3, "email": EMAIL, "custom": CUSTOM,
}
# host each web channel's request goes to
CHANNEL_HOSTS = {
    "feishu": "open.feishu.example.com", "dingtalk": "oapi.dingtalk.example.com", "wecom": "qyapi.wecom.example.com",
    "telegram": "api.telegram.org", "pushplus": "www.pushplus.plus", "serverchan3": "sctapi.ftqq.com",
    "custom": "hooks.example.org",
}
SYSTEM_HOSTS = {
    "wecom": "sys-wecom.example.net", "feishu": "sys-feishu.example.net", "dingtalk": "sys-dingtalk.example.net",
    "custom": "sys-custom.example.net", "discord": "sys-discord.example.net", "slack": "sys-slack.example.net",
    "pushplus": "www.pushplus.plus",  # a real shared endpoint: system calls are told apart by their token
}
SYSTEM_CHANNEL_ENV = {
    "WECHAT_WEBHOOK_URL": f"https://{SYSTEM_HOSTS['wecom']}/hook",
    "FEISHU_WEBHOOK_URL": f"https://{SYSTEM_HOSTS['feishu']}/hook",
    "DINGTALK_WEBHOOK_URL": f"https://{SYSTEM_HOSTS['dingtalk']}/hook",
    "CUSTOM_WEBHOOK_URLS": f"https://{SYSTEM_HOSTS['custom']}/hook",
    "DISCORD_WEBHOOK_URL": f"https://{SYSTEM_HOSTS['discord']}/hook",
    "SLACK_WEBHOOK_URL": f"https://{SYSTEM_HOSTS['slack']}/hook",
    "TELEGRAM_BOT_TOKEN": "999999999:SYSTEMSYSTEMSYSTEMSYSTEMSYSTEM",
    "TELEGRAM_CHAT_ID": "-1009999999",
    "PUSHPLUS_TOKEN": "SYSTEMPUSHPLUSTOKEN",
    "EMAIL_SENDER": "system@sys.example.net",
    "EMAIL_PASSWORD": "system-mail-password",
    "FEISHU_APP_ID": "cli_system_app",
    "FEISHU_APP_SECRET": "system_app_secret",
    "FEISHU_CHAT_ID": "oc_system_chat",
}

_OK_BODY = b'{"code":0,"errcode":0,"errmsg":"ok","ok":true,"StatusCode":0,"msg":"ok"}'
_PUSHPLUS_OK = b'{"code":200,"msg":"ok"}'


class FakeHTTP:
    """Records and answers every ``requests`` call. ``routes`` maps a host to ``(status, headers, body)`` or a callable."""

    def __init__(self) -> None:
        self.calls: List[SimpleNamespace] = []
        self.routes: Dict[str, object] = {}

    def install(self, monkeypatch) -> "FakeHTTP":
        fake = self

        def send(adapter, request, **kwargs):
            body = request.body.decode("utf-8", "replace") if isinstance(request.body, bytes) else str(request.body or "")
            fake.calls.append(SimpleNamespace(
                method=request.method, url=request.url, host=urlparse(request.url).hostname, headers=dict(request.headers),
                body=body, kwargs=kwargs,
            ))
            status, headers, content = fake._answer(request)
            response = requests.Response()
            response.status_code = status
            response._content = content
            response.headers = CaseInsensitiveDict(headers)
            response.url = request.url
            response.request = request
            response.encoding = "utf-8"
            response.raw = io.BytesIO(b"")
            return response

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
        return self

    def _answer(self, request) -> Tuple[int, dict, bytes]:
        host = urlparse(request.url).hostname or ""
        route = self.routes.get(host)
        if callable(route):
            route = route(request)
        if route is not None:
            status, headers, content = route
            return status, headers, content if isinstance(content, bytes) else str(content).encode("utf-8")
        return 200, {}, _PUSHPLUS_OK if "pushplus" in host else _OK_BODY

    # -- queries
    def hosts(self) -> List[str]:
        return [call.host for call in self.calls]

    def calls_to(self, host: str) -> List[SimpleNamespace]:
        return [call for call in self.calls if call.host == host]

    def reset(self) -> None:
        self.calls.clear()


def redirect_to(location: str, status: int = 302) -> Callable:
    return lambda request: (status, {"Location": location}, b"")


def create_channel(client, channel_type: str, config: Optional[dict] = None, **extra):
    response = client.post(
        "/api/v1/me/notification-channels",
        json={"channel_type": channel_type, "config": config if config is not None else CONFIGS[channel_type], **extra},
    )
    return response


def only_hosts(fake: FakeHTTP, allowed) -> bool:
    return set(fake.hosts()) <= set(allowed)
