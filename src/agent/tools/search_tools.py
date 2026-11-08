# -*- coding: utf-8 -*-
"""
Search tools — wraps SearchService methods as agent-callable tools.

Tools:
- search_stock_news: search latest stock news
- search_comprehensive_intel: multi-dimensional intelligence search
"""

import logging

from src.agent.news_evidence import record_news_evidence
from src.agent.tools.execution import get_tool_stock_identity
from src.agent.tools.registry import ToolParameter, ToolDefinition, ToolPolicy

logger = logging.getLogger(__name__)

_NEWS_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read", "db_write_cache"],
    permissions=["news:read"],
    scope_dimensions=["stock"],
    supported_asset_types=("stock", "index"),
)
_INTEL_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["network_read", "db_write_cache"],
    permissions=["intel:read"],
    scope_dimensions=["stock"],
    supported_asset_types=("stock", "index"),
)


def _get_db():
    """Lazy import for DatabaseManager."""
    from src.storage import get_db
    return get_db()


def _get_search_service():
    """Return shared SearchService singleton."""
    from src.search_service import get_search_service
    return get_search_service()


def _canonical_search_code(stock_code: str) -> str:
    identity = get_tool_stock_identity(stock_code)
    if identity is not None:
        return identity.stock_code
    from data_provider.base import canonical_stock_code, normalize_stock_code
    from src.services.stock_list_parser import ParseStatus, parse_analysis_target

    raw = str(stock_code or "").strip()
    target = parse_analysis_target(raw)
    if target.asset_type == ParseStatus.INDEX and target.canonical_id:
        return target.canonical_id
    return canonical_stock_code(normalize_stock_code(raw))


def _resolve_search_subject(stock_code: str, stock_name: str) -> tuple[str, str]:
    identity = get_tool_stock_identity(stock_code)
    if identity is not None:
        if identity.asset_type == "index":
            return "", identity.stock_name or identity.stock_code
        # A model-provided label is not evidence for the accepted subject.
        return identity.stock_code, identity.stock_name or identity.stock_code
    from src.services.stock_list_parser import ParseStatus, parse_analysis_target

    target = parse_analysis_target(stock_code)
    if target.asset_type == ParseStatus.INDEX and target.matched_index is not None:
        return "", target.matched_index.display_name
    return stock_code, stock_name


def _persist_news_response(
    *,
    stock_code: str,
    stock_name: str,
    dimension: str,
    response,
) -> None:
    """Best-effort news persistence for Agent search tools."""
    if not response or not getattr(response, "success", False) or not getattr(response, "results", None):
        return

    code = _canonical_search_code(stock_code)
    try:
        saved_count = _get_db().save_news_intel(
            code=code,
            name=stock_name,
            dimension=dimension,
            query=response.query,
            response=response,
            query_context=None,
        )
        logger.info(
            "Agent news intel persisted for %s (dimension=%s, new_records=%s)",
            code,
            dimension,
            saved_count,
        )
    except Exception as exc:
        logger.warning(
            "Agent news intel persistence failed for %s (dimension=%s): %s",
            code,
            dimension,
            exc,
        )


def _handle_search_stock_news(stock_code: str, stock_name: str) -> dict:
    """Search latest news for a stock."""
    service = _get_search_service()
    query_code, query_name = _resolve_search_subject(stock_code, stock_name)

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    response = service.search_stock_news(query_code, query_name, max_results=5)

    if not response.success:
        # 检索已发起但失败：Agent 这一轮没有拿到新闻证据，必须记 0 而不是不记，
        # 否则报告会把「搜过但失败」误报成「未配置搜索渠道」。
        record_news_evidence(0)
        return {
            "query": response.query,
            "success": False,
            "error": response.error_message,
        }

    record_news_evidence(len(response.results))

    _persist_news_response(
        stock_code=stock_code,
        stock_name=query_name,
        dimension="latest_news",
        response=response,
    )

    return {
        "query": response.query,
        "provider": response.provider,
        "success": True,
        "results_count": len(response.results),
        "results": [
            {
                "title": r.title,
                "snippet": r.snippet,
                "url": r.url,
                "source": r.source,
                "published_date": r.published_date,
            }
            for r in response.results
        ],
    }


search_stock_news_tool = ToolDefinition(
    name="search_stock_news",
    description="Search for the latest news articles about a specific stock. "
                "Requires both stock_code and stock_name for accurate search. "
                "Returns news titles, snippets, sources, and URLs.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_stock_news,
    category="search",
    policy=_NEWS_READ_POLICY,
)


# ============================================================
# search_comprehensive_intel
# ============================================================

def _handle_search_comprehensive_intel(stock_code: str, stock_name: str) -> dict:
    """Multi-dimensional intelligence search."""
    service = _get_search_service()
    query_code, query_name = _resolve_search_subject(stock_code, stock_name)

    if not service.is_available:
        return {"error": "No search engine available (no API keys configured)"}

    intel_results = service.search_comprehensive_intel(
        stock_code=query_code,
        stock_name=query_name,
        max_searches=6,
    )

    if not intel_results:
        # 多维检索已发起但整体没有结果，同样必须记 0（见 _handle_search_stock_news）。
        record_news_evidence(0)
        return {"error": "Comprehensive intel search returned no results"}

    # Format into readable report
    report = service.format_intel_report(intel_results, query_name)

    # 本次真正交给 Agent 的证据条数，按维度累计后一次性记录。
    evidence_count = 0

    # Also return structured data
    dimensions = {}
    for dim_name, response in intel_results.items():
        if response and response.success:
            evidence_count += len(response.results)
            _persist_news_response(
                stock_code=stock_code,
                stock_name=query_name,
                dimension=dim_name,
                response=response,
            )
            dimensions[dim_name] = {
                "query": response.query,
                "results_count": len(response.results),
                "results": [
                    {
                        "title": r.title,
                        "snippet": r.snippet,
                        "source": r.source,
                    }
                    for r in response.results[:3]  # limit to 3 per dimension to save tokens
                ],
            }

    record_news_evidence(evidence_count)

    return {
        "report": report,
        "dimensions": dimensions,
    }


search_comprehensive_intel_tool = ToolDefinition(
    name="search_comprehensive_intel",
    description="Multi-dimensional intelligence search: latest news, market analysis, "
                "risk checking, earnings outlook, and industry trends for a stock. "
                "Returns a formatted report and structured results.",
    parameters=[
        ToolParameter(
            name="stock_code",
            type="string",
            description="Stock code, e.g., '600519'",
        ),
        ToolParameter(
            name="stock_name",
            type="string",
            description="Stock name in Chinese, e.g., '贵州茅台'",
        ),
    ],
    handler=_handle_search_comprehensive_intel,
    category="search",
    policy=_INTEL_READ_POLICY,
)


# ============================================================
# search_market_news
# ============================================================

_MARKET_NEWS_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["db_read", "network_read"],
    permissions=["news:read"],
)
_MARKET_NEWS_MAX_DAYS = 7
_MARKET_NEWS_POOL_LIMIT = 15
_MARKET_NEWS_WEB_LIMIT = 8
_MARKET_NEWS_MIN_POOL_HITS = 3
_MARKET_NEWS_SUMMARY_CHARS = 200


def _market_news_terms(keyword: str) -> list:
    return [term for term in str(keyword or "").replace("，", " ").replace(",", " ").split() if term]


def _pool_news_hits(terms: list, days: int) -> list:
    """Stored flash / announcement rows (news pool) whose title, summary or boards mention any term."""
    from datetime import timedelta

    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_pool_service import china_now

    repo = IntelligenceRepository()
    now = china_now()
    rows = repo.list_news_rows(since=now - timedelta(days=days), until=now)
    folded = [term.casefold() for term in terms]
    hits = []
    for row in rows:
        boards = repo.loads_list(row.board_names)
        haystack = " ".join([row.title or "", row.summary or "", *[str(b) for b in boards]]).casefold()
        if not any(term in haystack for term in folded):
            continue
        published = row.published_at or row.fetched_at
        summary = (row.summary or "").strip()
        hits.append({
            "title": row.title,
            "summary": summary[:_MARKET_NEWS_SUMMARY_CHARS],
            "source": row.source_name or row.source,
            "published_at": published.strftime("%Y-%m-%d %H:%M") if published else None,
            "importance": row.importance,
            "boards": boards,
            "stock_codes": repo.loads_list(row.stock_codes),
        })
        if len(hits) >= _MARKET_NEWS_POOL_LIMIT:
            break
    return hits


def _handle_search_market_news(keyword: str, days: int = 3) -> dict:
    """Market / theme news for a keyword: stored news pool first, web topic search as a fallback."""
    terms = _market_news_terms(keyword)
    if not terms:
        return {"error": "请提供关键词，例如「寒潮」或「天然气 涨价」"}
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 3
    days = max(1, min(days, _MARKET_NEWS_MAX_DAYS))

    result = {"keyword": " ".join(terms), "days": days}
    pool_hits = []
    try:
        pool_hits = _pool_news_hits(terms, days)
    except Exception as exc:  # noqa: BLE001 - the pool is optional; fall through to web search
        logger.warning("[search_market_news] news pool read failed: %s", exc)
        result["pool_error"] = "资讯库读取失败"
    result["pool_results"] = pool_hits

    if len(pool_hits) < _MARKET_NEWS_MIN_POOL_HITS:
        service = _get_search_service()
        if not service.is_available:
            result["web_error"] = "未配置搜索引擎"
        else:
            response = service.search_topic_news(
                result["keyword"], max_results=_MARKET_NEWS_WEB_LIMIT, focus_keywords=terms + ["最新消息"],
            )
            if response.success:
                result["web_results"] = [
                    {"title": r.title, "snippet": r.snippet, "url": r.url, "source": r.source,
                     "published_date": r.published_date}
                    for r in response.results
                ]
            else:
                result["web_error"] = response.error_message or "网页搜索失败"

    evidence = len(pool_hits) + len(result.get("web_results") or [])
    record_news_evidence(evidence)
    if not evidence:
        result["note"] = f"最近 {days} 天没有找到和「{result['keyword']}」相关的新闻"
    return result


search_market_news_tool = ToolDefinition(
    name="search_market_news",
    description="Search recent market / theme news by keyword, not tied to one stock "
                "(e.g. '寒潮', '天然气 涨价', '低空经济'). Reads the stored news flashes "
                "(财联社 / 东方财富, kept for 3 days) first and falls back to web search when "
                "they have few hits. Returns titles, summaries, sources, times and related "
                "boards / A-share codes.",
    parameters=[
        ToolParameter(
            name="keyword",
            type="string",
            description="Theme keywords separated by spaces, e.g. '寒潮 供暖'",
        ),
        ToolParameter(
            name="days",
            type="integer",
            description="How many recent days to search, 1-7 (default 3)",
            required=False,
            default=3,
        ),
    ],
    handler=_handle_search_market_news,
    category="search",
    policy=_MARKET_NEWS_POLICY,
)


ALL_SEARCH_TOOLS = [
    search_stock_news_tool,
    search_comprehensive_intel_tool,
    search_market_news_tool,
]
