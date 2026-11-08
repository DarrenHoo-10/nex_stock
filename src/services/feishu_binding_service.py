# -*- coding: utf-8 -*-
"""Feishu notification target, assistant session mapping and push recording.

* Target order for pushes: active ``/bind`` binding -> ``FEISHU_CHAT_ID`` ->
  (caller falls back to the legacy webhook). Both app targets need
  ``FEISHU_APP_ID`` + ``FEISHU_APP_SECRET``.
* Session mapping: in p2p every plain message opens a new topic and session
  (``feishu:<chat_id>:<root message id>``), and messages inside that topic stay
  in it; a group has one per topic/thread (``feishu:<chat_id>:<thread>``) and
  ``feishu:<chat_id>`` for plain messages. A message replying to a known push /
  bot answer continues that message's session. Pushes are recorded into
  ``feishu:<chat_id>``.
* A pushed notification is recorded into the target chat's assistant session so
  follow-up questions have it in context.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Tuple

from src.repositories.feishu_binding_repo import FeishuBindingRepository

logger = logging.getLogger(__name__)

SESSION_PREFIX = "feishu"
TARGET_SOURCE_BINDING = "binding"
TARGET_SOURCE_CONFIG = "config"
CHAT_TYPE_P2P = "p2p"
CHAT_TYPE_GROUP = "group"

_TITLE_MAX_CHARS = 60
_HIDDEN_META_RE = re.compile(r"^\[[^\]]+\]:\s*#")


@dataclass(frozen=True)
class FeishuTarget:
    """Where bot-app pushes go."""

    chat_id: str
    receive_id_type: str  # chat_id / open_id
    source: str  # binding / config
    session_id: Optional[str] = None  # known for bindings; resolved from the send response otherwise


def has_app_credentials(config: Any) -> bool:
    return bool(
        str(getattr(config, "feishu_app_id", None) or "").strip()
        and str(getattr(config, "feishu_app_secret", None) or "").strip()
    )


def chat_session_id(chat_id: str) -> str:
    return f"{SESSION_PREFIX}:{chat_id}"


def thread_session_id(chat_id: str, thread_id: str) -> str:
    return f"{SESSION_PREFIX}:{chat_id}:{thread_id}"


def resolve_session_id(
    *,
    chat_id: str,
    chat_type: str,
    thread_id: Optional[str] = None,
    root_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    message_id: Optional[str] = None,
    repo: Optional[FeishuBindingRepository] = None,
) -> str:
    """Map an incoming Feishu message to its assistant session id.

    * Reply to a known push / bot answer -> that message's session.
    * p2p: a plain message starts a new session keyed by its own id (the bot
      answers in a topic rooted at it); messages inside that topic carry the
      root id and continue it.
    * group: per topic/thread, otherwise one session per chat.
    """
    linked: Optional[str] = None
    if parent_id or root_id:
        try:
            linked = (repo or FeishuBindingRepository()).lookup_message_session(parent_id, root_id)
        except Exception as exc:  # a lookup failure must not block the answer
            logger.warning("[Feishu] message->session lookup failed: %s", exc)
    if linked:
        return linked
    if chat_type == CHAT_TYPE_P2P:
        topic = root_id or thread_id or message_id
        return thread_session_id(chat_id, topic) if topic else chat_session_id(chat_id)
    topic = thread_id or root_id
    if topic:
        return thread_session_id(chat_id, topic)
    return chat_session_id(chat_id)


def get_binding(repo: Optional[FeishuBindingRepository] = None) -> Optional[dict]:
    try:
        return (repo or FeishuBindingRepository()).get_binding()
    except Exception as exc:
        logger.warning("[Feishu] reading notification binding failed: %s", exc)
        return None


def resolve_notification_target(
    config: Any, repo: Optional[FeishuBindingRepository] = None,
) -> Optional[FeishuTarget]:
    """Bot-app push target: active binding, else ``FEISHU_CHAT_ID``; ``None`` -> webhook fallback."""
    if not has_app_credentials(config):
        return None
    binding = get_binding(repo)
    if binding:
        return FeishuTarget(
            chat_id=binding["chat_id"],
            receive_id_type="chat_id",
            source=TARGET_SOURCE_BINDING,
            session_id=binding["session_id"],
        )
    chat_id = str(getattr(config, "feishu_chat_id", None) or "").strip()
    if not chat_id:
        return None
    receive_id_type = str(getattr(config, "feishu_receive_id_type", None) or "chat_id").strip().lower()
    if receive_id_type not in ("chat_id", "open_id"):
        receive_id_type = "chat_id"
    return FeishuTarget(
        chat_id=chat_id,
        receive_id_type=receive_id_type,
        source=TARGET_SOURCE_CONFIG,
        session_id=chat_session_id(chat_id) if receive_id_type == "chat_id" else None,
    )


def bind_chat(
    chat_id: str,
    chat_type: str,
    bound_by_open_id: Optional[str] = None,
    repo: Optional[FeishuBindingRepository] = None,
) -> dict:
    """Make ``chat_id`` the notification target (replaces any previous binding)."""
    return (repo or FeishuBindingRepository()).set_binding(
        chat_id=chat_id,
        chat_type=chat_type if chat_type in (CHAT_TYPE_P2P, CHAT_TYPE_GROUP) else CHAT_TYPE_GROUP,
        session_id=chat_session_id(chat_id),
        bound_by_open_id=bound_by_open_id,
    )


def unbind_chat(repo: Optional[FeishuBindingRepository] = None) -> bool:
    return (repo or FeishuBindingRepository()).clear_binding()


def derive_push_title(content: str, route_type: Optional[str] = None) -> str:
    """First meaningful line of a push, used as the recorded notification title."""
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped or _HIDDEN_META_RE.match(stripped):
            continue
        stripped = stripped.lstrip("#>*- ").rstrip("* ").strip()
        if stripped:
            return stripped[:_TITLE_MAX_CHARS]
    return route_type or "notification"


def record_push(
    target: FeishuTarget,
    content: str,
    *,
    route_type: Optional[str] = None,
    sent: Iterable[Tuple[str, Optional[str]]] = (),
    repo: Optional[FeishuBindingRepository] = None,
    db_manager: Any = None,
) -> Optional[str]:
    """Record a delivered push as an assistant message of the target chat's session.

    ``sent`` holds ``(message_id, chat_id)`` pairs from the send responses; they
    link the pushed messages to the session so replies continue it. Never raises:
    recording is best effort and must not turn a delivered push into a failure.

    Returns the session id written to, or ``None`` when nothing was recorded.
    """
    try:
        sent_list = [(mid, cid) for mid, cid in sent if mid]
        session_id = target.session_id
        response_chat_id = next((cid for _, cid in sent_list if cid), None)
        if session_id is None and response_chat_id:
            # open_id target: the real chat id comes back with the send response.
            session_id = chat_session_id(response_chat_id)
        if session_id is None:
            logger.info("[Feishu] push delivered but its chat is unknown; not recorded into a session")
            return None

        if db_manager is None:
            from src.storage import DatabaseManager

            db_manager = DatabaseManager.get_instance()
        title = derive_push_title(content, route_type)
        db_manager.save_chat_notification_message(
            session_id,
            content,
            {
                "kind": "notification",
                "route_type": route_type or "",
                "title": title,
                "time": datetime.now().isoformat(timespec="seconds"),
            },
        )
        link_repo = repo or FeishuBindingRepository(db_manager)
        for message_id, chat_id in sent_list:
            link_repo.remember_message_session(
                message_id, chat_id or response_chat_id or target.chat_id, session_id, kind="push",
            )
        return session_id
    except Exception as exc:
        logger.warning("[Feishu] recording push into assistant session failed: %s", exc)
        return None
