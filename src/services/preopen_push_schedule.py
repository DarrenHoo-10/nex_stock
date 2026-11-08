# -*- coding: utf-8 -*-
"""Scheduler glue for the 8:45 pre-open push (docs/design/core-backend-design.md 5.5).

Same shape as ``board_pipeline_schedule``: ``build_preopen_push_jobs`` turns ``PREOPEN_PUSH_*`` into
daily-job entries for ``Scheduler.add_daily_job``; both scheduler owners use it (the Web/API
``RuntimeSchedulerService`` and the CLI ``--schedule`` loop in ``main.py``). The job runs once per
trading day at ``PREOPEN_PUSH_TIME`` (default 08:15, host local time, so the push lands by 08:45) and
never raises. It is off by default (``PREOPEN_PUSH_ENABLED=false``) and independent of ``SCHEDULE_TIME``.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from src.services.board_pipeline_schedule import _china_today, _is_cn_trading_day

logger = logging.getLogger(__name__)

PREOPEN_PUSH_JOB_NAME = "preopen_push"
DEFAULT_PREOPEN_PUSH_TIME = "08:15"
_RUN_LOCK = threading.Lock()


def run_scheduled_preopen_push(
    config_provider: Callable[[], Any],
    *,
    today: Optional[date] = None,
    is_trading_day: Callable[[date], bool] = _is_cn_trading_day,
    runner: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """One scheduled occurrence: skip when disabled / off-day / still running, else run the push.

    Returns ``{"status": ...}`` (``skipped`` carries a ``reason``); exceptions are caught and
    reported as ``failed`` so the scheduler loop is never affected.
    """
    try:
        config = config_provider()
        if not getattr(config, "preopen_push_enabled", False):
            logger.info("[PreopenPush] 定时任务已关闭 (PREOPEN_PUSH_ENABLED=false)，跳过")
            return {"status": "skipped", "reason": "disabled"}
        day = today or _china_today()
        if not is_trading_day(day):
            logger.info("[PreopenPush] %s 不是交易日，跳过", day)
            return {"status": "skipped", "reason": "not_trading_day", "trade_date": day.isoformat()}
        if not _RUN_LOCK.acquire(blocking=False):
            logger.warning("[PreopenPush] 上一次运行仍在进行，跳过 %s", day)
            return {"status": "skipped", "reason": "already_running", "trade_date": day.isoformat()}
        try:
            if runner is None:
                from src.services.preopen_push import run_preopen_push as runner
            result = runner(config, day=day)
        finally:
            _RUN_LOCK.release()
    except Exception as exc:  # noqa: BLE001 - a scheduled job must never take the scheduler down
        logger.exception("[PreopenPush] 定时运行异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}

    status = result.get("status")
    if status == "succeeded":
        logger.info("[PreopenPush] %s 已推送盘前版", day)
    elif status == "partial":
        logger.warning("[PreopenPush] %s 盘前版部分降级: %s", day, result.get("degraded"))
    else:
        logger.error("[PreopenPush] %s 盘前版失败: %s", day, result.get("analysis"))
    return result


def build_preopen_push_jobs(
    config: Any,
    *,
    config_provider: Callable[[], Any],
) -> List[Dict[str, Any]]:
    """Daily-job entries (kwargs of ``Scheduler.add_daily_job``); empty when the push is disabled."""
    if not getattr(config, "preopen_push_enabled", False):
        return []
    schedule_time = (getattr(config, "preopen_push_time", "") or DEFAULT_PREOPEN_PUSH_TIME).strip()

    def _time_provider() -> str:
        latest = getattr(config_provider(), "preopen_push_time", "") or DEFAULT_PREOPEN_PUSH_TIME
        return latest.strip()

    return [{
        "task": lambda: run_scheduled_preopen_push(config_provider),
        "schedule_time": schedule_time,
        "schedule_time_provider": _time_provider,
        "name": PREOPEN_PUSH_JOB_NAME,
    }]
