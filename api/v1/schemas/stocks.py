# -*- coding: utf-8 -*-
"""
===================================
股票数据相关模型
===================================

职责：
1. 定义股票实时行情模型
2. 定义历史 K 线数据模型
"""

from typing import Dict, Literal, Optional, List

from pydantic import BaseModel, ConfigDict, Field

from api.v1.schemas.history import HistoryItem
from api.v1.schemas.intelligence import IntelligenceItem
from api.v1.schemas.research_artifact import ResearchArtifact

StockProfileStatus = Literal["fresh", "partial", "unavailable"]


class StockQuote(BaseModel):
    """股票实时行情"""
    
    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    current_price: float = Field(..., description="当前价格")
    change: Optional[float] = Field(None, description="涨跌额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    open: Optional[float] = Field(None, description="开盘价")
    high: Optional[float] = Field(None, description="最高价")
    low: Optional[float] = Field(None, description="最低价")
    prev_close: Optional[float] = Field(None, description="昨收价")
    volume: Optional[float] = Field(None, description="成交量（股）")
    amount: Optional[float] = Field(None, description="成交额（元）")
    turnover_rate: Optional[float] = Field(None, description="换手率 (%)")
    float_shares: Optional[float] = Field(
        None,
        description="流通股本（股），由 流通市值/现价 或 成交量/换手率 推算；数据源缺字段时为空",
    )
    update_time: Optional[str] = Field(None, description="更新时间")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "current_price": 1800.00,
            "change": 15.00,
            "change_percent": 0.84,
            "open": 1785.00,
            "high": 1810.00,
            "low": 1780.00,
            "prev_close": 1785.00,
            "volume": 10000000,
            "amount": 18000000000,
            "update_time": "2024-01-01T15:00:00"
        }
    })


class KLineData(BaseModel):
    """K 线数据点"""
    
    date: str = Field(..., description="日期")
    open: float = Field(..., description="开盘价")
    high: float = Field(..., description="最高价")
    low: float = Field(..., description="最低价")
    close: float = Field(..., description="收盘价")
    volume: Optional[float] = Field(None, description="成交量")
    amount: Optional[float] = Field(None, description="成交额")
    change_percent: Optional[float] = Field(None, description="涨跌幅 (%)")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "date": "2024-01-01",
            "open": 1785.00,
            "high": 1810.00,
            "low": 1780.00,
            "close": 1800.00,
            "volume": 10000000,
            "amount": 18000000000,
            "change_percent": 0.84
        }
    })


class ExtractItem(BaseModel):
    """单条提取结果（代码、名称、置信度）"""

    code: Optional[str] = Field(None, description="股票代码，None 表示解析失败")
    name: Optional[str] = Field(None, description="股票名称（如有）")
    confidence: str = Field("medium", description="置信度：high/medium/low")


class ExtractFromImageResponse(BaseModel):
    """图片股票代码提取响应"""

    codes: List[str] = Field(..., description="提取的股票代码（已去重，向后兼容）")
    items: List[ExtractItem] = Field(default_factory=list, description="提取结果明细（代码+名称+置信度）")
    raw_text: Optional[str] = Field(None, description="原始 LLM 响应（调试用）")


class StockHistoryResponse(BaseModel):
    """股票历史行情响应"""
    
    stock_code: str = Field(..., description="股票代码")
    stock_name: Optional[str] = Field(None, description="股票名称")
    period: str = Field(..., description="K 线周期")
    data: List[KLineData] = Field(default_factory=list, description="K 线数据列表")
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "period": "daily",
            "data": []
        }
    })


class IntradayBar(BaseModel):
    """分时（5 分钟）K 线数据点"""

    time: str = Field(..., description="K 线结束时间（北京时间，YYYY-MM-DD HH:MM:SS）")
    open: Optional[float] = Field(None, description="开盘价")
    high: Optional[float] = Field(None, description="最高价")
    low: Optional[float] = Field(None, description="最低价")
    close: float = Field(..., description="收盘价")
    volume: Optional[float] = Field(None, description="成交量（股）")
    amount: Optional[float] = Field(None, description="成交额（元）")


class StockIntradayResponse(BaseModel):
    """某个交易日的分时（5 分钟）行情响应"""

    stock_code: str = Field(..., description="股票代码")
    trade_date: str = Field(..., description="交易日 YYYY-MM-DD")
    interval: str = Field("5min", description="K 线间隔")
    source: str = Field(..., description="数据来源（tushare / biying）")
    as_of: str = Field(..., description="最后一根 K 线时间")
    fetched_at: str = Field(..., description="本次取数时间")
    is_stale: bool = Field(False, description="是否为过期数据（刷新失败后沿用旧数据，或盘中数据滞后）")
    stale_reason: Optional[str] = Field(None, description="过期原因")
    data: List[IntradayBar] = Field(default_factory=list, description="分时 K 线，按时间升序")


class StockProfileQuoteBlock(BaseModel):
    status: StockProfileStatus
    data: Optional[StockQuote] = None
    limitations: List[str] = Field(default_factory=list)


class StockProfileHistoryBlock(BaseModel):
    status: StockProfileStatus
    period: Literal["daily"] = "daily"
    data: List[KLineData] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class StockProfileResearchData(BaseModel):
    latest_report: Optional[HistoryItem] = None
    recent_reports: List[HistoryItem] = Field(default_factory=list)
    structured_report: Optional[ResearchArtifact] = None


class StockProfileResearchBlock(BaseModel):
    status: StockProfileStatus
    data: StockProfileResearchData = Field(default_factory=StockProfileResearchData)
    limitations: List[str] = Field(default_factory=list)


class StockProfileIntelligenceBlock(BaseModel):
    status: StockProfileStatus
    items: List[IntelligenceItem] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)


class StockProfilePortfolioRelation(BaseModel):
    held: bool = False
    matched_markets: List[str] = Field(default_factory=list)


class StockProfilePortfolioBlock(BaseModel):
    status: StockProfileStatus
    data: StockProfilePortfolioRelation = Field(default_factory=StockProfilePortfolioRelation)
    limitations: List[str] = Field(default_factory=list)


class StockProfileMonitorData(BaseModel):
    total_rule_count: int = 0
    enabled_rule_count: int = 0
    rule_ids: List[int] = Field(default_factory=list)


class StockProfileMonitorBlock(BaseModel):
    status: StockProfileStatus
    data: StockProfileMonitorData = Field(default_factory=StockProfileMonitorData)
    limitations: List[str] = Field(default_factory=list)


class StockProfileEvidenceQuality(BaseModel):
    status: StockProfileStatus
    blocks: Dict[str, StockProfileStatus] = Field(default_factory=dict)
    limitations: List[str] = Field(default_factory=list)


class StockProfileResponse(BaseModel):
    requested_code: str
    canonical_code: str
    market: Literal["cn", "hk", "us", "jp", "kr", "tw"]
    as_of: str
    quote: StockProfileQuoteBlock
    history: StockProfileHistoryBlock
    research: StockProfileResearchBlock
    intelligence: StockProfileIntelligenceBlock
    portfolio: StockProfilePortfolioBlock
    monitors: StockProfileMonitorBlock
    evidence_quality: StockProfileEvidenceQuality


class StockEventItem(BaseModel):
    """一条公司事件（业绩预告 / 快报、股东增减持、回购、限售解禁、股权质押、机构调研）。"""

    kind: Literal["forecast", "express", "holder_trade", "repurchase", "share_float", "pledge", "survey"] = Field(
        ..., description="事件类型"
    )
    kind_label: str = Field(..., description="事件类型中文名")
    event_date: str = Field(..., description="公告日；解禁为解禁日（可能在未来）；质押为统计截止日")
    title: str = Field(..., description="一句话中文摘要")
    payload: Dict[str, object] = Field(default_factory=dict, description="Tushare 原始字段")
    source: str = Field(..., description="数据来源（tushare）")


class StockEventsResponse(BaseModel):
    """个股公司事件列表和按固定规则算出的风险。"""

    stock_code: str
    as_of: str = Field(..., description="计算风险时的日期")
    events: List[StockEventItem]
    risks: List[str] = Field(default_factory=list, description="按固定规则得出的风险：近期解禁、大股东减持、高质押、业绩预告转差")
    fetch: Dict[str, object] = Field(default_factory=dict, description="本次按需从 Tushare 补取时各类事件的结果（没有补取时为空）")
