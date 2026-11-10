# -*- coding: utf-8 -*-
"""Assistant notes: one Markdown page per scope (LLM-wiki style).

Writes happen only through one of three paths: a user-confirmed proposal, a
direct manual edit, or a revert. Every write bumps ``version`` and records an
``assistant_note_revision`` row. Proposals are stored separately and never
touch the page until confirmed; confirmation is guarded by the version the
proposal was based on (optimistic concurrency).
"""

from __future__ import annotations

import difflib
import uuid
from datetime import date, datetime, time as dt_time
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, select

from src.config import get_primary_user_id
from src.storage import (
    AssistantNote,
    AssistantNoteProposal,
    AssistantNoteRevision,
    DatabaseManager,
)

SCOPE_GLOBAL = "global"
SCOPE_STOCK = "stock"
SCOPE_BOARD = "board"
NOTE_SCOPES = (SCOPE_GLOBAL, SCOPE_STOCK, SCOPE_BOARD)

SOURCE_USER_SAID = "user_said"
SOURCE_SUGGESTED = "suggested"
SOURCE_MANUAL_EDIT = "manual_edit"
PROPOSAL_SOURCES = (SOURCE_USER_SAID, SOURCE_SUGGESTED)

GLOBAL_PAGE_TITLE = "偏好"
NOTE_BODY_MAX_CHARS = 20000
NOTE_TITLE_MAX_CHARS = 64
NOTE_SUBJECT_MAX_CHARS = 32

PROPOSAL_PENDING = "pending"
PROPOSAL_CONFIRMED = "confirmed"
PROPOSAL_REJECTED = "rejected"


class NoteError(Exception):
    """Base class; ``code`` is stable for API clients."""

    code = "note_error"

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)
        self.message = message or self.code


class NoteValidationError(NoteError):
    code = "invalid_note"


class NoteNotFound(NoteError):
    code = "note_not_found"


class NoteVersionConflict(NoteError):
    code = "note_version_conflict"

    def __init__(self, current_version: int, message: str = ""):
        super().__init__(message or "笔记已被修改，请刷新后重试")
        self.current_version = current_version


class ProposalNotFound(NoteError):
    code = "proposal_not_found"


class ProposalNotPending(NoteError):
    code = "proposal_not_pending"


class ProposalLimitReached(NoteError):
    code = "proposal_limit_reached"


def normalize_scope_subject(scope: Any, subject: Any) -> tuple[str, Optional[str]]:
    scope_text = str(scope or "").strip().lower()
    if scope_text not in NOTE_SCOPES:
        raise NoteValidationError(f"scope 必须是 {'/'.join(NOTE_SCOPES)}")
    subject_text = str(subject).strip() if subject is not None else ""
    if scope_text == SCOPE_GLOBAL:
        if subject_text:
            raise NoteValidationError("global 页面不能带 subject")
        return scope_text, None
    if not subject_text:
        raise NoteValidationError(f"{scope_text} 页面必须提供 subject")
    if len(subject_text) > NOTE_SUBJECT_MAX_CHARS:
        raise NoteValidationError("subject 过长")
    if scope_text == SCOPE_STOCK:
        subject_text = subject_text.upper()
    return scope_text, subject_text


def _validate_body(body_md: Any) -> str:
    if not isinstance(body_md, str):
        raise NoteValidationError("body_md 必须是字符串")
    if len(body_md) > NOTE_BODY_MAX_CHARS:
        raise NoteValidationError(f"笔记正文不能超过 {NOTE_BODY_MAX_CHARS} 字符")
    return body_md


def _resolve_title(scope: str, subject: Optional[str], title: Optional[str], fallback: Optional[str] = None) -> str:
    candidate = (title or "").strip() or (fallback or "").strip()
    if not candidate:
        candidate = GLOBAL_PAGE_TITLE if scope == SCOPE_GLOBAL else str(subject)
    if len(candidate) > NOTE_TITLE_MAX_CHARS:
        raise NoteValidationError(f"标题不能超过 {NOTE_TITLE_MAX_CHARS} 字符")
    return candidate


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _note_dict(note: AssistantNote) -> Dict[str, Any]:
    return {
        "id": int(note.id),
        "scope": note.scope,
        "subject": note.subject,
        "title": note.title,
        "body_md": note.body_md or "",
        "version": int(note.version),
        "updated_at": _iso(note.updated_at),
    }


def _revision_dict(row: AssistantNoteRevision) -> Dict[str, Any]:
    return {
        "id": int(row.id),
        "note_id": int(row.note_id),
        "version": int(row.version),
        "body_md": row.body_md or "",
        "source": row.source,
        "origin_session_id": row.origin_session_id,
        "origin_message_id": row.origin_message_id,
        "created_at": _iso(row.created_at),
    }


def _proposal_dict(row: AssistantNoteProposal) -> Dict[str, Any]:
    return {
        "proposal_id": row.id,
        "scope": row.scope,
        "subject": row.subject,
        "title": row.title,
        "base_version": int(row.base_version),
        "before_md": row.before_md or "",
        "after_md": row.after_md or "",
        "diff": make_diff(row.before_md or "", row.after_md or ""),
        "source": row.source,
        "origin_session_id": row.origin_session_id,
        "origin_message_id": row.origin_message_id,
        "status": row.status,
        "created_at": _iso(row.created_at),
        "resolved_at": _iso(row.resolved_at),
    }


def make_diff(before_md: str, after_md: str) -> str:
    """Unified line diff shown to the user before they confirm."""
    return "\n".join(difflib.unified_diff(
        before_md.splitlines(), after_md.splitlines(),
        fromfile="修改前", tofile="修改后", lineterm="", n=1,
    ))


class AssistantNoteService:
    """Notes, revisions and proposals of ONE user.

    Bound to ``user_id`` at construction (API: the authenticated user; ``None`` = primary user for
    bots, CLI and other context-less callers; resolved once). A page, revision or proposal owned by
    another user behaves exactly like a missing one (``NoteNotFound`` / ``ProposalNotFound``).
    """

    def __init__(
        self, db_manager: Optional[DatabaseManager] = None, config: Any = None, user_id: Optional[str] = None,
    ):
        self.db = db_manager or DatabaseManager.get_instance()
        self._config = config
        self.user_id = user_id or get_primary_user_id()

    # -- config ----------------------------------------------------------

    def _cfg(self, name: str, default: Any) -> Any:
        config = self._config
        if config is None:
            # Note limits (ASSISTANT_NOTE_*) are personal settings: a non-primary user's own view applies.
            if self.user_id != get_primary_user_id():
                from src.services.user_config_service import get_user_config

                config = get_user_config(self.user_id)
            else:
                from src.config import get_config

                config = get_config()
        value = getattr(config, name, default)
        return default if value is None else value

    @property
    def daily_proposal_limit(self) -> int:
        return int(self._cfg("assistant_note_daily_proposal_limit", 2))

    @property
    def inject_max_chars(self) -> int:
        return int(self._cfg("assistant_note_inject_max_chars", 3000))

    # -- reads -----------------------------------------------------------

    def get_page(self, scope: str, subject: Optional[str] = None) -> Optional[Dict[str, Any]]:
        scope, subject = normalize_scope_subject(scope, subject)
        with self.db.get_session() as session:
            note = self._find(session, scope, subject)
            return _note_dict(note) if note else None

    def list_pages(self, scope: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(AssistantNote).where(AssistantNote.user_id == self.user_id).order_by(
                AssistantNote.scope, AssistantNote.subject, AssistantNote.id,
            )
            if scope:
                scope_text = str(scope).strip().lower()
                if scope_text not in NOTE_SCOPES:
                    raise NoteValidationError(f"scope 必须是 {'/'.join(NOTE_SCOPES)}")
                stmt = stmt.where(AssistantNote.scope == scope_text)
            return [_note_dict(note) for note in session.execute(stmt).scalars()]

    def list_revisions(self, note_id: int) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            if self._owned_note(session, note_id) is None:
                raise NoteNotFound()
            rows = session.execute(
                select(AssistantNoteRevision)
                .where(AssistantNoteRevision.note_id == note_id)
                .order_by(AssistantNoteRevision.version.desc())
            ).scalars().all()
            return [_revision_dict(row) for row in rows]

    def list_proposals(self, status: Optional[str] = PROPOSAL_PENDING) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            stmt = select(AssistantNoteProposal).where(
                AssistantNoteProposal.user_id == self.user_id,
            ).order_by(AssistantNoteProposal.created_at.desc())
            if status:
                stmt = stmt.where(AssistantNoteProposal.status == status)
            return [_proposal_dict(row) for row in session.execute(stmt).scalars()]

    def usage_summary(self) -> Dict[str, Any]:
        """Total note size against the injection cap (drives the 'needs tidy' hint)."""
        with self.db.get_session() as session:
            total = session.execute(
                select(func.coalesce(func.sum(func.length(AssistantNote.body_md)), 0)).where(
                    AssistantNote.user_id == self.user_id,
                )
            ).scalar_one()
        limit = self.inject_max_chars
        return {"total_chars": int(total or 0), "inject_max_chars": limit, "needs_tidy": int(total or 0) > limit}

    def _owned_note(self, session, note_id: int) -> Optional[AssistantNote]:
        note = session.get(AssistantNote, note_id)
        return note if note is not None and note.user_id == self.user_id else None

    def _owned_proposal(self, session, proposal_id: str) -> Optional[AssistantNoteProposal]:
        proposal = session.get(AssistantNoteProposal, proposal_id)
        return proposal if proposal is not None and proposal.user_id == self.user_id else None

    def _find(self, session, scope: str, subject: Optional[str]) -> Optional[AssistantNote]:
        stmt = select(AssistantNote).where(AssistantNote.user_id == self.user_id, AssistantNote.scope == scope)
        stmt = stmt.where(AssistantNote.subject.is_(None) if subject is None else AssistantNote.subject == subject)
        return session.execute(stmt).scalars().first()

    # -- proposals -------------------------------------------------------

    def propose_edit(
        self,
        scope: str,
        subject: Optional[str],
        body_md: str,
        *,
        source: str = SOURCE_USER_SAID,
        title: Optional[str] = None,
        origin_session_id: Optional[str] = None,
        origin_message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a pending proposal (page untouched) holding the full new page text."""
        scope, subject = normalize_scope_subject(scope, subject)
        body_md = _validate_body(body_md)
        if source not in PROPOSAL_SOURCES:
            raise NoteValidationError(f"source 必须是 {'/'.join(PROPOSAL_SOURCES)}")
        limit = self.daily_proposal_limit

        def write(session) -> Dict[str, Any]:
            if source == SOURCE_SUGGESTED:
                start = datetime.combine(date.today(), dt_time.min)
                used = session.execute(
                    select(func.count(AssistantNoteProposal.id)).where(
                        AssistantNoteProposal.user_id == self.user_id,
                        AssistantNoteProposal.source == SOURCE_SUGGESTED,
                        AssistantNoteProposal.created_at >= start,
                    )
                ).scalar_one()
                if used >= limit:
                    raise ProposalLimitReached(f"助手每天最多主动提议 {limit} 次笔记修改")
            note = self._find(session, scope, subject)
            before = note.body_md if note else ""
            if before == body_md:
                raise NoteValidationError("修改内容与当前页面相同")
            proposal = AssistantNoteProposal(
                id=str(uuid.uuid4()),
                user_id=self.user_id,
                scope=scope,
                subject=subject,
                title=_resolve_title(scope, subject, title, note.title if note else None),
                base_version=int(note.version) if note else 0,
                before_md=before,
                after_md=body_md,
                source=source,
                origin_session_id=origin_session_id,
                origin_message_id=origin_message_id,
                status=PROPOSAL_PENDING,
                created_at=datetime.now(),
            )
            session.add(proposal)
            session.flush()
            return _proposal_dict(proposal)

        return self.db._run_write_transaction("assistant_note_propose", write)

    def confirm_proposal(self, proposal_id: str) -> Dict[str, Any]:
        def write(session) -> Dict[str, Any]:
            proposal = self._owned_proposal(session, proposal_id)
            if proposal is None:
                raise ProposalNotFound()
            if proposal.status != PROPOSAL_PENDING:
                raise ProposalNotPending("该提议已处理")
            note = self._find(session, proposal.scope, proposal.subject)
            current = int(note.version) if note else 0
            if current != int(proposal.base_version):
                raise NoteVersionConflict(current, "页面在提议生成后被修改，请重新生成提议")
            note = self._write_version(
                session, note, proposal.scope, proposal.subject, proposal.title, proposal.after_md,
                source=proposal.source,
                origin_session_id=proposal.origin_session_id,
                origin_message_id=proposal.origin_message_id,
            )
            proposal.status = PROPOSAL_CONFIRMED
            proposal.resolved_at = datetime.now()
            return _note_dict(note)

        return self.db._run_write_transaction("assistant_note_confirm", write)

    def reject_proposal(self, proposal_id: str) -> Dict[str, Any]:
        def write(session) -> Dict[str, Any]:
            proposal = self._owned_proposal(session, proposal_id)
            if proposal is None:
                raise ProposalNotFound()
            if proposal.status != PROPOSAL_PENDING:
                raise ProposalNotPending("该提议已处理")
            proposal.status = PROPOSAL_REJECTED
            proposal.resolved_at = datetime.now()
            return _proposal_dict(proposal)

        return self.db._run_write_transaction("assistant_note_reject", write)

    # -- direct writes ---------------------------------------------------

    def manual_edit(
        self,
        scope: str,
        subject: Optional[str],
        body_md: str,
        *,
        title: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create or overwrite a page directly (Web editor)."""
        scope, subject = normalize_scope_subject(scope, subject)
        body_md = _validate_body(body_md)

        def write(session) -> Dict[str, Any]:
            note = self._find(session, scope, subject)
            current = int(note.version) if note else 0
            if expected_version is not None and int(expected_version) != current:
                raise NoteVersionConflict(current)
            if note is not None and note.body_md == body_md and (not title or title.strip() == note.title):
                return _note_dict(note)
            note = self._write_version(
                session, note, scope, subject,
                _resolve_title(scope, subject, title, note.title if note else None), body_md,
                source=SOURCE_MANUAL_EDIT,
            )
            return _note_dict(note)

        return self.db._run_write_transaction("assistant_note_manual_edit", write)

    def revert(self, note_id: int, version: int, *, expected_version: Optional[int] = None) -> Dict[str, Any]:
        """Write a new version whose body equals an earlier revision."""
        def write(session) -> Dict[str, Any]:
            note = self._owned_note(session, note_id)
            if note is None:
                raise NoteNotFound()
            if expected_version is not None and int(expected_version) != int(note.version):
                raise NoteVersionConflict(int(note.version))
            revision = session.execute(
                select(AssistantNoteRevision).where(
                    AssistantNoteRevision.note_id == note_id,
                    AssistantNoteRevision.version == version,
                )
            ).scalars().first()
            if revision is None:
                raise NoteNotFound("历史版本不存在")
            note = self._write_version(
                session, note, note.scope, note.subject, note.title, revision.body_md or "",
                source=SOURCE_MANUAL_EDIT,
            )
            return _note_dict(note)

        return self.db._run_write_transaction("assistant_note_revert", write)

    def delete_page(self, scope: str, subject: Optional[str] = None) -> bool:
        scope, subject = normalize_scope_subject(scope, subject)

        def write(session) -> bool:
            note = self._find(session, scope, subject)
            if note is None:
                return False
            session.execute(delete(AssistantNoteRevision).where(AssistantNoteRevision.note_id == note.id))
            session.execute(delete(AssistantNote).where(AssistantNote.id == note.id))
            # Pending proposals were based on a page that no longer exists.
            for proposal in session.execute(select(AssistantNoteProposal).where(
                AssistantNoteProposal.user_id == self.user_id,
                AssistantNoteProposal.scope == scope,
                AssistantNoteProposal.status == PROPOSAL_PENDING,
            )).scalars():
                if proposal.subject == subject:
                    proposal.status = PROPOSAL_REJECTED
                    proposal.resolved_at = datetime.now()
            return True

        return self.db._run_write_transaction("assistant_note_delete", write)

    def clear_all(self) -> Dict[str, int]:
        def write(session) -> Dict[str, int]:
            own_note_ids = select(AssistantNote.id).where(AssistantNote.user_id == self.user_id)
            revisions = session.execute(
                delete(AssistantNoteRevision).where(AssistantNoteRevision.note_id.in_(own_note_ids))
            ).rowcount or 0
            notes = session.execute(delete(AssistantNote).where(AssistantNote.user_id == self.user_id)).rowcount or 0
            proposals = session.execute(
                delete(AssistantNoteProposal).where(AssistantNoteProposal.user_id == self.user_id)
            ).rowcount or 0
            return {"notes": notes, "revisions": revisions, "proposals": proposals}

        return self.db._run_write_transaction("assistant_note_clear_all", write)

    # -- internals -------------------------------------------------------

    def _write_version(
        self,
        session,
        note: Optional[AssistantNote],
        scope: str,
        subject: Optional[str],
        title: str,
        body_md: str,
        *,
        source: str,
        origin_session_id: Optional[str] = None,
        origin_message_id: Optional[int] = None,
    ) -> AssistantNote:
        now = datetime.now()
        if note is None:
            note = AssistantNote(
                user_id=self.user_id, scope=scope, subject=subject, title=title, body_md=body_md, version=1, updated_at=now,
            )
            session.add(note)
        else:
            note.title = title
            note.body_md = body_md
            note.version = int(note.version) + 1
            note.updated_at = now
        session.flush()
        session.add(AssistantNoteRevision(
            note_id=note.id, version=note.version, body_md=body_md, source=source,
            origin_session_id=origin_session_id, origin_message_id=origin_message_id, created_at=now,
        ))
        session.flush()
        return note
