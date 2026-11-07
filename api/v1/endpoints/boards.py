# -*- coding: utf-8 -*-
"""Board anomaly endpoints (mounted at /api/v1/boards)."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_board_anomaly_service, get_system_config_service
from api.v1.errors import api_error
from api.v1.schemas.boards import (
    BoardAnomalyListResponse,
    BoardDetail,
    BoardState,
    BoardType,
    ThresholdListResponse,
    ThresholdUpdateRequest,
)
from api.v1.schemas.common import ErrorResponse
from src.services.system_config_service import (
    ConfigConflictError,
    ConfigValidationError,
    SystemConfigService,
)

logger = logging.getLogger(__name__)

router = APIRouter()

THRESHOLDS_CONFIG_KEY = "BOARD_ANOMALY_THRESHOLDS"

_UNAVAILABLE_RESPONSE = {"description": "次日计划服务未就绪", "model": ErrorResponse}


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error("%s: %s", message, exc, exc_info=True)
    return api_error(500, "internal_error", f"{message}: {exc}")


def _read_config_version(config_service: SystemConfigService) -> str:
    config_data = config_service.get_config(include_schema=False)
    return config_data.get("config_version", "")


@router.get(
    "/anomalies",
    response_model=BoardAnomalyListResponse,
    responses={
        500: {"model": ErrorResponse},
        503: _UNAVAILABLE_RESPONSE,
    },
    summary="市场概况与板块异动列表",
)
def list_board_anomalies(
    trade_date: Optional[date] = Query(None, description="交易日 YYYY-MM-DD，缺省为最近交易日"),
    board_type: Optional[BoardType] = Query(None, description="板块类型 industry | concept"),
    state: Optional[BoardState] = Query(None, description="板块状态"),
    service: Any = Depends(get_board_anomaly_service),
) -> BoardAnomalyListResponse:
    try:
        payload = service.list_anomalies(trade_date, board_type, state)
        return BoardAnomalyListResponse.model_validate(payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("List board anomalies failed", exc)


# 必须先于 /{board_id} 声明，避免 "thresholds" 被当作 board_id。
@router.get(
    "/thresholds",
    response_model=ThresholdListResponse,
    responses={
        500: {"model": ErrorResponse},
        503: _UNAVAILABLE_RESPONSE,
    },
    summary="板块异动阈值列表（含依据等级、默认值、当前值）",
)
def list_thresholds(
    service: Any = Depends(get_board_anomaly_service),
) -> ThresholdListResponse:
    try:
        return ThresholdListResponse.model_validate({"items": service.get_threshold_definitions()})
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("List board thresholds failed", exc)


@router.put(
    "/thresholds",
    response_model=ThresholdListResponse,
    responses={
        400: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: _UNAVAILABLE_RESPONSE,
    },
    summary="覆盖板块异动阈值",
    description="先校验再写入 BOARD_ANOMALY_THRESHOLDS；overrides 为空对象表示恢复默认。",
)
def update_thresholds(
    request: ThresholdUpdateRequest,
    service: Any = Depends(get_board_anomaly_service),
    config_service: SystemConfigService = Depends(get_system_config_service),
) -> ThresholdListResponse:
    try:
        errors = service.validate_threshold_overrides(request.overrides)
        if errors:
            raise api_error(
                400,
                "validation_error",
                "；".join(errors),
                detail=list(errors),
            )
        value = (
            json.dumps(request.overrides, ensure_ascii=False, sort_keys=True)
            if request.overrides
            else ""
        )
        try:
            config_service.update(
                config_version=_read_config_version(config_service),
                items=[{"key": THRESHOLDS_CONFIG_KEY, "value": value}],
                mask_token="******",
                reload_now=True,
            )
        except ConfigValidationError as exc:
            raise api_error(
                400,
                "validation_error",
                "阈值配置校验失败",
                detail=exc.issues,
            )
        except ConfigConflictError as exc:
            raise api_error(
                409,
                "config_version_conflict",
                "配置已被修改，请刷新后重试",
                detail={"current_config_version": exc.current_version},
            )
        return ThresholdListResponse.model_validate({"items": service.get_threshold_definitions()})
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Update board thresholds failed", exc)


@router.get(
    "/{board_id}",
    response_model=BoardDetail,
    responses={
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: _UNAVAILABLE_RESPONSE,
    },
    summary="板块详情",
)
def get_board_detail(
    board_id: str,
    trade_date: Optional[date] = Query(None, description="交易日 YYYY-MM-DD，缺省为最近交易日"),
    service: Any = Depends(get_board_anomaly_service),
) -> BoardDetail:
    try:
        payload = service.get_board_detail(board_id, trade_date)
        if payload is None:
            raise api_error(404, "not_found", f"未找到板块 {board_id}")
        return BoardDetail.model_validate(payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Get board detail failed", exc)
