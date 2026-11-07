# -*- coding: utf-8 -*-
"""
Board tools — closing board judgments (and an intraday view during market hours).

Tools:
- get_board_signal: latest closing judgment of an industry / concept board (state, tags,
  reason, evidence, data gaps); during market hours also an intraday aggregate computed from
  the cached full-market snapshot over the board's members of the previous trading day.

Read-only and not stock-scoped. The intraday figure is a live view only: it never rewrites the
closing judgment or the next-day plan (docs/design/core-backend-design.md 3.1).
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

from src.agent.tools.registry import ToolParameter, ToolDefinition, ToolPolicy

logger = logging.getLogger(__name__)

_BOARD_READ_POLICY = ToolPolicy.declared(
    read_only=True,
    side_effects=["db_read", "network_read"],
    permissions=["board_signal:read"],
)

_CN_TZ = timezone(timedelta(hours=8))
INTRADAY_NOTE = "这是盘中实时强弱，只作参考；收盘判定和次日计划不会因此改变。"
MIN_INTRADAY_COVERAGE = 0.8


def _china_today() -> date:
    return datetime.now(_CN_TZ).date()


def _market_is_open() -> bool:
    """True while the A-share session is open (fail closed: unknown means not open)."""
    try:
        from src.core.trading_calendar import build_market_phase_context

        return bool(build_market_phase_context(market="cn").is_market_open_now)
    except Exception as exc:  # noqa: BLE001
        logger.debug("market phase unavailable: %s", exc)
        return False


def _pct(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 2)


def _closing_payload(repo: Any, board: Dict[str, Any]) -> Dict[str, Any]:
    from src.services.board_anomaly_service import STATE_LABELS, BoardAnomalyService

    run = repo.latest_completed_run(None)
    if run is None:
        return {
            "available": False,
            "note": "暂无板块收盘判定（尚未完成板块日度运行）",
        }
    detail = BoardAnomalyService(repo=repo).get_board_detail(board["ref"], run["trade_date"]) or {}
    return {
        "available": True,
        "trade_date": run["trade_date"].isoformat(),
        "run_status": run["status"],
        "state": detail.get("state", "none"),
        "state_label": STATE_LABELS.get(detail.get("state", "none"), detail.get("state")),
        "tags": detail.get("tags", []),
        "reason": detail.get("reason", ""),
        "verdict": detail.get("verdict_text", ""),
        "change_pct": detail.get("change_pct"),
        "change_5d_pct": detail.get("change_5d_pct"),
        "excess_pct": detail.get("excess_pct"),
        "amount_ratio": detail.get("amount_ratio"),
        "breadth_pct": detail.get("breadth_pct"),
        "limit_up_count": detail.get("limit_up_count"),
        "broken_rate_pct": detail.get("broken_rate_pct"),
        "driver": detail.get("drivers", ""),
        "driver_kind": detail.get("driver_kind", "unknown"),
        "evidence": detail.get("evidence", []),
        "data_gaps": detail.get("data_gaps", []),
        "can_pick": detail.get("can_pick", False),
    }


def _intraday_payload(repo: Any, board: Dict[str, Any], closing_date: Optional[date]) -> Dict[str, Any]:
    """Aggregate the live snapshot over the board's previous-trading-day members."""
    from src.services.intraday_snapshot_service import (
        IntradayDataUnavailable,
        aggregate_snapshot,
        get_intraday_snapshot_service,
    )

    unavailable = {"available": False, "reason": "盘中板块数据不可用，仅回答上一交易日收盘后的判定"}
    if closing_date is None:
        return unavailable
    members = sorted(repo.get_members_on(board["id"], closing_date))
    if not members:
        return {**unavailable, "reason": "该板块没有成分数据，无法计算盘中强弱"}
    try:
        snapshot = get_intraday_snapshot_service().get_market_snapshot()
    except IntradayDataUnavailable as exc:
        logger.info("[board tool] intraday snapshot unavailable: %s", exc)
        return unavailable
    except Exception as exc:  # noqa: BLE001 - never break the closing answer
        logger.warning("[board tool] intraday snapshot failed: %s", exc)
        return unavailable

    rows = repo.get_stock_rows(closing_date, members)
    weights = {code: row["float_mv"] for code, row in rows.items() if row.get("float_mv") and row["float_mv"] > 0}
    stats = aggregate_snapshot(snapshot, codes=members, weights=weights or None)
    market = aggregate_snapshot(snapshot)
    gaps = []
    if stats.matched == 0:
        return {**unavailable, "reason": "快照中没有该板块成分股的行情"}
    coverage = stats.matched / stats.requested if stats.requested else 0.0
    if coverage < MIN_INTRADAY_COVERAGE:
        gaps.append(f"快照只覆盖 {stats.matched}/{stats.requested} 只成分股，强弱仅供参考")
    if stats.cap_weighted_change_pct is None:
        gaps.append("缺少市值权重，加权涨幅不可用")
    elif weights and stats.weight_coverage < MIN_INTRADAY_COVERAGE:
        gaps.append(f"仅 {stats.weight_coverage * 100:.0f}% 的成分股有权重，加权涨幅仅供参考")
    if snapshot.is_stale:
        gaps.append("数据延迟（快照刷新失败或数据源时间滞后）")
    excess = None
    if stats.cap_weighted_change_pct is not None and market.cap_weighted_change_pct is not None:
        excess = stats.cap_weighted_change_pct - market.cap_weighted_change_pct
    return {
        "available": True,
        "as_of": stats.as_of,
        "source": stats.source,
        "is_stale": stats.is_stale,
        "members_as_of": closing_date.isoformat(),
        "cap_weighted_change_pct": _pct(stats.cap_weighted_change_pct),
        "equal_weight_change_pct": _pct(stats.equal_weight_change_pct),
        "excess_vs_all_a_pct": _pct(excess),
        "up_count": stats.up_count,
        "down_count": stats.down_count,
        "up_ratio_pct": _pct(stats.up_count / stats.matched * 100.0),
        "members_matched": stats.matched,
        "members_requested": stats.requested,
        "turnover_yi": _pct(stats.total_amount / 1e8),
        "all_a_cap_weighted_change_pct": _pct(market.cap_weighted_change_pct),
        "data_gaps": gaps,
        "note": INTRADAY_NOTE,
    }


def _handle_get_board_signal(board: str, include_intraday: bool = True) -> dict:
    """Latest closing judgment of a board, plus intraday strength while the market is open."""
    from src.repositories.board_analysis_repo import BoardAnalysisRepository

    query = str(board or "").strip()
    if not query:
        return {"error": "请提供板块 id（如 concept:BK1184）或板块名称"}
    repo = BoardAnalysisRepository()
    try:
        matches = repo.find_boards(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[board tool] board lookup failed: %s", exc)
        return {"error": "板块数据读取失败"}
    if not matches:
        return {"error": f"未找到板块 {query}"}
    if len(matches) > 1:
        return {
            "error": f"板块 {query} 有 {len(matches)} 个匹配，请指定 id",
            "candidates": [{"id": m["ref"], "name": m["name"]} for m in matches[:10]],
        }
    target = matches[0]

    try:
        closing = _closing_payload(repo, target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[board tool] closing judgment failed: %s", exc, exc_info=True)
        return {"error": "板块收盘判定读取失败"}
    result: Dict[str, Any] = {
        "board": {"id": target["ref"], "name": target["name"], "board_type": target["board_type"]},
        "closing": closing,
    }
    if include_intraday and _market_is_open():
        closing_date = date.fromisoformat(closing["trade_date"]) if closing.get("available") else None
        if closing_date is not None and closing_date >= _china_today():
            closing_date = None  # members must come from the previous trading day
        result["intraday"] = _intraday_payload(repo, target, closing_date)
    return result


get_board_signal_tool = ToolDefinition(
    name="get_board_signal",
    description=(
        "Get the latest closing judgment of an A-share industry or concept board: state "
        "(first launch / strengthening / divergence / ebbing / abnormal drop / insufficient history / none), "
        "tags, one-sentence reason, evidence and data gaps. During market hours it also returns a live "
        "intraday strength estimate (as-of time and source included) computed over the board's members of the "
        "previous trading day. The intraday view never changes the closing judgment or the next-day plan."
    ),
    parameters=[
        ToolParameter(
            name="board",
            type="string",
            description="Board id such as 'concept:BK1184' or 'industry:BK0448', or a board name such as '商业航天'",
            required=True,
        ),
        ToolParameter(
            name="include_intraday",
            type="boolean",
            description="Add the intraday aggregate when the market is open (default: true)",
            required=False,
            default=True,
        ),
    ],
    handler=_handle_get_board_signal,
    category="market",
    policy=_BOARD_READ_POLICY,
)


ALL_BOARD_TOOLS = [
    get_board_signal_tool,
]
