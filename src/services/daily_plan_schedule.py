# -*- coding: utf-8 -*-
"""Scheduler glue for the daily next-day plan run (docs/daily-plan-api.md, docs/design/core-product-spec.md 7.1).

Same shape as ``board_pipeline_schedule``: ``build_daily_plan_jobs`` turns ``DAILY_PLAN_ENABLED`` and the
schedule time(s) into daily-job entries for ``Scheduler.add_daily_job``; both scheduler owners use it (the Web/API
``RuntimeSchedulerService`` and the CLI ``--schedule`` loop in ``main.py``).

In multi-user mode the scheduled run is the primary user's (their ``.env`` watchlist and plan account); other
users trigger their own runs through the API until per-user scheduling lands.

The plan run reuses ``SCHEDULE_TIME`` / ``SCHEDULE_TIMES`` (the Web settings page edits them as the plan's run
time) but is a **separate job**: it runs in its own thread and never touches the legacy per-stock daily analysis
that the same setting drives, so the two never block or replace each other. It runs once per A-share trading day
per configured time and never raises: a failed run is logged and recorded on the run itself.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from src.scheduler import normalize_schedule_times
from src.services.board_pipeline_schedule import _china_today, _is_cn_trading_day

logger = logging.getLogger(__name__)

DAILY_PLAN_JOB_NAME = "daily_plan"
DEFAULT_DAILY_PLAN_TIME = "18:00"


def configured_times(config: Any) -> List[str]:
    """The plan run times: ``SCHEDULE_TIMES`` when set, else ``SCHEDULE_TIME`` (same rule as the daily analysis)."""
    return normalize_schedule_times(
        getattr(config, "schedule_times", None),
        fallback_time=getattr(config, "schedule_time", DEFAULT_DAILY_PLAN_TIME) or DEFAULT_DAILY_PLAN_TIME,
    )


def run_scheduled_daily_plan(
    config_provider: Callable[[], Any],
    index: int = 0,
    *,
    today: Optional[date] = None,
    is_trading_day: Callable[[date], bool] = _is_cn_trading_day,
    service_factory: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """One scheduled occurrence: skip when disabled / off-day / another run is going, else run the plan inline.

    ``index`` selects which configured time fired; a job whose time was removed from the settings since it was
    registered skips. Returns ``{"status": ..., ...}``; exceptions are caught and reported as ``failed``.
    """
    try:
        config = config_provider()
        if not getattr(config, "daily_plan_enabled", False):
            logger.info("[DailyPlan] 定时任务已关闭 (DAILY_PLAN_ENABLED=false)，跳过")
            return {"status": "skipped", "reason": "disabled"}
        if index >= len(configured_times(config)):
            return {"status": "skipped", "reason": "time_removed"}
        day = today or _china_today()
        if not is_trading_day(day):
            logger.info("[DailyPlan] %s 不是交易日，跳过", day)
            return {"status": "skipped", "reason": "not_trading_day", "trade_date": day.isoformat()}
        if service_factory is None:
            from src.config import get_primary_user_id
            from src.services.daily_plan_service import DailyPlanService

            # P2: the scheduled run belongs to the primary user (per-user scheduling is P3).
            def service_factory() -> Any:
                return DailyPlanService(user_id=get_primary_user_id())
        from src.services.daily_plan_service import DailyPlanRunConflictError

        try:
            run = service_factory().trigger_run("schedule", wait=True)
        except DailyPlanRunConflictError:
            logger.warning("[DailyPlan] 已有次日计划运行在进行，跳过 %s 的定时运行", day)
            return {"status": "skipped", "reason": "already_running", "trade_date": day.isoformat()}
    except Exception as exc:  # noqa: BLE001 - a scheduled job must never take the scheduler down
        logger.exception("[DailyPlan] 定时运行异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}

    status = run.get("status")
    if status in ("succeeded", "skipped"):
        logger.info("[DailyPlan] %s 完成 (run_id=%s, %s)", day, run.get("id"), status)
    elif status == "partial":
        problems = [f"{s['key']}:{s['status']}" for s in run.get("steps", []) if s.get("status") in ("partial", "failed")]
        logger.warning("[DailyPlan] %s 部分完成 (run_id=%s): %s", day, run.get("id"), problems)
    else:
        logger.error("[DailyPlan] %s 失败 (run_id=%s)", day, run.get("id"))
    return {"status": status, "run_id": run.get("id"), "trade_date": run.get("trade_date")}


def build_daily_plan_jobs(
    config: Any,
    *,
    config_provider: Callable[[], Any],
) -> List[Dict[str, Any]]:
    """Daily-job entries (kwargs of ``Scheduler.add_daily_job``), one per configured run time.

    Empty when ``DAILY_PLAN_ENABLED`` is off. The switch and the times are re-read from the live config on every
    firing / poll, so Web or ``.env`` changes apply without a restart.
    """
    if not getattr(config, "daily_plan_enabled", False):
        logger.info("[DailyPlan] DAILY_PLAN_ENABLED=false，不注册次日计划定时任务")
        return []
    jobs: List[Dict[str, Any]] = []
    for index, schedule_time in enumerate(configured_times(config)):
        def _time_provider(index: int = index, fallback: str = schedule_time) -> str:
            times = configured_times(config_provider())
            return times[index] if index < len(times) else fallback

        jobs.append({
            "task": lambda index=index: run_scheduled_daily_plan(config_provider, index),
            "schedule_time": schedule_time,
            "schedule_time_provider": _time_provider,
            "name": f"{DAILY_PLAN_JOB_NAME}_{schedule_time.replace(':', '')}",
        })
    return jobs
