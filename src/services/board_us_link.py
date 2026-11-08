# -*- coding: utf-8 -*-
"""17:00 board pipeline step: overnight US linkage (docs/design/core-backend-design.md 5.1 - 5.3).

Fetches the previous completed US session of the US leaders (Eastmoney via AKShare first, then Tushare,
then yfinance; stored in ``stock_daily``), and for every leader that crossed its move threshold appends
``evidence["us_link"]`` to the signals of the A-share boards mapped from that leader.

Only the evidence JSON is extended: signal state, flags, reason and driver kind are never touched,
so the state rules stay purely price-based. ``BoardAnomalyService`` shows the extra text as additional
``evidence`` lines and in ``drivers``. The step never raises (the caller records it as a warning).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.repositories.board_analysis_repo import BoardAnalysisRepository
from src.services.us_leaders import BoardResolution, get_us_leaders, resolve_leader_boards
from src.services.us_overnight_service import SOURCES_ALL, UsMove, UsOvernightResult, UsOvernightService

logger = logging.getLogger(__name__)

EVIDENCE_KEY = "us_link"
BOARD_RUN_TIME = dtime(17, 0)
_CN_TZ = ZoneInfo("Asia/Shanghai")


def board_run_as_of(day: date, now: Optional[datetime] = None) -> datetime:
    """Reference time for "the previous US session" of the board run of ``day``: ``min(now, day 17:00 CN)``."""
    nominal = datetime.combine(day, BOARD_RUN_TIME, tzinfo=_CN_TZ)
    now = now or datetime.now(_CN_TZ)
    return min(nominal, now)


def move_line(move: UsMove) -> str:
    gap = f"，开盘跳空 {move.gap_pct:+.2f}%" if move.gap_pct is not None else ""
    pct = f"{move.pct_chg:+.2f}%" if move.pct_chg is not None else "—"
    return f"{move.name}({move.symbol}) {move.trade_date.isoformat()} 收盘 {pct}{gap}（阈值 ±{move.threshold:g}%）"


def build_us_link_evidence(session: date, moves: List[UsMove]) -> Dict[str, Any]:
    """The ``evidence["us_link"]`` payload for one board (``moves`` = its crossed leaders)."""
    ordered = sorted(moves, key=lambda m: abs(m.pct_chg or 0.0), reverse=True)
    text = "隔夜美股联动：" + "；".join(
        f"{m.name}({m.symbol}) {m.pct_chg:+.2f}%" if m.pct_chg is not None else f"{m.name}({m.symbol})"
        for m in ordered
    )
    return {
        "session": session.isoformat(),
        "leaders": [m.to_dict() for m in ordered],
        "lines": ["隔夜美股：" + move_line(m) for m in ordered],
        "text": text,
    }


def attach_us_link(
    run_id: int,
    day: date,
    repo: BoardAnalysisRepository,
    *,
    us_service: Optional[UsOvernightService] = None,
    now: Optional[datetime] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Run the step; returns ``(status, detail, data)`` for the pipeline's step record."""
    try:
        leaders_config = get_us_leaders()
        us_service = us_service or UsOvernightService(leaders=leaders_config.leaders)
        if not us_service.leaders:
            return "skipped", "未配置美股龙头 (US_LEADERS 为空数组)", {}
        result = us_service.refresh(board_run_as_of(day, now), sources=SOURCES_ALL)
        resolution = resolve_leader_boards(us_service.leaders, repo.list_catalog())
        return _summarize(run_id, repo, result, resolution, leaders_config.warnings)
    except Exception as exc:  # noqa: BLE001 - this step is a warning, never fatal
        logger.warning("[BoardPipeline] 隔夜美股联动失败: %s", exc, exc_info=True)
        return "failed", f"{type(exc).__name__}: {str(exc)[:200]}", {}


def _summarize(
    run_id: int,
    repo: BoardAnalysisRepository,
    result: UsOvernightResult,
    resolution: BoardResolution,
    config_warnings: List[str],
) -> Tuple[str, str, Dict[str, Any]]:
    unresolved = resolution.unresolved_names()
    if unresolved:
        logger.warning("[BoardPipeline] 美股龙头映射不到 board_catalog 的板块名: %s", unresolved)
    data: Dict[str, Any] = {"us": result.to_dict(), "unresolved_boards": unresolved, "config_warnings": config_warnings}
    if not result.ok:
        detail = "没有取到已收盘的美股日线：" + (
            "；".join(f"{s}: {r}" for s, r in list(result.failures.items())[:3]) or "；".join(result.notes) or "无数据"
        )
        return "partial", detail, data

    crossed = result.crossed
    by_board: Dict[int, List[UsMove]] = {}
    boards_by_id: Dict[int, str] = {}
    moves_by_symbol = {m.symbol: m for m in crossed}
    for symbol, matches in resolution.by_leader.items():
        move = moves_by_symbol.get(symbol)
        if move is None:
            continue
        for match in matches:
            by_board.setdefault(match.id, []).append(move)
            boards_by_id[match.id] = match.name
    additions = {bid: {EVIDENCE_KEY: build_us_link_evidence(result.session, moves)} for bid, moves in by_board.items()}
    attached = repo.merge_signal_evidence(run_id, additions)

    data.update(attached_boards=attached, mapped_boards=sorted(boards_by_id.values()))
    detail = (
        f"美股 {result.session}：{len(result.moves)} 个龙头，{len(crossed)} 个超过阈值"
        f"（{', '.join(m.symbol for m in crossed) or '无'}）；{attached} 个板块已追加隔夜美股依据"
    )
    if unresolved:
        detail += f"；未映射的板块名 {unresolved}"
    problems = []
    if result.failures:
        problems.append(f"{len(result.failures)} 个龙头无数据 {sorted(result.failures)}")
    if result.stale:
        problems.append(f"{len(result.stale)} 个龙头不是最新交易日 {sorted(result.stale)}")
    if problems:
        return "partial", detail + "；" + "；".join(problems), data
    return "succeeded", detail, data
