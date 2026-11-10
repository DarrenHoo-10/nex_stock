# -*- coding: utf-8 -*-
"""Assemble the assistant-memory block of a chat turn (notes + long-term recall).

Priority is fixed: ledger / rules / tool facts > notes > recall. Everything here
is best effort: any failure degrades to "no memory block content" and the chat
turn proceeds unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.agent.recall import (
    RECALL_STATUS_DISABLED,
    RECALL_STATUS_UNAVAILABLE,
    RECALL_STATUS_USED,
    RecallItem,
    RecallQuery,
    RecallTurn,
    get_recall_service,
)

logger = logging.getLogger(__name__)

RECALL_ITEM_MAX_CHARS = 300
RECALL_TOP_K = 5
NOT_USED_EVIDENCE = "未使用长期回忆"

_ENTRY_START_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")


@dataclass(frozen=True)
class MemoryInjection:
    """What was injected into one chat turn, plus data needed afterwards."""

    section: str = ""
    note_titles: List[str] = field(default_factory=list)
    needs_tidy: bool = False
    recall_status: str = RECALL_STATUS_DISABLED
    recall_reason: Optional[str] = None
    recall_items: List[RecallItem] = field(default_factory=list)
    object_tags: List[str] = field(default_factory=list)
    market_phase: Optional[str] = None
    context_snapshot: Optional[Dict[str, Any]] = None


# -- notes ---------------------------------------------------------------


def split_entries(body_md: str) -> List[str]:
    """Split a page into whole entries: a bullet plus its continuation lines."""
    entries: List[str] = []
    for line in body_md.splitlines():
        if not line.strip():
            continue
        if _ENTRY_START_RE.match(line) or not entries or not line[:1].isspace():
            entries.append(line.rstrip())
        else:
            entries[-1] += "\n" + line.rstrip()
    return entries


def fit_pages(pages: List[Dict[str, Any]], max_chars: int) -> Tuple[List[Tuple[str, str]], bool]:
    """Render pages in priority order within ``max_chars``.

    Returns ``([(title, text)], needs_tidy)``. A page that fits is taken whole.
    When it does not, only whole leading entries that fit are included (never a
    partial entry) and ``needs_tidy`` is set.
    """
    rendered: List[Tuple[str, str]] = []
    needs_tidy = False
    remaining = max_chars
    for page in pages:
        body = (page.get("body_md") or "").strip()
        if not body:
            continue
        header = f"#### {page['title']}\n"
        if len(header) + len(body) <= remaining:
            rendered.append((page["title"], header + body))
            remaining -= len(header) + len(body) + 1
            continue
        needs_tidy = True
        kept: List[str] = []
        used = len(header)
        for entry in split_entries(body):
            if used + len(entry) + 1 > remaining:
                break
            kept.append(entry)
            used += len(entry) + 1
        if kept:
            rendered.append((page["title"], header + "\n".join(kept)))
            remaining -= used
    return rendered, needs_tidy


def _collect_pages(db, context: Dict[str, Any], user_id: str) -> List[Dict[str, Any]]:
    from src.services.assistant_note_service import (
        AssistantNoteService, SCOPE_BOARD, SCOPE_GLOBAL, SCOPE_STOCK,
    )

    service = AssistantNoteService(db, user_id=user_id)
    wanted: List[Tuple[str, Optional[str]]] = [(SCOPE_GLOBAL, None)]
    stock_code = str(context.get("stock_code") or "").strip()
    if stock_code:
        wanted.append((SCOPE_STOCK, stock_code))
    board_id = str(context.get("board_id") or "").strip()
    if board_id:
        wanted.append((SCOPE_BOARD, board_id))
    pages = []
    for scope, subject in wanted:
        try:
            page = service.get_page(scope, subject)
        except Exception as exc:
            logger.warning("Assistant note %s/%s could not be read: %s", scope, subject, exc)
            continue
        if page:
            pages.append(page)
    return pages


# -- context object / snapshot --------------------------------------------


def object_tags_for(context: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    stock_code = str(context.get("stock_code") or "").strip().upper()
    if stock_code:
        tags.append(f"stock:{stock_code}")
    board_id = str(context.get("board_id") or "").strip()
    if board_id:
        tags.append(f"board:{board_id}")
    return tags


def build_context_snapshot(context: Dict[str, Any], now: datetime) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Market phase, data time and context object for ``conversation_messages.context_snapshot``."""
    phase_ctx: Optional[Dict[str, Any]] = None
    raw = context.get("market_phase_context")
    if isinstance(raw, dict) and raw:
        phase_ctx = raw
    else:
        try:
            from src.core.trading_calendar import build_market_phase_context, get_market_for_stock

            market = get_market_for_stock(str(context.get("stock_code") or "")) or "cn"
            phase_ctx = build_market_phase_context(market=market, current_time=None).to_dict()
        except Exception as exc:
            logger.debug("Market phase unavailable for context snapshot: %s", exc)
    obj = {key: context[key] for key in ("stock_code", "stock_name", "board_id") if context.get(key)}
    snapshot: Dict[str, Any] = {"captured_at": now.isoformat()}
    phase = None
    if phase_ctx:
        phase = phase_ctx.get("phase")
        snapshot.update({
            "market_phase": phase,
            "market": phase_ctx.get("market"),
            "data_time": phase_ctx.get("market_local_time"),
            "effective_daily_bar_date": phase_ctx.get("effective_daily_bar_date"),
        })
    if obj:
        snapshot["context_object"] = obj
    return snapshot, phase


# -- prompt ----------------------------------------------------------------

_RULES = (
    "## 助手记忆（辅助参考）\n\n"
    "使用规则：\n"
    "1. 优先级固定：账本、仓位规则和工具读到的当前数据 > 用户笔记 > 过往对话回忆；冲突时以前者为准。\n"
    "2. 笔记和回忆里的价格、持仓、涨跌幅等数字都是当时的历史数字，回答前必须重新通过工具取当前值，不得当作现在的数字。\n"
    "3. 持仓、成本、现金、仓位规则、次日计划、板块判定不从笔记或回忆里取，以工具读到的当前值为准。\n"
    "4. 笔记和回忆只作背景参考，不要当作用户本轮说的话复述。\n"
)


def _render_section(
    notes: List[Tuple[str, str]], needs_tidy: bool, status: str, reason: Optional[str], items: List[RecallItem],
) -> str:
    has_content = bool(notes) or (status == RECALL_STATUS_USED and bool(items))
    parts = [_RULES if has_content else "## 助手记忆"]
    if notes:
        parts.append("### 用户笔记（用户确认过的内容）\n" + "\n\n".join(text for _, text in notes))
    if needs_tidy:
        parts.append("（笔记总长度超出上限，部分条目没有载入。可以提醒用户到「助手笔记」里整理。）")
    if status == RECALL_STATUS_USED and items:
        lines = []
        for item in items:
            text = " ".join(item.text.split())
            if len(text) > RECALL_ITEM_MAX_CHARS:
                text = text[:RECALL_ITEM_MAX_CHARS] + "…"
            lines.append(f"- [{item.date or '日期未知'}] {text}")
        parts.append("### 过往对话（长期回忆，仅供参考，数字均为当时的历史数字）\n" + "\n".join(lines))
    elif status == RECALL_STATUS_USED:
        parts.append("### 过往对话\n已查询长期回忆，没有相关内容。")
    else:
        why = {"timeout": "查询超时", "error": "服务不可用", "not_configured": "未配置完整"}.get(reason or "", "")
        detail = f"（{why}）" if why else ""
        parts.append(f"### 长期回忆状态\n本次{NOT_USED_EVIDENCE}{detail}。回答的「依据」里请注明「{NOT_USED_EVIDENCE}」。")
    return "\n\n".join(parts) + "\n"


def _recall_service_for(user_id: str, config: Any):
    """Long-term recall service of one user.

    The primary user keeps the system Hindsight bank (``HINDSIGHT_BANK_ID``). Every other user gets a
    service of their own: the system Hindsight server with a bank derived from the configured bank id and
    their user id, or their own Hindsight endpoint when they saved one (docs/assistant-memory.md). Two users
    never share a bank, and an unconfigured provider stays disabled.
    """
    from src.config import get_primary_user_id

    if user_id == get_primary_user_id():
        return get_recall_service(config)
    from src.agent.recall import get_user_recall_service
    from src.services.user_config_service import get_user_config

    return get_user_recall_service(user_id, get_user_config(user_id))


def build_memory_injection(
    *,
    message: str,
    effective_context: Optional[Dict[str, Any]],
    db_manager: Any = None,
    config: Any = None,
    now: Optional[datetime] = None,
    user_id: Optional[str] = None,
) -> MemoryInjection:
    """Never raises; a failure yields an empty-ish injection.

    ``user_id`` is the session owner whose notes are injected (``None`` = primary user: bots, CLI and
    other context-less callers).
    """
    from src.config import get_primary_user_id

    user_id = user_id or get_primary_user_id()
    context = dict(effective_context or {})
    now = now or datetime.now()
    try:
        if config is None:
            from src.config import get_config

            config = get_config()
        max_chars = int(getattr(config, "assistant_note_inject_max_chars", 3000) or 3000)

        notes: List[Tuple[str, str]] = []
        needs_tidy = False
        try:
            from src.storage import DatabaseManager

            db = db_manager or DatabaseManager.get_instance()
            notes, needs_tidy = fit_pages(_collect_pages(db, context, user_id), max_chars)
        except Exception as exc:
            logger.warning("Assistant notes injection skipped: %s", exc)

        tags = object_tags_for(context)
        service = _recall_service_for(user_id, config)
        outcome = service.recall(RecallQuery(text=message, tags=tags, limit=RECALL_TOP_K, now=now))

        snapshot, phase = build_context_snapshot(context, now)
        if snapshot is not None:
            snapshot["recall_status"] = outcome.status
            if notes:
                snapshot["notes_injected"] = [title for title, _ in notes]
            if needs_tidy:
                snapshot["notes_need_tidy"] = True
        return MemoryInjection(
            section=_render_section(notes, needs_tidy, outcome.status, outcome.reason, outcome.items),
            note_titles=[title for title, _ in notes],
            needs_tidy=needs_tidy,
            recall_status=outcome.status,
            recall_reason=outcome.reason,
            recall_items=list(outcome.items),
            object_tags=tags,
            market_phase=phase,
            context_snapshot=snapshot,
        )
    except Exception as exc:  # the chat turn must never fail because of memory
        logger.warning("Assistant memory injection failed; continuing without it: %s", exc)
        return MemoryInjection(
            section=_render_section([], False, RECALL_STATUS_UNAVAILABLE, "error", []),
            recall_status=RECALL_STATUS_UNAVAILABLE,
            recall_reason="error",
        )


def retain_chat_turn(
    *,
    session_id: str,
    user_message_id: Optional[int],
    user_text: str,
    assistant_text: str,
    memory: Optional[MemoryInjection],
    config: Any = None,
    user_id: Optional[str] = None,
) -> None:
    """Queue a finished turn for long-term retention; never raises, never blocks.

    ``user_id`` is the session owner (``None`` = primary user); only the primary user's turns are retained.
    """
    if user_message_id is None or not assistant_text:
        return
    try:
        from src.config import get_primary_user_id

        service = _recall_service_for(user_id or get_primary_user_id(), config)
        if not service.configured:
            return
        service.retain_async(RecallTurn(
            session_id=session_id,
            message_id=int(user_message_id),
            user_text=user_text,
            assistant_text=assistant_text,
            occurred_at=datetime.now(),
            tags=list(memory.object_tags) if memory else [],
            market_phase=memory.market_phase if memory else None,
        ))
    except Exception as exc:
        logger.warning("Recall retain was not queued: %s", exc)
