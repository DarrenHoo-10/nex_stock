# -*- coding: utf-8 -*-
"""Small helpers shared by the board metric / signal / anomaly services."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ``dc_member`` only has per-day members since this date (mirrors
# ``market_snapshot_service.DC_MEMBER_START_DATE``; kept here so the read path does not
# import the Tushare ingestion stack). Earlier membership is back-projected.
MEMBERSHIP_OBSERVED_FROM = date(2024, 12, 20)

CalendarFn = Callable[[date, date], Optional[Sequence[date]]]


def as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return datetime.strptime(text, "%Y-%m-%d").date()


def default_calendar_fn(start: date, end: date) -> Optional[List[date]]:
    """A-share sessions in ``[start, end]`` from the exchange calendar, or ``None``."""
    try:
        from src.core.trading_calendar import get_trading_dates

        index = get_trading_dates("cn", start, end)
    except Exception as exc:  # noqa: BLE001 - calendar is best effort
        logger.debug("trading calendar unavailable: %s", exc)
        return None
    if index is None or len(index) == 0:
        return None
    return [ts.date() for ts in index]


def session_axis(
    end: date,
    count: int,
    ingested_desc: Sequence[date],
    calendar_fn: Optional[CalendarFn] = None,
) -> List[date]:
    """The last ``count`` trading sessions ending at ``end`` (newest first).

    With a calendar the axis contains every real session, so a day whose ingestion failed
    shows up as a gap (callers see it as "no data" rather than silently shifting the
    window). Without a calendar, or when it disagrees with what was ingested, the
    ingested dates are used.
    """
    known = [d for d in ingested_desc if d <= end]
    fallback = known[:count]
    if calendar_fn is None:
        return fallback
    try:
        sessions = calendar_fn(end - timedelta(days=int(count * 1.8) + 14), end)
    except Exception as exc:  # noqa: BLE001
        logger.debug("calendar lookup failed: %s", exc)
        return fallback
    if not sessions:
        return fallback
    ordered = sorted({s for s in sessions if s <= end}, reverse=True)
    if not ordered or ordered[0] != end:
        return fallback
    axis = ordered[:count]
    if len(axis) < min(count, len(fallback)):  # calendar shorter than what was ingested
        return fallback
    if not {d for d in known if d >= axis[-1]}.issubset(set(axis)):
        return fallback
    return axis
