# -*- coding: utf-8 -*-
"""In-process sliding-window counter per (kind, user) shared by the multi-user abuse limits."""

from __future__ import annotations

import collections
import threading
import time
from typing import Dict, Tuple


class SlidingWindowLimiter:
    """Per-user count of events in a window (default one hour). In memory: a restart resets it - this is abuse
    control, not accounting."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[Tuple[str, str], "collections.deque[float]"] = {}

    def allow(self, kind: str, user_id: str, limit: int, *, window_seconds: float = 3600.0) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events.setdefault((kind, user_id), collections.deque())
            while events and now - events[0] >= window_seconds:
                events.popleft()
            if len(events) >= max(1, int(limit)):
                return False
            events.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
