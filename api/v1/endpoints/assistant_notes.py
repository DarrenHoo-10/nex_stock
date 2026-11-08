# -*- coding: utf-8 -*-
"""Assistant notes API (mounted under ``/api/v1/agent/notes``)."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.v1.schemas.assistant_notes import (
    NoteClearResponse,
    NoteDeleteResponse,
    NoteEditRequest,
    NoteItem,
    NoteListResponse,
    NotePageResponse,
    NoteProposalItem,
    NoteProposalListResponse,
    NoteProposalRequest,
    NoteRevertRequest,
    NoteRevisionsResponse,
)
from src.services.assistant_note_service import (
    AssistantNoteService,
    NoteError,
    NoteNotFound,
    NoteValidationError,
    NoteVersionConflict,
    ProposalLimitReached,
    ProposalNotFound,
    ProposalNotPending,
)

router = APIRouter()


def get_assistant_note_service() -> AssistantNoteService:
    return AssistantNoteService()


def _http_error(exc: NoteError) -> HTTPException:
    if isinstance(exc, (NoteNotFound, ProposalNotFound)):
        status = 404
    elif isinstance(exc, (NoteVersionConflict, ProposalNotPending)):
        status = 409
    elif isinstance(exc, ProposalLimitReached):
        status = 429
    elif isinstance(exc, NoteValidationError):
        status = 422
    else:
        status = 400
    detail = {"error": exc.code, "message": exc.message}
    if isinstance(exc, NoteVersionConflict):
        detail["current_version"] = exc.current_version
    return HTTPException(status_code=status, detail=detail)


@router.get("", response_model=NoteListResponse)
def list_notes(
    scope: Optional[str] = Query(default=None, description="global / stock / board"),
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    """List all note pages, plus total size against the injection cap."""
    try:
        return NoteListResponse(notes=service.list_pages(scope), usage=service.usage_summary())
    except NoteError as exc:
        raise _http_error(exc) from exc


@router.get("/page", response_model=NotePageResponse)
def get_note_page(
    scope: str,
    subject: Optional[str] = None,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    try:
        note = service.get_page(scope, subject)
    except NoteError as exc:
        raise _http_error(exc) from exc
    return NotePageResponse(note=NoteItem(**note) if note else None)


@router.put("/page", response_model=NoteItem)
def edit_note_page(
    request: NoteEditRequest,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    """Direct manual edit (create or overwrite). ``expected_version`` guards concurrent edits."""
    try:
        return NoteItem(**service.manual_edit(
            request.scope, request.subject, request.body_md,
            title=request.title, expected_version=request.expected_version,
        ))
    except NoteError as exc:
        raise _http_error(exc) from exc


@router.delete("/page", response_model=NoteDeleteResponse)
def delete_note_page(
    scope: str,
    subject: Optional[str] = None,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    try:
        return NoteDeleteResponse(deleted=service.delete_page(scope, subject))
    except NoteError as exc:
        raise _http_error(exc) from exc


@router.delete("", response_model=NoteClearResponse)
def clear_notes(
    confirm: bool = Query(default=False, description="Must be true to delete every page and revision."),
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail={"error": "confirmation_required", "message": "清空全部笔记需要 confirm=true"},
        )
    return NoteClearResponse(**service.clear_all())


@router.get("/{note_id}/revisions", response_model=NoteRevisionsResponse)
def list_note_revisions(
    note_id: int,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    try:
        return NoteRevisionsResponse(revisions=service.list_revisions(note_id))
    except NoteError as exc:
        raise _http_error(exc) from exc


@router.post("/{note_id}/revert", response_model=NoteItem)
def revert_note(
    note_id: int,
    request: NoteRevertRequest,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    """Write a new version whose body equals an earlier revision."""
    try:
        return NoteItem(**service.revert(
            note_id, request.version, expected_version=request.expected_version,
        ))
    except NoteError as exc:
        raise _http_error(exc) from exc


@router.get("/proposals", response_model=NoteProposalListResponse)
def list_note_proposals(
    status: Optional[str] = Query(default="pending"),
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    return NoteProposalListResponse(proposals=service.list_proposals(status))


@router.post("/proposals", response_model=NoteProposalItem)
def create_note_proposal(
    request: NoteProposalRequest,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    """Create a pending edit (page untouched) with a before/after diff for the user to confirm."""
    try:
        return NoteProposalItem(**service.propose_edit(
            request.scope, request.subject, request.body_md,
            source=request.source, title=request.title,
            origin_session_id=request.origin_session_id,
            origin_message_id=request.origin_message_id,
        ))
    except NoteError as exc:
        raise _http_error(exc) from exc


@router.post("/proposals/{proposal_id}/confirm", response_model=NoteItem)
def confirm_note_proposal(
    proposal_id: str,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    """Write the proposal; 409 when the page changed since the proposal was made."""
    try:
        return NoteItem(**service.confirm_proposal(proposal_id))
    except NoteError as exc:
        raise _http_error(exc) from exc


@router.post("/proposals/{proposal_id}/reject", response_model=NoteProposalItem)
def reject_note_proposal(
    proposal_id: str,
    service: AssistantNoteService = Depends(get_assistant_note_service),
):
    try:
        return NoteProposalItem(**service.reject_proposal(proposal_id))
    except NoteError as exc:
        raise _http_error(exc) from exc
