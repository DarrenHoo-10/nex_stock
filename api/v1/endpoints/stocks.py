# -*- coding: utf-8 -*-
"""
===================================
股票数据接口
===================================

职责：
1. POST /api/v1/stocks/extract-from-image 从图片提取股票代码
2. POST /api/v1/stocks/parse-import 解析 CSV/Excel/剪贴板
3. GET /api/v1/stocks/{code}/quote 实时行情接口
4. GET /api/v1/stocks/{code}/history 历史行情接口
5. GET /api/v1/stocks/{code}/intraday?date= 某个交易日的分时（5 分钟）行情
"""

import logging
from typing import Optional
import re

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, Depends

from api.deps import get_system_config_service

from api.v1.schemas.stocks import (
    ExtractFromImageResponse,
    ExtractItem,
    IntradayBar,
    KLineData,
    StockEventItem,
    StockEventsResponse,
    StockQaItem,
    StockQaResponse,
    StockHistoryResponse,
    StockIntradayResponse,
    StockProfileResponse,
    StockQuote,
)
from api.v1.schemas.history import WatchlistRequest, WatchlistResponse
from api.v1.schemas.common import ErrorResponse
from src.services.intraday_snapshot_service import (
    IntradayDataUnavailable,
    get_intraday_snapshot_service,
)
from src.services.image_stock_extractor import (
    ALLOWED_MIME,
    MAX_SIZE_BYTES,
    extract_stock_codes_from_image,
)
from src.services.import_parser import (
    MAX_FILE_BYTES,
    parse_import_from_bytes,
    parse_import_from_text,
)
from src.services.stock_code_guard import LegacyBseCodeError, resolve_bse_code
from src.services.stock_service import StockService
from src.services.stock_profile_service import InvalidStockProfileCode, StockProfileService
from src.services.run_diagnostics import sanitize_diagnostic_text
from src.services.stock_list_parser import split_stock_list
from src.services.system_config_service import SystemConfigService
from data_provider.base import normalize_stock_code

logger = logging.getLogger(__name__)

router = APIRouter()

# 须在 /{stock_code} 路由之前定义
ALLOWED_MIME_STR = ", ".join(ALLOWED_MIME)


def _read_watchlist_codes(service: SystemConfigService) -> list:
    """Read STOCK_LIST codes as-is (no normalization)."""
    config_data = service.get_config(include_schema=False)
    stock_list_str = ""
    for item in config_data.get("items", []):
        if item.get("key") == "STOCK_LIST":
            stock_list_str = str(item.get("value", ""))
            break
    return split_stock_list(stock_list_str)


def _write_watchlist_codes(service: SystemConfigService, codes: list) -> None:
    """Persist stock codes to STOCK_LIST as-is (no normalization)."""
    config_data = service.get_config(include_schema=False)
    config_version = config_data.get("config_version", "")
    service.update(
        config_version=config_version,
        items=[{"key": "STOCK_LIST", "value": ",".join(codes)}],
        mask_token="******",
        reload_now=True,
    )


# Stock code validation patterns (aligned with frontend validateStockCode)
_STOCK_CODE_RE = re.compile(
    r"^(?:\d{6}"                              # A-share 6-digit
    r"|(?:SH|SZ|BJ)\d{6}"                     # exchange-prefixed A-share
    r"|\d{6}\.(?:SH|SZ|SS|BJ)"                # exchange-suffixed A-share
    r"|\d{1,5}\.HK"                           # HK suffix format
    r"|HK\d{1,5}"                             # HK prefix format
    r"|\d{5}"                                 # bare 5-digit HK code
    r"|\d{4,5}\.T"                            # Japan Yahoo suffix format
    r"|\d{6}\.(?:KS|KQ)"                     # Korea Yahoo suffix format
    r"|\d{4,6}\.(?:TW|TWO)"                  # Taiwan Yahoo suffix format
    r"|[A-Z]{1,5}(?:\.(?:US|[A-Z]))?"         # US ticker
    r")$",
    re.IGNORECASE,
)


def _validate_and_normalize_stock_code(code: str) -> str:
    """Validate stock code format and return canonical form.

    Raises HTTPException(400) if the code does not match supported formats.
    """
    stripped = code.strip()
    if not stripped:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_stock_code", "message": "股票代码不能为空"},
        )
    if not _STOCK_CODE_RE.match(stripped):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_stock_code",
                "message": f"'{stripped}' 不是合法的股票代码格式",
            },
        )
    return normalize_stock_code(stripped)


def _watchlist_match_key(code: str) -> str:
    """Return the equivalence key used for watchlist add/remove matching."""
    normalized = normalize_stock_code(code.strip())
    if re.fullmatch(r"\d{5}", normalized):
        return f"HK{normalized}"
    return normalized.upper()


@router.post(
    "/extract-from-image",
    response_model=ExtractFromImageResponse,
    responses={
        200: {"description": "提取的股票代码"},
        400: {"description": "图片无效", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="从图片提取股票代码",
    description="上传截图/图片，通过 Vision LLM 提取股票代码。支持 JPEG、PNG、WebP、GIF，最大 5MB。",
)
def extract_from_image(
    file: Optional[UploadFile] = File(None, description="图片文件（表单字段名 file）"),
    include_raw: bool = Query(False, description="是否在结果中包含原始 LLM 响应"),
) -> ExtractFromImageResponse:
    """
    从上传的图片中提取股票代码（使用 Vision LLM）。

    表单字段请使用 file 上传图片。优先级：Gemini / Anthropic / OpenAI（首个可用）。
    """
    if not file or not file.filename:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_request", "message": "未提供文件，请使用表单字段 file 上传图片"},
        )

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_type",
                "message": f"不支持的类型: {content_type}。允许: {ALLOWED_MIME_STR}",
            },
        )

    try:
        # 先读取限定大小，再检查是否还有剩余（语义清晰：超出则拒绝）
        data = file.file.read(MAX_SIZE_BYTES)
        if file.file.read(1):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "file_too_large",
                    "message": f"图片超过 {MAX_SIZE_BYTES // (1024 * 1024)}MB 限制",
                },
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"读取上传文件失败: {e}")
        raise HTTPException(
            status_code=400,
            detail={"error": "read_failed", "message": "读取上传文件失败"},
        )

    try:
        items, raw_text = extract_stock_codes_from_image(data, content_type)
        extract_items = [
            ExtractItem(code=code, name=name, confidence=conf) for code, name, conf in items
        ]
        codes = [i.code for i in extract_items]
        return ExtractFromImageResponse(
            codes=codes,
            items=extract_items,
            raw_text=raw_text if include_raw else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"error": "extract_failed", "message": str(e)})
    except Exception as e:
        logger.error(f"图片提取失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "图片提取失败"},
        )


@router.post(
    "/parse-import",
    response_model=ExtractFromImageResponse,
    responses={
        200: {"description": "解析结果"},
        400: {"description": "未提供数据或解析失败", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="解析 CSV/Excel/剪贴板",
    description="上传 CSV/Excel 文件或粘贴文本，自动解析股票代码。文件上限 2MB，文本上限 100KB。",
)
async def parse_import(request: Request) -> ExtractFromImageResponse:
    """
    解析 CSV/Excel 文件或剪贴板文本。

    - multipart/form-data + file: 上传文件
    - application/json + {"text": "..."}: 粘贴文本
    - 优先使用 file，若同时提供则忽略 text
    """
    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception as e:
            logger.warning("[parse_import] JSON parse failed: %s", e)
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_json", "message": f"JSON 解析失败: {e}"},
            )
        text = body.get("text") if isinstance(body, dict) else None
        if not text or not isinstance(text, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "bad_request", "message": "未提供 text，请使用 {\"text\": \"...\"}"},
            )
        try:
            items = parse_import_from_text(text)
        except ValueError as e:
            text_bytes = len(text.encode("utf-8"))
            logger.warning(
                "[parse_import] parse_import_from_text failed: text_bytes=%d, error=%s",
                text_bytes,
                e,
            )
            raise HTTPException(status_code=400, detail={"error": "parse_failed", "message": str(e)})
    elif "multipart" in content_type:
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            raise HTTPException(
                status_code=400,
                detail={"error": "bad_request", "message": "未提供文件，请使用表单字段 file"},
            )
        file_size = getattr(file, "size", None)
        if isinstance(file_size, int) and file_size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "file_too_large",
                    "message": f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 限制",
                },
            )
        try:
            data = file.file.read(MAX_FILE_BYTES)
            if file.file.read(1):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "file_too_large",
                        "message": f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 限制",
                    },
                )
        except HTTPException:
            raise
        except Exception as e:
            filename = getattr(file, "filename", None) or ""
            size = getattr(file, "size", None)
            logger.warning(
                "[parse_import] file read failed: filename=%r, size=%s, error=%s",
                filename,
                size,
                e,
            )
            raise HTTPException(
                status_code=400,
                detail={"error": "read_failed", "message": "读取文件失败"},
            )
        filename = getattr(file, "filename", None) or ""
        try:
            items = parse_import_from_bytes(data, filename=filename)
        except ValueError as e:
            ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            logger.warning(
                "[parse_import] parse_import_from_bytes failed: filename=%r, ext=%r, bytes=%d, error=%s",
                filename,
                ext,
                len(data),
                e,
            )
            raise HTTPException(status_code=400, detail={"error": "parse_failed", "message": str(e)})
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "bad_request",
                "message": "请使用 multipart/form-data 上传文件，或 application/json 提交 {\"text\": \"...\"}",
            },
        )

    extract_items = [
        ExtractItem(code=code, name=name, confidence=conf)
        for code, name, conf in items
    ]
    codes = list(dict.fromkeys(i.code for i in extract_items if i.code))
    return ExtractFromImageResponse(codes=codes, items=extract_items, raw_text=None)


@router.get(
    "/watchlist",
    response_model=WatchlistResponse,
    responses={
        200: {"description": "当前自选队列"},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取自选队列",
    description="返回当前 STOCK_LIST 配置中的所有股票代码。",
)
def get_watchlist(
    service: SystemConfigService = Depends(get_system_config_service),
) -> WatchlistResponse:
    try:
        codes = _read_watchlist_codes(service)
        return WatchlistResponse(stock_codes=codes, message=f"当前自选 {len(codes)} 只股票")
    except Exception as e:
        logger.error(f"获取自选队列失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"获取自选队列失败: {str(e)}"},
        )


@router.post(
    "/watchlist/add",
    response_model=WatchlistResponse,
    responses={
        200: {"description": "已加入自选"},
        400: {"description": "参数错误", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="加入自选队列",
    description="将指定股票代码加入 STOCK_LIST。",
)
def add_to_watchlist(
    request: WatchlistRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> WatchlistResponse:
    try:
        validated = _validate_and_normalize_stock_code(request.stock_code)
        entered = request.stock_code.strip()
        try:
            renamed = resolve_bse_code(validated)
        except LegacyBseCodeError as exc:
            raise HTTPException(status_code=400, detail={"error": "legacy_bse_code", "message": str(exc)})
        if renamed != validated:
            # Old 北交所 codes return frozen quotes in some sources: store the new code.
            validated, entered = renamed, renamed
        codes = _read_watchlist_codes(service)
        existing_keys = [_watchlist_match_key(c) for c in codes]
        if _watchlist_match_key(validated) not in existing_keys:
            codes.append(entered)
            _write_watchlist_codes(service, codes)
        note = f"（{request.stock_code.strip()} 是北交所旧代码，已换成新代码）" if entered != request.stock_code.strip() else ""
        return WatchlistResponse(stock_codes=codes, message=f"已加入 {entered}{note}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"加入自选失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"加入自选失败: {str(e)}"},
        )


@router.post(
    "/watchlist/remove",
    response_model=WatchlistResponse,
    responses={
        200: {"description": "已从自选删除"},
        400: {"description": "参数错误", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="从自选队列删除",
    description="从 STOCK_LIST 中移除指定股票代码。",
)
def remove_from_watchlist(
    request: WatchlistRequest,
    service: SystemConfigService = Depends(get_system_config_service),
) -> WatchlistResponse:
    try:
        validated = _validate_and_normalize_stock_code(request.stock_code)
        codes = _read_watchlist_codes(service)
        existing_keys = [_watchlist_match_key(c) for c in codes]
        requested_key = _watchlist_match_key(validated)
        if requested_key in existing_keys:
            idx = existing_keys.index(requested_key)
            codes.pop(idx)
            _write_watchlist_codes(service, codes)
        return WatchlistResponse(stock_codes=codes, message=f"已移除 {request.stock_code.strip()}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"从自选删除失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"从自选删除失败: {str(e)}"},
        )


@router.get(
    "/{stock_code}/profile",
    response_model=StockProfileResponse,
    responses={
        400: {"description": "股票代码无效", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取个股研究聚合档案",
    description="按独立质量状态聚合行情、历史、研究产物、资讯、持仓关系和监控规则。",
)
def get_stock_profile(
    stock_code: str,
    history_days: int = Query(60, ge=1, le=365, description="日线历史天数"),
) -> StockProfileResponse:
    """Return partial profile data without failing on one optional block."""
    _validate_and_normalize_stock_code(stock_code)
    try:
        return StockProfileResponse(
            **StockProfileService().get_profile(stock_code, history_days=history_days)
        )
    except InvalidStockProfileCode:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_stock_code", "message": "股票代码与交易所不匹配"},
        )
    except Exception as exc:
        sanitized = sanitize_diagnostic_text(str(exc), max_length=300) or "internal profile error"
        logger.error("获取个股研究聚合档案失败: %s", sanitized)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "获取个股研究聚合档案失败"},
        )


@router.get(
    "/{stock_code}/quote",
    response_model=StockQuote,
    responses={
        200: {"description": "行情数据"},
        404: {"description": "股票不存在", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取股票实时行情",
    description="获取指定股票的最新行情数据"
)
def get_stock_quote(stock_code: str) -> StockQuote:
    """
    获取股票实时行情
    
    获取指定股票的最新行情数据
    
    Args:
        stock_code: 股票代码（如 600519、00700、AAPL）
        
    Returns:
        StockQuote: 实时行情数据
        
    Raises:
        HTTPException: 404 - 股票不存在
    """
    try:
        service = StockService()
        
        # 使用 def 而非 async def，FastAPI 自动在线程池中执行
        result = service.get_realtime_quote(stock_code)
        
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "not_found",
                    "message": f"未找到股票 {stock_code} 的行情数据"
                }
            )
        
        return StockQuote(
            stock_code=result.get("stock_code", stock_code),
            stock_name=result.get("stock_name"),
            current_price=result.get("current_price", 0.0),
            change=result.get("change"),
            change_percent=result.get("change_percent"),
            open=result.get("open"),
            high=result.get("high"),
            low=result.get("low"),
            prev_close=result.get("prev_close"),
            volume=result.get("volume"),
            amount=result.get("amount"),
            turnover_rate=result.get("turnover_rate"),
            float_shares=result.get("float_shares"),
            update_time=result.get("update_time")
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取实时行情失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": f"获取实时行情失败: {str(e)}"
            }
        )


@router.get(
    "/{stock_code}/history",
    response_model=StockHistoryResponse,
    responses={
        200: {"description": "历史行情数据"},
        422: {"description": "不支持的周期参数", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取股票历史行情",
    description="获取指定股票的历史 K 线数据"
)
def get_stock_history(
    stock_code: str,
    period: str = Query("daily", description="K 线周期", pattern="^(daily|weekly|monthly)$"),
    days: int = Query(30, ge=1, le=1000, description="获取天数（数据源按约 2 倍日历日取数）")
) -> StockHistoryResponse:
    """
    获取股票历史行情
    
    获取指定股票的历史 K 线数据
    
    Args:
        stock_code: 股票代码
        period: K 线周期 (daily/weekly/monthly)
        days: 获取天数
        
    Returns:
        StockHistoryResponse: 历史行情数据
    """
    try:
        service = StockService()
        
        # 使用 def 而非 async def，FastAPI 自动在线程池中执行
        result = service.get_history_data(
            stock_code=stock_code,
            period=period,
            days=days
        )
        
        # 转换为响应模型
        data = [
            KLineData(
                date=item.get("date"),
                open=item.get("open"),
                high=item.get("high"),
                low=item.get("low"),
                close=item.get("close"),
                volume=item.get("volume"),
                amount=item.get("amount"),
                change_percent=item.get("change_percent")
            )
            for item in result.get("data", [])
        ]
        
        return StockHistoryResponse(
            stock_code=stock_code,
            stock_name=result.get("stock_name"),
            period=period,
            data=data
        )
    
    except ValueError as e:
        # period 参数不支持的错误（如 weekly/monthly）
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_period",
                "message": str(e)
            }
        )
    except Exception as e:
        logger.error(f"获取历史行情失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": f"获取历史行情失败: {str(e)}"
            }
        )


# 503 intraday_unavailable: user-facing message per ``IntradayDataUnavailable.reason`` (top-level ``reason``).
INTRADAY_UNAVAILABLE_MESSAGES = {
    "biying_not_configured": "历史交易日的分时来自必盈数据源，还没有配置必盈 licence：请在设置的数据源里填写 BIYING_LICENCE 后重试。",
    "not_configured": "没有可用的分钟线数据源：请在设置的数据源里配置 Tushare 或必盈 licence（BIYING_LICENCE）。",
    "no_bars": "数据源没有这一天的分时数据，可能当天停牌或不是交易日。",
    "source_failed": "分钟线数据源请求失败，请稍后重试；多次失败请检查数据源配置和网络。",
}


@router.get(
    "/{stock_code}/events",
    response_model=StockEventsResponse,
    responses={
        200: {"description": "公司事件和风险"},
        400: {"description": "不是 A 股个股代码（指数代码、港美股等）", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取个股公司事件（业绩预告、增减持、回购、解禁、质押、调研）",
    description=(
        "返回最近 days 天的公司事件（解禁额外包含未来 30 天），以及按固定规则得出的风险："
        "未来 30 天解禁占流通股 1% 以上、近 30 天单个股东减持占总股本 0.5% 以上、股权质押比例 50% 以上、"
        "近 60 天业绩预告为预减 / 首亏 / 续亏 / 略减。数据来自 Tushare，每天全市场入库一次；"
        "本地没有近期数据时会按需补取这只股票。kinds 用逗号分隔，可选 forecast, express, holder_trade, "
        "repurchase, share_float, pledge, survey。"
    ),
)
def get_stock_events(
    stock_code: str,
    kinds: Optional[str] = Query(None, description="逗号分隔的事件类型，留空返回全部"),
    days: int = Query(30, ge=1, le=120, description="回看天数"),
) -> StockEventsResponse:
    from src.services.stock_code_guard import require_stock_code
    from src.services.stock_event_service import KIND_LABELS, StockEventService

    try:
        code = require_stock_code(stock_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_stock_code", "message": str(exc)})
    wanted = [k.strip() for k in (kinds or "").split(",") if k.strip()] or None
    unknown = [k for k in (wanted or []) if k not in KIND_LABELS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_kind", "message": f"未知事件类型：{', '.join(unknown)}"},
        )
    try:
        result = StockEventService().events_for(code, kinds=wanted, days=days)
    except Exception as e:
        logger.error(f"获取公司事件失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "获取公司事件失败"})
    return StockEventsResponse(
        stock_code=code,
        as_of=result["as_of"],
        events=[StockEventItem(**{**event, "kind_label": KIND_LABELS[event["kind"]]})
                for event in result["events"] if event["kind"] in KIND_LABELS],
        risks=result["risks"],
        fetch=result["fetch"],
    )


_QA_REFRESH_HOURS = 6


@router.get(
    "/{stock_code}/qa",
    response_model=StockQaResponse,
    responses={
        200: {"description": "已回复的投资者问答"},
        400: {"description": "不是 A 股个股代码", "model": ErrorResponse},
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取个股投资者问答（互动易 / 上证e互动）",
    description=(
        "深市公司来自巨潮互动易，沪市公司来自上证e互动，只返回已回复的问答，按回复时间倒序。"
        "问答存进资讯库（item_kind=qa）；本地近 6 小时没有取过这只股票时，会先从平台刷新一次。"
        "两个平台只保留约近 1 个月，北交所公司两个平台都没有。"
    ),
)
def get_stock_qa(
    stock_code: str,
    days: int = Query(30, ge=1, le=60, description="回看天数"),
) -> StockQaResponse:
    import json as _json
    from datetime import timedelta as _timedelta

    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_pool_service import NewsPoolService, china_now
    from src.services.sources.investor_qa import route
    from src.services.stock_code_guard import require_stock_code

    try:
        code = require_stock_code(stock_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_stock_code", "message": str(exc)})
    platform = route(code)
    labels = {"irm": "巨潮互动易", "sse_e": "上证e互动"}
    if platform is None:
        return StockQaResponse(stock_code=code, platform=None, items=[],
                               coverage="北交所公司不在互动易和上证e互动上，没有问答数据")
    coverage = f"{labels[platform]}，只含已回复的问答，平台只保留约近 1 个月"
    try:
        repo = IntelligenceRepository()
        now = china_now()
        since = now - _timedelta(days=days)

        def stored():
            return repo.list_stock_news(code=code, since=since, kinds=("qa",), limit=100)

        rows = stored()
        fetched_at = max((row.fetched_at for row in rows if row.fetched_at), default=None)
        fetch: dict = {}
        if fetched_at is None or now - fetched_at > _timedelta(hours=_QA_REFRESH_HOURS):
            fetch = NewsPoolService(repo).fetch_qa_for_codes([code], now=now, since=since)
            rows = stored()
    except Exception as e:
        logger.error(f"获取投资者问答失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "internal_error", "message": "获取投资者问答失败"})
    items = []
    for row in rows:
        try:
            payload = _json.loads(row.raw_payload) if row.raw_payload else {}
        except (TypeError, ValueError):
            payload = {}
        question, answer = payload.get("question"), payload.get("answer")
        if not question or not answer:
            continue
        items.append(StockQaItem(
            question=question, answer=answer, asked_at=(payload.get("asked_at") or "")[:16].replace("T", " ") or None,
            answered_at=row.published_at.strftime("%Y-%m-%d %H:%M") if row.published_at else None,
            platform=payload.get("platform") or platform, platform_label=labels[payload.get("platform") or platform],
        ))
    return StockQaResponse(stock_code=code, platform=platform, coverage=coverage, items=items, fetch=fetch)


@router.get(
    "/{stock_code}/intraday",
    response_model=StockIntradayResponse,
    responses={
        200: {"description": "指定交易日的分时（5 分钟）行情"},
        422: {"description": "日期格式错误或为未来日期", "model": ErrorResponse},
        503: {
            "description": (
                "取不到分时：error=intraday_unavailable，reason 为 biying_not_configured / "
                "not_configured / no_bars / source_failed，message 是对应的中文说明"
            ),
            "model": ErrorResponse,
        },
        500: {"description": "服务器错误", "model": ErrorResponse},
    },
    summary="获取个股某个交易日的分时行情",
    description=(
        "按需返回 A 股某个交易日的 5 分钟线（不落库）。当天走 Tushare / 必盈实时链路，"
        "历史交易日走必盈历史分钟线（需配置 BIYING_LICENCE）。"
    ),
)
def get_stock_intraday(
    stock_code: str,
    date: str = Query(..., description="交易日 YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> StockIntradayResponse:
    try:
        result = get_intraday_snapshot_service().get_session_bars(stock_code, date)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_date", "message": str(e)},
        )
    except IntradayDataUnavailable as e:
        reason = getattr(e, "reason", "source_failed")
        logger.info("分时行情不可用 %s %s (%s): %s", stock_code, date, reason, e)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "intraday_unavailable",
                "message": INTRADAY_UNAVAILABLE_MESSAGES.get(reason, INTRADAY_UNAVAILABLE_MESSAGES["source_failed"]),
                # Top level, not under ``detail``: the Web error parser reads a ``detail`` object as the message.
                "reason": reason,
            },
        )
    except Exception as e:
        logger.error(f"获取分时行情失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": "获取分时行情失败"},
        )

    return StockIntradayResponse(
        stock_code=result.code,
        trade_date=result.trade_date or date,
        source=result.source,
        as_of=result.as_of,
        fetched_at=result.fetched_at,
        is_stale=result.is_stale,
        stale_reason=result.stale_reason,
        data=[IntradayBar(**bar) for bar in result.bars],
    )
