# -*- coding: utf-8 -*-
"""
===================================
分析服务层
===================================

职责：
1. 封装股票分析逻辑
2. 调用 analyzer 和 pipeline 执行分析
3. 保存分析结果到数据库
"""

import logging
import copy
import uuid
from typing import Optional, Dict, Any, Callable, List

from src.llm.credentials_guard import ModelKeyRequiredError
from src.repositories.analysis_repo import AnalysisRepository
from src.report_language import (
    get_sentiment_label,
    get_localized_stock_name,
    localize_operation_advice,
    localize_trend_prediction,
    normalize_report_language,
)
from src.market_phase_summary import extract_market_phase_summary
from src.schemas.decision_action import build_action_fields
from src.services.run_diagnostics import (
    activate_run_diagnostic_context,
    build_run_diagnostic_summary,
    get_current_diagnostic_context,
    reset_run_diagnostic_context,
)
from src.services.empty_news import empty_news_disclosure
from src.services.user_config_service import require_usable_model, resolve_user_config
from src.utils.data_processing import parse_json_field
from src.utils.url_safety import UnsafeUrlError

logger = logging.getLogger(__name__)


def asset_type_from_canonical_code(code: Any) -> Optional[str]:
    """Derive the authoritative ``asset_type`` for a canonical stock/index code.

    Uses :func:`parse_analysis_target` — the single asset-type authority — on
    the *canonical* code, never the display code, so ``sh000016`` (index) and
    bare ``000016`` (stock) are distinguished by the parser rather than by
    display normalization. Returns ``None`` for market review / empty /
    unsupported codes, so legacy clients and market reviews simply omit the
    optional field.
    """
    text = str(code or "").strip()
    if not text:
        return None
    if text.upper() == "MARKET":
        return None

    from src.services.stock_list_parser import ParseStatus, parse_analysis_target

    target = parse_analysis_target(text)
    if target.asset_type == ParseStatus.INDEX:
        return "index"
    if target.asset_type == ParseStatus.STOCK:
        return "stock"
    return None


class AnalysisService:
    """
    分析服务
    
    封装股票分析相关的业务逻辑
    """
    
    def __init__(self):
        """初始化分析服务"""
        self.repo = AnalysisRepository()
        self.last_error: Optional[str] = None
    
    def analyze_stock(
        self,
        stock_code: str,
        report_type: str = "detailed",
        force_refresh: bool = False,
        query_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        send_notification: bool = True,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        skills: Optional[List[str]] = None,
        analysis_phase: str = "auto",
        query_source: str = "api",
        portfolio_context: Optional[Dict[str, Any]] = None,
        report_language: Optional[str] = None,
        analysis_target: Optional[Any] = None,
        user_id: Optional[str] = None,
        allow_reuse: bool = False,
        reuse_metadata: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        执行股票分析
        
        Args:
            stock_code: 股票代码
            report_type: 报告类型 (simple/detailed)
            force_refresh: 是否强制刷新
            query_id: 查询 ID（可选）
            send_notification: 是否发送通知（API 触发默认发送）
            analysis_phase: 请求的分析阶段覆盖（auto/premarket/intraday/postmarket）
            analysis_target: 可选的结构化分析目标（指数目标贯穿到 pipeline，
                否则指数会退化为股票语义）
            user_id: 发起分析的用户（``None`` = 主用户）。非主用户使用自己的配置视图（模型 / 搜索 Key、
                Agent 持仓读取），没有可用模型时抛出 ``ModelKeyRequiredError``；不发送系统推送渠道
            allow_reuse: 允许直接复用已有的共享报告（同股票 / 交易日 / 模型渠道 / 策略集 / 语言 / 报告类型，见
                ``report_reuse_service``）；命中时不调用模型，也不需要模型 Key。``force_refresh``、
                ``portfolio_context``（持仓）和非 ``auto`` 的 ``analysis_phase`` 会关闭直接复用
            reuse_metadata: 需要重新分析时，复用同股票同交易日更早一次分析已采集的输入（行情 / 筹码 / 基本面 /
                市场结构 / 资讯，见 ``analysis_metadata_reuse``），不复用任何结论。``force_refresh`` 会关闭

        Returns:
            分析结果字典，包含:
            - stock_code: 股票代码
            - stock_name: 股票名称
            - report: 分析报告
        """
        try:
            self.last_error = None
            # 导入分析相关模块
            from src.config import get_config, get_primary_user_id
            from src.core.pipeline import StockAnalysisPipeline
            from src.enums import ReportType
            
            # 生成 query_id
            if query_id is None:
                query_id = uuid.uuid4().hex
            effective_trace_id = trace_id or query_id
            diag_token = None
            if get_current_diagnostic_context() is None:
                diag_token = activate_run_diagnostic_context(
                    trace_id=effective_trace_id,
                    query_id=query_id,
                    stock_code=stock_code,
                    trigger_source=query_source or "api",
                )
            
            # 获取配置：主用户 / 无用户上下文 = 系统配置；其他用户 = 本人的配置视图
            config = get_config()
            pipeline_user_kwargs: Dict[str, Any] = {}
            if user_id and user_id != get_primary_user_id():
                config = resolve_user_config(user_id)
                pipeline_user_kwargs["user_id"] = user_id
                send_notification = False  # 个人推送渠道尚未开放，绝不落到系统渠道
            normalized_report_language = normalize_report_language(report_language, default="")
            if normalized_report_language:
                config = copy.copy(config)
                object.__setattr__(config, "report_language", normalized_report_language)

            # 共享报告直接复用：命中就直接返回已有报告（不调用模型、不需要 Key）。带持仓、指定分析阶段、
            # 强制刷新的请求永远重新分析；持仓报告只属于发起人，不会被直接复用，也不会被直接给别人
            if allow_reuse and not force_refresh and not portfolio_context and (analysis_phase or "auto") == "auto":
                reused = self._reuse_existing_report(
                    stock_code=stock_code,
                    report_type=report_type,
                    config=config,
                    report_language=normalized_report_language or None,
                    analysis_target=analysis_target,
                    skills=skills,
                )
                if reused is not None:
                    return reused

            # 需要真正生成报告：非主用户必须有自己的模型（没有就拒绝，永远不落到系统 Key）
            require_usable_model(config)

            # 不能直接复用报告时，仍复用同股票同交易日更早一次分析已采集的输入；来源只取共享报告和
            # 请求人自己的报告，绝不取别人的私有报告。没有来源就照常取数
            metadata = None
            if reuse_metadata and not force_refresh:
                from src.services.analysis_metadata_reuse import load_reusable_metadata

                metadata = load_reusable_metadata(
                    stock_code=stock_code,
                    viewer_user_id=pipeline_user_kwargs.get("user_id") or get_primary_user_id(),
                    analysis_target=analysis_target,
                )

            # 创建分析流水线
            pipeline = StockAnalysisPipeline(
                config=config,
                query_id=query_id,
                trace_id=effective_trace_id,
                query_source=query_source or "api",
                progress_callback=progress_callback,
                analysis_skills=skills,
                analysis_phase=analysis_phase,
                portfolio_context=portfolio_context,
                **pipeline_user_kwargs,
                **({"reused_metadata": metadata} if metadata is not None else {}),
            )
            
            # 确定报告类型 (API: simple/detailed/full/brief -> ReportType)
            rt = ReportType.from_str(report_type)
            
            # 执行分析
            result = pipeline.process_single_stock(
                code=stock_code,
                skip_analysis=False,
                single_stock_notify=send_notification,
                report_type=rt,
                analysis_target=analysis_target,
            )
            
            if result is None:
                logger.warning(f"分析股票 {stock_code} 返回空结果")
                self.last_error = self.last_error or f"分析股票 {stock_code} 返回空结果"
                return None

            if not getattr(result, "success", True):
                self.last_error = getattr(result, "error_message", None) or f"分析股票 {stock_code} 失败"
                logger.warning(f"分析股票 {stock_code} 未成功完成: {self.last_error}")
                return None
            
            # 构建响应
            response = self._build_analysis_response(result, query_id, report_type=rt.value)
            if metadata is not None and metadata.used:
                response["metadata_reused_from"] = metadata.source_report_id
            return response

        except (ModelKeyRequiredError, UnsafeUrlError):
            raise
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"分析股票 {stock_code} 失败: {e}", exc_info=True)
            return None
        finally:
            reset_run_diagnostic_context(locals().get("diag_token"))
    
    def _reuse_existing_report(
        self,
        *,
        stock_code: str,
        report_type: str,
        config: Any,
        report_language: Optional[str],
        analysis_target: Optional[Any],
        skills: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Analysis response built from an existing shared report, or ``None`` when nothing can be reused."""
        from src.enums import ReportType
        from src.services.history_service import HistoryService
        from src.services.report_reuse_service import find_reusable_report

        rt = ReportType.from_str(report_type)
        record = find_reusable_report(
            stock_code=stock_code,
            report_type=rt.value,
            config=config,
            report_language=report_language,
            analysis_target=analysis_target,
            skills=skills,
        )
        if record is None:
            return None
        try:
            raw_result = parse_json_field(getattr(record, "raw_result", None))
            result = HistoryService()._rebuild_analysis_result(
                raw_result if isinstance(raw_result, dict) else {}, record
            )
            if result is None:
                return None
            # The report keeps its own query_id: it is the key every history / detail lookup uses.
            response = self._build_analysis_response(
                result, getattr(record, "query_id", None) or uuid.uuid4().hex, report_type=rt.value
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable stored report is a miss, not a failure
            logger.warning("复用已有报告失败，改为重新生成: %s (%s)", stock_code, exc)
            return None
        response["reused"] = True
        response["reused_report_id"] = getattr(record, "id", None)
        logger.info("复用共享报告: %s report_id=%s", stock_code, getattr(record, "id", None))
        return response

    def _build_analysis_response(
        self,
        result: Any, 
        query_id: str,
        report_type: str = "detailed",
    ) -> Dict[str, Any]:
        """
        构建分析响应
        
        Args:
            result: AnalysisResult 对象
            query_id: 查询 ID
            report_type: 归一化后的报告类型
            
        Returns:
            格式化的响应字典
        """
        # 获取狙击点位
        sniper_points = {}
        if hasattr(result, 'get_sniper_points'):
            sniper_points = result.get_sniper_points() or {}
        
        # 计算情绪标签
        report_language = normalize_report_language(getattr(result, "report_language", "zh"))
        sentiment_label = get_sentiment_label(result.sentiment_score, report_language)
        stock_name = get_localized_stock_name(getattr(result, "name", None), result.code, report_language)
        action_fields = build_action_fields(
            operation_advice=getattr(result, "operation_advice", None),
            explicit_action=getattr(result, "action", None),
            report_type=report_type,
            report_language=report_language,
            sentiment_score=getattr(result, "sentiment_score", None),
            guardrail_reason=getattr(result, "guardrail_reason", None),
            align_with_score=True,
        )
        diagnostic_context = get_current_diagnostic_context()
        trace_id = diagnostic_context.trace_id if diagnostic_context is not None else query_id
        diagnostic_snapshot = diagnostic_context.snapshot() if diagnostic_context is not None else None
        diagnostic_context_snapshot = getattr(result, "diagnostic_context_snapshot", None)
        market_phase_summary = extract_market_phase_summary(diagnostic_context_snapshot)
        if isinstance(diagnostic_context_snapshot, dict):
            context_snapshot = dict(diagnostic_context_snapshot)
            if diagnostic_snapshot is not None:
                context_snapshot["diagnostics"] = diagnostic_snapshot
        elif diagnostic_snapshot is not None:
            context_snapshot = {"diagnostics": diagnostic_snapshot}
        else:
            context_snapshot = None
        diagnostic_summary = build_run_diagnostic_summary(
            context_snapshot=context_snapshot,
            raw_result=result.to_dict() if hasattr(result, "to_dict") else None,
            report_saved=True,
            query_id=query_id,
            stock_code=result.code,
        )
        
        # 构建报告结构
        report = {
            "meta": {
                "query_id": query_id,
                "trace_id": trace_id,
                "stock_code": result.code,
                "stock_name": stock_name,
                "report_type": report_type,
                "report_language": report_language,
                "current_price": result.current_price,
                "change_pct": result.change_pct,
                "model_used": getattr(result, "model_used", None),
                "market_phase_summary": market_phase_summary,
                "asset_type": asset_type_from_canonical_code(result.code),
            },
            "summary": {
                "analysis_summary": result.analysis_summary,
                "operation_advice": localize_operation_advice(result.operation_advice, report_language),
                "action": action_fields["action"],
                "action_label": action_fields["action_label"],
                "trend_prediction": localize_trend_prediction(result.trend_prediction, report_language),
                "sentiment_score": result.sentiment_score,
                "sentiment_label": sentiment_label,
            },
            "strategy": {
                "ideal_buy": sniper_points.get("ideal_buy"),
                "secondary_buy": sniper_points.get("secondary_buy"),
                "stop_loss": sniper_points.get("stop_loss"),
                "take_profit": sniper_points.get("take_profit"),
            },
            "details": {
                "news_summary": result.news_summary,
                "empty_news_disclosure": empty_news_disclosure(result, report_language),
                "technical_analysis": result.technical_analysis,
                "fundamental_analysis": result.fundamental_analysis,
                "risk_warning": result.risk_warning,
            }
        }
        if hasattr(result, "to_dict"):
            raw_result_payload = result.to_dict()
            if isinstance(raw_result_payload, dict):
                report["details"]["raw_result"] = raw_result_payload

        return {
            "query_id": query_id,
            "trace_id": trace_id,
            "stock_code": result.code,
            "stock_name": stock_name,
            "report": report,
            "diagnostic_summary": diagnostic_summary,
        }
