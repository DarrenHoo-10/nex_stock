# -*- coding: utf-8 -*-
"""Persisted screening tasks for the Web picks page.

Every ``POST /screening/screen/tasks`` call creates a ``screening_tasks`` row before the job is queued; the
job writes its progress and final status (completed -> ``run_id`` of the stored ``screening_runs`` result,
failed -> error text) back to that row. The in-memory task queue only drives execution, so the task list and
details survive page switches and process restarts. A row still marked pending/processing whose job is no
longer in the queue (the process restarted) is reported and saved as ``interrupted``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException

from src.storage import utc_naive_now

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset({"pending", "processing"})
INTERRUPTED_MESSAGE = "服务重启，任务没有完成，请重新发起"


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("error") or "选股任务失败")
        return str(detail or "选股任务失败")
    return str(exc) or type(exc).__name__


class ScreeningTaskTracker:
    """Writes one task's lifecycle into ``screening_tasks`` (fail-open for progress, see storage)."""

    def __init__(self, db_manager: Any, task_id: str) -> None:
        self.db = db_manager
        self.task_id = task_id

    def started(self, progress: int, message: str) -> None:
        self.db.update_screening_task(
            self.task_id, status="processing", progress=progress, message=message, started_at=utc_naive_now(),
        )

    def progress(self, progress: int, message: str) -> None:
        self.db.update_screening_task(self.task_id, status="processing", progress=progress, message=message)

    def completed(self, result: Dict[str, Any]) -> None:
        self.db.update_screening_task(
            self.task_id,
            status="completed",
            progress=100,
            message=f"选出 {result.get('candidate_count', 0)} 只",
            run_id=str(result.get("run_id") or "") or None,
            candidate_count=result.get("candidate_count"),
            finished_at=utc_naive_now(),
        )

    def failed(self, exc: Exception) -> None:
        self.db.update_screening_task(
            self.task_id, status="failed", error=_error_text(exc), message="选股失败", finished_at=utc_naive_now(),
        )

    def run(self, job: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        """Run ``job()`` and record the outcome; exceptions are recorded and re-raised."""
        try:
            result = job()
        except Exception as exc:
            self.failed(exc)
            raise
        self.completed(result)
        return result


def reconcile_interrupted(db_manager: Any, task: Dict[str, Any], is_live: Callable[[str], bool]) -> Dict[str, Any]:
    """Mark an active row whose job is gone from the in-memory queue as interrupted."""
    if task.get("status") not in ACTIVE_STATUSES or is_live(task["task_id"]):
        return task
    db_manager.update_screening_task(
        task["task_id"], status="interrupted", error=INTERRUPTED_MESSAGE, message="已中断", finished_at=utc_naive_now(),
    )
    return {**task, "status": "interrupted", "error": INTERRUPTED_MESSAGE, "message": "已中断"}


def list_tasks(
    db_manager: Any, *, limit: int, offset: int, is_live: Callable[[str], bool],
) -> Tuple[List[Dict[str, Any]], int]:
    tasks, total = db_manager.list_screening_tasks(limit=limit, offset=offset)
    return [reconcile_interrupted(db_manager, task, is_live) for task in tasks], total


def get_task(db_manager: Any, task_id: str, *, is_live: Callable[[str], bool]) -> Optional[Dict[str, Any]]:
    """Task row plus, when completed, the stored screening result (``result``)."""
    task = db_manager.get_screening_task(task_id)
    if task is None:
        return None
    task = reconcile_interrupted(db_manager, task, is_live)
    result: Optional[Dict[str, Any]] = None
    if task.get("status") == "completed" and task.get("run_id"):
        run = db_manager.get_screening_run(task["run_id"])
        if run is not None and isinstance(run.get("result"), dict):
            result = run["result"]
    return {**task, "result": result}
