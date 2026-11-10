# -*- coding: utf-8 -*-
"""Per-user schedule settings (docs/design/multi-user.md section 6, P3a).

What a user can set and what the scheduler derives from it:

* ``DAILY_PLAN_ENABLED`` / ``DAILY_PLAN_TIME`` - the next-day plan. ``DAILY_PLAN_TIME`` is a dedicated user-scope key
  (one ``HH:MM``). ``SCHEDULE_TIME(S)`` stays a system-scope key: it also drives the primary user's legacy per-stock
  analysis, so it cannot be a personal setting. Blank = the system default, i.e. ``SCHEDULE_TIME(S)``; the primary
  user (``.env``) keeps exactly that unless ``DAILY_PLAN_TIME`` is set in ``.env`` too.
* ``PREOPEN_PUSH_ENABLED`` / ``PREOPEN_PUSH_TIME`` - the pre-open push (blank / unset = the system default).

Time constraints, enforced when a personal value is saved (``UserSettingsService``) and again when the schedule is
derived (a value that became invalid because an administrator changed the system times is clamped, not obeyed):

* the plan reads the day's close, so it must run after the board pipeline has finished: the earliest allowed time is
  ``BOARD_PIPELINE_SCHEDULE_TIME`` (or the market close when the pipeline is off) plus
  ``DAILY_PLAN_DATA_READY_BUFFER_MINUTES``;
* the pre-open push has to land before the market opens, so its latest allowed start is ``PREOPEN_LATEST_TIME``; and it
  reads the overnight US session, so its earliest allowed start is the US close (16:00 New York, the later of summer /
  winter time in China) plus ``US_DATA_READY_BUFFER_MINUTES`` (``earliest_preopen_time``). Both bounds exist because a
  firing before the session is final would compute - and cache for the whole day - an incomplete overnight context
  that every other user of the day would then share.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from src.config import get_config, get_primary_user_id

logger = logging.getLogger(__name__)

DEFAULT_PLAN_TIME = "18:00"
DEFAULT_PREOPEN_TIME = "08:15"
MARKET_CLOSE_TIME = "15:00"
#: Latest start of the pre-open push: the run takes ~30 minutes and the A-share session opens at 09:30.
PREOPEN_LATEST_TIME = "09:00"
#: Minutes after the US close before the overnight bars (Eastmoney / yfinance) are treated as final.
US_DATA_READY_BUFFER_MINUTES = 30
#: A pre-open batch that has not finished by this time (China time) is late: users not started are skipped, running
#: ones abandoned. The A-share session opens at 09:30; a push after 09:20 is of no use.
PREOPEN_BATCH_CUTOFF_TIME = "09:20"
SCHEDULE_TIME_KEYS = frozenset({"DAILY_PLAN_TIME", "PREOPEN_PUSH_TIME"})

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ScheduleTimeError(ValueError):
    """A personal schedule time was rejected; ``code`` is stable, ``limit`` carries the earliest / latest allowed time."""

    def __init__(self, code: str, message: str, *, limit_name: Optional[str] = None, limit: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.limit_name = limit_name
        self.limit = limit


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def _hhmm(total_minutes: int) -> str:
    total_minutes = max(0, min(total_minutes, 23 * 60 + 59))
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def earliest_plan_time(system_config: Any = None) -> str:
    """The earliest time a next-day plan may run: when the system's board data for the day is ready."""
    config = system_config if system_config is not None else get_config()
    enabled = getattr(config, "board_pipeline_enabled", True)
    base = str(getattr(config, "board_pipeline_schedule_time", "") or "").strip() if enabled else ""
    base = base if _HHMM_RE.match(base) else (MARKET_CLOSE_TIME if not enabled else "17:00")
    buffer_minutes = getattr(config, "daily_plan_data_ready_buffer_minutes", 30)
    buffer_minutes = buffer_minutes if isinstance(buffer_minutes, int) and buffer_minutes >= 0 else 30
    return _hhmm(_minutes(base) + buffer_minutes)


def latest_preopen_time() -> str:
    return PREOPEN_LATEST_TIME


def earliest_preopen_time() -> str:
    """Earliest start of a pre-open push: the US close in China time (the later of the summer / winter close, so the
    value does not move with daylight saving time) plus ``US_DATA_READY_BUFFER_MINUTES``."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from src.services.us_overnight_service import _NY_TZ, _SESSION_CLOSE

    china = ZoneInfo("Asia/Shanghai")
    year = date.today().year
    closes = [
        datetime.combine(date(year, month, 15), _SESSION_CLOSE, tzinfo=_NY_TZ).astimezone(china)
        for month in (1, 7)  # EST / EDT
    ]
    latest_close = max(close.hour * 60 + close.minute for close in closes)
    return _hhmm(latest_close + US_DATA_READY_BUFFER_MINUTES)


def validate_schedule_time(key: str, value: str, *, system_config: Any = None) -> str:
    """Normalized personal schedule time (``""`` = system default); raises :class:`ScheduleTimeError`."""
    text = (value or "").strip()
    if not text:
        return ""
    if not _HHMM_RE.match(text):
        raise ScheduleTimeError("invalid_value", f"{key} must be a time in HH:MM format")
    if key == "DAILY_PLAN_TIME":
        earliest = earliest_plan_time(system_config)
        if _minutes(text) < _minutes(earliest):
            raise ScheduleTimeError(
                "plan_time_too_early",
                f"The next-day plan needs the day's board data: the earliest time is {earliest}",
                limit_name="earliest_time",
                limit=earliest,
            )
    elif key == "PREOPEN_PUSH_TIME":
        earliest = earliest_preopen_time()
        if _minutes(text) < _minutes(earliest):
            raise ScheduleTimeError(
                "preopen_time_too_early",
                f"The pre-open push needs the final overnight US session: the earliest time is {earliest}",
                limit_name="earliest_time",
                limit=earliest,
            )
        latest = latest_preopen_time()
        if _minutes(text) > _minutes(latest):
            raise ScheduleTimeError(
                "preopen_time_too_late",
                f"The pre-open push must start before the market opens: the latest time is {latest}",
                limit_name="latest_time",
                limit=latest,
            )
    return text


# --------------------------------------------------------------------------- effective schedule

def effective_plan_times(config: Any, *, system_config: Any = None) -> List[str]:
    """Run times of a *non-primary* user's next-day plan (their view's ``DAILY_PLAN_TIME``, else the system times).

    The primary user's times stay in ``daily_plan_schedule.configured_times`` (unchanged). Times earlier than the
    data-ready time are dropped; if none is left the earliest allowed time is used.
    """
    from src.services.daily_plan_schedule import configured_times

    earliest = earliest_plan_time(system_config)
    own = str(getattr(config, "daily_plan_time", "") or "").strip()
    candidates = [own] if _HHMM_RE.match(own) else configured_times(config)
    allowed = [item for item in candidates if _minutes(item) >= _minutes(earliest)]
    return sorted(set(allowed)) or [earliest]


def effective_preopen_time(config: Any) -> str:
    """Pre-open start of a non-primary user, clamped into ``[earliest_preopen_time(), PREOPEN_LATEST_TIME]``."""
    value = str(getattr(config, "preopen_push_time", "") or "").strip()
    value = value if _HHMM_RE.match(value) else DEFAULT_PREOPEN_TIME
    earliest = earliest_preopen_time()
    if _minutes(value) < _minutes(earliest):
        return earliest
    return value if _minutes(value) <= _minutes(PREOPEN_LATEST_TIME) else PREOPEN_LATEST_TIME


def plan_enabled(config: Any) -> bool:
    return bool(getattr(config, "daily_plan_enabled", False))


def preopen_enabled(config: Any) -> bool:
    return bool(getattr(config, "preopen_push_enabled", False))


# --------------------------------------------------------------------------- who is scheduled

def scheduled_user_ids() -> List[str]:
    """Known non-primary users (``app_user``), oldest first. The primary user runs through the ``.env`` jobs."""
    from src.repositories.user_repo import UserRepository

    primary = get_primary_user_id()
    return [row["user_id"] for row in UserRepository().list_app_users() if row["user_id"] != primary]


def _user_view(user_id: str) -> Any:
    from src.services.user_config_service import get_user_config

    return get_user_config(user_id)


def user_plan_schedule(user_id: str) -> Dict[str, Any]:
    config = _user_view(user_id)
    return {"enabled": plan_enabled(config), "times": effective_plan_times(config)}


def user_preopen_schedule(user_id: str) -> Dict[str, Any]:
    config = _user_view(user_id)
    return {"enabled": preopen_enabled(config), "time": effective_preopen_time(config)}


def _schedules(
    reader: Callable[[str], Dict[str, Any]], user_ids: Optional[Sequence[str]],
) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """``(user_id, schedule)`` of every user whose settings can be read; a user whose settings are broken is skipped
    (logged by id only) so that one user never keeps the others' jobs from being registered or run."""
    for user_id in (scheduled_user_ids() if user_ids is None else user_ids):
        try:
            yield user_id, reader(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cannot read the schedule of user %s: %s", user_id, type(exc).__name__)


def plan_times_in_use(user_ids: Optional[Sequence[str]] = None) -> List[str]:
    """De-duplicated plan times of every user with the plan enabled (what the scheduler has to register)."""
    times = set()
    for _user_id, schedule in _schedules(user_plan_schedule, user_ids):
        if schedule["enabled"]:
            times.update(schedule["times"])
    return sorted(times)


def preopen_times_in_use(user_ids: Optional[Sequence[str]] = None) -> List[str]:
    times = set()
    for _user_id, schedule in _schedules(user_preopen_schedule, user_ids):
        if schedule["enabled"]:
            times.add(schedule["time"])
    return sorted(times)


def plan_users_due(schedule_time: str, user_ids: Optional[Sequence[str]] = None) -> List[str]:
    """Users whose next-day plan is enabled and runs at ``schedule_time``."""
    return [
        user_id for user_id, schedule in _schedules(user_plan_schedule, user_ids)
        if schedule["enabled"] and schedule_time in schedule["times"]
    ]


def preopen_users_due(schedule_time: str, user_ids: Optional[Sequence[str]] = None) -> List[str]:
    return [
        user_id for user_id, schedule in _schedules(user_preopen_schedule, user_ids)
        if schedule["enabled"] and schedule["time"] == schedule_time
    ]


def describe_schedule(user_id: str) -> Dict[str, Any]:
    """Read model of ``GET /me/schedule``: effective values of the user and the allowed range."""
    from src.services.daily_plan_schedule import configured_times

    primary = user_id == get_primary_user_id()
    config = get_config() if primary else _user_view(user_id)
    plan_times = configured_times(config) if primary else effective_plan_times(config)
    return {
        "user_id": user_id,
        "is_primary": primary,
        "managed_in_system_settings": primary,
        "daily_plan": {
            "enabled": plan_enabled(config),
            "times": plan_times,
            "earliest_time": earliest_plan_time(),
            "default_time": DEFAULT_PLAN_TIME,
        },
        "preopen_push": {
            "enabled": preopen_enabled(config),
            "time": effective_preopen_time(config) if not primary else (
                str(getattr(config, "preopen_push_time", "") or DEFAULT_PREOPEN_TIME)
            ),
            "earliest_time": earliest_preopen_time(),
            "latest_time": latest_preopen_time(),
            "default_time": DEFAULT_PREOPEN_TIME,
        },
    }
