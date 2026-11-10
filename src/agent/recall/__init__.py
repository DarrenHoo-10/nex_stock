# -*- coding: utf-8 -*-
"""Pluggable long-term recall for the assistant (weak dependency).

The default ``none`` provider does nothing. The ``hindsight`` provider lives in
its own module and is imported only when selected.
"""

from __future__ import annotations

from typing import Optional, Sequence

from src.agent.recall.base import (
    RecallError,
    RecallHealth,
    RecallItem,
    RecallProvider,
    RecallQuery,
    RecallTurn,
)
from src.agent.recall.masking import mask_sensitive_numbers
from src.agent.recall.registry import create_recall_provider, resolve_recall_provider_id
from src.agent.recall.service import (
    RECALL_STATUS_DISABLED,
    RECALL_STATUS_UNAVAILABLE,
    RECALL_STATUS_USED,
    RecallOutcome,
    RecallService,
    derive_user_bank_id,
    get_recall_service,
    get_user_recall_service,
    reset_recall_service,
)


def forget_documents(document_ids: Sequence[str], user_id: Optional[str] = None) -> None:
    """Queue deletion of retained documents (no-op without a configured provider).

    ``user_id`` is the owner of the documents: the primary user (``None``) forgets in the system bank, any
    other user in their own bank only.
    """
    from src.config import get_primary_user_id

    if user_id and user_id != get_primary_user_id():
        from src.services.user_config_service import get_user_config

        get_user_recall_service(user_id, get_user_config(user_id)).forget_async(document_ids)
        return
    get_recall_service().forget_async(document_ids)


__all__ = [
    "RECALL_STATUS_DISABLED",
    "RECALL_STATUS_UNAVAILABLE",
    "RECALL_STATUS_USED",
    "RecallError",
    "RecallHealth",
    "RecallItem",
    "RecallOutcome",
    "RecallProvider",
    "RecallQuery",
    "RecallService",
    "RecallTurn",
    "create_recall_provider",
    "derive_user_bank_id",
    "forget_documents",
    "get_recall_service",
    "get_user_recall_service",
    "mask_sensitive_numbers",
    "reset_recall_service",
    "resolve_recall_provider_id",
]
