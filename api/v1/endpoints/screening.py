# -*- coding: utf-8 -*-
"""Stock screening routes."""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from api.deps import CurrentUser, get_config_dep, get_current_user, get_database_manager, require_admin
from api.v1.errors import api_error
from src.config import Config, get_primary_user_id
from src.services.screening.config import Config as ScreeningRuntimeConfig
from src.services.screening.filter import check_hard_filters, validate_check_snapshot
from src.services.screening.strategy import load_all_strategies_with_extra
from src.services import screening_task_service
from src.services.screening_service import ScreeningService, _ensure_screening_enabled
from src.services.screening_task_service import ScreeningTaskTracker
from src.services.task_queue import TaskStatus as QueueTaskStatus
from src.services.task_queue import get_task_queue
from src.storage import DatabaseManager

from src.services.screening.scope import ScreeningScope

router = APIRouter()


class ScreeningScreenRequest(BaseModel):
    market: str = Field("cn", min_length=1, max_length=16)
    strategy: str = Field("dual_low", min_length=1, max_length=64)
    max_results: int = Field(20, ge=1, le=100)
    variant_seed: str = Field("", max_length=128)
    scope: ScreeningScope = Field(default_factory=ScreeningScope)

    @model_validator(mode="after")
    def validate_scope_market(self):
        if self.market != "cn" and (self.scope.boards or self.scope.topics):
            raise ValueError("交易板块和题材筛选仅支持 A 股")
        return self


class ScreeningCheckRequest(BaseModel):
    market: Literal["cn", "us"] = "cn"
    strategy: str = Field("dual_low", min_length=1, max_length=64)
    snapshot: Dict[str, Any] = Field(description="One supplied row; no live data is fetched")

    @field_validator("snapshot", mode="before")
    @classmethod
    def validate_snapshot(cls, value: Any) -> Dict[str, Any]:
        try:
            return validate_check_snapshot(value)
        except ValueError as exc:
            raise api_error(422, "screening_invalid_snapshot", str(exc)) from exc


class ScreeningConditionCheck(BaseModel):
    filter: str
    field: str
    source_field: Optional[str]
    threshold: Any
    current_value: Any
    status: Literal["pass", "fail", "missing"]
    passed: Optional[bool]
    missing_reason: Optional[str]


class ScreeningCheckResponse(BaseModel):
    strategy: str
    strategy_version: str
    market: Literal["cn", "us"]
    provenance: Literal["supplied_snapshot"] = "supplied_snapshot"
    passed: bool
    checks: List[ScreeningConditionCheck]


class ScreeningStrategyResponse(BaseModel):
    id: str
    name: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    tag: str = ""
    tags: List[str] = Field(default_factory=list)
    market_scope: List[str] = Field(default_factory=list)
    market: str = ""
    analysis_skills: List[str] = Field(default_factory=list)


class ScreeningScreenAccepted(BaseModel):
    task_id: str
    trace_id: str
    status: str = "pending"
    message: str
    strategy: str
    market: str
    max_results: int


class ScreeningScreenTaskStatus(BaseModel):
    task_id: str
    trace_id: Optional[str] = None
    status: str
    progress: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    # Persisted task fields (absent for tasks started before task persistence existed).
    strategy: Optional[str] = None
    market: Optional[str] = None
    max_results: Optional[int] = None
    scope: Optional[Dict[str, Any]] = None
    run_id: Optional[str] = None
    candidate_count: Optional[int] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class ScreeningTaskListResponse(BaseModel):
    tasks: List[ScreeningScreenTaskStatus]
    total: int


def _service(config: Config, db_manager: Any = None) -> ScreeningService:
    usable_db = db_manager if callable(getattr(db_manager, "save_screening_run", None)) else None
    return ScreeningService(config=config, db_manager=usable_db)


def _task_store(db_manager: Any) -> Any:
    """The database manager when it can persist screening tasks (tests may inject a bare stub)."""
    return db_manager if callable(getattr(db_manager, "create_screening_task", None)) else None


def _queue_knows(task_id: str) -> bool:
    task = get_task_queue().get_task(task_id)
    return task is not None and task.report_type == "screening_screen"


def _screening_task_not_found(task_id: str) -> HTTPException:
    return api_error(
        404,
        "screening_screen_task_not_found",
        f"选股任务 {task_id} 不存在或已过期",
    )


@router.get("/status")
def screening_status(config: Config = Depends(get_config_dep)) -> Dict[str, Any]:
    return _service(config).status()


@router.get("/strategies")
def screening_strategies(
    request: Request,
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).strategies()


@router.get("/topics")
def screening_topics(
    q: str = Query("", max_length=80),
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).search_topics(q)


@router.get("/hotspots")
def screening_hotspots(
    provider: str = Query("", max_length=32),
    top: int = Query(12, ge=1, le=50),
    refresh: bool = Query(False),
    include_details: bool = Query(False),
    sort_by: str = Query("heat", pattern="^(heat|limit_up_count)$"),
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    if sort_by == "limit_up_count":
        return _service(config).limit_up_hotspots(top=top, refresh=refresh_value)
    include_details_value = (
        include_details
        if isinstance(include_details, bool)
        else bool(getattr(include_details, "default", False))
    )
    return _service(config).hotspots(
        provider=provider,
        top=top,
        refresh=refresh_value,
        include_details=include_details_value,
    )


@router.get("/hotspots/{topic:path}")
def screening_hotspot_detail(
    topic: str,
    provider: str = Query("", max_length=32),
    refresh: bool = Query(False),
    include_search: bool = Query(False),
    config: Config = Depends(get_config_dep),
    user: CurrentUser = Depends(get_current_user),
) -> Dict[str, Any]:
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    include_search_value = (
        include_search
        if isinstance(include_search, bool)
        else bool(getattr(include_search, "default", False))
    )
    if isinstance(user, CurrentUser) and not user.is_admin:
        include_search_value = False  # web search uses the system search keys; personal accounts skip it
    return _service(config).hotspot_detail(
        topic=topic,
        provider=provider,
        refresh=refresh_value,
        include_search=include_search_value,
    )


@router.post("/screen/check", response_model=ScreeningCheckResponse)
def screening_check(
    request: ScreeningCheckRequest,
    config: Config = Depends(get_config_dep),
) -> ScreeningCheckResponse:
    """Check supplied values against all strategy hard filters, without fetching."""
    _ensure_screening_enabled(config)
    strategies = load_all_strategies_with_extra(ScreeningRuntimeConfig.from_env())
    # Lookup catalog IDs only; never interpolate caller input into a file path.
    strategy = strategies.get(request.strategy)
    if strategy is None:
        raise api_error(422, "screening_invalid_strategy", "Unknown or disabled screening strategy.")
    if request.market not in strategy.screening.market_scope:
        raise api_error(422, "screening_invalid_market", "Strategy does not support this market.")
    checks = check_hard_filters(request.snapshot, strategy.screening.hard_filters)
    return ScreeningCheckResponse(
        strategy=strategy.name,
        strategy_version=strategy.version,
        market=request.market,
        passed=all(item["passed"] is True for item in checks),
        checks=checks,
    )


# Screening ranks candidates with the SYSTEM model and enriches them with the SYSTEM search keys (its runtime
# bridges them through process-wide environment variables), so running it is an administrator action; every user
# can still read the shared results (history, strategies, hotspots).
@router.post(
    "/screen/tasks", status_code=202, response_model=ScreeningScreenAccepted,
    dependencies=[Depends(require_admin)],
)
def screening_start_screen_task(
    request: ScreeningScreenRequest,
    http_request: Request,
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
    user: CurrentUser = Depends(get_current_user),
) -> ScreeningScreenAccepted:
    task_id = uuid.uuid4().hex
    task_queue = get_task_queue()
    store = _task_store(db_manager)
    tracker = ScreeningTaskTracker(store, task_id) if store is not None else None
    if store is not None:
        store.create_screening_task(
            task_id=task_id,
            strategy=request.strategy,
            market=request.market,
            max_results=request.max_results,
            scope=request.scope.model_dump(),
            message="选股任务已提交",
        )

    def run_screen() -> Dict[str, Any]:
        started_message = "正在执行选股，外部数据源较慢时会持续后台运行"
        task_queue.update_task_progress(task_id, 20, started_message)
        if tracker is not None:
            tracker.started(20, started_message)

        def report_progress(progress: int, message: str) -> None:
            task_queue.update_task_progress(task_id, progress, message)
            if tracker is not None:
                tracker.progress(progress, message)

        def job() -> Dict[str, Any]:
            result = _service(config, db_manager).screen(
                strategy=request.strategy,
                market=request.market,
                max_results=request.max_results,
                selection_seed=request.variant_seed,
                scope=request.scope,
                progress_callback=report_progress,
            )
            task_queue.update_task_progress(
                task_id,
                98,
                f"选股已完成，正在整理 {result.get('candidate_count', 0)} 条候选",
            )
            return result

        return tracker.run(job) if tracker is not None else job()

    task = task_queue.submit_background_task(
        run_screen,
        stock_code="screening_screen",
        stock_name=f"{request.strategy} / {request.market}",
        report_type="screening_screen",
        message="选股任务已提交",
        task_id=task_id,
        trace_id=task_id,
        # Only the submitting administrator sees the task in the task list / stream.
        **(
            {"user_id": user.user_id}
            if isinstance(user, CurrentUser) and user.user_id != get_primary_user_id()
            else {}
        ),
    )
    return ScreeningScreenAccepted(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        message=task.message or "选股任务已提交",
        strategy=request.strategy,
        market=request.market,
        max_results=request.max_results,
    )


@router.get("/screen/tasks", response_model=ScreeningTaskListResponse)
def screening_list_screen_tasks(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> ScreeningTaskListResponse:
    """Persisted screening tasks, newest first (no results; open one task for its candidates)."""
    store = _task_store(db_manager)
    if store is None:
        return ScreeningTaskListResponse(tasks=[], total=0)
    tasks, total = screening_task_service.list_tasks(store, limit=limit, offset=offset, is_live=_queue_knows)
    return ScreeningTaskListResponse(
        tasks=[ScreeningScreenTaskStatus(trace_id=task["task_id"], **task) for task in tasks],
        total=total,
    )


@router.get("/screen/tasks/{task_id}", response_model=ScreeningScreenTaskStatus)
def screening_screen_task_status(
    task_id: str,
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> ScreeningScreenTaskStatus:
    store = _task_store(db_manager)
    persisted = (
        screening_task_service.get_task(store, task_id, is_live=_queue_knows) if store is not None else None
    )
    task = get_task_queue().get_task(task_id)
    if task is not None and task.report_type == "screening_screen":
        result = task.result if task.status == QueueTaskStatus.COMPLETED and isinstance(task.result, dict) else None
        fields = {key: value for key, value in (persisted or {}).items() if key not in {"status", "progress", "message", "error", "result", "task_id"}}
        return ScreeningScreenTaskStatus(
            task_id=task.task_id,
            trace_id=task.trace_id or task.task_id,
            status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
            progress=task.progress,
            message=task.message,
            error=task.error,
            result=result if result is not None else (persisted or {}).get("result"),
            **fields,
        )
    if persisted is None:
        raise _screening_task_not_found(task_id)
    return ScreeningScreenTaskStatus(trace_id=persisted["task_id"], **persisted)


@router.post("/screen", dependencies=[Depends(require_admin)])
def screening_screen(
    request: ScreeningScreenRequest,
    http_request: Request,
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    return _service(config, db_manager).screen(
        strategy=request.strategy,
        market=request.market,
        max_results=request.max_results,
        selection_seed=request.variant_seed,
        scope=request.scope,
    )


@router.get("/history")
def screening_history(
    limit: int = Query(20, ge=1, le=100),
    strategy: str = Query("", max_length=64),
    market: str = Query("", max_length=16),
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    return _service(config, db_manager).history(
        limit=limit,
        strategy=strategy,
        market=market,
    )


@router.get("/history/{run_id}")
def screening_history_detail(
    run_id: str,
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    return _service(config, db_manager).history_detail(run_id)


@router.get("/source-history")
def screening_source_history(
    limit: int = Query(100, ge=1, le=100),
    config: Config = Depends(get_config_dep),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> Dict[str, Any]:
    return _service(config, db_manager).source_history(limit=limit)
