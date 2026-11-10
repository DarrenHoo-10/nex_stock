# -*- coding: utf-8 -*-
"""Recall provider registry (same shape as ``src/llm/backend_registry.py``)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable, Dict

from src.agent.recall.base import RecallProvider
from src.agent.recall.none_provider import NONE_PROVIDER_ID, NoneRecallProvider

logger = logging.getLogger(__name__)

HINDSIGHT_PROVIDER_ID = "hindsight"
SUPPORTED_RECALL_PROVIDERS = frozenset({NONE_PROVIDER_ID, HINDSIGHT_PROVIDER_ID})


def _read(config: Any, name: str, default: Any) -> Any:
    if isinstance(config, Mapping):
        return config.get(name, default)
    try:
        values = vars(config)
    except TypeError:
        values = {}
    if name in values:
        return values[name]
    try:
        return object.__getattribute__(config, name)
    except AttributeError:
        return default


def resolve_recall_provider_id(config: Any) -> str:
    """Configured provider id; unknown values fall back to ``none`` with a warning."""
    provider_id = str(_read(config, "assistant_recall_provider", NONE_PROVIDER_ID) or "").strip().lower()
    provider_id = provider_id or NONE_PROVIDER_ID
    if provider_id not in SUPPORTED_RECALL_PROVIDERS:
        logger.warning("Unsupported ASSISTANT_RECALL_PROVIDER=%r; long-term recall disabled", provider_id)
        return NONE_PROVIDER_ID
    return provider_id


def _create_none(config: Any) -> RecallProvider:
    return NoneRecallProvider()


def _create_hindsight(config: Any) -> RecallProvider:
    # Imported only when selected: nothing Hindsight-specific loads otherwise.
    from src.agent.recall.hindsight_provider import HindsightRecallProvider

    return HindsightRecallProvider(
        base_url=_read(config, "hindsight_base_url", "") or "",
        bank_id=_read(config, "hindsight_bank_id", "") or "",
        api_key=_read(config, "hindsight_api_key", None),
        timeout_s=resolve_recall_timeout_s(config),
        # Only the per-user settings of a user-supplied server (``user_recall_settings``) turn this off.
        allow_redirects=bool(_read(config, "hindsight_allow_redirects", True)),
    )


_FACTORIES: Dict[str, Callable[[Any], RecallProvider]] = {
    NONE_PROVIDER_ID: _create_none,
    HINDSIGHT_PROVIDER_ID: _create_hindsight,
}


def resolve_recall_timeout_s(config: Any) -> float:
    try:
        value = float(_read(config, "assistant_recall_timeout_s", 2.0))
    except (TypeError, ValueError):
        return 2.0
    return value if value > 0 else 2.0


def create_recall_provider(config: Any) -> RecallProvider:
    """Build the provider selected by config; any construction error means ``none``."""
    provider_id = resolve_recall_provider_id(config)
    try:
        return _FACTORIES[provider_id](config)
    except Exception as exc:
        logger.warning("Recall provider %s could not be created, using none: %s", provider_id, exc)
        return NoneRecallProvider()
