# -*- coding: utf-8 -*-
"""Scheduler glue for the daily board pipeline (docs/design/core-backend-design.md 2.9).

``build_board_pipeline_jobs`` turns the ``BOARD_PIPELINE_*`` config into daily-job entries for
``Scheduler.add_daily_job``; both scheduler owners use it: the Web/API runtime scheduler
(``RuntimeSchedulerService``) and the CLI ``--schedule`` loop (``main.py``). The job runs
``run_board_pipeline`` once per trading day at ``BOARD_PIPELINE_SCHEDULE_TIME`` (default 17:00,
host local time like ``SCHEDULE_TIME``) and never raises: a failed run is logged and a
``partial`` run is a warning.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BOARD_PIPELINE_JOB_NAME = "board_pipeline"
DEFAULT_BOARD_PIPELINE_TIME = "17:00"
_CN_TZ = ZoneInfo("Asia/Shanghai")
_RUN_LOCK = threading.Lock()


def _china_today() -> date:
    return datetime.now(_CN_TZ).date()


def _is_cn_trading_day(day: date) -> bool:
    from src.core.trading_calendar import is_market_open

    return is_market_open("cn", day)  # fail-open when the calendar library is unavailable


def run_scheduled_board_pipeline(
    config_provider: Callable[[], Any],
    *,
    today: Optional[date] = None,
    is_trading_day: Callable[[date], bool] = _is_cn_trading_day,
    pipeline: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """One scheduled occurrence: skip off-days / missing token, else run the pipeline for today.

    Returns ``{"status": ...}`` (``skipped`` carries a ``reason``); exceptions are caught and
    reported as ``failed`` so the scheduler loop is never affected.
    """
    try:
        config = config_provider()
        if not getattr(config, "board_pipeline_enabled", False):
            logger.info("[BoardPipeline] 定时任务已关闭 (BOARD_PIPELINE_ENABLED=false)，跳过")
            return {"status": "skipped", "reason": "disabled"}
        day = today or _china_today()
        if not is_trading_day(day):
            logger.info("[BoardPipeline] %s 不是交易日，跳过", day)
            return {"status": "skipped", "reason": "not_trading_day", "trade_date": day.isoformat()}
        if not (getattr(config, "tushare_token", None) or "").strip():
            logger.info("[BoardPipeline] 未配置 TUSHARE_TOKEN，跳过 %s 的板块异动流程", day)
            return {"status": "skipped", "reason": "no_tushare_token", "trade_date": day.isoformat()}
        if not _RUN_LOCK.acquire(blocking=False):
            logger.warning("[BoardPipeline] 上一次运行仍在进行，跳过 %s", day)
            return {"status": "skipped", "reason": "already_running", "trade_date": day.isoformat()}
        try:
            if pipeline is None:
                from src.services.board_pipeline import run_board_pipeline as pipeline
            result = pipeline(day)
        finally:
            _RUN_LOCK.release()
    except Exception as exc:  # noqa: BLE001 - a scheduled job must never take the scheduler down
        logger.exception("[BoardPipeline] 定时运行异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}

    status = result.get("status")
    if status == "succeeded":
        logger.info("[BoardPipeline] %s 完成 (run_id=%s)", day, result.get("run_id"))
    elif status == "partial":
        problems = [f"{s['key']}:{s['status']}" for s in result.get("steps", []) if s.get("status") in ("partial", "failed")]
        logger.warning("[BoardPipeline] %s 部分完成 (run_id=%s): %s", day, result.get("run_id"), problems)
    elif status == "skipped":
        logger.info("[BoardPipeline] %s 跳过: %s", day, result.get("reason"))
    else:
        logger.error("[BoardPipeline] %s 失败 (run_id=%s): %s", day, result.get("run_id"), result.get("error"))
    return result


def build_board_pipeline_jobs(
    config: Any,
    *,
    config_provider: Callable[[], Any],
) -> List[Dict[str, Any]]:
    """Daily-job entries (kwargs of ``Scheduler.add_daily_job``) for the board pipeline.

    Empty when ``BOARD_PIPELINE_ENABLED`` is off. The Tushare token is checked on every run (so a
    token added in the Web settings takes effect without a restart); a missing token only logs.
    """
    if not getattr(config, "board_pipeline_enabled", False):
        logger.info("[BoardPipeline] BOARD_PIPELINE_ENABLED=false，不注册板块异动定时任务")
        return []
    schedule_time = (getattr(config, "board_pipeline_schedule_time", "") or DEFAULT_BOARD_PIPELINE_TIME).strip()
    if not (getattr(config, "tushare_token", None) or "").strip():
        logger.info("[BoardPipeline] 当前未配置 TUSHARE_TOKEN：已注册板块异动定时任务，但运行时会跳过")

    def _time_provider() -> str:
        latest = getattr(config_provider(), "board_pipeline_schedule_time", "") or DEFAULT_BOARD_PIPELINE_TIME
        return latest.strip()

    return [{
        "task": lambda: run_scheduled_board_pipeline(config_provider),
        "schedule_time": schedule_time,
        "schedule_time_provider": _time_provider,
        "name": BOARD_PIPELINE_JOB_NAME,
    }]
