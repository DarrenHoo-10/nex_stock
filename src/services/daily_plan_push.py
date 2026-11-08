# -*- coding: utf-8 -*-
"""Feishu summary of a daily plan run (docs/design/core-product-spec.md 7.3).

``build_push_markdown`` renders the message (holdings first so the first screen has what matters, then the
switch decision, one line per watchlist stock, the strongest boards, data reminders); the sender and the
「configured」 check reuse the existing notification code. A push failure is reported to the caller and never
fails the run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

VERDICT_LABEL = {
    "buy": "买入", "wait": "等待", "skip": "不买", "suspended": "停牌",
    "hold": "持有", "add": "加仓", "reduce": "减仓", "clear": "清仓",
}
_FLAT_ORDER = {"buy": 0, "wait": 1, "skip": 2, "suspended": 3}
BOARD_STATE_TEXT = {
    "first_launch": "首次启动", "strengthening": "走强", "divergence": "分歧", "ebbing": "退潮",
    "abnormal_drop": "异常下跌",
}
MAX_BOARDS = 5
MAX_GAP_LINES = 8


def _label(plan: Dict[str, Any]) -> str:
    return f"{plan.get('name') or plan.get('code')}（{plan.get('code')}）"


def _default_horizon(plan: Dict[str, Any]) -> Dict[str, Any]:
    return (plan.get("horizons") or {}).get(plan.get("default_horizon") or "short") or {}


def _holding_line(plan: Dict[str, Any]) -> str:
    horizon = _default_horizon(plan)
    verdict = VERDICT_LABEL.get(plan.get("verdict") or "", "—")
    stop = (horizon.get("stop") or {}).get("price")
    extra = []
    if stop:
        extra.append(f"止损 {stop:.2f}")
    if plan.get("next_limit_down"):
        extra.append(f"明日跌停价 {plan['next_limit_down']:.2f}")
    tail = f"（{'，'.join(extra)}）" if extra else ""
    return f"- **{_label(plan)} {verdict}**：{plan.get('headline') or '—'}{tail}"


def _watch_line(plan: Dict[str, Any]) -> str:
    verdict = VERDICT_LABEL.get(plan.get("verdict") or "", "—")
    return f"- {_label(plan)} **{verdict}**：{plan.get('headline') or '—'}"


def build_push_markdown(
    *,
    trade_date: str,
    next_trade_date: str,
    holdings: Sequence[Dict[str, Any]],
    watchlist: Sequence[Dict[str, Any]],
    failures: Sequence[Dict[str, Any]],
    boards: Sequence[Dict[str, Any]],
    data_gaps: Sequence[str],
    duration_sec: Optional[float] = None,
    stock_total: int = 0,
    stock_failed: int = 0,
) -> str:
    """The Feishu message (also the preview shown in the Web run dialog)."""
    lines: List[str] = [f"# 次日计划 {next_trade_date}", f"基于 {trade_date} 收盘数据", ""]

    lines.append("## 今日结论（持仓）")
    if holdings:
        lines.extend(_holding_line(plan) for plan in holdings)
    else:
        lines.append("- 当前没有录入持仓")
    lines.append("")

    switches = [(plan, plan.get("switch")) for plan in holdings if plan.get("switch")]
    lines.append("## 换股判断")
    if switches:
        for plan, switch in switches:
            lines.append(f"- {plan.get('name') or plan.get('code')}：{switch.get('summary') or '不换'}")
            for condition in switch.get("conditions") or []:
                met = condition.get("met")
                mark = "已满足" if met else ("待确认" if met is None else "未满足")
                lines.append(f"  - {condition.get('text')}（{mark}）")
    else:
        lines.append("- 没有持满最多持股数，不涉及换股")
    lines.append("")

    lines.append("## 自选股")
    ordered = sorted(watchlist, key=lambda p: _FLAT_ORDER.get(p.get("verdict") or "", 9))
    if ordered:
        lines.extend(_watch_line(plan) for plan in ordered)
    else:
        lines.append("- 自选里没有其他股票")
    lines.append("")

    lines.append("## 板块异动")
    if boards:
        for board in list(boards)[:MAX_BOARDS]:
            state = BOARD_STATE_TEXT.get(board.get("state"), board.get("state") or "")
            reason = board.get("reason") or ""
            lines.append(f"- {board.get('name')}（{state}）{'：' + reason if reason else ''}")
    else:
        lines.append("- 没有可用的板块判定结果")
    lines.append("")

    reminders: List[str] = []
    reminders.extend(
        f"分析失败：{item.get('name') or ''} {item.get('code')}：{item.get('reason') or ''}".replace("  ", " ")
        for item in failures
    )
    reminders.extend(str(gap) for gap in data_gaps)
    lines.append("## 数据提醒")
    if reminders:
        lines.extend(f"- {text}" for text in reminders[:MAX_GAP_LINES])
        if len(reminders) > MAX_GAP_LINES:
            lines.append(f"- 另有 {len(reminders) - MAX_GAP_LINES} 条，见网页「设置 → 运行和推送」")
    else:
        lines.append("- 没有缺失的数据")
    lines.append("")

    stats = f"共 {stock_total} 只，失败 {stock_failed} 只"
    if duration_sec is not None:
        stats += f"，用时 {duration_sec:.0f} 秒"
    lines.append(f"{stats}。完整计划（价位、条件、股数怎么算的）请在网页「我的股票」查看。")
    return "\n".join(lines)


def feishu_configured(config: Any) -> bool:
    """Whether a Feishu route exists (app bot + bound chat / chat id, or the group webhook)."""
    try:
        from src.notification import NotificationChannel, NotificationService

        return NotificationChannel.FEISHU in NotificationService.detect_configured_channels(config)
    except Exception as exc:  # noqa: BLE001 - a broken notification module must not break the run
        logger.warning("[DailyPlan] 无法判断飞书是否已配置: %s", exc)
        return False


def send_feishu(content: str) -> None:
    """Send to Feishu only (the plan is Feishu-only, spec 7.3); raises when the send fails."""
    from src.notification import NotificationService

    if not NotificationService().send_to_feishu(content):
        raise RuntimeError("飞书推送失败，请检查飞书配置和机器人权限")
