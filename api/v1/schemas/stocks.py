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
    source: Optional[str] = Field(None, description="实际行情来源")
    provider_timestamp: Optional[str] = Field(None, description="来源观测时间；缺失时不能推断为当前市场时间")
    fetched_at: Optional[str] = Field(None, description="取数时间；不是市场观测时间")
    is_stale: Optional[bool] = Field(None, description="来源观测是否过期；未知时为空")
    market: Optional[str] = Field(None, description="源行情市场 cn/hk/us/jp/kr/tw")
    currency: Optional[str] = Field(None, description="源报价币种")
    canonical_id: Optional[str] = Field(None, description="解析后证券身份；指数保留交易所")
    asset_type: Optional[str] = Field(None, description="stock 或 index；未知不猜测")
    exchange: Optional[str] = Field(None, description="证券交易所")
    
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
    source: Optional[str] = Field(None, description="实际日线数据来源")
    
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


class OpeningAuctionPoint(BaseModel):
    time: str
    price: float
    volume: Optional[float] = Field(None, description="最终撮合成交量（股），试撮报价不提供")


class OpeningAuctionResponse(BaseModel):
    source: Optional[Literal["ashareapi", "tushare", "biying"]] = None
    points: List[OpeningAuctionPoint]


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
    """一条公司事件（业绩预告 / 快报、股东增减持、回购、限售解禁、股权质押、机构调研、券商研报、龙虎榜、
    打板、热股榜、东方财富风险名单）。"""

    kind: Literal[
        "forecast", "express", "holder_trade", "repurchase", "share_float", "pledge", "survey", "research",
        "dragon_tiger", "limit_board", "hot_rank", "risk_watch", "price_anomaly",
    ] = Field(..., description="事件类型")
    kind_label: str = Field(..., description="事件类型中文名")
    event_date: str = Field(
        ...,
        description=(
            "公告日；解禁为解禁日（可能在未来）；质押为统计截止日；研报为报告日；龙虎榜、打板、热股榜、异常波动为交易日；"
            "重点监控名单为名单开始日（结束日在 payload.end）"
        ),
    )
    title: str = Field(..., description="一句话中文摘要")
    payload: Dict[str, object] = Field(default_factory=dict, description="Tushare 原始字段")
    source: str = Field(..., description="数据来源（tushare / eastmoney）")


class StockEventsResponse(BaseModel):
    """个股公司事件列表和按固定规则算出的风险。"""

    stock_code: str
    as_of: str = Field(..., description="计算风险时的日期")
    events: List[StockEventItem]
    risks: List[str] = Field(default_factory=list, description="按固定规则得出的风险：近期解禁、大股东减持、高质押、业绩预告转差")
    fetch: Dict[str, object] = Field(default_factory=dict, description="本次按需从 Tushare 补取时各类事件的结果（没有补取时为空）")


class StockResearchResponse(BaseModel):
    """个股券商研报：每份报告的评级、目标价、盈利预测，以及按机构去重后的一致预期。"""

    stock_code: str
    as_of: str
    summary: Dict[str, object] = Field(
        default_factory=dict,
        description=(
            "一致预期：reports 报告数、institutions 机构数、ratings 评级分布、consensus 按预测年度的 EPS（元）和"
            "净利润（net_profit_yi，亿元）的均值 / 中位数 / 最小 / 最大、target_price 目标价；每家机构只算最新一份"
        ),
    )
    reports: List[StockEventItem] = Field(default_factory=list, description="研报列表（kind=research），新的在前")
    fetch: Dict[str, object] = Field(default_factory=dict, description="本次按需从 Tushare 补取的结果（没有补取时为空）")


class StockCapitalResponse(BaseModel):
    """个股资金面：两融、主力资金、龙虎榜、北向。各部分独立，失败时该部分为 {status: failed, error}。"""

    stock_code: str
    as_of: str
    margin: Dict[str, object] = Field(
        default_factory=dict,
        description="两融（Tushare margin_detail）：rows 每日融资余额 / 买入 / 偿还 / 融券余额（元），summary 为亿元汇总",
    )
    fund_flow: Dict[str, object] = Field(
        default_factory=dict, description="主力资金（新浪个股历史资金流）：rows 每日主力净流入（元），summary 近 5 日合计（亿元）",
    )
    dragon_tiger: List[StockEventItem] = Field(
        default_factory=list, description="近 30 天龙虎榜（每日入库，payload 含上榜原因、席位、机构和沪深股通净额）",
    )
    dragon_tiger_status: Dict[str, object] = Field(default_factory=dict)
    northbound: Dict[str, object] = Field(
        default_factory=dict, description="全市场北向 / 南向（Tushare moneyflow_hsgt，百万元），口径见 note",
    )


class StockQaItem(BaseModel):
    """一条已回复的投资者问答。"""

    question: str
    answer: str
    asked_at: Optional[str] = Field(None, description="提问时间（北京时间）")
    answered_at: Optional[str] = Field(None, description="回复时间（北京时间）")
    platform: Literal["irm", "sse_e"] = Field(..., description="irm 巨潮互动易（深市）/ sse_e 上证e互动（沪市）")
    platform_label: str


class StockQaResponse(BaseModel):
    """个股投资者问答（只含已回复）。"""

    stock_code: str
    platform: Optional[Literal["irm", "sse_e"]] = Field(None, description="该股所在的问答平台；北交所为空")
    coverage: str = Field(..., description="覆盖说明")
    items: List[StockQaItem]
    fetch: Dict[str, object] = Field(default_factory=dict, description="本次是否从平台刷新及结果")
