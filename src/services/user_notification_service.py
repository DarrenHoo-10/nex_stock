# -*- coding: utf-8 -*-
"""Personal push channels (docs/design/multi-user.md section 5, P3a).

A non-primary user's pushes go to the channels stored in ``user_notification_channel`` and nowhere else: never to
the ``.env`` channels, never to a bot app (``FEISHU_APP_*`` / ``FEISHU_CHAT_ID``, DingTalk / Discord / Slack bots).
The primary user keeps using ``.env`` exactly as before; ``notifier_for_user`` is the one place that decides.

Reuse, not a second sender stack: every channel row is rendered into the same config attributes the existing
``src/notification_sender/*`` classes read, and handed to a ``NotificationService`` built for that one channel
(``UserNotificationService``). A user's service is the composite of its channel rows' services, so the report
rendering, chunking, retries and per-channel diagnostics (``NotificationDispatchResult``) are the system's own.

Safety on top of the existing senders (the user supplies the targets, the server makes the request):

* every URL is a public https URL and every SMTP host a public host, checked when the channel is saved *and* again
  right before each request / connection (``src/utils/safe_requests.py``, ``src/utils/safe_smtp.py``);
* no redirect is ever followed; the SMTP connection is pinned to the checked address and requires TLS;
* the channel configuration is encrypted with the owner's own data key (``src/user_secrets.py``) and is never
  logged, never returned unmasked and never part of an error message; a test send reports a generic category only;
* the noise-control state (dedup / cooldown) is namespaced per user, so one user's messages never suppress another's;
* report history comparison is off (it would read the primary user's view of past reports);
* a user-supplied target cannot hold a server hostage: every channel send has a hard wall-clock deadline (DNS, connect,
  TLS, response and SMTP included, ``USER_PUSH_SEND_DEADLINE_SECONDS``), one push (all of a user's channels) has a
  total budget (``USER_PUSH_TOTAL_DEADLINE_SECONDS``), at most 64 KB of a response is read, and a user has at most one
  test send / one background push in flight (``submit_user_push``);
* a personal push never writes the shared ``reports/`` directory and never uploads a file (``FEISHU_SEND_AS_FILE``
  is the primary user's ``.env`` channel option; a personal notifier has it off and ``save_report_to_file`` is a no-op).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import dataclasses
import functools
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.config import Config, get_config, get_primary_user_id
from src.notification import (
    ChannelAttemptResult,
    ChannelDetector,
    NotificationChannel,
    NotificationDispatchResult,
    NotificationService,
)
from src.repositories.user_repo import UserRepository
from src.user_secrets import UserSecretError, UserSecretStore
from src.utils.rate_limit import SlidingWindowLimiter
from src.utils.safe_requests import BlockedRequestError, call_with_deadline, scoped_resolver, user_outbound_requests
from src.utils.safe_smtp import ALLOWED_SMTP_PORTS, check_smtp_endpoint
from src.utils.url_safety import RESOLUTION_DEPENDENT_REASONS, UnsafeUrlError, check_public_https_url

logger = logging.getLogger(__name__)

MASK_TOKEN = "******"
DEFAULT_MAX_CHANNELS_PER_USER = 10
NO_MODEL_NOTICE_KEY = "no_model_reminder"
TEST_SEND_MIN_INTERVAL_SECONDS = 10.0
DEFAULT_SEND_DEADLINE_SECONDS = 30.0
DEFAULT_PUSH_DEADLINE_SECONDS = 60.0
DEFAULT_PUSH_MAX_WORKERS = 4
DEFAULT_CHAT_SEND_MAX_PER_HOUR = 30
DEFAULT_ALERT_PUSH_MAX_PER_HOUR = 60
_DEADLINE_GRACE_SECONDS = 5.0  # callers that wait for a push give the in-send watchdog this much time to act first
TEST_MESSAGE = "【NexStock】推送测试：这是一条测试消息，收到说明该渠道可用。"
MAX_RECEIVERS = 5
_MAX_NAME_LENGTH = 64


class ChannelError(ValueError):
    """A personal channel request was rejected; ``code`` is stable and the message never contains a secret value."""

    def __init__(self, code: str, message: str, issues: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.issues = issues or []


# --------------------------------------------------------------------------- channel type registry

_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,128}$")
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{3,20}:[A-Za-z0-9_-]{20,100}$")
_TELEGRAM_CHAT_RE = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{5,64})$")
_THREAD_RE = re.compile(r"^\d{1,12}$")
_EMAIL_RE = re.compile(r"^[^@\s<>,;\"'\\]{1,64}@[^@\s<>,;\"'\\]{1,190}\.[^@\s<>,;\"'\\]{2,63}$")
_PLAIN_SECRET_RE = re.compile(r"^\S{1,512}$")


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str = "text"            # text | url | token | email | email_list | port | host | template
    required: bool = False
    secret: bool = False
    max_length: int = 256


@dataclass(frozen=True)
class ChannelTypeSpec:
    channel_type: str
    label: str
    channel: NotificationChannel
    fields: Tuple[FieldSpec, ...]
    # config key -> Config attribute read by the existing sender; a callable builds the value from the whole config.
    attrs: Callable[[Mapping[str, Any]], Dict[str, Any]]


def _spec_attrs_feishu(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "feishu_webhook_url": c["webhook_url"],
        "feishu_webhook_secret": c.get("secret") or None,
        "feishu_webhook_keyword": c.get("keyword") or None,
    }


def _spec_attrs_dingtalk(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {"dingtalk_webhook_url": c["webhook_url"], "dingtalk_secret": c.get("secret") or None}


def _spec_attrs_wecom(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {"wechat_webhook_url": c["webhook_url"]}


def _spec_attrs_telegram(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "telegram_bot_token": c["bot_token"],
        "telegram_chat_id": c["chat_id"],
        "telegram_message_thread_id": c.get("message_thread_id") or None,
    }


def _spec_attrs_pushplus(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {"pushplus_token": c["token"], "pushplus_topic": c.get("topic") or None}


def _spec_attrs_serverchan3(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {"serverchan3_sendkey": c["sendkey"]}


def _spec_attrs_email(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "email_sender": c["sender"],
        "email_password": c["password"],
        "email_receivers": list(c.get("receivers") or []) or [c["sender"]],
        "email_smtp_host": c["smtp_host"],
        "email_smtp_port": int(c["smtp_port"]),
        "email_smtp_username": c.get("username") or None,
    }


def _spec_attrs_custom(c: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "custom_webhook_urls": [c["webhook_url"]],
        "custom_webhook_bearer_token": c.get("bearer_token") or None,
        "custom_webhook_body_template": c.get("body_template") or None,
    }


CHANNEL_TYPES: Dict[str, ChannelTypeSpec] = {
    spec.channel_type: spec
    for spec in (
        ChannelTypeSpec("feishu", "飞书机器人 Webhook", NotificationChannel.FEISHU, (
            FieldSpec("webhook_url", "Webhook 地址", "url", required=True, secret=True, max_length=1024),
            FieldSpec("secret", "签名密钥（可选）", "token", secret=True, max_length=200),
            FieldSpec("keyword", "关键词（可选）", "text", max_length=100),
        ), _spec_attrs_feishu),
        ChannelTypeSpec("dingtalk", "钉钉机器人 Webhook", NotificationChannel.DINGTALK, (
            FieldSpec("webhook_url", "Webhook 地址", "url", required=True, secret=True, max_length=1024),
            FieldSpec("secret", "加签密钥（可选）", "token", secret=True, max_length=200),
        ), _spec_attrs_dingtalk),
        ChannelTypeSpec("wecom", "企业微信机器人 Webhook", NotificationChannel.WECHAT, (
            FieldSpec("webhook_url", "Webhook 地址", "url", required=True, secret=True, max_length=1024),
        ), _spec_attrs_wecom),
        ChannelTypeSpec("telegram", "Telegram", NotificationChannel.TELEGRAM, (
            FieldSpec("bot_token", "Bot Token", "telegram_token", required=True, secret=True, max_length=128),
            FieldSpec("chat_id", "Chat ID", "telegram_chat", required=True, max_length=64),
            FieldSpec("message_thread_id", "话题 ID（可选）", "thread", max_length=12),
        ), _spec_attrs_telegram),
        ChannelTypeSpec("pushplus", "PushPlus", NotificationChannel.PUSHPLUS, (
            FieldSpec("token", "Token", "alnum_token", required=True, secret=True, max_length=128),
            FieldSpec("topic", "群组编码（可选）", "topic", max_length=64),
        ), _spec_attrs_pushplus),
        ChannelTypeSpec("serverchan3", "Server酱3", NotificationChannel.SERVERCHAN3, (
            FieldSpec("sendkey", "SendKey", "alnum_token", required=True, secret=True, max_length=128),
        ), _spec_attrs_serverchan3),
        ChannelTypeSpec("email", "邮件（自己的 SMTP 账号）", NotificationChannel.EMAIL, (
            FieldSpec("smtp_host", "SMTP 服务器", "host", required=True, max_length=253),
            FieldSpec("smtp_port", "SMTP 端口（465 / 587 / 25）", "port", required=True, max_length=5),
            FieldSpec("username", "登录用户名（可选，默认同发件人）", "text", max_length=254),
            FieldSpec("password", "密码 / 授权码", "password", required=True, secret=True, max_length=256),
            FieldSpec("sender", "发件人邮箱", "email", required=True, max_length=254),
            FieldSpec("receivers", f"收件人邮箱（最多 {MAX_RECEIVERS} 个，留空发给自己）", "email_list", max_length=1024),
        ), _spec_attrs_email),
        ChannelTypeSpec("custom", "自定义 Webhook", NotificationChannel.CUSTOM, (
            FieldSpec("webhook_url", "Webhook 地址", "url", required=True, secret=True, max_length=1024),
            FieldSpec("bearer_token", "Bearer Token（可选）", "token", secret=True, max_length=512),
            FieldSpec("body_template", "请求体模板（可选，JSON）", "template", max_length=4096),
        ), _spec_attrs_custom),
    )
}


def channel_type_catalog() -> List[Dict[str, Any]]:
    """Types and fields for a settings form (no values)."""
    return [
        {
            "channel_type": spec.channel_type,
            "label": spec.label,
            "fields": [
                {"name": f.name, "label": f.label, "kind": f.kind, "required": f.required, "secret": f.secret}
                for f in spec.fields
            ],
        }
        for spec in CHANNEL_TYPES.values()
    ]


# --------------------------------------------------------------------------- validation

def _issue(field: str, code: str, message: str) -> Dict[str, Any]:
    return {"key": field, "code": code, "message": message, "severity": "error"}


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


def _reported_reason(exc: UnsafeUrlError) -> str:
    """The reason shown to the user. What depends on DNS / address data (private address, unresolvable name) is one
    generic ``unsafe_target`` - telling them apart would let a user probe what the server's resolver sees; syntax
    problems (scheme, credentials, port, numeric alias, local name) keep their specific code."""
    return "unsafe_target" if exc.reason in RESOLUTION_DEPENDENT_REASONS else exc.reason


def _normalize_field(spec: FieldSpec, raw: Any, *, resolve: bool, resolver: Optional[Callable] = None) -> Any:
    """Normalized value of one field, or raise ``ValueError(code)``; secrets never appear in the message."""
    if spec.kind == "email_list":
        items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;\n]", _as_text(raw))
        emails = [str(item).strip() for item in items if str(item).strip()]
        emails = list(dict.fromkeys(emails))
        if len(emails) > MAX_RECEIVERS:
            raise ValueError("too_many_receivers")
        if any(not _EMAIL_RE.match(item) or len(item) > 254 for item in emails):
            raise ValueError("invalid_email")
        return emails
    text = _as_text(raw)
    if not text:
        return ""
    if len(text) > spec.max_length:
        raise ValueError("too_long")
    if spec.kind not in ("template",) and any(ch in text for ch in "\r\n\x00"):
        raise ValueError("invalid_characters")
    if spec.kind == "url":
        try:
            if resolve:
                check_public_https_url(text, resolver=resolver or scoped_resolver)
            else:
                check_public_https_url(text, resolve=False)
        except UnsafeUrlError as exc:
            raise ValueError(f"unsafe_url:{_reported_reason(exc)}") from exc
        return text
    if spec.kind == "host":
        text = text.lower().rstrip(".")
        if not re.match(r"^[a-z0-9.-]{1,253}$", text):
            raise ValueError("invalid_host")
        return text
    if spec.kind == "port":
        if not text.isdigit() or int(text) not in ALLOWED_SMTP_PORTS:
            raise ValueError("port_not_allowed")
        return int(text)
    if spec.kind == "email":
        if not _EMAIL_RE.match(text):
            raise ValueError("invalid_email")
        return text
    if spec.kind == "telegram_token":
        if not _TELEGRAM_TOKEN_RE.match(text):
            raise ValueError("invalid_token")
        return text
    if spec.kind == "telegram_chat":
        if not _TELEGRAM_CHAT_RE.match(text):
            raise ValueError("invalid_chat_id")
        return text
    if spec.kind == "thread":
        if not _THREAD_RE.match(text):
            raise ValueError("invalid_thread_id")
        return text
    if spec.kind == "alnum_token":
        if not _TOKEN_RE.match(text):
            raise ValueError("invalid_token")
        return text
    if spec.kind == "topic":
        if not _TOPIC_RE.match(text):
            raise ValueError("invalid_topic")
        return text
    if spec.kind == "token" and not _PLAIN_SECRET_RE.match(text):
        raise ValueError("invalid_token")
    if spec.kind == "password" and ("\r" in text or "\n" in text):
        raise ValueError("invalid_characters")
    return text


def validate_channel_config(
    channel_type: str,
    submitted: Mapping[str, Any],
    *,
    current: Optional[Mapping[str, Any]] = None,
    resolve: bool = True,
    resolver: Optional[Callable] = None,
) -> Dict[str, Any]:
    """The full, normalized configuration, merging ``submitted`` onto ``current`` (the stored config on update).

    A secret field that is missing, empty or the mask keeps the stored value; any other empty field clears it.
    The SMTP host is checked here as well (public, allowed port); the web URLs are resolved when ``resolve``.
    Raises :class:`ChannelError` (``invalid_config`` / ``unsafe_target``) with per-field issues, never with values.
    """
    spec = CHANNEL_TYPES.get(channel_type)
    if spec is None:
        raise ChannelError("invalid_channel_type", f"Unsupported channel type; use one of {', '.join(CHANNEL_TYPES)}")
    known = {f.name for f in spec.fields}
    unknown = sorted(set(submitted) - known)
    if unknown:
        raise ChannelError(
            "invalid_config", "Unknown channel fields", [_issue(name, "unknown_field", "Unknown field") for name in unknown]
        )

    merged: Dict[str, Any] = {}
    issues: List[Dict[str, Any]] = []
    unsafe = False
    for field in spec.fields:
        stored = (current or {}).get(field.name)
        if field.name in submitted:
            value = submitted[field.name]
            keep = field.secret and (value is None or _as_text(value) in ("", MASK_TOKEN))
            candidate = stored if keep else value
        else:
            candidate = stored
        try:
            normalized = _normalize_field(field, candidate, resolve=resolve, resolver=resolver)
        except ValueError as exc:
            reason = str(exc)
            if reason.startswith("unsafe_url:") or reason == "port_not_allowed":
                unsafe = True
            issues.append(_issue(field.name, reason.split(":")[-1] if reason.startswith("unsafe_url:") else reason,
                                 "Not an acceptable value"))
            continue
        if field.required and normalized in ("", [], None):
            issues.append(_issue(field.name, "required", "This field is required"))
            continue
        merged[field.name] = normalized
    if not issues and channel_type == "email":
        try:
            check_smtp_endpoint(merged["smtp_host"], int(merged["smtp_port"]), resolver=resolver) if resolve else None
        except UnsafeUrlError as exc:
            unsafe = True
            issues.append(_issue("smtp_host", _reported_reason(exc), "Not an acceptable SMTP server"))
    if not issues and channel_type == "custom" and merged.get("body_template"):
        try:
            json.dumps(merged["body_template"])
        except (TypeError, ValueError):
            issues.append(_issue("body_template", "invalid_template", "Not an acceptable template"))
    if issues:
        raise ChannelError(
            "unsafe_target" if unsafe else "invalid_config",
            "The channel target is not allowed (public https URLs / public SMTP hosts only)" if unsafe
            else "The channel configuration is not valid",
            issues,
        )
    return merged


def mask_config(channel_type: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    """Display form: secret fields are ``******`` when set, everything else as stored."""
    spec = CHANNEL_TYPES.get(channel_type)
    if spec is None:
        return {}
    shown: Dict[str, Any] = {}
    for field in spec.fields:
        value = config.get(field.name)
        if field.secret:
            shown[field.name] = MASK_TOKEN if value else ""
        else:
            shown[field.name] = value if value is not None else ""
    return shown


# --------------------------------------------------------------------------- notification config / service

# Config attributes that describe *where* a push goes (credentials, endpoints, routing, image channels). A personal
# notifier starts from the dataclass defaults of all of them, so nothing of the system's channels can leak in.
_CHANNEL_ATTR_PREFIXES = (
    "wechat_", "dingtalk_", "feishu_", "telegram_", "email_", "pushover_", "ntfy_", "gotify_", "custom_webhook_",
    "discord_", "slack_", "astrbot_", "pushplus_", "serverchan3_", "stock_email_groups", "markdown_to_image_",
    "md2img_", "notification_report_channels", "notification_alert_channels", "notification_system_error_channels",
    "webhook_verify_ssl",
)
_ROUTE_ATTRS = {
    "NOTIFICATION_REPORT_CHANNELS": "notification_report_channels",
    "NOTIFICATION_ALERT_CHANNELS": "notification_alert_channels",
    "NOTIFICATION_SYSTEM_ERROR_CHANNELS": "notification_system_error_channels",
}


@functools.lru_cache(maxsize=1)
def _channel_attr_defaults() -> Dict[str, Any]:
    defaults: Dict[str, Any] = {}
    for item in dataclasses.fields(Config):
        if not item.name.startswith(_CHANNEL_ATTR_PREFIXES):
            continue
        if item.default is not dataclasses.MISSING:
            defaults[item.name] = item.default
        elif item.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            defaults[item.name] = item.default_factory()  # type: ignore[misc]
    return defaults


class UserNotificationConfig:
    """Config-like object of one personal notifier.

    Preferences (report type / language, summary only, quiet hours, ...) are read from the user's own config view;
    every channel attribute is the dataclass default unless this channel sets it, TLS verification is forced on and
    history comparison is off. Read-only by convention (attributes are set once in ``__init__``).
    """

    def __init__(self, base: Any, channel_attrs: Optional[Mapping[str, Any]] = None, *, routes: Optional[Mapping[str, Any]] = None):
        self.__dict__["_base"] = base
        values = {key: (list(value) if isinstance(value, list) else value) for key, value in _channel_attr_defaults().items()}
        values.update(channel_attrs or {})
        values.update(routes or {})
        values["webhook_verify_ssl"] = True
        values["report_history_compare_n"] = 0
        self.__dict__.update(values)

    def __getattr__(self, name: str) -> Any:
        base = self.__dict__.get("_base")
        if base is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(base, name)


# --------------------------------------------------------------------------- deadlines, concurrency, rate limits

def _config_number(name: str, default: float, *, minimum: float = 1.0) -> float:
    try:
        value = float(getattr(get_config(), name, default))
    except (TypeError, ValueError, AttributeError):
        return default
    return value if value >= minimum else default


def send_deadline_seconds() -> float:
    """Hard wall-clock limit of one channel send (``USER_PUSH_SEND_DEADLINE_SECONDS``)."""
    return _config_number("user_push_send_deadline_seconds", DEFAULT_SEND_DEADLINE_SECONDS)


def push_deadline_seconds() -> float:
    """Hard wall-clock limit of one push over all of a user's channels (``USER_PUSH_TOTAL_DEADLINE_SECONDS``)."""
    return _config_number("user_push_total_deadline_seconds", DEFAULT_PUSH_DEADLINE_SECONDS)


# Monotonic end of the push in progress (set by the composite notifier); a channel send never outlives it.
_push_deadline: "contextvars.ContextVar[Optional[float]]" = contextvars.ContextVar("user_push_deadline", default=None)


def _send_budget_seconds() -> float:
    seconds = send_deadline_seconds()
    until = _push_deadline.get()
    if until is not None:
        seconds = min(seconds, until - time.monotonic())
    return seconds


_limiter = SlidingWindowLimiter()


def _hourly_limit(name: str, default: int) -> int:
    return int(_config_number(name, float(default)))


def allow_chat_send(user_id: str) -> bool:
    """``/agent/chat/send`` relays text of the user's choosing to their channels: at most ``USER_CHAT_SEND_MAX_PER_HOUR``."""
    return _limiter.allow("chat_send", user_id, _hourly_limit("user_chat_send_max_per_hour", DEFAULT_CHAT_SEND_MAX_PER_HOUR))


def allow_alert_push(user_id: str) -> bool:
    """Personal alert pushes per hour (``USER_ALERT_PUSH_MAX_PER_HOUR``): a rule that fires every cycle cannot spam a channel."""
    return _limiter.allow("alert_push", user_id, _hourly_limit("user_alert_push_max_per_hour", DEFAULT_ALERT_PUSH_MAX_PER_HOUR))


def reset_rate_limits() -> None:
    _limiter.reset()


_push_pool_lock = threading.Lock()
_push_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_push_inflight: set = set()


def submit_user_push(user_id: str, job: Callable[[], Any]) -> Optional["concurrent.futures.Future"]:
    """Run ``job`` (a user's push) in the shared bounded push pool and return its future, or ``None`` when that user
    already has a push running (one in flight per user: a hung target can occupy at most one worker, and the others'
    pushes - and the caller's own work - never wait for it). The pool size is ``USER_PUSH_MAX_WORKERS``."""
    global _push_pool
    with _push_pool_lock:
        if user_id in _push_inflight:
            return None
        if _push_pool is None:
            workers = max(1, int(_config_number("user_push_max_workers", DEFAULT_PUSH_MAX_WORKERS)))
            _push_pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="user-push")
        _push_inflight.add(user_id)
        pool = _push_pool

    def run() -> Any:
        try:
            return job()
        finally:
            with _push_pool_lock:
                _push_inflight.discard(user_id)

    try:
        return pool.submit(run)
    except Exception:  # noqa: BLE001 - e.g. interpreter shutdown
        with _push_pool_lock:
            _push_inflight.discard(user_id)
        raise


def push_wait_seconds(jobs: int = 1) -> float:
    """How long a caller should wait for ``jobs`` sequential pushes of one user (each bounded by the push deadline)."""
    return max(1, jobs) * (push_deadline_seconds() + _DEADLINE_GRACE_SECONDS)


_GUARDED_SENDERS = (
    "send_to_wechat", "_send_wechat_image", "send_to_feishu", "send_feishu_file", "send_to_dingtalk",
    "send_to_telegram", "_send_telegram_photo", "send_to_email", "_send_email_with_inline_image",
    "send_to_pushplus", "send_to_serverchan3", "send_to_custom", "_send_custom_webhook_image",
    "test_custom_webhooks",
)
_USER_CHANNELS = frozenset(spec.channel for spec in CHANNEL_TYPES.values())


class UserNotificationService(NotificationService):
    """``NotificationService`` for one user's personal channels (a composite of one leaf service per channel row).

    The leaf holds exactly one channel's configuration; the composite holds none itself, takes the union of its
    leaves' channel types as ``_available_channels`` and fans a send out to the leaves of that type. Everything
    else (rendering, ``send_with_results``, diagnostics) is inherited unchanged. Every network call of a leaf runs
    inside ``user_outbound_requests`` (public https only, no redirects).
    """

    def __init__(
        self,
        user_id: str,
        config: UserNotificationConfig,
        *,
        children: Optional[Sequence["UserNotificationService"]] = None,
        row_id: Optional[int] = None,
    ):
        self.user_id = user_id
        self._composite = children is not None
        self._children = list(children or ())
        self._row_id = row_id
        self._noise_scope = f"user:{user_id}"
        self.row_outcomes: List[Dict[str, Any]] = []
        super().__init__(config=config)
        self._feishu_send_as_file = False  # the "upload the report as a file" mode is the primary user's .env option

    # -- channels
    def _detect_all_channels(self) -> List[NotificationChannel]:
        if self._composite:  # the composite holds no channel of its own: it is the union of its channel rows
            types = {child_type for child in self._children for child_type in child._available_channels}
            return [channel for channel in NotificationChannel if channel in types]
        detected = self.detect_configured_channels(self._config)
        return [channel for channel in detected if channel in _USER_CHANNELS]

    def _send_to_static_channel(self, channel: NotificationChannel, content: str, **kwargs: Any) -> bool:
        if channel not in _USER_CHANNELS:
            logger.warning("Channel %s is not available for personal notifications", channel.value)
            return False
        if not self._composite:
            return super()._send_to_static_channel(channel, content, **kwargs)
        delivered = False
        for child in self._children:
            if channel not in child._available_channels:
                continue
            try:
                ok = bool(child._send_to_static_channel(channel, content, **kwargs))
            except Exception as exc:  # noqa: BLE001 - one channel never breaks the others
                logger.warning("Personal channel %s of user %s failed: %s", channel.value, self.user_id, type(exc).__name__)
                ok = False
            self.row_outcomes.append({"channel_id": child._row_id, "channel_type": channel.value, "success": ok})
            delivered = delivered or ok
        return delivered

    def send_with_results(self, content: str, *args: Any, **kwargs: Any) -> NotificationDispatchResult:
        self.row_outcomes = []
        if not self._composite or _push_deadline.get() is not None:
            return super().send_with_results(content, *args, **kwargs)
        token = _push_deadline.set(time.monotonic() + push_deadline_seconds())  # one budget for all of the user's channels
        try:
            return super().send_with_results(content, *args, **kwargs)
        finally:
            _push_deadline.reset(token)

    def save_report_to_file(self, content: str, filename: Optional[str] = None) -> Optional[str]:
        """A personal notifier never writes the shared ``reports/`` directory (the primary user's files live there)."""
        logger.debug("Personal notifier of user %s does not save report files", self.user_id)
        return None

    # -- isolation between users
    def evaluate_noise_control(self, content: str, *, route_type=None, severity=None, dedup_key=None, cooldown_key=None):
        digest = dedup_key or hashlib.sha256((content or "").encode("utf-8")).hexdigest()
        return super().evaluate_noise_control(
            content,
            route_type=route_type,
            severity=severity,
            dedup_key=f"{self._noise_scope}|{digest}",
            cooldown_key=f"{self._noise_scope}|{cooldown_key or 'default'}",
        )

    def has_enabled_channels(self) -> bool:
        return bool(self._available_channels)


def _guarded(method: Callable) -> Callable:
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        seconds = _send_budget_seconds()
        if seconds <= 0:  # the push's total budget is spent: the remaining channels are not tried
            raise BlockedRequestError("deadline")
        with user_outbound_requests(deadline_seconds=seconds):
            return method(self, *args, **kwargs)

    return wrapper


for _name in _GUARDED_SENDERS:
    setattr(UserNotificationService, _name, _guarded(getattr(NotificationService, _name)))
del _name


def _routes_from_own_settings(own: Mapping[str, str]) -> Dict[str, List[str]]:
    """Report / alert routing the user set themselves; the system's routing names system channels and is not inherited."""
    routes: Dict[str, List[str]] = {}
    for key, attr in _ROUTE_ATTRS.items():
        value = own.get(key)
        if isinstance(value, str) and value.strip():
            routes[attr] = [item.strip().lower() for item in value.split(",") if item.strip()]
    return routes


def _own_settings(user_id: str) -> Dict[str, str]:
    try:
        from src.services.user_settings_service import UserSettingsService

        return UserSettingsService().get_plain_values(user_id)
    except Exception as exc:  # noqa: BLE001 - preferences are optional
        logger.debug("Personal notification routing unavailable for user %s: %s", user_id, exc)
        return {}


# --------------------------------------------------------------------------- service

_test_lock = threading.Lock()
_last_test_at: Dict[Tuple[str, int], float] = {}
_tests_in_flight: set = set()


def max_channels_per_user() -> int:
    return max(1, int(getattr(get_config(), "notification_channels_max_per_user", DEFAULT_MAX_CHANNELS_PER_USER) or 1))


class UserNotificationChannelService:
    """CRUD, masking and test send of one user's personal channels (the API's backing service)."""

    def __init__(self, repo: Optional[UserRepository] = None, secrets: Optional[UserSecretStore] = None):
        self._repo = repo or UserRepository()
        self._secrets = secrets or UserSecretStore(self._repo)

    # -- helpers
    @staticmethod
    def _reject_primary(user_id: str) -> None:
        if user_id == get_primary_user_id():
            raise ChannelError(
                "primary_user_uses_env",
                "The primary user's push channels are the .env channels; edit them in the system settings",
            )

    def _decrypt(self, user_id: str, row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(self._secrets.decrypt(user_id, row["config_encrypted"]))
        except (UserSecretError, ValueError, TypeError):
            logger.warning("Personal channel %s of user %s cannot be read (damaged or destroyed data key)", row.get("id"), user_id)
            return None
        return data if isinstance(data, dict) else None

    def _present(self, user_id: str, row: Mapping[str, Any]) -> Dict[str, Any]:
        config = self._decrypt(user_id, row)
        return {
            "id": row["id"],
            "channel_type": row["channel_type"],
            "name": row["name"],
            "enabled": bool(row["enabled"]),
            "source": "user",
            "editable": True,
            "readable": config is not None,
            "config": mask_config(row["channel_type"], config) if config is not None else {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _clean_name(name: Any, channel_type: str) -> str:
        text = _as_text(name) if name is not None else ""
        if len(text) > _MAX_NAME_LENGTH or any(ch in text for ch in "\r\n"):
            raise ChannelError("invalid_config", "The channel name is not valid", [_issue("name", "too_long", "Not an acceptable value")])
        return text or CHANNEL_TYPES[channel_type].label

    # -- reads
    def list(self, user_id: str) -> Dict[str, Any]:
        if user_id == get_primary_user_id():
            return {
                "user_id": user_id,
                "is_primary": True,
                "managed_in_system_settings": True,
                "note": "主用户的推送渠道就是系统设置里的 .env 渠道，请在系统设置中管理。",
                "max_channels": 0,
                "channels": [
                    {
                        "id": None,
                        "channel_type": channel.value,
                        "name": ChannelDetector.get_channel_name(channel),
                        "enabled": True,
                        "source": "env",
                        "editable": False,
                        "readable": True,
                        "config": {},
                        "created_at": None,
                        "updated_at": None,
                    }
                    for channel in NotificationService.detect_configured_channels(get_config())
                ],
                "channel_types": [],
            }
        return {
            "user_id": user_id,
            "is_primary": False,
            "managed_in_system_settings": False,
            "note": "",
            "max_channels": max_channels_per_user(),
            "channels": [self._present(user_id, row) for row in self._repo.list_channels(user_id)],
            "channel_types": channel_type_catalog(),
        }

    # -- writes
    def create(self, user_id: str, channel_type: str, config: Mapping[str, Any], *, name: Any = None, enabled: bool = True) -> Dict[str, Any]:
        self._reject_primary(user_id)
        normalized = validate_channel_config(_as_text(channel_type).lower(), config or {})
        channel_type = _as_text(channel_type).lower()
        clean_name = self._clean_name(name, channel_type)
        try:
            encrypted = self._secrets.encrypt(user_id, json.dumps(normalized, ensure_ascii=False))
        except UserSecretError as exc:
            raise ChannelError("secret_store_unavailable", "Channel secrets cannot be stored right now") from exc
        row = self._repo.create_channel(
            user_id, channel_type, clean_name, encrypted, bool(enabled), max_channels=max_channels_per_user()
        )
        if row is None:
            raise ChannelError("channel_limit_reached", f"At most {max_channels_per_user()} push channels per user")
        return self._present(user_id, row)

    def update(
        self, user_id: str, channel_id: int, *, config: Optional[Mapping[str, Any]] = None, name: Any = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        self._reject_primary(user_id)
        row = self._repo.get_channel(user_id, channel_id)
        if row is None:
            raise ChannelError("channel_not_found", "Push channel not found")
        encrypted = None
        if config is not None:
            current = self._decrypt(user_id, row)
            if current is None:
                # The stored secrets are gone: the channel can only be replaced by a complete configuration.
                current = {}
            normalized = validate_channel_config(row["channel_type"], config, current=current)
            try:
                encrypted = self._secrets.encrypt(user_id, json.dumps(normalized, ensure_ascii=False))
            except UserSecretError as exc:
                raise ChannelError("secret_store_unavailable", "Channel secrets cannot be stored right now") from exc
        clean_name = self._clean_name(name, row["channel_type"]) if name is not None else None
        updated = self._repo.update_channel(user_id, channel_id, name=clean_name, config_encrypted=encrypted, enabled=enabled)
        if updated is None:
            raise ChannelError("channel_not_found", "Push channel not found")
        return self._present(user_id, updated)

    def delete(self, user_id: str, channel_id: int) -> None:
        self._reject_primary(user_id)
        if not self._repo.delete_channel(user_id, channel_id):
            raise ChannelError("channel_not_found", "Push channel not found")

    # -- test send
    def test(self, user_id: str, channel_id: int) -> Dict[str, Any]:
        """Send the fixed test message through one channel. Only a generic category and status are returned.

        One test per user at a time (a second one is ``rate_limited``) and one per channel every
        ``TEST_SEND_MIN_INTERVAL_SECONDS``; the allowance is recorded when the test starts *and* when it ends, so a hung
        target cannot be re-tested in a loop to pile up threads. No timing is reported (a latency would tell an open
        port from a closed or filtered one), and the whole send is bounded by the hard push deadline.
        """
        self._reject_primary(user_id)
        row = self._repo.get_channel(user_id, channel_id)
        if row is None:
            raise ChannelError("channel_not_found", "Push channel not found")
        now = time.monotonic()
        with _test_lock:
            if user_id in _tests_in_flight:
                raise ChannelError("rate_limited", "A test send is still running; wait for it to finish")
            last = _last_test_at.get((user_id, channel_id))
            if last is not None and now - last < TEST_SEND_MIN_INTERVAL_SECONDS:
                raise ChannelError("rate_limited", "Wait a few seconds before testing this channel again")
            _last_test_at[(user_id, channel_id)] = now
            _tests_in_flight.add(user_id)

        def work() -> Dict[str, Any]:
            try:
                return self._send_test(user_id, row)
            finally:  # released by the worker itself: an abandoned (hung) send keeps the user's slot until it really ends
                with _test_lock:
                    _tests_in_flight.discard(user_id)
                    _last_test_at[(user_id, channel_id)] = time.monotonic()

        try:
            return call_with_deadline(work, push_wait_seconds(), name="channel-test")
        except TimeoutError:
            logger.warning("Personal channel test of user %s exceeded its deadline", user_id)
            return {"success": False, "status": "failed", "error_category": "send_failed"}

    def _send_test(self, user_id: str, row: Mapping[str, Any]) -> Dict[str, Any]:
        config = self._decrypt(user_id, row)
        if config is None:
            return {"success": False, "status": "failed", "error_category": "unreadable"}
        try:
            normalized = validate_channel_config(row["channel_type"], config, current=config)  # public target, right now
        except ChannelError:
            return {"success": False, "status": "blocked", "error_category": "target_not_allowed"}
        leaf = _build_leaf(user_id, row["id"], row["channel_type"], normalized, base_config=_user_base_config(user_id), routes={})
        try:
            sent = bool(leaf._send_to_static_channel(
                CHANNEL_TYPES[row["channel_type"]].channel, TEST_MESSAGE,
                image_bytes=None, email_stock_codes=None, email_send_to_all=False, route_type=None,
            ))
        except Exception as exc:  # noqa: BLE001 - reported as a generic category only
            logger.warning("Personal channel test failed for user %s: %s", user_id, type(exc).__name__)
            sent = False
        return {"success": sent, "status": "sent" if sent else "failed", "error_category": None if sent else "send_failed"}


# --------------------------------------------------------------------------- notifier construction

def _user_base_config(user_id: str) -> Any:
    from src.services.user_config_service import get_user_config

    return get_user_config(user_id)


def _build_leaf(
    user_id: str, row_id: Optional[int], channel_type: str, config: Mapping[str, Any], *, base_config: Any,
    routes: Mapping[str, Any],
) -> UserNotificationService:
    spec = CHANNEL_TYPES[channel_type]
    notification_config = UserNotificationConfig(base_config, spec.attrs(config), routes=routes)
    return UserNotificationService(user_id, notification_config, row_id=row_id)


def build_user_notifier(
    user_id: str,
    *,
    repo: Optional[UserRepository] = None,
    secrets: Optional[UserSecretStore] = None,
    base_config: Any = None,
) -> UserNotificationService:
    """The personal notifier of a non-primary user: one leaf per enabled, readable, valid channel row.

    A channel that cannot be decrypted or no longer validates (the host started to resolve to a private address, an
    old row from before a rule changed) is skipped and logged by id only; the others still work. A user without
    channels gets a notifier whose ``is_available()`` is False - never the system's channels.
    """
    if user_id == get_primary_user_id():
        raise ValueError("The primary user's notifier is the system NotificationService")
    repo = repo or UserRepository()
    secrets = secrets or UserSecretStore(repo)
    base = base_config if base_config is not None else _user_base_config(user_id)
    routes = _routes_from_own_settings(_own_settings(user_id))
    children: List[UserNotificationService] = []
    service = UserNotificationChannelService(repo, secrets)
    for row in repo.list_channels(user_id, enabled_only=True):
        spec = CHANNEL_TYPES.get(row["channel_type"])
        config = service._decrypt(user_id, row)
        if spec is None or config is None:
            continue
        try:
            normalized = validate_channel_config(row["channel_type"], config, current=config, resolve=False)
        except ChannelError:
            logger.warning("Personal channel %s of user %s no longer validates; skipped", row["id"], user_id)
            continue
        children.append(_build_leaf(user_id, row["id"], row["channel_type"], normalized, base_config=base, routes=routes))
    return UserNotificationService(user_id, UserNotificationConfig(base, routes=routes), children=children)


def notifier_for_user(user_id: Optional[str]) -> NotificationService:
    """Where ``user_id``'s pushes go: the system (``.env``) service for the primary user, else their personal channels."""
    if user_id is None or user_id == get_primary_user_id():
        return NotificationService()
    return build_user_notifier(user_id)


def user_has_channels(user_id: str) -> bool:
    """Whether a non-primary user has at least one usable personal channel (False for the primary user: use ``.env``)."""
    if user_id == get_primary_user_id():
        return False
    try:
        return build_user_notifier(user_id).has_enabled_channels()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot read the push channels of user %s: %s", user_id, type(exc).__name__)
        return False


def push_to_user(
    user_id: str,
    content: str,
    *,
    route_type: Optional[str] = "report",
    severity: Optional[str] = None,
    dedup_key: Optional[str] = None,
    notifier: Optional[UserNotificationService] = None,
) -> NotificationDispatchResult:
    """Push ``content`` to a non-primary user's own channels; never raises (a failing channel is in the result)."""
    try:
        service = notifier or build_user_notifier(user_id)
        if not service.is_available():
            return NotificationDispatchResult(
                dispatched=False, success=False, status="no_channel", message="user has no personal push channel"
            )
        return service.send_with_results(content, route_type=route_type, severity=severity, dedup_key=dedup_key, email_send_to_all=True)
    except Exception as exc:  # noqa: BLE001 - a push problem never breaks the caller
        logger.warning("Personal push for user %s failed: %s", user_id, type(exc).__name__)
        return NotificationDispatchResult(
            dispatched=False,
            success=False,
            status="exception",
            channel_results=[ChannelAttemptResult(channel="__dispatch__", success=False, error_code="exception", retryable=True)],
            message=type(exc).__name__,
        )


def push_once_notice(user_id: str, notice_key: str, content: str, *, repo: Optional[UserRepository] = None) -> str:
    """Push ``content`` to the user once per ``notice_key`` (until :func:`clear_notice`). Returns the outcome:
    ``already_sent`` / ``no_channel`` / ``sent`` / ``failed`` (a failed push is retried on the next call)."""
    repo = repo or UserRepository()
    if repo.has_notice(user_id, notice_key):
        return "already_sent"
    if user_id == get_primary_user_id():
        return "no_channel"
    result = push_to_user(user_id, content, route_type="alert", severity="warning")
    if result.status == "no_channel":
        return "no_channel"
    if result.success:
        repo.mark_notice(user_id, notice_key)
        return "sent"
    return "failed"


def clear_notice(user_id: str, notice_key: str, *, repo: Optional[UserRepository] = None) -> None:
    (repo or UserRepository()).clear_notice(user_id, notice_key)
