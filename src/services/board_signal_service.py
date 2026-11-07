# -*- coding: utf-8 -*-
"""Board anomaly signals (docs/design/core-product-spec.md 3.2 / 3.3).

For one trade date this turns the stored ``board_daily`` facts into one ``board_signal``
row per board: state, tags, evidence (every condition with its actual value, threshold
and result), reason sentence and driver.

State names follow the API (``first_launch`` / ``strengthening`` / ``divergence`` /
``ebbing`` / ``abnormal_drop`` / ``insufficient_history`` / ``none``).

Three-valued logic
------------------
Each condition is ``True`` / ``False`` / ``None`` (data not available). "No data" is never
folded into "condition not met": ``AND`` is False if any operand is False, else None if any
is None; ``OR`` is True if any operand is True, else None if any is None. A state whose
result is ``None`` is *undetermined*; a board that is notable today (strong / weak day or a
top 5-day performer) with an undetermined state is reported as ``insufficient_history``,
and the missing inputs are listed in ``evidence["data_gaps"]``.

State priority when several hold: abnormal_drop > ebbing > divergence > strengthening >
first_launch. Protection rules: broad-rally days drop the limit-up / breadth based
conditions (rank only); single-stock-driven rises are not reported; new concepts in their
cooling period never become ``first_launch``; boards with too few / too many members are
not judged (state ``none``).
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.repositories.board_analysis_repo import BoardAnalysisRepository
from src.services.board_common import CalendarFn, as_date, default_calendar_fn, session_axis
from src.services.board_metrics_service import CALC_VERSION
from src.services.board_thresholds import (
    overridden_keys,
    load_current_thresholds,
)

logger = logging.getLogger(__name__)

STATE_PRIORITY: Tuple[str, ...] = ("abnormal_drop", "ebbing", "divergence", "strengthening", "first_launch")
# Which board is shown for a merged group: higher first.
_STATE_DISPLAY_RANK = {
    "first_launch": 5, "strengthening": 4, "abnormal_drop": 3, "divergence": 3,
    "ebbing": 2, "insufficient_history": 1, "none": 0,
}
TAG_ORDER = ("mainline", "supported", "single_stock", "cooling", "merged")
MIN_AXIS_SESSIONS = 20

_OPS: Dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt,
}
_OP_TEXT = {">=": "≥", "<=": "≤", ">": ">", "<": "<"}


# ---------------------------------------------------------------------------
# three-valued logic
# ---------------------------------------------------------------------------
def t_and(*items: Optional[bool]) -> Optional[bool]:
    if any(x is False for x in items):
        return False
    if any(x is None for x in items):
        return None
    return True


def t_or(*items: Optional[bool]) -> Optional[bool]:
    if any(x is True for x in items):
        return True
    if any(x is None for x in items):
        return None
    return False


def t_not(x: Optional[bool]) -> Optional[bool]:
    return None if x is None else (not x)


def t_at_least(items: Sequence[Optional[bool]], k: int) -> Optional[bool]:
    trues = sum(1 for x in items if x is True)
    unknown = sum(1 for x in items if x is None)
    if trues >= k:
        return True
    if trues + unknown < k:
        return False
    return None


def _num(value: Any, digits: int = 2) -> str:
    if value is None:
        return "缺失"
    number = float(value)
    if number.is_integer() and digits <= 2 and abs(number) >= 10:
        return str(int(number))
    return f"{number:.{digits}f}"


def cond(
    key: str,
    label: str,
    actual: Optional[float],
    op: str,
    threshold: float,
    *,
    unit: str = "",
    digits: int = 2,
    gap: Optional[str] = None,
    met_override: Optional[bool] = None,
    force_unmet_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """One comparison. ``met`` is None when ``actual`` is None (data missing)."""
    if force_unmet_reason is not None:
        met: Optional[bool] = False
        text = f"{label}：{force_unmet_reason}"
    elif actual is None:
        met = None
        text = f"{label}：数据缺失"
    else:
        met = _OPS[op](actual, threshold)
        text = f"{label} {_num(actual, digits)}{unit} {_OP_TEXT[op]} {_num(threshold, digits)}{unit}"
    if met_override is not None:
        met = met_override
    return {
        "key": key, "label": label,
        "actual": None if actual is None else round(float(actual), 4),
        "op": op, "threshold": threshold, "met": met, "text": text,
        "gap": gap if met is None else None,
    }


def skipped(key: str, label: str, why: str) -> Dict[str, Any]:
    return {"key": key, "label": label, "met": None, "skipped": why, "text": f"{label}：{why}，不参与判定"}


@dataclass
class Eval:
    """Result of evaluating one state: tri-state value, the conditions, data gaps."""

    result: Optional[bool] = None
    conds: List[Dict[str, Any]] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    blocked_by: Optional[str] = None

    def counted(self) -> List[Dict[str, Any]]:
        return [c for c in self.conds if not c.get("skipped")]

    def finish_all(self) -> "Eval":
        self.result = t_and(*[c["met"] for c in self.counted()])
        self._collect_gaps()
        return self

    def _collect_gaps(self) -> None:
        for c in self.counted():
            if c["met"] is None:
                self.gaps.append(c.get("gap") or f"{c['label']}：数据缺失")


@dataclass
class _Ctx:
    board: Dict[str, Any]
    rows: List[Optional[Dict[str, Any]]]  # offset 0 = trade date, 1 = previous session, ...
    broad: List[Optional[bool]]
    th: Dict[str, Any]

    @property
    def is_concept(self) -> bool:
        return self.board["board_type"] == "concept"

    def v(self, i: int, column: str) -> Optional[float]:
        if i >= len(self.rows) or self.rows[i] is None:
            return None
        return self.rows[i].get(column)

    def has_row(self, i: int) -> bool:
        return i < len(self.rows) and self.rows[i] is not None

    def is_broad(self, i: int) -> bool:
        return bool(self.broad[i]) if i < len(self.broad) else False

    @property
    def floor(self) -> float:
        return float(self.th["excess_floor"][self.board["board_type"]])


# ---------------------------------------------------------------------------
# primitive day conditions (usable for any offset)
# ---------------------------------------------------------------------------
def strong_day(ctx: _Ctx, i: int) -> Optional[bool]:
    rank, excess = ctx.v(i, "rank_pct"), ctx.v(i, "excess_ret")
    c_rank = None if rank is None else rank >= 100.0 - ctx.th["rank_strong_top_pct"]
    c_excess = None if excess is None else excess >= ctx.floor
    return t_and(c_rank, c_excess)


def weak_day(ctx: _Ctx, i: int) -> Optional[bool]:
    rank = ctx.v(i, "rank_pct")
    return None if rank is None else rank <= ctx.th["rank_weak_bottom_pct"]


def outperform(ctx: _Ctx, i: int) -> Optional[bool]:
    excess = ctx.v(i, "excess_ret")
    return None if excess is None else excess > 0


def _sessions_gap(ctx: _Ctx, offsets: Sequence[int]) -> Optional[str]:
    missing = [i for i in offsets if not ctx.has_row(i)]
    if not missing:
        return None
    return f"缺少前 {', '.join(str(i) for i in missing)} 个交易日的板块指标" if missing != [0] else "缺少当日板块指标"


# ---------------------------------------------------------------------------
# state evaluators
# ---------------------------------------------------------------------------
def eval_strengthening(ctx: _Ctx, i: int = 0) -> Eval:
    th = ctx.th
    confirm = th["confirm"]
    days = int(confirm["days"])
    ev = Eval()
    window = list(range(i, i + days))

    outs = [outperform(ctx, j) for j in window]
    c1 = {
        "key": "outperform_days", "label": f"连续 {days} 日跑赢基准", "met": t_and(*outs),
        "actual": [ctx.v(j, "excess_ret") for j in window], "threshold": 0, "op": ">",
        "text": f"连续 {days} 日跑赢基准：" + ("满足" if t_and(*outs) is True else "不满足" if t_and(*outs) is False else "数据不足"),
        "gap": _sessions_gap(ctx, window) or "超额收益数据缺失",
    }
    strongs = [strong_day(ctx, j) for j in window]
    need = int(confirm["strong_days"])
    c2_met = t_at_least(strongs, need)
    c2 = {
        "key": "strong_days", "label": f"其中至少 {need} 天为强日", "met": c2_met,
        "actual": sum(1 for s in strongs if s is True), "threshold": need, "op": ">=",
        "text": f"其中强日 {sum(1 for s in strongs if s is True)} 天，要求 ≥ {need} 天"
                + ("" if c2_met is not None else "（数据不足）"),
        "gap": _sessions_gap(ctx, window) or "排名数据缺失",
    }
    rank5 = ctx.v(i, "rank_5d_pct")
    top = th["strengthening_rank_5d_top_pct"]
    c3 = cond("rank_5d", f"近 5 日超额排名前 {_num(top, 0)}%", None if rank5 is None else rank5, ">=", 100.0 - top,
              digits=1, gap="近 5 日超额排名缺失（历史不足 5 个交易日）")
    if rank5 is not None:
        c3["text"] = f"近 5 日超额排名前 {_num(100.0 - rank5, 0)}%（要求前 {_num(top, 0)}%）"
    s = [ctx.v(j, "amount_share") for j in (i, i + 1, i + 2)]
    fall01 = None if s[0] is None or s[1] is None else s[0] < s[1]
    fall12 = None if s[1] is None or s[2] is None else s[1] < s[2]
    c4_met = t_not(t_and(fall01, fall12))
    c4 = {
        "key": "share_not_falling", "label": "成交额占全市场比例未连续下降", "met": c4_met,
        "actual": [None if x is None else round(x * 100, 3) for x in s], "op": "not_falling", "threshold": None,
        "text": "成交额占比 " + " ← ".join("缺失" if x is None else f"{x * 100:.2f}%" for x in s) + "（今日 ← 前日 ← 前两日）",
        "gap": "成交额占比数据缺失",
    }
    ev.conds = [c1, c2, c3, c4]
    return ev.finish_all()


def eval_first_launch(ctx: _Ctx) -> Eval:
    th = ctx.th
    fl = th["first_launch"]
    ev = Eval()
    broad = ctx.is_broad(0)
    prior = int(fl["prior_days"])
    prior_strong = [strong_day(ctx, j) for j in range(1, prior + 1)]
    c_prior_met = t_not(t_or(*prior_strong))
    ev.conds.append({
        "key": "no_prior_strong_day", "label": f"前 {prior} 日没有强日", "met": c_prior_met,
        "actual": sum(1 for s in prior_strong if s is True), "threshold": 0, "op": "==",
        "text": f"前 {prior} 日强日 {sum(1 for s in prior_strong if s is True)} 天"
                + ("" if c_prior_met is not None else "（部分交易日数据缺失）"),
        "gap": _sessions_gap(ctx, range(1, prior + 1)) or "前期强日判定所需数据缺失",
    })
    rank5_prev = ctx.v(1, "rank_5d_pct")
    not_top = fl["not_top_pct"]
    ev.conds.append(cond(
        "prev_rank_5d", f"此前 5 日超额不在前 {_num(not_top, 0)}%", rank5_prev, "<", 100.0 - not_top, digits=1,
        gap="前一交易日的 5 日排名缺失（历史不足）",
    ))
    if rank5_prev is not None:
        ev.conds[-1]["text"] = f"此前 5 日超额排名前 {_num(100.0 - rank5_prev, 0)}%（要求不在前 {_num(not_top, 0)}%）"
    strong = strong_day(ctx, 0)
    rank, excess = ctx.v(0, "rank_pct"), ctx.v(0, "excess_ret")
    ev.conds.append({
        "key": "strong_day", "label": "当日为强日", "met": strong,
        "actual": {"rank_pct": rank, "excess_ret": excess},
        "threshold": {"rank_pct": 100.0 - th["rank_strong_top_pct"], "excess_ret": ctx.floor}, "op": ">=",
        "text": ("当日排名前 %s%%、超额 %s%%（要求前 %s%%、≥ %s%%）" % (
            _num(None if rank is None else 100.0 - rank, 0), _num(excess), _num(th["rank_strong_top_pct"], 0), _num(ctx.floor)))
        if strong is not None else "当日排名或超额缺失",
        "gap": "当日排名或超额缺失",
    })
    ev.conds.append(cond(
        "amount_ratio", "成交额 / 近 5 日均值", ctx.v(0, "amount_ratio_5d"), ">=", th["amount_ratio_mild"],
        unit=" 倍", gap="近 5 日成交额均值缺失（历史不足 5 个交易日）",
    ))
    if broad:
        ev.conds.append(skipped("breadth", "上涨家数占比", "普涨日保护"))
    else:
        ev.conds.append(cond("breadth", "上涨家数占比", ctx.v(0, "up_ratio"), ">=", fl["breadth_pct"], unit="%", digits=1))
    if ctx.is_concept:
        if broad:
            ev.conds.append(skipped("limit_up_effect", "板块内涨停家数", "普涨日保护"))
        else:
            ev.conds.append(cond("limit_up_effect", "板块内涨停家数", ctx.v(0, "limit_up_count"), ">=",
                                 th["limit_up_effect"], unit=" 家", digits=0))
    ev.conds.append(_single_stock_cond(ctx, expect_single=False))
    return ev.finish_all()


def _single_stock_cond(ctx: _Ctx, expect_single: bool) -> Dict[str, Any]:
    """``met`` = the board is NOT single-stock driven (so it can be AND-ed into a rise state)."""
    share = ctx.v(0, "top_contrib_share")
    limit = ctx.th["single_stock_share_pct"]
    ret = ctx.v(0, "ret_cap")
    if share is None:
        if ret is not None and ret <= 0:
            return {"key": "not_single_stock", "label": "非单股驱动", "met": True, "actual": None,
                    "threshold": limit, "op": "<=", "text": "板块当日未上涨，单股驱动不适用", "gap": None}
        return {"key": "not_single_stock", "label": "非单股驱动", "met": None, "actual": None,
                "threshold": limit, "op": "<=", "text": "非单股驱动：贡献占比缺失", "gap": "个股贡献占比缺失"}
    met = share <= limit
    return {"key": "not_single_stock", "label": "非单股驱动", "met": met, "actual": round(share, 2),
            "threshold": limit, "op": "<=", "text": f"最大单股贡献 {share:.1f}% ≤ {_num(limit, 0)}%", "gap": None}


def eval_divergence(ctx: _Ctx, i: int = 0) -> Eval:
    th = ctx.th
    dv = th["divergence"]
    ev = Eval()
    broad = ctx.is_broad(i)
    st_prev = eval_strengthening(ctx, i + 1)
    rank5 = ctx.v(i, "rank_5d_pct")
    top = th["strengthening_rank_5d_top_pct"]
    c_rank5_met = None if rank5 is None else rank5 >= 100.0 - top
    a_met = t_or(st_prev.result, c_rank5_met)
    ev.conds.append({
        "key": "was_strong", "label": "此前处于持续走强，或近 5 日超额在前列", "met": a_met,
        "actual": {"prev_strengthening": st_prev.result, "rank_5d_pct": rank5},
        "threshold": {"rank_5d_pct": 100.0 - top}, "op": "or",
        "text": "前一日持续走强：%s；近 5 日排名 %s" % (
            {True: "是", False: "否", None: "数据不足"}[st_prev.result],
            "缺失" if rank5 is None else f"前 {_num(100.0 - rank5, 0)}%"),
        "gap": "前一日持续走强与近 5 日排名均无法判断（历史不足）",
    })

    excess = ctx.v(i, "excess_ret")
    ratio = ctx.v(i, "amount_ratio_5d")
    parts: List[Dict[str, Any]] = []
    # b1: 超额 <= 0 或 上涨占比 <= 50%
    b1_excess = None if excess is None else excess <= 0
    if broad:
        b1_breadth = None
        b1_text = f"超额 {_num(excess)}% ≤ 0（上涨占比条件：普涨日保护，不参与）"
        b1_met = b1_excess
    else:
        up_ratio = ctx.v(i, "up_ratio")
        b1_breadth = None if up_ratio is None else up_ratio <= dv["breadth_pct"]
        b1_met = t_or(b1_excess, b1_breadth)
        b1_text = f"超额 {_num(excess)}% ≤ 0，或上涨占比 {_num(up_ratio, 1)}% ≤ {_num(dv['breadth_pct'], 0)}%"
    parts.append({"key": "excess_or_breadth", "label": "超额 ≤ 0 或上涨占比走弱", "met": b1_met, "text": b1_text,
                  "actual": {"excess_ret": excess}, "threshold": 0, "op": "<=", "gap": "超额或上涨占比缺失"})
    # b2: 放量滞涨
    b2_vol = None if ratio is None else ratio >= th["amount_ratio_mild"]
    b2_met = t_and(b2_vol, b1_excess)
    parts.append({
        "key": "volume_stall", "label": "放量但超额 ≤ 0（放量滞涨）", "met": b2_met,
        "actual": {"amount_ratio": ratio, "excess_ret": excess}, "threshold": th["amount_ratio_mild"], "op": ">=",
        "text": f"成交额 {_num(ratio)} 倍（≥ {_num(th['amount_ratio_mild'])} 倍为放量），超额 {_num(excess)}%",
        "gap": "成交额放大倍数或超额缺失",
    })
    if ctx.is_concept:
        if broad:
            parts.append(skipped("break_rate", "炸板率", "普涨日保护"))
            parts.append(skipped("limit_up_drop", "涨停数较前日减少", "普涨日保护"))
        else:
            touched = ctx.v(i, "touched_up_count")
            br = ctx.v(i, "break_rate")
            if br is None and touched is not None and touched < th["break_min_touched"]:
                parts.append(cond("break_rate", "炸板率", None, ">=", th["break_rate_warn"], unit="%", digits=1,
                                  force_unmet_reason=f"触及涨停 {int(touched)} 家，不足 {th['break_min_touched']} 家不计算"))
            else:
                parts.append(cond("break_rate", "炸板率", br, ">=", th["break_rate_warn"], unit="%", digits=1))
            l0, l1 = ctx.v(i, "limit_up_count"), ctx.v(i + 1, "limit_up_count")
            if l0 is None or l1 is None:
                drop_met: Optional[bool] = None
            elif l1 <= 0:
                drop_met = False
            else:
                drop_met = (l1 - l0) / l1 * 100.0 >= dv["limit_up_drop_pct"]
            parts.append({
                "key": "limit_up_drop", "label": f"涨停数较前日减少 ≥ {_num(dv['limit_up_drop_pct'], 0)}%", "met": drop_met,
                "actual": {"today": l0, "previous": l1}, "threshold": dv["limit_up_drop_pct"], "op": ">=",
                "text": f"涨停 {_num(l1, 0)} 家 → {_num(l0, 0)} 家", "gap": "当日或前一日涨停家数缺失",
            })
    need = int(dv["min_conditions"])
    counted = [p for p in parts if not p.get("skipped")]
    b_met = t_at_least([p["met"] for p in counted], need)
    ev.conds.extend(parts)
    ev.conds.append({
        "key": "weakness_conditions", "label": f"满足 ≥ {need} 条走弱条件", "met": b_met,
        "actual": sum(1 for p in counted if p["met"] is True), "threshold": need, "op": ">=",
        "text": f"走弱条件满足 {sum(1 for p in counted if p['met'] is True)} 条，要求 ≥ {need} 条", "gap": "走弱条件数据不足",
    })
    ev.result = t_and(a_met, b_met)
    # only the roll-up conditions decide; sub-conditions are explanatory
    for c in (ev.conds[0], ev.conds[-1]):
        if c["met"] is None:
            ev.gaps.append(c.get("gap") or f"{c['label']}：数据缺失")
    return ev


def _ebb_day(ctx: _Ctx, i: int) -> Tuple[Optional[bool], List[Dict[str, Any]]]:
    th = ctx.th
    eb = th["ebb"]
    broad = ctx.is_broad(i)
    parts: List[Dict[str, Any]] = []
    parts.append(cond("excess_negative", "超额 < 0", ctx.v(i, "excess_ret"), "<", 0.0, unit="%"))
    if ctx.is_concept:
        if broad:
            parts.append(skipped("limit_up_low", "涨停家数", "普涨日保护"))
        else:
            parts.append(cond("limit_up_low", "涨停家数", ctx.v(i, "limit_up_count"), "<=", eb["limit_up_max"],
                              unit=" 家", digits=0))
    amount, peak = ctx.v(i, "amount"), ctx.v(i, "amount_peak_20d")
    if amount is None or peak is None or peak <= 0:
        parts.append(cond("below_peak", "成交额 / 近期峰值", None, "<", th["ebb_peak_pct"], gap="成交额峰值缺失（历史不足）"))
    else:
        parts.append(cond("below_peak", "成交额 / 近期峰值", amount / peak * 100.0, "<", th["ebb_peak_pct"], unit="%", digits=0))
    if broad:
        parts.append(skipped("breadth_weak", "上涨家数占比", "普涨日保护"))
    else:
        parts.append(cond("breadth_weak", "上涨家数占比", ctx.v(i, "up_ratio"), "<=", th["breadth_pct"]["weak"], unit="%", digits=1))
    out = eb["rank_5d_out_pct"]
    parts.append(cond("rank_5d_out", f"近 5 日排名跌出前 {_num(out, 0)}%", ctx.v(i, "rank_5d_pct"), "<", 100.0 - out, digits=1,
                      gap="近 5 日排名缺失（历史不足）"))
    counted = [p for p in parts if not p.get("skipped")]
    met = t_at_least([p["met"] for p in counted], int(eb["min_conditions"]))
    return met, parts


def eval_ebbing(ctx: _Ctx) -> Eval:
    th = ctx.th
    eb = th["ebb"]
    ev = Eval()
    lookback = int(eb["lookback_days"])
    prior = [t_or(eval_strengthening(ctx, j).result, eval_divergence(ctx, j).result) for j in range(1, lookback + 1)]
    e0 = t_or(*prior)
    ev.conds.append({
        "key": "recently_strong", "label": f"近 {lookback} 日出现过持续走强或高位分歧", "met": e0,
        "actual": sum(1 for p in prior if p is True), "threshold": 1, "op": ">=",
        "text": f"近 {lookback} 日走强 / 分歧 {sum(1 for p in prior if p is True)} 天"
                + ("" if e0 is not None else "（历史数据不足，无法确认）"),
        "gap": f"近 {lookback} 日的走强 / 分歧状态无法判定（历史不足）",
    })
    days = int(eb["consecutive_days"])
    day_results: List[Optional[bool]] = []
    for i in range(days):
        met, parts = _ebb_day(ctx, i)
        day_results.append(met)
        counted = [p for p in parts if not p.get("skipped")]
        label = "当日" if i == 0 else f"前 {i} 日"
        ev.conds.append({
            "key": f"decay_day_{i}", "label": f"{label}满足 ≥ {int(eb['min_conditions'])} 条退潮条件", "met": met,
            "actual": sum(1 for p in counted if p["met"] is True), "threshold": int(eb["min_conditions"]), "op": ">=",
            "text": f"{label}：" + ("；".join(p["text"] for p in parts if p["met"] is True or p.get("skipped")) or "无满足项"),
            "gap": f"{label}退潮条件数据不足", "children": parts,
        })
    ev.result = t_and(e0, *day_results)
    ev._collect_gaps()
    return ev


def eval_abnormal_drop(ctx: _Ctx) -> Eval:
    th = ctx.th
    ev = Eval()
    broad = ctx.is_broad(0)
    rank = ctx.v(0, "rank_pct")
    weak_top = th["rank_weak_bottom_pct"]
    ev.conds.append(cond("weak_day", f"当日超额排名后 {_num(weak_top, 0)}%", rank, "<=", weak_top, digits=1))
    if rank is not None:
        ev.conds[-1]["text"] = f"当日超额排名后 {_num(rank, 0)}%（要求后 {_num(weak_top, 0)}%）"
    ev.conds.append(cond("excess_floor", "当日超额", ctx.v(0, "excess_ret"), "<=", -ctx.floor, unit="%"))
    ev.conds.append(cond("amount_ratio", "成交额 / 近 5 日均值", ctx.v(0, "amount_ratio_5d"), ">=", th["amount_ratio_mild"],
                         unit=" 倍", gap="近 5 日成交额均值缺失（历史不足 5 个交易日）"))
    if broad:
        ev.conds.append(skipped("breadth_weak", "上涨家数占比", "普涨日保护"))
    else:
        ev.conds.append(cond("breadth_weak", "上涨家数占比", ctx.v(0, "up_ratio"), "<=", th["breadth_pct"]["weak"], unit="%", digits=1))
    if ctx.is_concept:
        ad = th["abnormal_drop"]
        c_ld = cond("limit_down", "跌停家数", ctx.v(0, "limit_down_count"), ">=", ad["limit_down_count"], unit=" 家", digits=0)
        c_d7 = cond("drop7", "跌幅超 7% 的个股占比", ctx.v(0, "drop7_ratio"), ">=", ad["drop7_ratio_pct"], unit="%", digits=1)
        met = t_or(c_ld["met"], c_d7["met"])
        ev.conds.append({
            "key": "limit_down_or_drop7", "label": "跌停集中或深跌个股占比高", "met": met,
            "actual": {"limit_down_count": ctx.v(0, "limit_down_count"), "drop7_ratio": ctx.v(0, "drop7_ratio")},
            "threshold": dict(ad), "op": "or", "text": f"{c_ld['text']}；{c_d7['text']}",
            "gap": "跌停家数与深跌占比均缺失", "children": [c_ld, c_d7],
        })
    return ev.finish_all()


def eval_mainline(ctx: _Ctx) -> Optional[bool]:
    ml = ctx.th["mainline"]
    if not ctx.is_concept or ctx.is_broad(0):
        return None
    days, min_days = int(ml["days"]), int(ml["min_days"])
    limit_ok = []
    for j in range(days):
        value = ctx.v(j, "limit_up_count")
        limit_ok.append(None if value is None else value >= ml["limit_up"])
    streak = ctx.v(0, "max_consecutive")
    c_streak = None if streak is None else streak >= ml["max_consecutive"]
    sustained = [outperform(ctx, j) for j in range(min_days)]
    return t_and(*limit_ok, c_streak, *sustained)


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
@dataclass
class SignalBatch:
    trade_date: date
    signals: List[Dict[str, Any]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)
    history_sessions: int = 0
    broad_rally: Optional[bool] = None
    evaluated: int = 0
    not_evaluated: Dict[str, int] = field(default_factory=dict)
    core_missing: int = 0
    warnings: List[str] = field(default_factory=list)
    requirements: Dict[str, int] = field(default_factory=dict)
    backfill_membership: bool = False

    def summary(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "signals": len(self.signals),
            "counts": dict(self.counts),
            "history_sessions": self.history_sessions,
            "broad_rally": self.broad_rally,
            "evaluated": self.evaluated,
            "not_evaluated": dict(self.not_evaluated),
            "core_missing": self.core_missing,
            "requirements": dict(self.requirements),
            "backfill_membership": self.backfill_membership,
            "warnings": list(self.warnings),
        }


class BoardSignalService:
    def __init__(
        self,
        repo: Optional[BoardAnalysisRepository] = None,
        thresholds: Optional[Dict[str, Any]] = None,
        calendar_fn: Optional[CalendarFn] = default_calendar_fn,
    ) -> None:
        self._repo = repo
        self._thresholds = thresholds
        self._calendar_fn = calendar_fn
        self.threshold_warnings: List[str] = []

    @property
    def repo(self) -> BoardAnalysisRepository:
        if self._repo is None:
            self._repo = BoardAnalysisRepository()
        return self._repo

    @property
    def thresholds(self) -> Dict[str, Any]:
        if self._thresholds is None:
            self._thresholds, self.threshold_warnings = load_current_thresholds()
        return self._thresholds

    def thresholds_snapshot(self) -> Dict[str, Any]:
        th = self.thresholds
        return {
            "values": th,
            "overridden": sorted(overridden_keys(th)),
            "warnings": list(self.threshold_warnings),
            "calc_version": CALC_VERSION,
        }

    # ------------------------------------------------------------------
    def required_sessions(self) -> Dict[str, int]:
        th = self.thresholds
        return {
            "strengthening": max(5, int(th["confirm"]["days"])),
            "first_launch": max(5, int(th["first_launch"]["prior_days"])) + 1,
            "divergence": 6,
            "abnormal_drop": 6,
            "ebbing": int(th["ebb"]["lookback_days"]) + 6,
        }

    def evaluate_trade_date(self, trade_date: Any) -> SignalBatch:
        """Compute signals for ``trade_date`` from stored facts (no writes)."""
        day = as_date(trade_date)
        th = self.thresholds
        batch = SignalBatch(trade_date=day, warnings=list(self.threshold_warnings))
        batch.requirements = self.required_sessions()

        n_axis = max(MIN_AXIS_SESSIONS, int(th["ebb"]["lookback_days"]) + 8)
        ingested = self.repo.list_ingested_dates(upto=day, limit=n_axis * 2)
        axis = session_axis(day, n_axis, ingested, self._calendar_fn)
        if not axis or axis[0] != day:
            axis = [day] + [d for d in axis if d < day]

        market = self.repo.get_market_daily_range(axis)
        broad: List[Optional[bool]] = []
        for d in axis:
            ret = (market.get(d) or {}).get("all_a_ret")
            broad.append(None if ret is None else ret >= th["broad_rally_pct"])
        batch.broad_rally = broad[0]
        if broad[0] is None:
            batch.warnings.append("全 A 涨幅缺失，无法判断普涨日保护，按非普涨日处理")

        board_rows = self.repo.get_board_daily_range(axis)
        catalog = [b for b in self.repo.list_catalog(("industry", "concept")) if (day, b["id"]) in board_rows]
        batch.history_sessions = len({d for (d, _bid), r in board_rows.items() if r.get("calc_version")})

        signals: Dict[int, Dict[str, Any]] = {}
        for board in catalog:
            today = board_rows[(day, board["id"])]
            rows: List[Optional[Dict[str, Any]]] = []
            for d in axis:
                r = board_rows.get((d, board["id"]))
                usable = r is not None and r.get("calc_version") and r.get("excluded_reason") not in ("broad", "too_few")
                rows.append(r if usable else None)
            signals[board["id"]] = self._judge_board(board, today, rows, broad, batch)

        self._apply_merge(catalog, signals)
        for board in catalog:
            sig = signals[board["id"]]
            sig["flags"]["tags"] = [t for t in TAG_ORDER if t in sig["flags"]["tags"]]
            batch.signals.append({"board_id": board["id"], **sig})
            batch.counts[sig["signal_type"]] = batch.counts.get(sig["signal_type"], 0) + 1
        return batch

    # ------------------------------------------------------------------
    def _judge_board(
        self,
        board: Dict[str, Any],
        today: Dict[str, Any],
        rows: List[Optional[Dict[str, Any]]],
        broad: List[Optional[bool]],
        batch: SignalBatch,
    ) -> Dict[str, Any]:
        th = self.thresholds
        quality = _loads(today.get("quality"))
        excluded = today.get("excluded_reason")
        base_evidence: Dict[str, Any] = {
            "trade_date": batch.trade_date.isoformat(),
            "calc_version": today.get("calc_version"),
            "broad_rally": broad[0],
            "board": {
                "ref": board["ref"], "excluded_reason": excluded,
                "member_count": today.get("member_count"), "valid_member_count": today.get("valid_member_count"),
                "age_days": quality.get("age_days"), "age_known": quality.get("age_known"),
                "member_origin": quality.get("member_origin"),
            },
            "data_gaps": [],
        }
        flags: Dict[str, Any] = {"tags": []}

        if excluded in ("broad", "too_few"):
            if "no_membership" in (quality.get("flags") or []):
                key, why = "no_membership", "暂无成分数据，未参与判定"
            elif excluded == "broad":
                key, why = "broad", f"成分股超过 {th['broad_max_members']} 只，视为宽基，不参与判定"
            else:
                key, why = "too_few", f"有效成分股不足 {th['min_members']} 只，不参与判定"
            batch.not_evaluated[key] = batch.not_evaluated.get(key, 0) + 1
            flags["excluded"] = key
            base_evidence["lines"] = [why]
            return self._signal("none", flags, base_evidence, why, "unknown", None)
        batch.evaluated += 1

        # today's row is usable even when the board is only in its cooling period
        ctx = _Ctx(board=board, rows=[today] + rows[1:], broad=broad, th=th)

        core = {"ret_cap": today.get("ret_cap"), "excess_ret": today.get("excess_ret"), "rank_pct": today.get("rank_pct")}
        inputs = _inputs(today)
        base_evidence["inputs"] = inputs
        if any(v is None for v in core.values()):
            gaps = _core_gaps(today, quality)
            batch.core_missing += 1
            flags["core_missing"] = True
            base_evidence["data_gaps"] = gaps
            reason = "当日关键数据缺失，无法判定：" + "；".join(gaps)
            base_evidence["lines"] = [reason]
            return self._signal("insufficient_history", flags, base_evidence, reason, "unknown", None)

        evals: Dict[str, Eval] = {
            "abnormal_drop": eval_abnormal_drop(ctx),
            "ebbing": eval_ebbing(ctx),
            "divergence": eval_divergence(ctx, 0),
            "strengthening": eval_strengthening(ctx, 0),
            "first_launch": eval_first_launch(ctx),
        }
        # strengthening also must not be single-stock driven
        single_c = _single_stock_cond(ctx, expect_single=False)
        st = evals["strengthening"]
        st.conds.append(single_c)
        st.result = t_and(st.result, single_c["met"])
        if single_c["met"] is None:
            st.gaps.append(single_c["gap"])

        cooling = today.get("excluded_reason") == "cooling"
        single_stock = single_c["met"] is False

        def _ignoring_single(ev: Eval) -> Optional[bool]:
            """Would the rise state hold if single-stock driving were ignored?"""
            return t_and(*[c["met"] for c in ev.counted() if c["key"] != "not_single_stock"])

        chosen: Optional[str] = None
        undetermined: List[str] = []
        blocked_by: Optional[str] = None
        for state in STATE_PRIORITY:
            ev = evals[state]
            result = ev.result
            if state == "first_launch" and cooling and _ignoring_single(ev) is True:
                blocked_by = "cooling"
                continue
            if result is True:
                chosen = state
                break
            if result is None:
                undetermined.append(state)
            elif state in ("first_launch", "strengthening") and single_stock and _ignoring_single(ev) is True:
                blocked_by = blocked_by or "single_stock"
        notable = self._notable(ctx)
        tags: List[str] = []
        if cooling and (notable or blocked_by == "cooling"):
            tags.append("cooling")
        if blocked_by == "single_stock":
            tags.append("single_stock")

        gaps = _dedupe(g for name in undetermined for g in evals[name].gaps)
        if chosen is None:
            if blocked_by == "cooling":
                state = "insufficient_history"
                age = quality.get("age_days")
                reason = f"新概念上线第 {age} 个交易日，冷却期 {th['cooling_days']} 日内只记录，不判「首次启动」"
            elif blocked_by == "single_stock":
                state = "none"
                reason = f"当日涨幅主要来自单只股票（贡献 {_num(today.get('top_contrib_share'), 0)}%），不按板块异动处理"
            elif undetermined and notable:
                state = "insufficient_history"
                reason = ("历史数据不足，无法判定：" + "；".join(gaps[:3])) if gaps else "历史数据不足，无法判定"
            else:
                state = "none"
                reason = ""
        else:
            state = chosen
            reason = _reason(state, ctx, evals[state])
            if state == "strengthening" and eval_mainline(ctx) is True:
                tags.append("mainline")
            if state == "divergence" and ctx.is_concept and not ctx.is_broad(0):
                amount0, amount1 = ctx.v(0, "amount"), ctx.v(1, "amount")
                limit0 = ctx.v(0, "limit_up_count")
                if amount0 is not None and amount1 is not None and limit0 is not None and amount0 >= amount1 and limit0 >= 1:
                    tags.append("supported")

        driver_kind, driver_text = _driver(state, today, th)
        evidence = dict(base_evidence)
        evidence.update(
            {
                "state": state,
                "evaluations": {
                    name: {
                        "result": ev.result,
                        "conditions": [_compact(c) for c in ev.conds],
                        **({"blocked_by": blocked_by} if blocked_by and name in ("first_launch", "strengthening") else {}),
                    }
                    for name, ev in evals.items()
                },
                "undetermined": undetermined,
                "blocked_by": blocked_by,
                "data_gaps": gaps,
                "lines": _evidence_lines(state, evals.get(state), reason, blocked_by),
                "thresholds_used": _thresholds_used(state, th),
                "driver": {"kind": driver_kind, "text": driver_text},
            }
        )
        flags["tags"] = tags
        flags["notable"] = notable
        if blocked_by:
            flags["blocked_by"] = blocked_by
        return self._signal(state, flags, evidence, reason, driver_kind, today)

    @staticmethod
    def _signal(
        state: str, flags: Dict[str, Any], evidence: Dict[str, Any], reason: str, driver: str, today: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        return {"signal_type": state, "flags": flags, "evidence": evidence, "driver": driver, "reason": reason}

    def _notable(self, ctx: _Ctx) -> bool:
        top5 = ctx.v(0, "rank_5d_pct")
        in_top5 = top5 is not None and top5 >= 100.0 - ctx.th["strengthening_rank_5d_top_pct"]
        return strong_day(ctx, 0) is True or weak_day(ctx, 0) is True or in_top5

    # ------------------------------------------------------------------
    def _apply_merge(self, catalog: List[Dict[str, Any]], signals: Dict[int, Dict[str, Any]]) -> None:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for board in catalog:
            if board.get("merge_group"):
                groups.setdefault(board["merge_group"], []).append(board)
        for name, members in groups.items():
            if len(members) < 2:
                continue

            def rank(b: Dict[str, Any]) -> Tuple[int, float, int]:
                sig = signals[b["id"]]
                excess = sig["evidence"].get("inputs", {}).get("excess_ret")
                return (_STATE_DISPLAY_RANK.get(sig["signal_type"], 0), excess if excess is not None else -1e9,
                        -b["id"])

            rep = max(members, key=rank)
            refs = [b["ref"] for b in members]
            for b in members:
                sig = signals[b["id"]]
                role = "representative" if b["id"] == rep["id"] else "member"
                sig["flags"]["merge"] = {"role": role, "group": name, "representative": rep["ref"], "members": refs}
                sig["evidence"]["merge"] = sig["flags"]["merge"]
                if role == "representative" and sig["signal_type"] != "none" and "merged" not in sig["flags"]["tags"]:
                    sig["flags"]["tags"].append("merged")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _loads(raw: Any) -> Dict[str, Any]:
    import json

    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dedupe(items: Any) -> List[str]:
    seen: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen


def _inputs(row: Dict[str, Any]) -> Dict[str, Any]:
    columns = (
        "ret_cap", "ret_eq", "excess_ret", "excess_vs_median", "rank_pct", "ret_5d", "excess_5d", "rank_5d_pct",
        "amount", "amount_ratio_5d", "amount_share", "up_ratio", "limit_up_count", "limit_down_count",
        "touched_up_count", "max_consecutive", "break_rate", "drop7_ratio", "top_contrib_code", "top_contrib_share",
    )
    out: Dict[str, Any] = {}
    for column in columns:
        value = row.get(column)
        out[column] = round(value, 4) if isinstance(value, float) else value
    return out


def _core_gaps(row: Dict[str, Any], quality: Dict[str, Any]) -> List[str]:
    gaps: List[str] = []
    flags = quality.get("flags") or []
    if row.get("ret_cap") is None:
        gaps.append("流通市值缺失，无法计算加权涨幅" if any(f.startswith("float_mv") for f in flags) else "板块涨幅无法计算")
    if row.get("excess_ret") is None and row.get("ret_cap") is not None:
        gaps.append("基准指数涨幅缺失，无法计算超额")
    if row.get("rank_pct") is None and row.get("excess_ret") is not None:
        gaps.append("同类板块数量不足，无法计算排名")
    return gaps or ["关键指标缺失"]


def _compact(c: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: c.get(k) for k in ("key", "label", "actual", "op", "threshold", "met", "text") if k in c}
    if c.get("skipped"):
        out["skipped"] = c["skipped"]
    if c.get("children"):
        out["children"] = [_compact(x) for x in c["children"]]
    return out


_STATE_THRESHOLD_KEYS = {
    "first_launch": ("rank_strong_top_pct", "excess_floor", "amount_ratio_mild", "first_launch", "limit_up_effect",
                     "single_stock_share_pct", "broad_rally_pct", "cooling_days"),
    "strengthening": ("rank_strong_top_pct", "excess_floor", "confirm", "strengthening_rank_5d_top_pct",
                      "single_stock_share_pct", "mainline", "broad_rally_pct"),
    "divergence": ("divergence", "amount_ratio_mild", "break_rate_warn", "break_min_touched",
                   "strengthening_rank_5d_top_pct", "broad_rally_pct"),
    "ebbing": ("ebb", "ebb_peak_pct", "breadth_pct", "broad_rally_pct"),
    "abnormal_drop": ("rank_weak_bottom_pct", "excess_floor", "amount_ratio_mild", "breadth_pct", "abnormal_drop",
                      "broad_rally_pct"),
}


def _thresholds_used(state: str, th: Dict[str, Any]) -> Dict[str, Any]:
    """Thresholds behind the verdict; a board without a state lists every rule it was tested against."""
    keys = _STATE_THRESHOLD_KEYS.get(state)
    if keys is None:
        keys = tuple(dict.fromkeys(k for ks in _STATE_THRESHOLD_KEYS.values() for k in ks))
    return {k: th[k] for k in keys}


def _evidence_lines(state: str, ev: Optional[Eval], reason: str, blocked_by: Optional[str]) -> List[str]:
    if ev is None or state in ("none", "insufficient_history"):
        return [reason] if reason else []
    lines: List[str] = []
    for c in ev.conds:
        if c.get("skipped"):
            lines.append(c["text"])
        elif c["met"] is True:
            lines.append(f"满足：{c['text']}")
    return lines


def _driver(state: str, row: Dict[str, Any], th: Dict[str, Any]) -> Tuple[str, str]:
    """Best effort from the closing data only (no news / earnings feed is wired in)."""
    ratio = row.get("amount_ratio_5d")
    if state in ("first_launch", "strengthening") and ratio is not None and ratio >= th["amount_ratio_strong"]:
        return "capital", f"资金推动：成交额放大至 {ratio:.1f} 倍；消息面与业绩未核实"
    return "unknown", "原因未明：未接入消息 / 业绩数据，仅按行情判断"


def _reason(state: str, ctx: _Ctx, ev: Eval) -> str:
    ex = ctx.v(0, "excess_ret")
    ratio = ctx.v(0, "amount_ratio_5d")
    up = ctx.v(0, "up_ratio")
    if state == "first_launch":
        text = f"放量启动：成交额 {_num(ratio, 1)} 倍于近 5 日均值，上涨家数占比 {_num(up, 0)}%，当日超额 {_signed(ex)}%"
        lim = ctx.v(0, "limit_up_count")
        if ctx.is_concept and lim is not None and not ctx.is_broad(0):
            text += f"，涨停 {int(lim)} 家"
        return text
    if state == "strengthening":
        days = int(ctx.th["confirm"]["days"])
        rank5 = ctx.v(0, "rank_5d_pct")
        return f"连续 {days} 日跑赢基准，近 5 日超额排在同类板块前 {_num(100.0 - rank5, 0) if rank5 is not None else '?'}%"
    if state == "divergence":
        fired = [c["label"] for c in ev.conds if c["key"] in ("excess_or_breadth", "volume_stall", "break_rate", "limit_up_drop")
                 and c.get("met") is True]
        return "高位分歧：" + "、".join(fired) if fired else "高位分歧"
    if state == "ebbing":
        return f"退潮：近期走强后连续 {int(ctx.th['ebb']['consecutive_days'])} 日满足退潮条件"
    if state == "abnormal_drop":
        return f"放量下跌：成交额 {_num(ratio, 1)} 倍，上涨家数占比 {_num(up, 0)}%，当日超额 {_signed(ex)}%"
    return ""


def _signed(value: Optional[float]) -> str:
    return "缺失" if value is None else f"{value:+.2f}"
