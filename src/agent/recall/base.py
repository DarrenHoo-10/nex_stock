# -*- coding: utf-8 -*-
"""Contracts of the pluggable long-term recall layer.

Recall is a weak dependency: callers must treat every provider failure as
"recall unavailable" and carry on with the chat turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Protocol, Sequence, runtime_checkable


class RecallError(Exception):
    """A recall provider could not complete a call (network, HTTP status, config)."""


@dataclass(frozen=True)
class RecallTurn:
    """One finished question/answer turn handed to ``retain``."""

    session_id: str
    message_id: int  # id of the user message that anchors the turn
    user_text: str
    assistant_text: str
    occurred_at: datetime
    tags: List[str] = field(default_factory=list)
    market_phase: Optional[str] = None

    @property
    def document_id(self) -> str:
        return f"{self.session_id}:{self.message_id}"


@dataclass(frozen=True)
class RecallQuery:
    """A per-question recall request."""

    text: str
    tags: List[str] = field(default_factory=list)
    limit: int = 5
    now: Optional[datetime] = None


@dataclass(frozen=True)
class RecallItem:
    """One recalled snippet, carrying the date it was said when known."""

    text: str
    date: Optional[str] = None  # YYYY-MM-DD when known
    document_id: Optional[str] = None
    score: Optional[float] = None


@dataclass(frozen=True)
class RecallHealth:
    """Status shown on the data-status surfaces."""

    provider_id: str
    status: str  # disabled | ok | error | not_configured
    message: Optional[str] = None
    latency_ms: Optional[int] = None
    pending_retain: int = 0
    pending_forget: int = 0


@runtime_checkable
class RecallProvider(Protocol):
    """Pluggable long-term recall backend."""

    provider_id: str

    def retain(self, turn: RecallTurn) -> None:
        """Write one turn. Raise ``RecallError`` on failure."""

    def recall(self, query: RecallQuery) -> Sequence[RecallItem]:
        """Return relevant past snippets. Raise ``RecallError`` on failure."""

    def forget(self, document_ids: List[str]) -> None:
        """Delete retained documents. Missing documents are not an error."""

    def health(self) -> RecallHealth:
        """Cheap liveness/configuration probe; must not raise."""
