# -*- coding: utf-8 -*-
"""板块异动接口的请求 / 响应模型（契约见次日计划 API 契约）。

比例字段统一使用百分数（98.7 表示 98.7%）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

BoardType = Literal["industry", "concept"]
BoardState = Literal[
    "first_launch",
    "strengthening",
    "divergence",
    "ebbing",
    "abnormal_drop",
    "insufficient_history",
    "none",
]
BoardTag = Literal["mainline", "supported", "single_stock", "cooling", "merged"]
Evidence = Literal["medium", "low", "inferred"]
DriverKind = Literal["news", "capital", "earnings", "unknown"]
RepresentativeRole = Literal["leader", "turnover", "follower"]

ThresholdValue = Union[int, float, str, Dict[str, Any]]


class MarketSummary(BaseModel):
    """市场概况。"""

    index_name: str = Field("中证全指", description="基准指数名称")
    index_change_pct: Optional[float] = Field(None, description="基准指数涨跌幅 %")
    up_count: Optional[int] = Field(None, description="上涨家数")
    down_count: Optional[int] = Field(None, description="下跌家数")
    limit_up_count: Optional[int] = Field(None, description="涨停家数")
    limit_down_count: Optional[int] = Field(None, description="跌停家数")
    turnover_yi: Optional[float] = Field(None, description="两市成交额（亿元）")
    break_rate_pct: Optional[float] = Field(None, description="炸板率 %（触及涨停后未封住的比例）")
    max_consecutive: Optional[int] = Field(None, description="最高连板数")
    promotion_rate_pct: Optional[float] = Field(None, description="晋级率 %：上一交易日涨停的股票今天继续涨停的比例")
    ladder: Optional[Dict[str, Any]] = Field(
        None, description="连板梯队：counts 为各连板高度的涨停家数；promotion 为上一交易日各高度的家数（prev）与晋级家数（promoted）",
    )
    broad_rally: bool = Field(False, description="普涨保护是否生效")
    note: Optional[str] = Field(None, description="补充说明")


class BoardRow(BaseModel):
    """板块列表行。"""

    id: str = Field(..., description="稳定 id，例如 industry:BK0448")
    name: str
    board_type: BoardType
    state: BoardState = "none"
    tags: List[BoardTag] = Field(default_factory=list)
    change_pct: Optional[float] = Field(None, description="当日涨幅 %")
    change_5d_pct: Optional[float] = Field(None, description="5 日涨幅 %")
    excess_pct: Optional[float] = Field(None, description="当日超额（相对基准）%")
    amount_ratio: Optional[float] = Field(None, description="成交额 / 5 日均值")
    breadth_pct: Optional[float] = Field(None, description="上涨家数占比 %")
    limit_up_count: Optional[int] = Field(None, description="涨停家数；行业为 null")
    broken_rate_pct: Optional[float] = Field(None, description="炸板率 %；样本不足为 null")
    reason: str = Field("", description="一句话原因")


class BoardAnomalyListResponse(BaseModel):
    """GET /boards/anomalies 响应。"""

    trade_date: str
    market: MarketSummary
    boards: List[BoardRow] = Field(default_factory=list)
    history_note: Optional[str] = None


class FollowedBoard(BaseModel):
    """关注的板块：关注时记下的名称和类型，加上最近一次运行里的当日数据（没有运行或已下线时为 null）。"""

    id: str
    name: str
    board_type: BoardType
    followed_at: Optional[str] = None
    today: Optional[BoardRow] = None


class FollowListResponse(BaseModel):
    """GET /boards/follows 响应。"""

    trade_date: Optional[str] = None
    boards: List[FollowedBoard] = Field(default_factory=list)


class RecommendationItem(BaseModel):
    rank: int
    reason: str = ""
    board: BoardRow


class RecommendationResponse(BaseModel):
    """GET /boards/recommendations 响应。"""

    trade_date: Optional[str] = None
    status: Literal["ready", "generating", "none", "no_run"] = "none"
    source: Optional[Literal["model", "rule"]] = Field(None, description="model 模型挑选；rule 模型不可用时按规则排序")
    model: Optional[str] = None
    note: Optional[str] = Field(None, description="按规则排序的原因")
    generated_at: Optional[str] = None
    items: List[RecommendationItem] = Field(default_factory=list)


class Metric(BaseModel):
    label: str
    value: str
    hint: Optional[str] = None


class SeriesPoint(BaseModel):
    trade_date: str
    excess_pct: Optional[float] = None


class Representative(BaseModel):
    code: str
    name: str
    role: RepresentativeRole
    close: Optional[float] = None
    change_pct: Optional[float] = None


class BoardDetail(BoardRow):
    """GET /boards/{board_id} 响应，继承 BoardRow 全部字段。"""

    verdict_text: str = ""
    drivers: str = ""
    driver_kind: DriverKind = "unknown"
    metrics: List[Metric] = Field(default_factory=list)
    excess_series: List[SeriesPoint] = Field(default_factory=list)
    representatives: List[Representative] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list, description="持续性判断依据")
    can_pick: bool = Field(False, description="是否显示「在此板块中选股」")
    data_gaps: List[str] = Field(default_factory=list)


class ThresholdItem(BaseModel):
    key: str
    label: str
    value: ThresholdValue = Field(..., description="当前生效值")
    default: ThresholdValue
    display: str
    unit: Optional[str] = None
    evidence: Evidence
    note: Optional[str] = None
    overridden: bool = False


class ThresholdListResponse(BaseModel):
    items: List[ThresholdItem] = Field(default_factory=list)


class ThresholdUpdateRequest(BaseModel):
    """PUT /boards/thresholds 请求体；空对象表示恢复默认。"""

    overrides: Dict[str, ThresholdValue] = Field(..., description="阈值覆盖 {key: value}")
