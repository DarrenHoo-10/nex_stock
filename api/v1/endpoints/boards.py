# -*- coding: utf-8 -*-
"""Board anomaly endpoints (mounted at /api/v1/boards)."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import CurrentUser, get_board_anomaly_service, get_current_user, get_system_config_service, require_admin
from api.v1.errors import api_error
from api.v1.schemas.boards import (
    BoardAnomalyListResponse,
    BoardDetail,
    BoardState,
    BoardType,
    FollowListResponse,
    RecommendationResponse,
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
    dependencies=[Depends(require_admin)],
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


def _preference_repo() -> Any:
    from src.repositories.board_preference_repo import BoardPreferenceRepository

    return BoardPreferenceRepository()


def _recommendation_service(anomaly_service: Any) -> Any:
    from src.services.board_recommendation_service import BoardRecommendationService

    return BoardRecommendationService(anomaly_service=anomaly_service)


def _follow_payload(service: Any, user_id: str) -> dict:
    follows = _preference_repo().list_follows(user_id)
    day, rows = service.board_rows([item["board_ref"] for item in follows])
    return {
        "trade_date": day.isoformat() if day else None,
        "boards": [
            {
                "id": item["board_ref"],
                "name": item["board_name"],
                "board_type": item["board_type"],
                "followed_at": item["followed_at"],
                "today": rows.get(item["board_ref"]),
            }
            for item in follows
        ],
    }


@router.get(
    "/follows",
    response_model=FollowListResponse,
    responses={500: {"model": ErrorResponse}, 503: _UNAVAILABLE_RESPONSE},
    summary="我关注的板块（含最近一次运行的当日数据）",
)
def list_follows(
    service: Any = Depends(get_board_anomaly_service),
    user: CurrentUser = Depends(get_current_user),
) -> FollowListResponse:
    try:
        return FollowListResponse.model_validate(_follow_payload(service, user.user_id))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("List followed boards failed", exc)


@router.put(
    "/follows/{board_id}",
    response_model=FollowListResponse,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}, 503: _UNAVAILABLE_RESPONSE},
    summary="关注板块（重复关注无副作用），返回最新的关注列表",
)
def follow_board(
    board_id: str,
    service: Any = Depends(get_board_anomaly_service),
    user: CurrentUser = Depends(get_current_user),
) -> FollowListResponse:
    try:
        matches = [b for b in service.repo.find_boards(board_id) if b.get("ref") == board_id]
        if not matches:
            raise api_error(404, "not_found", f"未找到板块：{board_id}")
        board = matches[0]
        _preference_repo().follow(user.user_id, board["ref"], board["name"], board["board_type"])
        return FollowListResponse.model_validate(_follow_payload(service, user.user_id))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Follow board failed", exc)


@router.delete(
    "/follows/{board_id}",
    response_model=FollowListResponse,
    responses={500: {"model": ErrorResponse}, 503: _UNAVAILABLE_RESPONSE},
    summary="取消关注板块（未关注时无副作用），返回最新的关注列表",
)
def unfollow_board(
    board_id: str,
    service: Any = Depends(get_board_anomaly_service),
    user: CurrentUser = Depends(get_current_user),
) -> FollowListResponse:
    try:
        _preference_repo().unfollow(user.user_id, board_id)
        return FollowListResponse.model_validate(_follow_payload(service, user.user_id))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Unfollow board failed", exc)


@router.get(
    "/recommendations",
    response_model=RecommendationResponse,
    responses={500: {"model": ErrorResponse}, 503: _UNAVAILABLE_RESPONSE},
    summary="推荐重点跟踪的板块（17:00 板块运行后由分析模型挑选；模型不可用时按规则排序）",
)
def get_recommendations(
    trade_date: Optional[date] = Query(None, description="交易日 YYYY-MM-DD，缺省为最近一次运行"),
    service: Any = Depends(get_board_anomaly_service),
) -> RecommendationResponse:
    try:
        return RecommendationResponse.model_validate(_recommendation_service(service).view(trade_date))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Get board recommendations failed", exc)


@router.post(
    "/recommendations/refresh",
    dependencies=[Depends(require_admin)],
    status_code=202,
    response_model=RecommendationResponse,
    responses={500: {"model": ErrorResponse}, 503: _UNAVAILABLE_RESPONSE},
    summary="后台重新生成最近一次运行的推荐板块；用 GET /recommendations 轮询状态",
)
def refresh_recommendations(service: Any = Depends(get_board_anomaly_service)) -> RecommendationResponse:
    try:
        from src.services.board_recommendation_service import start_refresh

        start_refresh(lambda: _recommendation_service(service))
        return RecommendationResponse.model_validate(_recommendation_service(service).view(None))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Refresh board recommendations failed", exc)


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
