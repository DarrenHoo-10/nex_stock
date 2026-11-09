# -*- coding: utf-8 -*-
"""Data capability and quality endpoints."""

from __future__ import annotations

import logging

import re
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.deps import get_config_dep
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.data_capability import DataCapabilityOverviewResponse
from src.config import Config
from src.services.data_capability_service import DataCapabilityService

logger = logging.getLogger(__name__)

router = APIRouter()


def _overview_response(config: Config, *, runtime_scheduler: object = None) -> DataCapabilityOverviewResponse:
    try:
        payload = DataCapabilityService(
            config=config,
            runtime_scheduler=runtime_scheduler,
        ).get_overview()
        return DataCapabilityOverviewResponse.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - keep diagnostics fail-open at API boundary.
        logger.error("Failed to build data capability overview: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Failed to build data capability overview",
            },
        )


@router.get(
    "/overview",
    response_model=DataCapabilityOverviewResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get data capability overview",
    description="Return provider capabilities, dataset quality, and source priority without exposing secrets.",
)
def get_data_overview(
    request: Request,
    config: Config = Depends(get_config_dep),
) -> DataCapabilityOverviewResponse:
    """Return the canonical read-only data overview."""
    return _overview_response(
        config,
        runtime_scheduler=getattr(request.app.state, "runtime_scheduler_service", None),
    )


@router.get(
    "/capabilities",
    response_model=DataCapabilityOverviewResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get data provider capabilities",
    description="Alias of /data/overview for clients that only need capability metadata.",
)
def get_data_capabilities(
    request: Request,
    config: Config = Depends(get_config_dep),
) -> DataCapabilityOverviewResponse:
    """Return the data overview under the capability-oriented alias."""
    return _overview_response(
        config,
        runtime_scheduler=getattr(request.app.state, "runtime_scheduler_service", None),
    )


@router.get(
    "/macro",
    responses={500: {"model": ErrorResponse}},
    summary="获取宏观和主要指数数据",
    description=(
        "PMI（制造业、非制造业商务活动、综合产出）、货币供应量 M0 / M1 / M2 及同比、社会融资规模增量和存量、"
        "LPR（1 年 / 5 年）按期间倒序，以及最近一个交易日主要指数的 PE / PE TTM / PB / 换手率。"
        "数据来自 Tushare，每天随资讯任务刷新一次（每个序列 20 小时内不重复取），这里只读本地表；"
        "单位见 units，各序列最近一次获取时间见 fetched_at。"
    ),
)
def get_macro_overview(months: int = Query(13, ge=1, le=36, description="月度序列返回的期数")) -> Dict[str, Any]:
    from src.services.macro_service import MacroService

    try:
        return MacroService().overview(months=months)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read macro data: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "读取宏观数据失败"})


def _index_ts_code(value: str) -> str:
    text = (value or "").strip().upper()
    match = re.fullmatch(r"(SH|SZ)(\d{6})", text)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    return text


@router.get(
    "/indices/{index_code}/constituents",
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="获取指数成分和权重",
    description=(
        "Tushare index_weight 的最近一期月度快照（成分股 6 位代码和权重 %，按权重降序）。"
        "目前存沪深300（000300.SH）、中证500（000905.SH）、中证1000（000852.SH）、上证50（000016.SH）、"
        "创业板指（399006.SZ）、科创50（000688.SH）；代码可写 000300.SH 或 sh000300。没有存的指数返回 404。"
    ),
)
def get_index_constituents(index_code: str) -> Dict[str, Any]:
    from src.services.macro_service import MacroService

    code = _index_ts_code(index_code)
    try:
        snapshot = MacroService().constituents(code)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to read index constituents: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "读取指数成分失败"})
    if snapshot is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": f"没有 {code} 的成分数据"})
    return {**snapshot, "count": len(snapshot.get("constituents") or [])}
