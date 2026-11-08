# -*- coding: utf-8 -*-
"""Read side of the news pool: digest, morning brief and source health.

Analysis (8:45 push, 17:00 board judgment, assistant) reads news only from storage through this
module; it never searches live. Consumers (stable contract):

``build_news_digest(since, until=None, codes=None, board_ids=None, min_importance=None, limit=30)``
    list of dicts with keys ``id, title, summary, source, published_at (ISO, China tz), scope,
    importance, is_breaking, board_ids, board_names, us_tickers, codes, url, kind``, sorted by
    ``is_breaking`` desc, ``importance`` desc, ``published_at`` desc; unclassified items
    (``importance`` None) come after the classified ones.

``get_morning_brief(day)``  -> the day's 财经早餐 item (digest keys plus ``content``) or ``None``.

``get_news_health()``       -> per-source last success / last error and the open gap windows.

Time conventions: ``since`` / ``until`` may be aware or naive (naive means China time). AKShare rows
are stored as naive China time; legacy rss / atom / newsnow rows are naive UTC. Announcements only
carry a date, so they are matched by calendar day (``since.date() <= date <= until.date()``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.repositories.intelligence_repo import IntelligenceRepository
from src.services.news_akshare_source import (
    CN_TZ,
    KIND_ANNOUNCEMENT,
    KIND_MORNING_BRIEF,
    NO_URL_PREFIX,
    SOURCE_LABELS,
    SOURCE_TYPE,
    normalize_a_share_code,
)
from src.storage import INTELLIGENCE_ITEM_NULL_SCOPE_VALUE

logger = logging.getLogger(__name__)

_DIGEST_SUMMARY_CHARS = 300
_UTC_SOURCE_TYPES = {"rss", "atom", "newsnow"}
_LEGACY_PAD = timedelta(hours=9)  # legacy rows are UTC; pad the SQL window, refine in Python
_SCOPE_FROM_ITEM_SCOPE = {"symbol": "stock", "market": "market", "sector": "board"}


def _as_china(value: datetime) -> datetime:
    """Aware China-time datetime from an aware or naive (= China) value."""
    if value.tzinfo is None:
        return value.replace(tzinfo=CN_TZ)
    return value.astimezone(CN_TZ)


def _row_time(row: Any) -> Optional[datetime]:
    """Aware China-time publish time of a stored row (falls back to the fetch time)."""
    published = row.published_at
    if published is not None:
        if row.source_type in _UTC_SOURCE_TYPES:
            return published.replace(tzinfo=timezone.utc).astimezone(CN_TZ)
        return published.replace(tzinfo=CN_TZ)
    if row.fetched_at is not None:
        if row.source_type == SOURCE_TYPE:
            return row.fetched_at.replace(tzinfo=CN_TZ)
        return row.fetched_at.astimezone(CN_TZ)  # legacy rows: naive = host-local clock
    return None


def _row_kind(row: Any) -> str:
    if row.item_kind:
        return row.item_kind
    return "flash" if row.source_type == "newsnow" else "article"


def _row_codes(row: Any) -> List[str]:
    codes = [str(c) for c in IntelligenceRepository.loads_list(row.stock_codes)]
    if row.scope_type == "symbol" and row.scope_value and row.scope_value != INTELLIGENCE_ITEM_NULL_SCOPE_VALUE:
        value = str(row.scope_value)
        if value not in codes:
            codes.append(value)
    return codes


def _row_to_dict(row: Any, *, when: Optional[datetime], summary_chars: Optional[int]) -> Dict[str, Any]:
    summary = row.summary or ""
    if summary_chars is not None:
        summary = summary[:summary_chars]
    url = row.url or ""
    return {
        "id": row.id,
        "title": row.title,
        "summary": summary,
        "source": row.source or row.source_name or "",
        "published_at": when.isoformat() if when else None,
        "scope": row.news_scope or _SCOPE_FROM_ITEM_SCOPE.get(row.scope_type, "market"),
        "importance": row.importance,
        "is_breaking": bool(row.is_breaking),
        "board_ids": [int(b) for b in IntelligenceRepository.loads_list(row.board_ids)],
        "board_names": [str(b) for b in IntelligenceRepository.loads_list(row.board_names)],
        "us_tickers": [str(t) for t in IntelligenceRepository.loads_list(row.us_tickers)],
        "codes": _row_codes(row),
        "url": "" if url.startswith(NO_URL_PREFIX) or url.startswith("no-url:") else url,
        "kind": _row_kind(row),
    }


def _sort_key(item: Dict[str, Any], when: Optional[datetime]) -> tuple:
    """is_breaking desc, classified before unclassified, importance desc, newest first."""
    return (
        0 if item["is_breaking"] else 1,
        0 if item["importance"] is not None else 1,
        -(item["importance"] or 0),
        -(when.timestamp() if when else 0.0),
    )


def _within(row: Any, when: Optional[datetime], since: datetime, until: datetime) -> bool:
    if when is None:
        return False
    if row.item_kind == KIND_ANNOUNCEMENT:  # date-only
        return since.date() <= when.date() <= until.date()
    return since <= when <= until


def build_news_digest(
    since: datetime,
    until: Optional[datetime] = None,
    codes: Optional[List[str]] = None,
    board_ids: Optional[List[int]] = None,
    min_importance: Optional[int] = None,
    limit: int = 30,
    *,
    repo: Optional[IntelligenceRepository] = None,
) -> List[Dict[str, Any]]:
    """Stored news between ``since`` and ``until`` (default now), filtered and ranked; see module doc."""
    repo = repo or IntelligenceRepository()
    since_cn = _as_china(since)
    until_cn = _as_china(until) if until is not None else datetime.now(CN_TZ)
    if until_cn < since_cn:
        return []

    wanted_codes = {normalize_a_share_code(c) or str(c).strip().upper() for c in (codes or []) if str(c).strip()}
    wanted_boards = {int(b) for b in (board_ids or [])}

    rows = repo.list_news_rows(
        # padded: legacy rows are UTC, announcements are stored at 00:00 of their date
        since=min(since_cn.replace(tzinfo=None) - _LEGACY_PAD, datetime.combine(since_cn.date(), dtime.min)),
        until=until_cn.replace(tzinfo=None) + _LEGACY_PAD,
        min_importance=min_importance,
    )
    items: List[tuple] = []
    for row in rows:
        when = _row_time(row)
        if not _within(row, when, since_cn, until_cn):
            continue
        item = _row_to_dict(row, when=when, summary_chars=_DIGEST_SUMMARY_CHARS)
        if wanted_codes and not wanted_codes.intersection(item["codes"]):
            continue
        if wanted_boards and not wanted_boards.intersection(item["board_ids"]):
            continue
        items.append((_sort_key(item, when), item))
    items.sort(key=lambda pair: pair[0])
    return [item for _key, item in items[: max(0, int(limit))]]


def get_morning_brief(day: date, *, repo: Optional[IntelligenceRepository] = None) -> Optional[Dict[str, Any]]:
    """The 财经早餐 item published on ``day`` (China date); the newest one when there are several."""
    repo = repo or IntelligenceRepository()
    start = datetime.combine(day, dtime.min)
    rows = repo.list_news_rows(since=start - _LEGACY_PAD, until=start + timedelta(days=1) + _LEGACY_PAD)
    best: Optional[tuple] = None
    for row in rows:
        if row.item_kind != KIND_MORNING_BRIEF:
            continue
        when = _row_time(row)
        if when is None or when.date() != day:
            continue
        if best is None or when > best[0]:
            best = (when, row)
    if best is None:
        return None
    when, row = best
    payload = _row_to_dict(row, when=when, summary_chars=None)
    payload["content"] = row.summary or ""
    return payload


def get_news_health(*, repo: Optional[IntelligenceRepository] = None, now: Optional[datetime] = None,
                    config: Any = None) -> Dict[str, Any]:
    """Per-source last success / last error and the open (uncovered) gap windows."""
    repo = repo or IntelligenceRepository()
    if config is None:
        from src.config import get_config

        config = get_config()
    now_cn = _as_china(now) if now is not None else datetime.now(CN_TZ)
    flash_days = max(1, int(getattr(config, "news_flash_retention_days", 3)))

    def iso(value: Optional[datetime]) -> Optional[str]:
        return value.replace(tzinfo=CN_TZ).isoformat() if value is not None else None

    states = {state["source_key"]: state for state in repo.list_fetch_states()}
    sources: List[Dict[str, Any]] = []
    for key, label in SOURCE_LABELS.items():
        state = states.get(key) or {}
        sources.append({
            "source": key,
            "label": label,
            "last_status": state.get("last_status") or "never",
            "last_success_at": iso(state.get("last_success_at")),
            "last_fetch_at": iso(state.get("last_fetch_at")),
            "last_error": state.get("last_error"),
            "last_count": state.get("last_count"),
            "checkpoint_at": iso(state.get("checkpoint_at")),
            "gap_check": state.get("gap_check"),
        })

    since = now_cn.replace(tzinfo=None) - timedelta(days=flash_days)
    open_gaps = [
        {
            "source": gap["source_key"],
            "label": SOURCE_LABELS.get(gap["source_key"], gap["source_key"]),
            "from": iso(gap["gap_from"]),
            "to": iso(gap["gap_to"]),
            "detected_at": iso(gap["detected_at"]),
        }
        for gap in repo.list_fetch_gaps(since=since, covered=False)
    ]
    max_attempts = int(getattr(config, "news_classify_max_attempts", 5))
    return {
        "checked_at": now_cn.isoformat(),
        "sources": sources,
        "open_gaps": open_gaps,
        "incomplete_windows": bool(open_gaps),
        "unclassified_pending": repo.count_unclassified(max_attempts=max_attempts),
    }
