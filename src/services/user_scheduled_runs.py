# -*- coding: utf-8 -*-
"""Scheduled next-day plans and pre-open pushes of the non-primary users (docs/design/multi-user.md section 6, P3a).

The primary user keeps the fixed ``.env`` jobs (``daily_plan_schedule`` / ``preopen_push_schedule``, unchanged). This
module adds, for everyone else:

* a *dynamic* job group per kind: one daily job per time that any user has set (de-duplicated), re-read every minute so
  a change of a personal setting (``user_schedule_service``) adds or removes the job without a restart;
* at each time, the users whose effective setting matches run **in parallel** (``USER_SCHEDULE_MAX_WORKERS`` at once),
  each with an independent timeout (``USER_SCHEDULE_RUN_TIMEOUT_SECONDS``): a slow, hung or failing user only affects
  their own run - the others start and finish on time - and the outcome is recorded on that user's own run;
* the market side is computed once per trading day and reused by every user of the batch (board listing for the plan,
  overnight US + board mapping for the pre-open push) - but only a *complete* pre-open object is kept for the day: one
  built from a failed / partial fetch serves its own batch and is recomputed at the next firing;
* fairness and isolation: the due users run in a pseudo-random order that changes every day / time slot (never a fixed
  first-come order), a user never has two scheduled runs at once (a run that was abandoned at its timeout still holds
  that user's slot until its thread really ends), the push step of a run is bounded separately and much tighter than the
  run (``USER_PUSH_TOTAL_DEADLINE_SECONDS``, enforced inside the personal notifier), and a pre-open batch that has not
  finished by ``PREOPEN_BATCH_CUTOFF_TIME`` (before the market opens) is late: users not started are skipped
  (``late``), running ones abandoned. Limit: Python threads cannot be killed, so an abandoned run keeps running until it
  ends by itself (network I/O of a personal channel is cut at its deadline; a model call ends at its own timeout);
* a pre-open firing earlier than ``earliest_preopen_time`` (the final overnight session) does nothing (``too_early``);
* only users known in ``app_user`` with something to plan are run; a user without a usable model gets no model step,
  the run is marked "未配置模型" and (when they have a channel) one reminder is pushed once, not every day.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from src.config import get_config
from src.services.board_pipeline_schedule import _china_today, _is_cn_trading_day
from src.services.user_schedule_service import PREOPEN_BATCH_CUTOFF_TIME, earliest_preopen_time

logger = logging.getLogger(__name__)

USER_DAILY_PLAN_JOB_NAME = "user_daily_plan"
USER_PREOPEN_JOB_NAME = "user_preopen_push"
RESYNC_INTERVAL_SECONDS = 60.0
_CN_TZ = ZoneInfo("Asia/Shanghai")


# --------------------------------------------------------------------------- bounded parallel execution

_RUNNING_LOCK = threading.Lock()
_RUNNING_USERS: set = set()


def _claim_user(user_id: str) -> bool:
    with _RUNNING_LOCK:
        if user_id in _RUNNING_USERS:
            return False
        _RUNNING_USERS.add(user_id)
        return True


def _release_user(user_id: str) -> None:
    with _RUNNING_LOCK:
        _RUNNING_USERS.discard(user_id)


def fair_order(user_ids: Sequence[str], seed: str) -> List[str]:
    """``user_ids`` (de-duplicated) in a pseudo-random order derived from ``seed`` (the day and time slot).

    The same users get a different order every day and time slot, so nobody is permanently first (or last) when the
    batch is bigger than the workers can finish; within one firing the order is reproducible (a retry behaves the same).
    """
    ordered = list(dict.fromkeys(user_ids))
    random.Random(seed).shuffle(ordered)
    return ordered


def run_for_users(
    user_ids: Sequence[str],
    runner: Callable[[str], Dict[str, Any]],
    *,
    max_workers: int,
    timeout_seconds: float,
    on_timeout: Optional[Callable[[str], None]] = None,
    batch_timeout_seconds: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run ``runner(user_id)`` for every user, at most ``max_workers`` at once, each with its own timeout.

    One user's run is one daemon thread. A run that exceeds ``timeout_seconds`` is abandoned (``{"status": "timeout"}``,
    ``on_timeout(user_id)`` records it) and frees its slot at once, so a hung user never delays the users behind them;
    the abandoned thread simply finishes (or not) on its own. An exception in a runner is that user's ``failed`` result.

    A user has at most one run at a time, across batches: while an earlier (abandoned) run of the same user is still
    alive the new one is ``{"status": "skipped", "reason": "already_running"}``. ``batch_timeout_seconds`` is the
    time left for the whole batch (e.g. until the market opens): once it has passed, users not started are
    ``{"status": "skipped", "reason": "late"}`` and running ones are abandoned as ``{"status": "timeout", "reason": "late"}``.
    The order of ``user_ids`` is the start order (see :func:`fair_order`).
    """
    pending = list(dict.fromkeys(user_ids))
    results: Dict[str, Dict[str, Any]] = {}
    running: Dict[str, Dict[str, Any]] = {}
    changed = threading.Condition()
    max_workers = max(1, int(max_workers))
    batch_deadline = None if batch_timeout_seconds is None else time.monotonic() + max(0.0, float(batch_timeout_seconds))

    def _start(user_id: str) -> None:
        box: Dict[str, Any] = {"done": False, "result": None, "started": time.monotonic()}

        def _target() -> None:
            try:
                outcome = runner(user_id)
            except BaseException as exc:  # noqa: BLE001 - one user's failure is only theirs
                logger.exception("[UserSchedule] run of user %s failed", user_id)
                outcome = {"status": "failed", "error": type(exc).__name__}
            finally:
                _release_user(user_id)
            with changed:
                box["result"] = outcome if isinstance(outcome, dict) else {"status": "unknown"}
                box["done"] = True
                changed.notify_all()

        box["thread"] = threading.Thread(target=_target, daemon=True, name="user-schedule-run")
        running[user_id] = box
        box["thread"].start()

    while True:
        timed_out: List[str] = []
        with changed:
            now = time.monotonic()
            late = batch_deadline is not None and now >= batch_deadline
            for user_id, box in list(running.items()):  # first collect: a freed slot is refilled in this very pass
                if box["done"]:
                    results[user_id] = box["result"]
                    del running[user_id]
                elif late:
                    results[user_id] = {"status": "timeout", "reason": "late"}
                    del running[user_id]
                elif now - box["started"] >= timeout_seconds:
                    results[user_id] = {"status": "timeout"}
                    del running[user_id]
                    timed_out.append(user_id)
            if late:
                for user_id in pending:
                    results[user_id] = {"status": "skipped", "reason": "late"}
                pending = []
            while pending and len(running) < max_workers:
                user_id = pending.pop(0)
                if not _claim_user(user_id):
                    results[user_id] = {"status": "skipped", "reason": "already_running"}
                    continue
                _start(user_id)
            if not (pending or running) and not timed_out:
                break
            if not timed_out:
                soonest = min((timeout_seconds - (now - box["started"]) for box in running.values()), default=0.05)
                if batch_deadline is not None:
                    soonest = min(soonest, batch_deadline - now)
                changed.wait(timeout=max(0.01, min(soonest, 0.5)))
        for user_id in timed_out:  # recorded at once, outside the lock
            logger.warning("[UserSchedule] run of user %s exceeded %ss and was abandoned", user_id, timeout_seconds)
            if on_timeout is not None:
                try:
                    on_timeout(user_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[UserSchedule] recording the timeout of user %s failed: %s", user_id, type(exc).__name__)
    return results


_DAY_LOCK = threading.Lock()
_DAY_STATE: Dict[str, Any] = {"day": None, "plan_cache": {}, "preopen": None}


def _shared_for_day(day: date, key: str, factory: Callable[[], Any]) -> Any:
    """The scheduler's per-trading-day holder of shared market context.

    Two time slots of the same day (users at 18:00 and 19:30) reuse what the first one computed; a new trading day
    starts empty. The context is handed to every user's service explicitly - the services hold no global state.
    """
    with _DAY_LOCK:
        if _DAY_STATE["day"] != day:
            _DAY_STATE.update(day=day, plan_cache={}, preopen=None)
        if _DAY_STATE.get(key) is None:
            _DAY_STATE[key] = factory()
        return _DAY_STATE[key]


def _cached_for_day(day: date, key: str) -> Any:
    with _DAY_LOCK:
        if _DAY_STATE["day"] != day:
            _DAY_STATE.update(day=day, plan_cache={}, preopen=None)
        return _DAY_STATE.get(key)


def _store_for_day(day: date, key: str, value: Any) -> None:
    with _DAY_LOCK:
        if _DAY_STATE["day"] == day:
            _DAY_STATE[key] = value


def reset_shared_state() -> None:
    """Forget the shared per-day context (tests; a new trading day does this by itself)."""
    with _DAY_LOCK:
        _DAY_STATE.update(day=None, plan_cache={}, preopen=None)


def _limits(config: Any) -> Dict[str, Any]:
    workers = getattr(config, "user_schedule_max_workers", 4)
    timeout = getattr(config, "user_schedule_run_timeout_seconds", 1800)
    return {
        "max_workers": workers if isinstance(workers, int) and workers >= 1 else 4,
        "timeout_seconds": timeout if isinstance(timeout, (int, float)) and timeout > 0 else 1800,
    }


# --------------------------------------------------------------------------- next-day plans

def has_plan_content(user_id: str) -> bool:
    """Whether the user has anything to plan: a holding in the plan account or a watchlist entry."""
    from src.repositories.daily_plan_repo import DailyPlanRepository
    from src.services.watchlist_service import WatchlistService

    try:
        if DailyPlanRepository().get_account(user_id).get("holdings"):
            return True
        return bool(WatchlistService().list_codes(user_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[UserSchedule] cannot tell whether user %s has anything to plan: %s", user_id, type(exc).__name__)
        return False


def run_user_daily_plan(user_id: str, shared_board_cache: Dict[Any, Any]) -> Dict[str, Any]:
    """One scheduled next-day plan run of one user (their config view, their model, their own channels)."""
    from src.services.daily_plan_service import DailyPlanRunConflictError, DailyPlanService
    from src.services.user_config_service import get_user_config

    if not has_plan_content(user_id):
        return {"status": "skipped", "reason": "nothing_to_plan"}
    service = DailyPlanService(user_id=user_id, config=get_user_config(user_id), shared_board_cache=shared_board_cache)
    try:
        run = service.trigger_run("schedule", wait=True)
    except DailyPlanRunConflictError:
        return {"status": "skipped", "reason": "already_running"}
    return {"status": run.get("status"), "run_id": run.get("id"), "trade_date": run.get("trade_date")}


def _record_plan_timeout(user_id: str, seconds: Any) -> None:
    from src.repositories.daily_plan_repo import DailyPlanRepository

    DailyPlanRepository().fail_interrupted(user_id, None, f"运行超过 {seconds} 秒，已放弃（其他用户不受影响）")


def run_scheduled_user_daily_plans(
    schedule_time: str,
    *,
    today: Optional[date] = None,
    is_trading_day: Callable[[date], bool] = _is_cn_trading_day,
    user_ids: Optional[Sequence[str]] = None,
    runner: Optional[Callable[[str, Dict[Any, Any]], Dict[str, Any]]] = None,
    max_workers: Optional[int] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """The firing of the user plan job at ``schedule_time``: every due user, in parallel, never raising."""
    try:
        from src.services.user_schedule_service import plan_users_due

        day = today or _china_today()
        if not is_trading_day(day):
            logger.info("[UserDailyPlan] %s 不是交易日，跳过", day)
            return {"status": "skipped", "reason": "not_trading_day", "trade_date": day.isoformat()}
        due = plan_users_due(schedule_time, user_ids)
        if not due:
            return {"status": "skipped", "reason": "no_users_due", "trade_date": day.isoformat(), "users": {}}
        limits = _limits(get_config())
        if max_workers is not None:
            limits["max_workers"] = max_workers
        if timeout_seconds is not None:
            limits["timeout_seconds"] = timeout_seconds
        shared_board_cache = _shared_for_day(day, "plan_cache", dict)  # one board listing per trading day, shared by all
        task = runner or run_user_daily_plan
        outcomes = run_for_users(
            fair_order(due, f"plan|{day.isoformat()}|{schedule_time}"),
            lambda uid: task(uid, shared_board_cache),
            on_timeout=lambda uid: _record_plan_timeout(uid, limits["timeout_seconds"]),
            **limits,
        )
    except Exception as exc:  # noqa: BLE001 - a scheduled job must never take the scheduler down
        logger.exception("[UserDailyPlan] 定时运行异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
    logger.info("[UserDailyPlan] %s %s 完成：%s", day, schedule_time, {uid: o.get("status") for uid, o in outcomes.items()})
    return {"status": "ok", "trade_date": day.isoformat(), "users": outcomes}


# --------------------------------------------------------------------------- pre-open pushes

def shared_is_complete(shared: Any) -> bool:
    """Whether a ``SharedPreopen`` may be kept for the rest of the day: nothing degraded (the overnight fetch succeeded
    for every leader and the board mapping was read). Anything else is only good enough for the batch that built it."""
    return not getattr(shared, "degraded", None)


class _LazyShared:
    """Builds the batch-wide pre-open inputs the first time a user needs them (never when nobody does).

    ``on_built`` receives the built value (the scheduler stores it for the day when it is complete); a day-cached value
    can be handed in as ``cached`` and then nothing is built at all.
    """

    def __init__(self, build: Callable[[], Any], *, cached: Any = None, on_built: Optional[Callable[[Any], None]] = None):
        self._build = build
        self._on_built = on_built
        self._lock = threading.Lock()
        self._value: Any = cached
        self._built = cached is not None

    def get(self) -> Any:
        with self._lock:
            if not self._built:
                self._value = self._build()
                self._built = True
                if self._on_built is not None:
                    try:
                        self._on_built(self._value)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[UserPreopenPush] caching the shared context failed: %s", type(exc).__name__)
            return self._value


def run_user_preopen(user_id: str, shared: Any, *, day: date, now: datetime) -> Dict[str, Any]:
    """One scheduled pre-open push of one user. ``shared`` is a callable returning the batch's ``SharedPreopen``."""
    from src.services.preopen_push import run_preopen_push
    from src.services.user_config_service import get_user_config, has_usable_model
    from src.services.user_notification_service import NO_MODEL_NOTICE_KEY, clear_notice, push_once_notice
    from src.services.watchlist_service import WatchlistService

    config = get_user_config(user_id)
    if not WatchlistService(config=config).list_codes(user_id):
        return {"status": "skipped", "reason": "nothing_to_plan"}
    if not has_usable_model(config):
        # No model: the analysis step cannot run. Marked 未配置模型 and reminded once, not every day.
        reminder = push_once_notice(
            user_id,
            NO_MODEL_NOTICE_KEY,
            "【NexStock】未配置分析模型：你的次日计划 / 盘前推送已暂停。请在个人设置里配置自己的模型 Key，配置后会自动恢复推送。",
        )
        return {"status": "skipped", "reason": "no_model", "label": "未配置模型", "reminder": reminder}
    clear_notice(user_id, NO_MODEL_NOTICE_KEY)
    result = run_preopen_push(config, day=day, now=now, shared=shared(), user_id=user_id)
    return {"status": result.get("status"), "degraded": result.get("degraded"), "pushed": result.get("analysis", {}).get("pushed")}


def run_scheduled_user_preopens(
    schedule_time: str,
    *,
    today: Optional[date] = None,
    now: Optional[datetime] = None,
    is_trading_day: Callable[[date], bool] = _is_cn_trading_day,
    user_ids: Optional[Sequence[str]] = None,
    runner: Optional[Callable[..., Dict[str, Any]]] = None,
    shared_builder: Optional[Callable[[], Any]] = None,
    max_workers: Optional[int] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """The firing of the user pre-open job at ``schedule_time``: every due user, in parallel, never raising."""
    try:
        from src.services.user_schedule_service import preopen_users_due

        day = today or _china_today()
        if not is_trading_day(day):
            logger.info("[UserPreopenPush] %s 不是交易日，跳过", day)
            return {"status": "skipped", "reason": "not_trading_day", "trade_date": day.isoformat()}
        due = preopen_users_due(schedule_time, user_ids)
        if not due:
            return {"status": "skipped", "reason": "no_users_due", "trade_date": day.isoformat(), "users": {}}
        now = now or datetime.now(timezone.utc)
        now_cn = now.astimezone(_CN_TZ)
        earliest = earliest_preopen_time()
        if (now_cn.hour, now_cn.minute) < tuple(int(part) for part in earliest.split(":")):
            # The overnight session is not final yet: building (and storing) the shared context now would be a partial one.
            logger.warning("[UserPreopenPush] %s is before the earliest allowed time %s; nothing was run", schedule_time, earliest)
            return {"status": "skipped", "reason": "too_early", "trade_date": day.isoformat(), "earliest_time": earliest}
        limits = _limits(get_config())
        if max_workers is not None:
            limits["max_workers"] = max_workers
        if timeout_seconds is not None:
            limits["timeout_seconds"] = timeout_seconds
        cutoff_hour, cutoff_minute = (int(part) for part in PREOPEN_BATCH_CUTOFF_TIME.split(":"))
        cutoff = datetime.combine(now_cn.date(), dtime(cutoff_hour, cutoff_minute), tzinfo=_CN_TZ)
        batch_seconds = (cutoff - now_cn).total_seconds()  # <= 0: the batch is already late and every user is skipped

        def _build_shared() -> Any:
            from src.services.preopen_push import build_shared_preopen

            return build_shared_preopen(get_config(), day=day, now=now)

        # One overnight fetch + board mapping per trading day, built when the first user that needs it asks for it.
        # Only a complete result is kept for the day; a degraded one serves this batch and is recomputed next firing.
        shared = _LazyShared(
            shared_builder or _build_shared,
            cached=_cached_for_day(day, "preopen"),
            on_built=lambda value: _store_for_day(day, "preopen", value) if shared_is_complete(value) else None,
        )
        task = runner or run_user_preopen
        outcomes = run_for_users(
            fair_order(due, f"preopen|{day.isoformat()}|{schedule_time}"),
            lambda uid: task(uid, shared.get, day=day, now=now),
            batch_timeout_seconds=batch_seconds,
            **limits,
        )
    except Exception as exc:  # noqa: BLE001 - a scheduled job must never take the scheduler down
        logger.exception("[UserPreopenPush] 定时运行异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
    logger.info("[UserPreopenPush] %s %s 完成：%s", day, schedule_time, {uid: o.get("status") for uid, o in outcomes.items()})
    return {"status": "ok", "trade_date": day.isoformat(), "users": outcomes}


# --------------------------------------------------------------------------- job definitions

def multi_user_scheduling_enabled() -> bool:
    """Non-primary users only exist with ``AUTH_MODE=user_center``; local single-user mode registers nothing extra."""
    try:
        from src.auth import get_auth_mode

        return get_auth_mode() == "user_center"
    except Exception:  # noqa: BLE001
        return False


def build_user_schedule_jobs(config: Any = None, *, config_provider: Optional[Callable[[], Any]] = None) -> List[Dict[str, Any]]:
    """Dynamic job-group definitions for ``Scheduler.register_job_definition`` (empty outside multi-user mode).

    ``times_provider`` is polled by the scheduler loop (about once a minute): it returns the de-duplicated set of times of
    all users with the feature enabled, so a changed personal setting is picked up without a restart.
    """
    if not multi_user_scheduling_enabled():
        return []
    from src.services.user_schedule_service import plan_times_in_use, preopen_times_in_use

    return [
        {
            "dynamic": True,
            "name": USER_DAILY_PLAN_JOB_NAME,
            "times_provider": plan_times_in_use,
            "task_factory": lambda when: (lambda: run_scheduled_user_daily_plans(when)),
            "resync_interval_seconds": RESYNC_INTERVAL_SECONDS,
        },
        {
            "dynamic": True,
            "name": USER_PREOPEN_JOB_NAME,
            "times_provider": preopen_times_in_use,
            "task_factory": lambda when: (lambda: run_scheduled_user_preopens(when)),
            "resync_interval_seconds": RESYNC_INTERVAL_SECONDS,
        },
    ]
