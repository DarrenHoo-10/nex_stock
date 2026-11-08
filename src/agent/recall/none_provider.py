# -*- coding: utf-8 -*-
"""Default recall provider: does nothing."""

from __future__ import annotations

from typing import List, Sequence

from src.agent.recall.base import RecallHealth, RecallItem, RecallQuery, RecallTurn

NONE_PROVIDER_ID = "none"


class NoneRecallProvider:
    provider_id = NONE_PROVIDER_ID
    configured = False

    def retain(self, turn: RecallTurn) -> None:
        return None

    def recall(self, query: RecallQuery) -> Sequence[RecallItem]:
        return []

    def forget(self, document_ids: List[str]) -> None:
        return None

    def health(self) -> RecallHealth:
        return RecallHealth(provider_id=NONE_PROVIDER_ID, status="disabled", message="未启用长期回忆")
