# -*- coding: utf-8 -*-
"""Daily plan (次日计划) endpoints (mounted at /api/v1/plan)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Optional, Tuple, Type

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from api.deps import CurrentUser, get_current_user, get_daily_plan_service
from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.daily_plan import (
    CODE_PATTERN,
    AccountResponse,
    HoldingsSnapshotRequest,
    LatestRunResponse,
    PlanRun,
    Sizing,
    SizingRequest,
    StockPlan,
    StockPlanListResponse,
)

from src.config import get_primary_user_id
from src.llm.credentials_guard import ModelKeyRequiredError
from src.services.user_config_service import get_user_config, require_usable_model
from src.utils.url_safety import UnsafeUrlError

logger = logging.getLogger(__name__)

router = APIRouter()

_UNAVAILABLE_RESPONSE = {"description": "次日计划服务未就绪", "model": ErrorResponse}


def _service_exception_types() -> Tuple[Tuple[Type[BaseException], ...], Tuple[Type[BaseException], ...]]:
    """Resolve (conflict, validation) exception classes lazily.

    Returns empty tuples when the backend module is not importable, so the
    error mapping can never crash the handler.
    """
    try:
        from src.services.daily_plan_service import (
            DailyPlanRunConflictError,
            DailyPlanValidationError,
        )
    except ImportError:
        return (), ()
    return (DailyPlanRunConflictError,), (DailyPlanValidationError,)


def _map_exception(message: str, exc: Exception) -> HTTPException:
    conflict_types, validation_types = _service_exception_types()
    if validation_types and isinstance(exc, validation_types):
        return api_error(400, "validation_error", str(exc))
    if conflict_types and isinstance(exc, conflict_types):
        return api_error(409, "daily_plan_run_conflict", "已有次日计划运行在进行中，请稍后再试")
    logger.error("%s: %s", message, exc, exc_info=True)
    return api_error(500, "internal_error", f"{message}: {exc}")


def _call(message: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (HTTPException, ModelKeyRequiredError, UnsafeUrlError):
        raise
    except Exception as exc:
        raise _map_exception(message, exc)


def _not_found(message: str) -> HTTPException:
    return api_error(404, "not_found", message)


_ERROR_RESPONSES = {
    500: {"model": ErrorResponse},
    503: _UNAVAILABLE_RESPONSE,
}


@router.get(
    "/account",
    response_model=AccountResponse,
    responses=_ERROR_RESPONSES,
    summary="账户、持仓与生效的仓位规则",
)
def get_account(service: Any = Depends(get_daily_plan_service)) -> AccountResponse:
    return _call(
        "Get plan account failed",
        lambda: AccountResponse.model_validate(service.get_account()),
    )


@router.put(
    "/holdings",
    response_model=AccountResponse,
    responses={400: {"model": ErrorResponse}, **_ERROR_RESPONSES},
    summary="整体覆盖持仓快照",
)
def put_holdings(
    request: HoldingsSnapshotRequest,
    service: Any = Depends(get_daily_plan_service),
) -> AccountResponse:
    return _call(
        "Save holdings snapshot failed",
        lambda: AccountResponse.model_validate(
            service.set_holdings_snapshot(
                request.cash,
                [item.model_dump() for item in request.holdings],
            )
        ),
    )


@router.get(
    "/stocks",
    response_model=StockPlanListResponse,
    responses=_ERROR_RESPONSES,
    summary="自选 + 持仓的计划摘要列表",
)
def list_stock_plans(
    trade_date: Optional[date] = Query(None, description="交易日 YYYY-MM-DD，缺省为最近交易日"),
    service: Any = Depends(get_daily_plan_service),
) -> StockPlanListResponse:
    return _call(
        "List stock plans failed",
        lambda: StockPlanListResponse.model_validate(service.list_stock_plans(trade_date)),
    )


@router.get(
    "/stocks/{code}",
    response_model=StockPlan,
    responses={404: {"model": ErrorResponse}, **_ERROR_RESPONSES},
    summary="单只股票完整计划（含两个周期）",
)
def get_stock_plan(
    code: str = Path(..., pattern=CODE_PATTERN, description="6 位股票代码"),
    trade_date: Optional[date] = Query(None, description="交易日 YYYY-MM-DD，缺省为最近交易日"),
    service: Any = Depends(get_daily_plan_service),
) -> StockPlan:
    def _run() -> StockPlan:
        payload = service.get_stock_plan(code, trade_date)
        if payload is None:
            raise _not_found(f"未找到 {code} 的次日计划")
        return StockPlan.model_validate(payload)

    return _call("Get stock plan failed", _run)


@router.post(
    "/sizing",
    response_model=Sizing,
    responses={400: {"model": ErrorResponse}, **_ERROR_RESPONSES},
    summary="按仓位规则试算股数",
)
def preview_sizing(
    request: SizingRequest,
    service: Any = Depends(get_daily_plan_service),
) -> Sizing:
    return _call(
        "Preview sizing failed",
        lambda: Sizing.model_validate(
            service.preview_sizing(request.code, request.price, request.horizon)
        ),
    )


# 必须先于 /runs/{run_id} 声明，避免 "latest" 被当作 run_id。
@router.get(
    "/runs/latest",
    response_model=LatestRunResponse,
    responses=_ERROR_RESPONSES,
    summary="最近一次次日计划运行",
)
def get_latest_run(service: Any = Depends(get_daily_plan_service)) -> LatestRunResponse:
    def _run() -> LatestRunResponse:
        payload = service.get_latest_run()
        if payload is None:
            return LatestRunResponse(run=None)
        return LatestRunResponse(run=PlanRun.model_validate(payload))

    return _call("Get latest plan run failed", _run)


@router.get(
    "/runs/{run_id}",
    response_model=PlanRun,
    responses={404: {"model": ErrorResponse}, **_ERROR_RESPONSES},
    summary="指定运行详情",
)
def get_run(
    run_id: str,
    service: Any = Depends(get_daily_plan_service),
) -> PlanRun:
    def _run() -> PlanRun:
        payload = service.get_run(run_id)
        if payload is None:
            raise _not_found(f"未找到运行记录 {run_id}")
        return PlanRun.model_validate(payload)

    return _call("Get plan run failed", _run)


@router.post(
    "/runs",
    response_model=PlanRun,
    status_code=202,
    responses={409: {"model": ErrorResponse}, **_ERROR_RESPONSES},
    summary="手动运行次日计划",
)
def trigger_run(
    service: Any = Depends(get_daily_plan_service),
    user: CurrentUser = Depends(get_current_user),
) -> PlanRun:
    # The run uses the caller's own model and POSITION_* rules; a non-primary user without a model of their
    # own gets 400 model_key_required (viewing existing plans keeps working).
    if isinstance(user, CurrentUser) and user.user_id != get_primary_user_id():
        require_usable_model(get_user_config(user.user_id))
    return _call(
        "Trigger plan run failed",
        lambda: PlanRun.model_validate(service.trigger_run()),
    )
