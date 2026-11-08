# -*- coding: utf-8 -*-
"""Feishu -> assistant bridge.

Runs one plain-text question through the same assistant path the Web chat uses
(session state, StockScope, notes injection, recall, market phase) and returns
the final answer text. Also implements the in-conversation memory flow:

* ``记住：…`` creates a user-initiated note proposal and asks for 「确认」/「取消」;
* a following ``确认`` / ``取消`` in the same session confirms / rejects it.

User-initiated proposals (``source=user_said``) are not subject to the daily
proposal limit: the service only counts ``suggested`` (assistant-initiated)
proposals, and this module never creates those.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from src.services.assistant_note_service import (
    GLOBAL_PAGE_TITLE,
    NOTE_TITLE_MAX_CHARS,
    SCOPE_GLOBAL,
    SCOPE_STOCK,
    SOURCE_USER_SAID,
    AssistantNoteService,
    NoteError,
    NoteVersionConflict,
)

logger = logging.getLogger(__name__)

PENDING_PROPOSAL_TTL = timedelta(hours=1)

_REMEMBER_RE = re.compile(r"^\s*记住\s*[：:，,]?\s*(?P<body>.+?)\s*$", re.DOTALL)
_QUESTION_ENDINGS = ("吗", "么", "？", "?", "呢")
_CONFIRM_WORDS = frozenset({"确认", "确定", "确认保存", "保存"})
_CANCEL_WORDS = frozenset({"取消", "放弃", "不用了", "算了"})
_TRAILING_PUNCT = " \t\r\n。.!！~"

KIND_ANSWER = "answer"
KIND_MEMORY = "memory"
KIND_ERROR = "error"


@dataclass(frozen=True)
class AssistantAnswer:
    """Text to show the user; ``success`` is False for failures (placeholder shows an error)."""

    text: str
    success: bool = True
    kind: str = KIND_ANSWER


class FeishuAssistantService:
    def __init__(self, config: Any = None, db_manager: Any = None):
        self._config = config
        self._db = db_manager
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- plumbing ---------------------------------------------------------

    def _get_config(self) -> Any:
        if self._config is not None:
            return self._config
        from src.config import get_config

        return get_config()

    def _get_db(self) -> Any:
        if self._db is not None:
            return self._db
        from src.storage import DatabaseManager

        return DatabaseManager.get_instance()

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    # -- public API -------------------------------------------------------

    def ask(self, session_id: str, text: str) -> AssistantAnswer:
        """Answer one message of ``session_id``. Never raises."""
        text = (text or "").strip()
        if not text:
            return AssistantAnswer("请直接发送你的问题。", success=False, kind=KIND_ERROR)
        # One turn at a time per session: a group's members share its session.
        with self._session_lock(session_id):
            try:
                memory_answer = self._handle_memory(session_id, text)
                if memory_answer is not None:
                    return memory_answer
                return self._run_agent(session_id, text)
            except Exception as exc:  # last resort: the placeholder must always resolve
                logger.exception("[Feishu Assistant] unexpected failure: %s", exc)
                return AssistantAnswer("分析失败，请稍后重试。", success=False, kind=KIND_ERROR)

    # -- assistant turn ---------------------------------------------------

    def _run_agent(self, session_id: str, text: str) -> AssistantAnswer:
        from fastapi import HTTPException

        from api.v1.endpoints.agent import (
            ChatRequest,
            _clarification_result,
            _prepare_chat_request,
            _select_agent_chat_backend,
        )
        from src.services.agent_chat_session_service import AgentChatSessionService
        from src.storage import ChatSessionStateConflict

        config = self._get_config()
        try:
            backend_id = _select_agent_chat_backend(config)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            reason = str(detail.get("message") or detail.get("error") or "助手不可用")
            return AssistantAnswer(f"助手当前不可用：{reason[:200]}", success=False, kind=KIND_ERROR)

        session_service = AgentChatSessionService(self._get_db())
        try:
            executor, turn, accepted, clarification = _prepare_chat_request(
                ChatRequest(message=text, session_id=session_id), config, session_id, session_service,
            )
            if clarification is not None:
                result = _clarification_result(session_service, accepted, clarification, backend_id)
            else:
                kwargs: Dict[str, Any] = {}
                if backend_id == "codex_app_server":
                    kwargs["cancel_event"] = threading.Event()
                result = executor.execute_turn(turn, **kwargs)
        except ChatSessionStateConflict:
            return AssistantAnswer("会话状态刚刚发生变化，请再发送一次。", success=False, kind=KIND_ERROR)
        except Exception as exc:
            logger.exception("[Feishu Assistant] agent turn failed: %s", exc)
            return AssistantAnswer("分析失败，请稍后重试。", success=False, kind=KIND_ERROR)

        if result.success and (result.content or "").strip():
            return AssistantAnswer(result.content)
        reason = str(result.error or "").strip()
        message = f"分析失败：{reason[:200]}" if reason else "分析失败，请稍后重试。"
        return AssistantAnswer(message, success=False, kind=KIND_ERROR)

    # -- memory: 记住 / 确认 / 取消 ---------------------------------------

    def _notes(self) -> AssistantNoteService:
        return AssistantNoteService(self._get_db(), config=self._config)

    def _handle_memory(self, session_id: str, text: str) -> Optional[AssistantAnswer]:
        normalized = text.strip(_TRAILING_PUNCT)
        if normalized in _CONFIRM_WORDS or normalized in _CANCEL_WORDS:
            pending = self._pending_proposal(session_id)
            if pending is None:
                return None  # not a reply to a proposal: treat as an ordinary message
            return self._resolve_proposal(pending, confirm=normalized in _CONFIRM_WORDS)

        match = _REMEMBER_RE.match(text)
        if not match:
            return None
        body = match.group("body").strip()
        if len(body) < 2 or body.endswith(_QUESTION_ENDINGS):
            return None  # "记住了" / "记住吗？" are conversation, not a request to store
        return self._propose_note(session_id, body)

    def _pending_proposal(self, session_id: str) -> Optional[Dict[str, Any]]:
        cutoff = datetime.now() - PENDING_PROPOSAL_TTL
        for proposal in self._notes().list_proposals("pending"):  # newest first
            if proposal.get("origin_session_id") != session_id or proposal.get("source") != SOURCE_USER_SAID:
                continue
            created = proposal.get("created_at")
            try:
                created_at = datetime.fromisoformat(created) if created else None
            except ValueError:
                created_at = None
            if created_at is not None and created_at >= cutoff:
                return proposal
        return None

    def _resolve_proposal(self, proposal: Dict[str, Any], *, confirm: bool) -> AssistantAnswer:
        notes = self._notes()
        label = self._page_label(proposal)
        try:
            if confirm:
                notes.confirm_proposal(proposal["proposal_id"])
                return AssistantAnswer(f"已保存到{label}。", kind=KIND_MEMORY)
            notes.reject_proposal(proposal["proposal_id"])
            return AssistantAnswer("已取消，没有修改笔记。", kind=KIND_MEMORY)
        except NoteVersionConflict:
            return AssistantAnswer(
                f"{label}在提议生成后被修改过，没有保存。请重新发送「记住：…」。", success=False, kind=KIND_MEMORY,
            )
        except NoteError as exc:
            return AssistantAnswer(f"没有保存：{exc.message}", success=False, kind=KIND_MEMORY)

    def _propose_note(self, session_id: str, body: str) -> AssistantAnswer:
        scope, subject, new_title = self._pick_page(session_id, body)
        notes = self._notes()
        try:
            page = notes.get_page(scope, subject)
            before = (page or {}).get("body_md") or ""
            entry = f"- {body}（{date.today().isoformat()}）"
            new_body = f"{before.rstrip()}\n{entry}" if before.strip() else entry
            proposal = notes.propose_edit(
                scope, subject, new_body,
                source=SOURCE_USER_SAID,
                title=None if page else new_title,
                origin_session_id=session_id,
            )
        except NoteError as exc:
            return AssistantAnswer(f"没有生成笔记提议：{exc.message}", success=False, kind=KIND_MEMORY)
        label = self._page_label(proposal)
        return AssistantAnswer(
            f"我准备在{label}新增一条：\n\n{entry}\n\n回复「确认」保存，或回复「取消」放弃。", kind=KIND_MEMORY,
        )

    @staticmethod
    def _page_label(proposal: Dict[str, Any]) -> str:
        if proposal.get("scope") == SCOPE_GLOBAL:
            return f"「{proposal.get('title') or GLOBAL_PAGE_TITLE}」页（全局）"
        return f"「{proposal.get('title') or proposal.get('subject')}」页"

    def _pick_page(self, session_id: str, body: str) -> tuple:
        """Global page, or the session's active stock page when the text is clearly about it."""
        try:
            from src.services.agent_chat_session_service import AgentChatSessionService

            active = AgentChatSessionService(self._get_db()).get_session_detail(session_id, 0).active_stock_context
        except Exception as exc:
            logger.debug("[Feishu Assistant] active stock lookup failed: %s", exc)
            active = None
        if active:
            code = str(active.get("stock_code") or "").strip()
            name = str(active.get("stock_name") or "").strip()
            mentioned = (code and code.upper() in body.upper()) or (name and name in body)
            if code and mentioned:
                title = f"{name} {code}".strip() if name else code
                return SCOPE_STOCK, code, title[:NOTE_TITLE_MAX_CHARS]
        return SCOPE_GLOBAL, None, None
