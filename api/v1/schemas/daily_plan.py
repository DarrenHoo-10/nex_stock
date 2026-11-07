# -*- coding: utf-8 -*-
"""次日计划接口的请求 / 响应模型（契约见次日计划 API 契约）。

金额单位元，比例字段统一使用百分数（98.7 表示 98.7%）。
"""

from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

Horizon = Literal["short", "swing"]
BoardMarket = Literal["main", "chinext", "star", "bse"]
Verdict = Literal["buy", "wait", "skip", "suspended", "hold", "add", "reduce", "clear"]
Action = Literal["buy", "sell", "reduce", "clear", "hold", "wait", "skip"]
RunStatus = Literal["pending", "running", "succeeded", "partial", "failed", "skipped"]
StepStatus = RunStatus
StopMode = Literal["fixed", "nearer"]
PlanItemStatus = Literal["ready", "failed", "missing"]
LevelKind = Literal[
    "pressure",
    "breakout",
    "entry",
    "support",
    "stop",
    "take_profit",
    "limit_up",
    "limit_down",
    "cost",
    "close",
]
SizingStatus = Literal["ok", "below_lot", "max_holdings", "no_price"]
SwitchDecisionKind = Literal["switch", "keep"]
RunTrigger = Literal["schedule", "manual"]
PushStatus = Literal["sent", "failed", "skipped", "pending"]

MAX_HOLDINGS_INPUT = 50
CODE_PATTERN = r"^\d{6}$"


# ---------------------------------------------------------------- account


class PositionRules(BaseModel):
    stop_loss_pct: float
    max_total_pct: float
    max_holdings: int
    max_single_pct: float = Field(..., description="生效的单只上限 %")
    max_single_pct_derived: bool = Field(False, description="true = 由总仓位 ÷ 最多持股数推出")
    stop_mode: StopMode


class HoldingInput(BaseModel):
    """录入的单只持仓。"""

    code: str = Field(..., pattern=CODE_PATTERN, description="6 位股票代码")
    shares: int = Field(..., gt=0, description="持有股数")
    cost: float = Field(..., gt=0, description="持仓成本价")
    buy_date: Optional[str] = Field(None, description="买入日期 YYYY-MM-DD")
    horizon: Optional[Horizon] = None

    @field_validator("buy_date", mode="before")
    @classmethod
    def _normalize_buy_date(cls, value):
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            # PydanticCustomError keeps the 422 payload JSON-serializable (no ctx exception).
            raise PydanticCustomError("invalid_date", "buy_date 必须为 YYYY-MM-DD") from exc


class Holding(HoldingInput):
    """账户中的持仓（含行情与盈亏）。"""

    name: str = ""
    board_market: BoardMarket = "main"
    is_st: bool = False
    close: Optional[float] = None
    change_pct: Optional[float] = None
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    sellable: bool = True
    sellable_note: Optional[str] = None


class AccountResponse(BaseModel):
    cash: float
    market_value: float
    total_capital: float
    position_pct: float
    holdings: List[Holding] = Field(default_factory=list)
    rules: PositionRules
    updated_at: Optional[str] = None


class HoldingsSnapshotRequest(BaseModel):
    """PUT /plan/holdings 请求体，整体覆盖。"""

    cash: float = Field(..., ge=0, description="可用现金（元）")
    holdings: List[HoldingInput] = Field(default_factory=list, max_length=MAX_HOLDINGS_INPUT)


# ---------------------------------------------------------------- sizing


class StopInfo(BaseModel):
    price: float
    mode: StopMode
    formula: str = ""
    tech_support: Optional[float] = None
    warning: Optional[str] = None


class Sizing(BaseModel):
    status: SizingStatus
    message: Optional[str] = None
    price: float
    lot: int
    by_single: float
    by_total: float
    by_cash: float
    raw_shares: float
    shares: int
    amount: float
    capital_pct: float
    post_position_pct: float
    stop: StopInfo
    max_loss: float
    limit_down_price: float
    limit_down_loss: float
    steps: List[str] = Field(default_factory=list)


class SizingRequest(BaseModel):
    """POST /plan/sizing 请求体。"""

    code: str = Field(..., pattern=CODE_PATTERN)
    price: float = Field(..., gt=0)
    horizon: Horizon


# ---------------------------------------------------------------- plans


class Condition(BaseModel):
    if_text: str
    then_text: str
    action: Action
    shares: Optional[int] = None
    price: Optional[float] = None
    outside_limit: bool = False


class Level(BaseModel):
    kind: LevelKind
    price: float
    label: str


class HorizonPlan(BaseModel):
    verdict: Verdict
    headline: str = ""
    reason: Optional[str] = None
    entry_price: Optional[float] = None
    entry_zone: Optional[Tuple[float, float]] = None
    breakout_price: Optional[float] = None
    invalid_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    conditions: List[Condition] = Field(default_factory=list)
    sizing: Optional[Sizing] = None
    stop: Optional[StopInfo] = None


class Horizons(BaseModel):
    short: Optional[HorizonPlan] = None
    swing: Optional[HorizonPlan] = None


class SwitchCandidate(BaseModel):
    code: str
    name: str
    reason: str = ""
    verdict: Verdict


class SwitchCondition(BaseModel):
    text: str
    met: Optional[bool] = None


class SwitchDecision(BaseModel):
    decision: SwitchDecisionKind
    summary: str = ""
    candidate: Optional[SwitchCandidate] = None
    conditions: List[SwitchCondition] = Field(default_factory=list)
    costs: List[str] = Field(default_factory=list)


class ModelUsage(BaseModel):
    model: str
    fallback_from: Optional[str] = None
    fallback_reason: Optional[str] = None


class StockPlanSummary(BaseModel):
    code: str
    name: str = ""
    board_market: BoardMarket = "main"
    is_st: bool = False
    held: bool = False
    close: Optional[float] = None
    change_pct: Optional[float] = None
    verdict: Optional[Verdict] = Field(None, description="默认周期的结论；分析失败或缺失时为 null")
    headline: str = ""
    status: PlanItemStatus = "ready"
    failure_reason: Optional[str] = None


class StockPlanListResponse(BaseModel):
    trade_date: str
    next_trade_date: str
    items: List[StockPlanSummary] = Field(default_factory=list)


class StockPlan(StockPlanSummary):
    trade_date: str
    next_trade_date: str
    situation: str = ""
    horizons: Horizons = Field(default_factory=Horizons)
    default_horizon: Horizon = "short"
    limit_pct: float
    next_limit_up: Optional[float] = None
    next_limit_down: Optional[float] = None
    levels: List[Level] = Field(default_factory=list)
    switch: Optional[SwitchDecision] = None
    risks: List[str] = Field(default_factory=list)
    data_gaps: List[str] = Field(default_factory=list)
    model: Optional[ModelUsage] = None


# ---------------------------------------------------------------- runs


class RunStep(BaseModel):
    key: str
    label: str
    status: StepStatus
    finished_at: Optional[str] = None
    detail: Optional[str] = None


class RunFailure(BaseModel):
    code: str
    name: str = ""
    reason: str = ""


class PushInfo(BaseModel):
    channel: Literal["feishu"] = "feishu"
    status: PushStatus
    sent_at: Optional[str] = None
    error: Optional[str] = None
    configured: bool = False


class ModelCall(BaseModel):
    step: str
    calls: int
    model: str
    note: Optional[str] = None


class PlanRun(BaseModel):
    id: str
    trade_date: str
    status: RunStatus
    trigger: RunTrigger
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_sec: Optional[float] = None
    steps: List[RunStep] = Field(default_factory=list)
    stock_total: int = 0
    stock_succeeded: int = 0
    stock_failed: int = 0
    failures: List[RunFailure] = Field(default_factory=list)
    push: Optional[PushInfo] = None
    push_preview_markdown: Optional[str] = None
    model_calls: List[ModelCall] = Field(default_factory=list)
    data_gaps: List[str] = Field(default_factory=list)


class LatestRunResponse(BaseModel):
    run: Optional[PlanRun] = None
