# -*- coding: utf-8 -*-
"""Schemas for assistant notes (LLM-wiki pages) and recall status."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

NoteScope = Literal["global", "stock", "board"]


class NoteItem(BaseModel):
    id: int
    scope: NoteScope
    subject: Optional[str] = None
    title: str
    body_md: str
    version: int
    updated_at: Optional[str] = None


class NoteUsage(BaseModel):
    total_chars: int
    inject_max_chars: int
    needs_tidy: bool


class NoteListResponse(BaseModel):
    notes: List[NoteItem]
    usage: NoteUsage


class NotePageResponse(BaseModel):
    """``note`` is null when the page has not been written yet."""

    note: Optional[NoteItem] = None


class NoteEditRequest(BaseModel):
    scope: NoteScope
    subject: Optional[str] = Field(default=None, max_length=32)
    title: Optional[str] = Field(default=None, max_length=64)
    body_md: str
    expected_version: Optional[int] = Field(
        default=None, ge=0, description="Current page version (0 = page does not exist); mismatch returns 409."
    )


class NoteRevisionItem(BaseModel):
    id: int
    note_id: int
    version: int
    body_md: str
    source: Literal["user_said", "suggested", "manual_edit"]
    origin_session_id: Optional[str] = None
    origin_message_id: Optional[int] = None
    created_at: Optional[str] = None


class NoteRevisionsResponse(BaseModel):
    revisions: List[NoteRevisionItem]


class NoteRevertRequest(BaseModel):
    version: int = Field(ge=1)
    expected_version: Optional[int] = Field(default=None, ge=0)


class NoteProposalRequest(BaseModel):
    scope: NoteScope
    subject: Optional[str] = Field(default=None, max_length=32)
    title: Optional[str] = Field(default=None, max_length=64)
    body_md: str = Field(description="Full new page text; the diff against the current page is computed server-side.")
    source: Literal["user_said", "suggested"] = "user_said"
    origin_session_id: Optional[str] = Field(default=None, max_length=100)
    origin_message_id: Optional[int] = None


class NoteProposalItem(BaseModel):
    proposal_id: str
    scope: NoteScope
    subject: Optional[str] = None
    title: str
    base_version: int
    before_md: str
    after_md: str
    diff: str
    source: Literal["user_said", "suggested"]
    origin_session_id: Optional[str] = None
    origin_message_id: Optional[int] = None
    status: Literal["pending", "confirmed", "rejected"]
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None


class NoteProposalListResponse(BaseModel):
    proposals: List[NoteProposalItem]


class NoteDeleteResponse(BaseModel):
    deleted: bool


class NoteClearResponse(BaseModel):
    notes: int
    revisions: int
    proposals: int


class RecallStatus(BaseModel):
    """Long-term recall plugin status (additive field on ``GET /agent/status``)."""

    provider: str
    status: str  # disabled | ok | error | not_configured
    message: Optional[str] = None
    latency_ms: Optional[int] = None
    pending_retain: int = 0
    pending_forget: int = 0
