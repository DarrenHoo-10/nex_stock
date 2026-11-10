# -*- coding: utf-8 -*-
"""Recall orchestration: timeouts, masking and the background retain/forget queue.

Every public method is safe to call from a chat turn: failures and timeouts
are reported through the returned status or logged, never raised.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from typing import Any, Callable, List, Optional, Sequence, Tuple

from src.agent.recall.base import (
    RecallHealth,
    RecallItem,
    RecallProvider,
    RecallQuery,
    RecallTurn,
)
from src.agent.recall.masking import mask_sensitive_numbers
from src.agent.recall.none_provider import NONE_PROVIDER_ID
from src.agent.recall.registry import (
    _read,
    create_recall_provider,
    resolve_recall_timeout_s,
)

logger = logging.getLogger(__name__)

RECALL_STATUS_USED = "used"  # provider answered (possibly with zero items)
RECALL_STATUS_DISABLED = "disabled"  # provider none / not configured
RECALL_STATUS_UNAVAILABLE = "unavailable"  # timeout or error

RETAIN_MAX_ATTEMPTS = 3
RETAIN_RETRY_DELAYS_S: Tuple[float, ...] = (2.0, 10.0)
FORGET_RETRY_BASE_S = 5.0
FORGET_RETRY_MAX_S = 300.0
QUEUE_MAX_TASKS = 1000
HEALTH_CACHE_S = 30.0


@dataclass(frozen=True)
class RecallOutcome:
    status: str
    items: List[RecallItem] = field(default_factory=list)
    reason: Optional[str] = None


@dataclass
class _Task:
    kind: str  # retain | forget
    payload: Any
    attempts: int = 0
    not_before: float = 0.0

    @property
    def document_ids(self) -> List[str]:
        if self.kind == "retain":
            return [self.payload.document_id]
        return list(self.payload)


_RECALL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="assistant-recall")


class RecallService:
    def __init__(
        self,
        provider: RecallProvider,
        *,
        timeout_s: float = 2.0,
        mask_amounts: bool = True,
        retain_retry_delays: Sequence[float] = RETAIN_RETRY_DELAYS_S,
        forget_retry_base_s: float = FORGET_RETRY_BASE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self.timeout_s = float(timeout_s)
        self.mask_amounts = bool(mask_amounts)
        self._retain_retry_delays = tuple(retain_retry_delays)
        self._forget_retry_base_s = float(forget_retry_base_s)
        self._clock = clock
        self._cond = threading.Condition()
        self._tasks: List[_Task] = []
        self._in_flight: Optional[_Task] = None
        self._worker: Optional[threading.Thread] = None
        self._stopped = False
        self._health_cache: Optional[Tuple[float, RecallHealth]] = None
        self.dropped_retains = 0

    # -- state -----------------------------------------------------------

    @property
    def provider_id(self) -> str:
        return self.provider.provider_id

    @property
    def enabled(self) -> bool:
        return self.provider_id != NONE_PROVIDER_ID

    @property
    def configured(self) -> bool:
        return self.enabled and bool(getattr(self.provider, "configured", True))

    def _mask(self, text: str) -> str:
        return mask_sensitive_numbers(text) if self.mask_amounts else text

    # -- recall (synchronous, bounded) ----------------------------------

    def recall(self, query: RecallQuery) -> RecallOutcome:
        if not self.enabled:
            return RecallOutcome(RECALL_STATUS_DISABLED, reason="provider_none")
        if not self.configured:
            return RecallOutcome(RECALL_STATUS_UNAVAILABLE, reason="not_configured")
        masked = replace(query, text=self._mask(query.text))
        try:
            future = _RECALL_POOL.submit(self.provider.recall, masked)
        except Exception as exc:  # pool shut down etc.
            logger.warning("Recall could not be scheduled: %s", exc)
            return RecallOutcome(RECALL_STATUS_UNAVAILABLE, reason="error")
        try:
            items = list(future.result(timeout=self.timeout_s))
        except FutureTimeout:
            future.cancel()
            logger.warning("Long-term recall timed out after %.1fs; skipped", self.timeout_s)
            return RecallOutcome(RECALL_STATUS_UNAVAILABLE, reason="timeout")
        except Exception as exc:
            logger.warning("Long-term recall failed; skipped: %s", exc)
            return RecallOutcome(RECALL_STATUS_UNAVAILABLE, reason="error")
        return RecallOutcome(RECALL_STATUS_USED, items=items)

    # -- retain / forget (background) -----------------------------------

    def retain_async(self, turn: RecallTurn) -> bool:
        if not self.configured:
            return False
        if self.mask_amounts:
            turn = replace(
                turn,
                user_text=mask_sensitive_numbers(turn.user_text),
                assistant_text=mask_sensitive_numbers(turn.assistant_text),
            )
        return self._enqueue(_Task("retain", turn))

    def forget_async(self, document_ids: Sequence[str]) -> bool:
        ids = [str(item) for item in document_ids if item]
        if not ids or not self.configured:
            return False
        with self._cond:
            # A turn still waiting to be retained must not be written after its delete.
            drop = set(ids)
            self._tasks = [
                task for task in self._tasks
                if not (task.kind == "retain" and task.document_ids[0] in drop)
            ]
        return self._enqueue(_Task("forget", ids))

    def _enqueue(self, task: _Task) -> bool:
        with self._cond:
            if len(self._tasks) >= QUEUE_MAX_TASKS:
                logger.warning("Recall queue is full; dropping a %s task", task.kind)
                if task.kind == "retain":
                    self.dropped_retains += 1
                return False
            task.not_before = self._clock()
            self._tasks.append(task)
            self._ensure_worker_locked()
            self._cond.notify_all()
        return True

    def _ensure_worker_locked(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, name="assistant-recall-queue", daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                task = None
                while not self._stopped:
                    now = self._clock()
                    due = [t for t in self._tasks if t.not_before <= now]
                    if due:
                        task = due[0]
                        self._tasks.remove(task)
                        self._in_flight = task
                        break
                    wait = min((t.not_before for t in self._tasks), default=None)
                    self._cond.wait(None if wait is None else max(0.01, wait - now))
                if task is None:
                    return
            self._execute(task)

    def _execute(self, task: _Task) -> None:
        try:
            if task.kind == "retain":
                self.provider.retain(task.payload)
            else:
                self.provider.forget(task.payload)
        except Exception as exc:
            task.attempts += 1
            with self._cond:
                if task.kind == "retain" and task.attempts >= RETAIN_MAX_ATTEMPTS:
                    self.dropped_retains += 1
                    logger.warning(
                        "Recall retain gave up after %d attempts (%s): %s",
                        task.attempts, task.document_ids[0], exc,
                    )
                else:
                    task.not_before = self._clock() + self._retry_delay(task)
                    self._tasks.append(task)
                    logger.warning(
                        "Recall %s failed (attempt %d), will retry: %s", task.kind, task.attempts, exc,
                    )
                self._in_flight = None
                self._cond.notify_all()
            return
        with self._cond:
            self._in_flight = None
            self._cond.notify_all()

    def _retry_delay(self, task: _Task) -> float:
        if task.kind == "retain":
            index = min(task.attempts - 1, len(self._retain_retry_delays) - 1)
            return self._retain_retry_delays[index] if index >= 0 else 0.0
        return min(FORGET_RETRY_MAX_S, self._forget_retry_base_s * (2 ** (task.attempts - 1)))

    def pending_counts(self) -> Tuple[int, int]:
        with self._cond:
            tasks = list(self._tasks) + ([self._in_flight] if self._in_flight else [])
        retain = sum(1 for t in tasks if t.kind == "retain")
        forget = sum(len(t.payload) for t in tasks if t.kind == "forget")
        return retain, forget

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty (used by tests and graceful shutdown)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._tasks or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
        return True

    def shutdown(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    # -- health ----------------------------------------------------------

    def health(self, *, use_cache: bool = True) -> RecallHealth:
        now = time.monotonic()
        cached = self._health_cache
        if use_cache and cached and now - cached[0] < HEALTH_CACHE_S:
            base = cached[1]
        else:
            try:
                base = self.provider.health()
            except Exception as exc:  # health() must not raise, but never trust it
                base = RecallHealth(self.provider_id, "error", message=type(exc).__name__)
            self._health_cache = (now, base)
        retain, forget = self.pending_counts()
        return replace(base, pending_retain=retain, pending_forget=forget)


# -- process-wide service -----------------------------------------------------

_service_lock = threading.Lock()
_service: Optional[RecallService] = None
_service_key: Optional[tuple] = None


def _config_key(config: Any) -> tuple:
    return (
        str(_read(config, "assistant_recall_provider", NONE_PROVIDER_ID) or NONE_PROVIDER_ID).strip().lower(),
        _read(config, "hindsight_base_url", ""),
        _read(config, "hindsight_api_key", None),
        _read(config, "hindsight_bank_id", ""),
        resolve_recall_timeout_s(config),
        bool(_read(config, "assistant_recall_mask_amounts", True)),
    )


def get_recall_service(config: Any = None) -> RecallService:
    """Service for the current config; rebuilt when recall settings change."""
    global _service, _service_key
    if config is None:
        from src.config import get_config

        config = get_config()
    key = _config_key(config)
    with _service_lock:
        if _service is None or _service_key != key:
            previous = _service
            _service = RecallService(
                create_recall_provider(config),
                timeout_s=resolve_recall_timeout_s(config),
                mask_amounts=bool(_read(config, "assistant_recall_mask_amounts", True)),
            )
            _service_key = key
            if previous is not None:
                # Let its already-queued deletes/retains finish on the old provider.
                threading.Thread(
                    target=lambda: (previous.wait_idle(60.0), previous.shutdown()),
                    daemon=True,
                ).start()
        return _service


def reset_recall_service() -> None:
    """Drop the cached service (tests / config reload)."""
    global _service, _service_key
    with _service_lock:
        previous, _service, _service_key = _service, None, None
        user_services = list(_user_services.values())
        _user_services.clear()
    if previous is not None:
        previous.shutdown()
    for _key, user_service in user_services:
        user_service.shutdown()


# -- per-user services (multi-user, docs/design/multi-user.md 3.3) ---------------------------

# user_id -> (config key, service). One service per non-primary user; never shared between users.
_user_services: "dict[str, Tuple[tuple, RecallService]]" = {}
_USER_BANK_FALLBACK_BASE = "dsa"


def derive_user_bank_id(base_bank_id: str, user_id: str) -> str:
    """Memory bank of ``user_id`` on a Hindsight server shared by several users.

    ``<HINDSIGHT_BANK_ID>-u-<first 16 hex of sha256(user_id)>``: deterministic (the same user always reads
    the bank their turns were retained in), URL-safe whatever the user id contains, and different for
    every user. Without a system bank id the base is ``dsa``.
    """
    digest = hashlib.sha256((user_id or "").encode("utf-8")).hexdigest()[:16]
    base = (base_bank_id or "").strip() or _USER_BANK_FALLBACK_BASE
    return f"{base}-u-{digest}"


def _normalized_server(url: str) -> Optional[Tuple[str, int]]:
    """``(host, port)`` of a server URL with case, a trailing dot and the scheme's default port normalised.

    Scheme and path are deliberately not part of the identity: ``https://host/.``, ``https://host/x/..`` and
    ``https://host`` are the same server, and one machine port does not become another server by the path.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit((url or "").strip())
        host = (parts.hostname or "").lower().rstrip(".")
        scheme = (parts.scheme or "").lower()
        port = parts.port or {"http": 80, "https": 443}.get(scheme, 0)
    except ValueError:
        return None
    if not host:
        return None
    return host, port


def same_hindsight_server(url: str, system_url: str) -> bool:
    """Whether ``url`` names the system Hindsight server (same host and port, whatever the case / path / slashes)."""
    mine, system = _normalized_server(url), _normalized_server(system_url)
    return mine is not None and mine == system


def user_recall_settings(user_id: str, view: Any) -> dict:
    """Effective recall settings of a non-primary user (a mapping ``create_recall_provider`` understands).

    * Own Hindsight endpoint (the user saved their own ``HINDSIGHT_BASE_URL``): their URL, key and bank
      (a derived bank when they left the bank blank), after a call-time SSRF check of the URL. Redirects are
      never followed (a user-supplied host must not bounce the request, with its bearer key, elsewhere).
    * Otherwise the system Hindsight: the server's URL and key, but always a per-user derived bank. A
      bank id the user typed is ignored here, otherwise two users could point at the same bank. This also
      applies when the user's URL *is* the system URL (:func:`same_hindsight_server`): typing the system address
      is not an own endpoint, so neither a typed bank id nor a typed key reaches the system server (the key
      would be useless there, the bank id would let the user read / write another user's, or the primary's, bank).
    * Provider ``none`` / nothing configured: recall disabled.

    Residual: a different DNS name that merely resolves to the same server is not recognised as the system one
    (hosts are not compared by address: a shared CDN address would misclassify innocent users); the system
    Hindsight is expected to authenticate with ``HINDSIGHT_API_KEY``, which a user does not have.
    """
    from src.config import get_config
    from src.repositories.user_repo import UserRepository

    system = get_config()
    provider = str(_read(view, "assistant_recall_provider", NONE_PROVIDER_ID) or NONE_PROVIDER_ID).strip().lower()
    settings = {
        "assistant_recall_provider": provider,
        "assistant_recall_timeout_s": _read(view, "assistant_recall_timeout_s", 2.0),
        "assistant_recall_mask_amounts": _read(view, "assistant_recall_mask_amounts", True),
        "hindsight_base_url": "",
        "hindsight_api_key": None,
        "hindsight_bank_id": "",
    }
    if provider == NONE_PROVIDER_ID:
        return settings
    own_keys = {row["key"] for row in UserRepository().list_settings(user_id)}
    system_url = str(_read(system, "hindsight_base_url", "") or "").strip()
    system_bank = str(_read(system, "hindsight_bank_id", "") or "").strip()
    own_url = str(_read(view, "hindsight_base_url", "") or "").strip() if "HINDSIGHT_BASE_URL" in own_keys else ""
    if own_url and not same_hindsight_server(own_url, system_url):
        base_url = own_url
        try:
            from src.utils.url_safety import assert_public_https_url

            assert_public_https_url(base_url)
        except Exception as exc:  # unsafe or unresolvable: recall off for this user, never the system server
            logger.warning("Hindsight URL of user %s refused: %s", user_id, exc)
            settings["assistant_recall_provider"] = NONE_PROVIDER_ID
            return settings
        settings["hindsight_base_url"] = base_url
        settings["hindsight_api_key"] = _read(view, "hindsight_api_key", None)
        own_bank = str(_read(view, "hindsight_bank_id", "") or "").strip()
        settings["hindsight_bank_id"] = own_bank or derive_user_bank_id("", user_id)
        settings["hindsight_allow_redirects"] = False
        return settings
    if not system_url or not system_bank:
        settings["assistant_recall_provider"] = NONE_PROVIDER_ID  # system Hindsight not set up
        return settings
    settings["hindsight_base_url"] = system_url
    settings["hindsight_api_key"] = _read(system, "hindsight_api_key", None)
    settings["hindsight_bank_id"] = derive_user_bank_id(system_bank, user_id)
    return settings


def get_user_recall_service(user_id: str, view: Any) -> RecallService:
    """Recall service of one non-primary user (see :func:`user_recall_settings`); rebuilt when it changes."""
    settings = user_recall_settings(user_id, view)
    key = _config_key(settings)
    with _service_lock:
        cached = _user_services.get(user_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        service = RecallService(
            create_recall_provider(settings),
            timeout_s=resolve_recall_timeout_s(settings),
            mask_amounts=bool(_read(settings, "assistant_recall_mask_amounts", True)),
        )
        _user_services[user_id] = (key, service)
        if cached is not None:
            previous = cached[1]
            threading.Thread(
                target=lambda: (previous.wait_idle(60.0), previous.shutdown()), daemon=True,
            ).start()
        return service
