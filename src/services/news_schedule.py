# -*- coding: utf-8 -*-
"""Scheduler glue for the AKShare news pool (docs/design/core-backend-design.md 5.4).

Mirrors ``board_pipeline_schedule``: the builders turn config into scheduler entries and both
scheduler owners use them - the Web/API ``RuntimeSchedulerService`` and the CLI ``--schedule`` loop in
``main.py``. Everything is gated by the existing ``NEWS_INTEL_AUTO_FETCH_ENABLED`` switch (default off).

* ``news_poll`` background task, ticking every ``min(NEWS_POLL_INTERVAL_MINUTES,
  NEWS_POLL_OFFHOURS_INTERVAL_MINUTES)``; ``NewsPoolService.poll_tick`` decides whether the interval of
  the *current period* (trading hours 10 min / otherwise 30 min) has elapsed, using the stored
  per-source ``last_fetch_at`` so a scheduler restart does not refetch at once.
* ``news_classify`` background task (when ``NEWS_CLASSIFY_ENABLED``) on its own thread so a slow model
  never delays fetching; it is a no-op when nothing is unclassified.
* ``news_daily_fetch`` daily job at ``NEWS_DAILY_FETCH_TIME`` on trading days: announcements + 财经早餐.

Runs never raise; the scheduler skips an occurrence whose previous run is still alive.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from src.services.news_pool_service import _is_cn_trading_day, china_now

logger = logging.getLogger(__name__)

NEWS_POLL_TASK_NAME = "news_poll"
NEWS_CLASSIFY_TASK_NAME = "news_classify"
NEWS_DAILY_JOB_NAME = "news_daily_fetch"
DEFAULT_NEWS_DAILY_TIME = "08:05"


def _enabled(config: Any) -> bool:
    return getattr(config, "news_intel_auto_fetch_enabled", False) is True


def run_scheduled_news_poll(
    config_provider: Callable[[], Any],
    *,
    service: Any = None,
) -> Dict[str, Any]:
    """One tick of the flash poll; the service applies the interval gating."""
    try:
        config = config_provider()
        if not _enabled(config):
            return {"status": "skipped", "reason": "disabled"}
        if service is None:
            from src.services.news_pool_service import NewsPoolService

            service = NewsPoolService(config=config)
        return service.poll_tick()
    except Exception as exc:  # noqa: BLE001 - a scheduled task must never take the scheduler down
        logger.exception("[News] 快讯轮询异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}


def run_scheduled_news_classify(
    config_provider: Callable[[], Any],
    *,
    classifier: Any = None,
) -> Dict[str, Any]:
    try:
        config = config_provider()
        if not _enabled(config) or not getattr(config, "news_classify_enabled", True):
            return {"status": "skipped", "reason": "disabled"}
        if classifier is None:
            from src.services.news_classifier import NewsClassifier

            classifier = NewsClassifier(config=config)
        return classifier.classify_pending()
    except Exception as exc:  # noqa: BLE001
        logger.exception("[NewsClassify] 分类任务异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}


def run_scheduled_news_daily(
    config_provider: Callable[[], Any],
    *,
    today: Optional[date] = None,
    is_trading_day: Callable[[date], bool] = _is_cn_trading_day,
    service: Any = None,
) -> Dict[str, Any]:
    """Announcements + 财经早餐, once per trading day."""
    try:
        config = config_provider()
        if not _enabled(config):
            return {"status": "skipped", "reason": "disabled"}
        day = today or china_now().date()
        if not is_trading_day(day):
            logger.info("[News] %s 不是交易日，跳过公告与财经早餐", day)
            return {"status": "skipped", "reason": "not_trading_day", "day": day.isoformat()}
        if service is None:
            from src.services.news_pool_service import NewsPoolService

            service = NewsPoolService(config=config)
        result = service.run_daily_fetch(day=day)
        logger.info("[News] %s 公告/财经早餐: %s", day, result.get("status"))
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("[News] 公告/财经早餐任务异常: %s", exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}


def news_poll_tick_seconds(config: Any) -> int:
    minutes = min(
        int(getattr(config, "news_poll_interval_minutes", 10) or 10),
        int(getattr(config, "news_poll_offhours_interval_minutes", 30) or 30),
    )
    return max(1, minutes) * 60


def build_news_background_tasks(config: Any, *, config_provider: Callable[[], Any]) -> List[Dict[str, Any]]:
    """Background-task entries (kwargs of ``Scheduler.add_background_task``); empty when the switch is off."""
    if not _enabled(config):
        return []
    tasks = [{
        "task": lambda: run_scheduled_news_poll(config_provider),
        "interval_seconds": news_poll_tick_seconds(config),
        "run_immediately": True,
        "name": NEWS_POLL_TASK_NAME,
    }]
    if getattr(config, "news_classify_enabled", True):
        tasks.append({
            "task": lambda: run_scheduled_news_classify(config_provider),
            "interval_seconds": news_poll_tick_seconds(config),
            "run_immediately": False,
            "name": NEWS_CLASSIFY_TASK_NAME,
        })
    return tasks


def build_news_daily_jobs(config: Any, *, config_provider: Callable[[], Any]) -> List[Dict[str, Any]]:
    """Daily-job entries (kwargs of ``Scheduler.add_daily_job``); empty when the switch is off."""
    if not _enabled(config):
        return []
    schedule_time = (getattr(config, "news_daily_fetch_time", "") or DEFAULT_NEWS_DAILY_TIME).strip()

    def _time_provider() -> str:
        return (getattr(config_provider(), "news_daily_fetch_time", "") or DEFAULT_NEWS_DAILY_TIME).strip()

    return [{
        "task": lambda: run_scheduled_news_daily(config_provider),
        "schedule_time": schedule_time,
        "schedule_time_provider": _time_provider,
        "name": NEWS_DAILY_JOB_NAME,
    }]
