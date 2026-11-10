# -*- coding: utf-8 -*-
"""Bounds on the external data a signed-in user's request can trigger (docs/design/multi-user.md section 23).

The stock detail routes (company events, research, capital, investor Q&A) fetch from external data sources on
demand when the stored data does not cover the stock. Open to every user, that would let one user spend the
shared Tushare quota (and, with the old fetcher-per-request, bypass its limiter). Three rules:

* **One limiter per credential**: ``tushare_fetcher_for`` returns one process-wide ``TushareFetcher`` for the
  system token, so its per-minute counter is shared by every request and by the system jobs.
* **Own credentials first** (design 3.4): a non-primary user who saved their own ``TUSHARE_TOKEN`` fetches with it
  (a cached fetcher per user and token, so their own limiter persists and their own quota pays). Without one, the
  system token is used - data-source credentials may fall back - but only within the rate limit below.
* **Per-user rate limit** on fetches that actually reach an external source (``allow_on_demand_fetch``):
  ``USER_ONDEMAND_FETCH_MAX_PER_HOUR`` per non-primary user. The primary user (and CLI / system jobs, which have no
  user) is not limited. Cache and "already fetched" markers are checked first, so a stored answer costs no permit.
"""

from __future__ import annotations

import collections
import hashlib
import threading
import time
from typing import Any, Dict, Optional, Tuple

from src.utils.rate_limit import SlidingWindowLimiter

DEFAULT_MAX_PER_HOUR = 60
_WINDOW_SECONDS = 3600.0
_USER_FETCHER_CACHE_SIZE = 32

_lock = threading.Lock()
_limiter = SlidingWindowLimiter()
_shared: Optional[Tuple[str, Any]] = None  # (token, fetcher)
_per_user: "collections.OrderedDict[Tuple[str, str], Any]" = collections.OrderedDict()
_checked: Dict[str, float] = {}  # marker -> monotonic expiry
_CHECKED_MAX = 5000


class OnDemandFetchLimited(RuntimeError):
    """A non-primary user is over ``USER_ONDEMAND_FETCH_MAX_PER_HOUR``; the route answers 429."""


def require_on_demand_permit(user_id: Optional[str]) -> None:
    """``allow_on_demand_fetch`` that raises :class:`OnDemandFetchLimited` instead of returning ``False``."""
    if not allow_on_demand_fetch(user_id):
        raise OnDemandFetchLimited("on-demand fetch limit reached")


def reset_for_tests() -> None:
    global _shared
    _limiter.reset()
    with _lock:
        _per_user.clear()
        _checked.clear()
        _shared = None


def _is_primary(user_id: Optional[str]) -> bool:
    from src.config import get_primary_user_id

    return not user_id or user_id == get_primary_user_id()


def max_per_hour() -> int:
    from src.config import get_config

    try:
        value = int(getattr(get_config(), "user_ondemand_fetch_max_per_hour", DEFAULT_MAX_PER_HOUR))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PER_HOUR
    return value if value >= 1 else DEFAULT_MAX_PER_HOUR


def allow_on_demand_fetch(user_id: Optional[str]) -> bool:
    """Take one permit for an external fetch made for ``user_id``; ``False`` when the user is over their limit."""
    if _is_primary(user_id):
        return True
    return _limiter.allow("on_demand_fetch", str(user_id), max_per_hour(), window_seconds=_WINDOW_SECONDS)


def _system_token() -> str:
    from src.config import get_config

    return (getattr(get_config(), "tushare_token", None) or "").strip()


def shared_tushare_fetcher() -> Any:
    """The process-wide fetcher for the system Tushare token (rebuilt when the configured token changes)."""
    global _shared
    token = _system_token()
    with _lock:
        if _shared is None or _shared[0] != token:
            from data_provider.tushare_fetcher import TushareFetcher

            _shared = (token, TushareFetcher())
        return _shared[1]


def tushare_fetcher_for(user_id: Optional[str]) -> Any:
    """Fetcher for an on-demand request: the user's own token when they saved one, else the shared system fetcher."""
    if _is_primary(user_id):
        return shared_tushare_fetcher()
    from src.services.user_config_service import get_user_config

    own = (getattr(get_user_config(str(user_id)), "tushare_token", None) or "").strip()
    system = _system_token()
    if not own or own == system:
        return shared_tushare_fetcher()
    key = (str(user_id), hashlib.sha256(own.encode("utf-8")).hexdigest()[:16])
    with _lock:
        fetcher = _per_user.get(key)
        if fetcher is not None:
            _per_user.move_to_end(key)
            return fetcher
    from data_provider.tushare_fetcher import TushareFetcher

    fetcher = TushareFetcher(token=own, boost_priority=bool(system))
    with _lock:
        _per_user[key] = fetcher
        while len(_per_user) > _USER_FETCHER_CACHE_SIZE:
            _per_user.popitem(last=False)
    return fetcher


def checked_recently(marker: str) -> bool:
    """Whether ``mark_checked(marker, ...)`` was called and has not expired (an answer, even an empty one, is known)."""
    with _lock:
        expires = _checked.get(marker)
        if expires is None:
            return False
        if expires <= time.monotonic():
            _checked.pop(marker, None)
            return False
        return True


def mark_checked(marker: str, ttl_seconds: float) -> None:
    """Remember for ``ttl_seconds`` that ``marker`` was fetched (including a fetch that found nothing)."""
    now = time.monotonic()
    with _lock:
        if len(_checked) >= _CHECKED_MAX:
            for stale in [key for key, expires in _checked.items() if expires <= now]:
                _checked.pop(stale, None)
            if len(_checked) >= _CHECKED_MAX:
                _checked.clear()
        _checked[marker] = now + ttl_seconds
