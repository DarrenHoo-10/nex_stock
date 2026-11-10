# -*- coding: utf-8 -*-
"""One process-wide gate for every request to an ``*.eastmoney.com`` host.

East Money bans IPs that burst (community measurements collected by a-stock-data, 2026-05: more than
5 requests/s, 10 concurrent connections, 200 requests/min or 300 per 5 min), and each sub-domain
(``push2``, ``push2his``, ``datacenter-web``, ...) has its own firewall: one can be banned while
another still works. Our East Money traffic comes from AkShare, efinance, the screening snapshot,
hotspots and the news pool, each with its own pacing, so nothing bounded the total.

The gate hooks ``requests.Session.request`` (installed once, by ``install()``) so it covers library
calls as well as our own:

* per host, one request at a time, at least ``EASTMONEY_MIN_INTERVAL_SEC`` (default 1.0 s) plus
  0.1-0.5 s jitter apart;
* HTTP 403 is a ban signal: the host cools down for ``EASTMONEY_BAN_COOLDOWN_MINUTES`` (default 30)
  and every request to it raises ``EastmoneyCoolingDown`` (a ``requests`` ``ConnectionError``, so
  existing fallbacks switch source) until the cooldown ends. 403 is never retried;
* ``slot(host)`` holds the same per-host turn around work the hook cannot see, e.g. AkShare calls
  that run in a child process.

``status()`` feeds the data-status panel (``data_capability_service``).
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_MIN_INTERVAL_SEC = 1.0
DEFAULT_BAN_COOLDOWN_MINUTES = 30.0
JITTER_RANGE_SEC = (0.1, 0.5)
_DOMAIN = "eastmoney.com"


class EastmoneyCoolingDown(requests.exceptions.ConnectionError):
    """The host answered 403 recently; requests to it are refused until the cooldown ends."""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(float(raw), 0.0)
    except ValueError:
        logger.warning("[东财限速] %s=%r 不是数字，使用默认值 %s", name, raw, default)
        return default


def min_interval_sec() -> float:
    return _env_float("EASTMONEY_MIN_INTERVAL_SEC", DEFAULT_MIN_INTERVAL_SEC)


def ban_cooldown_sec() -> float:
    return _env_float("EASTMONEY_BAN_COOLDOWN_MINUTES", DEFAULT_BAN_COOLDOWN_MINUTES) * 60.0


def host_of(url: Any) -> Optional[str]:
    """East Money host of ``url`` (``None`` for any other site)."""
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return None
    if host == _DOMAIN or host.endswith("." + _DOMAIN):
        return host
    return None


class _HostState:
    __slots__ = ("lock", "last_at", "cooling_until", "last_error", "requests")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_at = 0.0
        self.cooling_until = 0.0
        self.last_error: Optional[str] = None
        self.requests = 0


class EastmoneyGate:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(*JITTER_RANGE_SEC),
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._jitter = jitter
        self._states: Dict[str, _HostState] = {}
        self._states_lock = threading.Lock()

    def _state(self, host: str) -> _HostState:
        with self._states_lock:
            state = self._states.get(host)
            if state is None:
                state = self._states[host] = _HostState()
            return state

    def cooling_down(self, host: str) -> float:
        """Seconds of cooldown left for ``host`` (0 when it may be called)."""
        state = self._states.get(host)
        return max(0.0, state.cooling_until - self._clock()) if state else 0.0

    def mark_banned(self, host: str, reason: str) -> None:
        state = self._state(host)
        cooldown = ban_cooldown_sec()
        state.cooling_until = self._clock() + cooldown
        state.last_error = reason
        logger.warning("[东财限速] %s 返回 %s，暂停 %.0f 分钟，期间改用其他来源", host, reason, cooldown / 60)

    @contextmanager
    def slot(self, host: str, *, interval: Optional[float] = None) -> Iterator[None]:
        """Hold ``host``'s turn: refuses during cooldown, waits for the interval, serialises."""
        left = self.cooling_down(host)
        if left > 0:
            raise EastmoneyCoolingDown(f"{host} 冷却中（东方财富返回 403），约 {left / 60:.0f} 分钟后恢复")
        state = self._state(host)
        with state.lock:
            left = self.cooling_down(host)  # a request queued ahead of us may have been banned
            if left > 0:
                raise EastmoneyCoolingDown(f"{host} 冷却中（东方财富返回 403），约 {left / 60:.0f} 分钟后恢复")
            gap = (min_interval_sec() if interval is None else max(interval, 0.0))
            if gap > 0 and state.last_at:
                wait = state.last_at + gap + self._jitter() - self._clock()
                if wait > 0:
                    self._sleep(wait)
            try:
                yield
            finally:
                state.last_at = self._clock()
                state.requests += 1

    def request(self, send: Callable[..., Any], method: str, url: str, *, interval: Optional[float] = None,
                **kwargs: Any) -> Any:
        """Send one request through the gate (``send`` is the underlying ``Session.request``)."""
        host = host_of(url)
        if host is None:
            return send(method, url, **kwargs)
        with self.slot(host, interval=interval):
            response = send(method, url, **kwargs)
        if getattr(response, "status_code", None) == 403:
            self.mark_banned(host, "HTTP 403")
        return response

    def status(self) -> List[Dict[str, Any]]:
        """Per-host request count and cooldown, for the data-status panel."""
        now = self._clock()
        rows = []
        with self._states_lock:
            items = list(self._states.items())
        for host, state in sorted(items):
            left = max(0.0, state.cooling_until - now)
            rows.append({
                "host": host,
                "requests": state.requests,
                "cooling_down": left > 0,
                "cooldown_seconds_left": round(left),
                "cooldown_until": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._wall_clock() + left)) if left else None
                ),
                "last_error": state.last_error,
            })
        return rows


GATE = EastmoneyGate()

_install_lock = threading.Lock()
_underlying_request: Optional[Callable[..., Any]] = None  # Session.request beneath the hook


def _in_user_scope() -> bool:
    try:
        from src.utils.safe_requests import in_user_outbound_scope
    except ImportError:  # data_provider used without the app package
        return False
    return in_user_outbound_scope()


def install() -> None:
    """Route every ``requests`` call to an East Money host through ``GATE`` (idempotent).

    The hook wraps whatever ``Session.request`` is current, so it composes with the opt-in
    ``src/patches/eastmoney_patch`` in either order.
    """
    global _underlying_request
    with _install_lock:
        current = requests.Session.request
        if getattr(current, "_eastmoney_gate", False):
            return
        underlying = current

        def gated_request(self, method, url, *args, **kwargs):  # type: ignore[no-untyped-def]
            # A user's own outbound request (personal webhook ...) is theirs: its 403 must not cool the host down
            # for everyone, and it must not take a turn in the shared per-host queue.
            if host_of(url) is None or _in_user_scope():
                return underlying(self, method, url, *args, **kwargs)
            return GATE.request(lambda m, u, **kw: underlying(self, m, u, *args, **kw), method, url, **kwargs)

        gated_request._eastmoney_gate = True  # type: ignore[attr-defined]
        requests.Session.request = gated_request
        _underlying_request = underlying


def is_installed() -> bool:
    return bool(getattr(requests.Session.request, "_eastmoney_gate", False))


def _raw_request(session: requests.Session, method: str, url: str, **kwargs: Any) -> Any:
    """``Session.request`` without the hook (calling the hook inside a held slot would deadlock)."""
    send = _underlying_request if is_installed() and _underlying_request is not None else requests.Session.request
    return send(session, method, url, **kwargs)


def get(url: str, *, session: Optional[requests.Session] = None, interval: Optional[float] = None,
        **kwargs: Any) -> requests.Response:
    """GET through the gate, optionally with a caller-specific interval; raises on HTTP errors."""
    sender = session or requests.Session()
    response = GATE.request(lambda m, u, **kw: _raw_request(sender, m, u, **kw), "GET", url,
                            interval=interval, **kwargs)
    response.raise_for_status()
    return response
