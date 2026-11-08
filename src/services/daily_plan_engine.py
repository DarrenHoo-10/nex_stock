# -*- coding: utf-8 -*-
"""Per-stock next-day plan: rule-based levels and verdicts, the model's judgement on top, and the switch decision.

Pure computation on already-loaded data (no database, no network) so it can run in worker threads:

1. ``compute_levels`` derives support / pressure / take-profit for the short (10-session) and swing (30-session)
   horizons from the local daily bars (moving averages, window highs / lows, average daily range),
2. ``rule_draft`` turns them into a deterministic verdict and price levels for each horizon,
3. the analysis model (optional, ``llm`` callable) may override the verdict and the price levels; its numbers are
   validated against the next day's price limits and discarded when implausible. It never provides shares: shares
   come from ``daily_plan_sizing`` (spec 5.3),
4. ``render_horizon`` writes the conditional plan rows, sizing and stop for each horizon; hard rules win over the
   model (close below the stop -> clear; max holdings -> no new buy; T+1),
5. ``build_switch`` judges 「换股」 for a held stock when the holdings are full (spec 5.4).

Model failure or an unusable reply never fails a plan: the rule draft is rendered instead and the plan says so in
``data_gaps``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from statistics import mean
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.services.daily_plan_sizing import (
    BOARD_BSE,
    BOARD_CHINEXT,
    BOARD_MAIN,
    BOARD_STAR,
    PositionRules,
    compute_sizing,
    compute_stop,
    fmt_num,
    fmt_price,
    limit_pct,
    limit_prices,
    round_price,
)

logger = logging.getLogger(__name__)

SHORT_WINDOW = 10  # sessions
SWING_WINDOW = 30
RANGE_WINDOW = 10  # sessions for the average daily range
MIN_BARS = 5  # fewer local sessions than this -> the plan fails with a reason
RECOMMENDED_BARS = 20
ZONE_RANGE_FRACTION = 0.25  # half width of the buy zone, in average daily ranges
NEAR_SUPPORT_RANGES = 1.5  # a close within this many ranges above support counts as 「贴近支撑」
TAKE_PROFIT_RANGE_MULT = {"short": 2.0, "swing": 5.0}
OHLC_MIN_COVERAGE = 0.6
PROMPT_BARS = 15
STRONG_BOARD_STATES = ("first_launch", "strengthening")
WEAK_BOARD_STATES = ("ebbing", "abnormal_drop")
BOARD_STATE_TEXT = {
    "first_launch": "首次启动", "strengthening": "走强", "divergence": "分歧", "ebbing": "退潮",
    "abnormal_drop": "异常下跌",
}
_BOARD_STATE_ORDER = {"first_launch": 0, "strengthening": 1, "divergence": 2, "abnormal_drop": 3, "ebbing": 4}

FLAT_VERDICTS = ("buy", "wait", "skip")
HELD_VERDICTS = ("hold", "add", "reduce", "clear")
HORIZONS = ("short", "swing")
HORIZON_LABEL = {"short": "短线", "swing": "波段"}
BOARD_LABEL = {BOARD_MAIN: "主板", BOARD_CHINEXT: "创业板", BOARD_STAR: "科创板", BOARD_BSE: "北交所"}

# (prompt, system_prompt) -> reply text; raises on failure
LlmFn = Callable[[str, str], str]

SYSTEM_PROMPT = (
    "你是 A 股收盘后的交易计划助手，只根据给出的数据判断，不编造数据，也不要给出买卖股数或仓位"
    "（股数由系统按规则计算）。只输出 JSON，不要输出其他文字。"
)


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
@dataclass
class Bar:
    date: date
    close: float
    pct_chg: Optional[float] = None
    amount: Optional[float] = None
    turnover: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None

    @property
    def has_ohlc(self) -> bool:
        return self.high is not None and self.low is not None


@dataclass
class HoldingInfo:
    code: str
    shares: int
    cost: float
    buy_date: Optional[date] = None
    horizon: Optional[str] = None
    sellable: bool = True
    sellable_note: Optional[str] = None
    name: str = ""
    close: Optional[float] = None
    market_value: float = 0.0


@dataclass
class AccountCtx:
    cash: float = 0.0
    market_value: float = 0.0
    held_count: int = 0

    @property
    def total_capital(self) -> float:
        return self.cash + self.market_value


@dataclass
class PlanInput:
    code: str
    name: str
    board_market: str
    is_st: bool
    suspended: bool
    bars: List[Bar]
    rules: PositionRules
    account: AccountCtx
    trade_date: date
    next_trade_date: date
    limit_state: Optional[str] = None
    boards: List[Dict[str, Any]] = field(default_factory=list)
    holding: Optional[HoldingInfo] = None
    gaps: List[str] = field(default_factory=list)

    @property
    def held(self) -> bool:
        return self.holding is not None


@dataclass
class PlanOutcome:
    payload: Dict[str, Any]
    meta: Dict[str, Any]
    model_called: bool = False
    model_error: Optional[str] = None
    model_used: bool = False


# ---------------------------------------------------------------------------
# levels
# ---------------------------------------------------------------------------
def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round_price(value)


def compute_levels(bars: Sequence[Bar]) -> Dict[str, Any]:
    """Moving averages, window highs / lows and the average daily range of the local bars (ascending)."""
    closes = [b.close for b in bars]
    close = closes[-1]

    def ma(n: int) -> Optional[float]:
        return mean(closes[-n:]) if len(closes) >= n else None

    diffs = [abs(closes[i] - closes[i - 1]) for i in range(max(1, len(closes) - RANGE_WINDOW), len(closes))]
    avg_range = mean(diffs) if diffs else 0.0
    if avg_range <= 0:
        avg_range = max(close * 0.005, 0.01)

    def window(n: int) -> Dict[str, Any]:
        part = list(bars[-n:])
        highs = [b.high if b.has_ohlc else b.close for b in part]
        lows = [b.low if b.has_ohlc else b.close for b in part]
        coverage = sum(1 for b in part if b.has_ohlc) / len(part)
        return {"high": max(highs), "low": min(lows), "coverage": coverage, "size": len(part)}

    ma20_prev = mean(closes[-25:-5]) if len(closes) >= 25 else None
    return {
        "close": close, "ma5": ma(5), "ma10": ma(10), "ma20": ma(20), "ma20_prev": ma20_prev,
        "mean_all": mean(closes), "avg_range": avg_range,
        "short": window(SHORT_WINDOW), "swing": window(SWING_WINDOW),
    }


def horizon_levels(horizon: str, levels: Dict[str, Any]) -> Dict[str, Any]:
    """Support / pressure / take profit / trend of one horizon."""
    close, avg_range = levels["close"], levels["avg_range"]
    window = levels[horizon]
    mas = [levels["ma5"], levels["ma10"]] if horizon == "short" else [levels["ma10"], levels["ma20"]]
    below = [x for x in [*mas, window["low"]] if x is not None and x < close - 1e-9]
    if below:
        support = max(below)
        support_estimated = False
    else:
        support = max(close - avg_range, 0.01)
        support_estimated = True
    above = [x for x in [*mas, window["high"]] if x is not None and x > close + 1e-9]
    pressure = min(above) if above else None
    breakout_state = pressure is None
    pressure_value = pressure if pressure is not None else close + avg_range
    top = max(window["high"], close)
    take_profit = top + TAKE_PROFIT_RANGE_MULT[horizon] * avg_range

    ref_ma = levels["ma10"] if horizon == "short" else levels["ma20"]
    ref_label = "10 日均线" if horizon == "short" else "20 日均线"
    if ref_ma is None:
        ref_ma, ref_label = levels["mean_all"], "区间均价"
    if horizon == "short":
        fast, slow = levels["ma5"], levels["ma10"]
        trend_up = close >= ref_ma and (fast is None or slow is None or fast >= slow)
    else:
        rising = levels["ma20_prev"] is None or levels["ma20"] is None or levels["ma20"] >= levels["ma20_prev"]
        trend_up = close >= ref_ma and rising
    below_zone = [x for x in [window["low"], levels["ma10"], levels["ma20"]] if x is not None and x < support - 1e-9]
    lower = max(below_zone) if below_zone else max(support - avg_range, 0.01)
    return {
        "support": support, "support_estimated": support_estimated, "pressure": pressure_value,
        "breakout_state": breakout_state, "take_profit": take_profit, "ref_ma": ref_ma, "ref_label": ref_label,
        "trend_up": trend_up, "lower_support": lower, "window_high": window["high"], "window_low": window["low"],
    }


# ---------------------------------------------------------------------------
# drafts (verdict + price levels of one horizon, before rendering)
# ---------------------------------------------------------------------------
@dataclass
class HorizonDraft:
    verdict: str
    reason: str = ""
    entry_price: Optional[float] = None
    entry_zone: Optional[Tuple[float, float]] = None
    breakout_price: Optional[float] = None
    invalid_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    support: Optional[float] = None
    pressure: Optional[float] = None
    from_model: bool = False


def _strongest_weak_board(boards: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    weak = [b for b in boards if b.get("state") in WEAK_BOARD_STATES]
    return weak[0] if weak else None


def _zone(entry: float, avg_range: float, close: float, limit_down: Optional[float]) -> Tuple[float, float]:
    half = max(avg_range * ZONE_RANGE_FRACTION, 0.01)
    low, high = round_price(entry - half), round_price(entry + half)
    if high >= close:
        high = round_price(close - 0.01)
    if limit_down is not None and low < limit_down:
        low = limit_down
    if low >= high:
        low = round_price(high - 0.01)
    return (low, high)


def rule_draft(horizon: str, inp: PlanInput, levels: Dict[str, Any]) -> HorizonDraft:
    """Deterministic verdict and price levels of one horizon."""
    hl = horizon_levels(horizon, levels)
    close, avg_range = levels["close"], levels["avg_range"]
    limits = limit_prices(close, inp.board_market, inp.is_st)
    support, pressure = hl["support"], hl["pressure"]
    near_support = (close - support) <= NEAR_SUPPORT_RANGES * avg_range
    weak_board = _strongest_weak_board(inp.boards)
    ref = f"{hl['ref_label']} {fmt_price(hl['ref_ma'])}"

    draft = HorizonDraft(
        verdict="skip", support=_round(support), pressure=_round(pressure),
        breakout_price=_round(pressure), take_profit_price=_round(hl["take_profit"]),
    )
    if inp.held:
        draft.verdict = "hold"
        if not hl["trend_up"]:
            draft.verdict = "reduce"
            draft.reason = f"收盘 {fmt_price(close)} 低于{ref}，趋势转弱"
        elif near_support and not hl["breakout_state"]:
            draft.verdict = "add"
            draft.reason = f"趋势向上，收盘贴近支撑 {fmt_price(support)}"
        else:
            draft.reason = f"趋势未破坏（收盘 {fmt_price(close)}，{ref}）"
        draft.entry_price = _round(support)
        if draft.entry_price is not None:
            draft.entry_zone = _zone(draft.entry_price, avg_range, close, limits["down"])
        return draft

    if inp.limit_state in ("up", "one_word_up"):
        draft.verdict = "wait"
        draft.reason = "收盘涨停，次日可能高开或一字，不追涨停，等开板回踩"
    elif not hl["trend_up"]:
        draft.verdict = "skip"
        draft.reason = f"收盘 {fmt_price(close)} 低于{ref}，趋势向下，先不买"
    elif near_support:
        draft.verdict = "buy"
        draft.reason = f"趋势向上，收盘贴近支撑 {fmt_price(support)}，可等回踩买入"
    else:
        draft.verdict = "wait"
        draft.reason = f"趋势向上，但收盘离支撑 {fmt_price(support)} 较远，不追高，等回踩"
    if weak_board is not None and draft.verdict == "buy":
        draft.verdict = "wait"
        draft.reason = (f"所属板块 {weak_board.get('name')} 处于{BOARD_STATE_TEXT.get(weak_board.get('state'), '走弱')}，"
                        f"先等待确认")
    if draft.verdict in ("buy", "wait"):
        draft.entry_price = _round(support)
        draft.entry_zone = _zone(draft.entry_price, avg_range, close, limits["down"])
        draft.entry_price = min(draft.entry_price, draft.entry_zone[1])
        lower = _round(hl["lower_support"])
        draft.invalid_price = lower if lower < draft.entry_zone[0] else round_price(draft.entry_zone[0] - avg_range)
    return draft


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------
def _pct_text(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def build_prompt(inp: PlanInput, levels: Dict[str, Any], drafts: Dict[str, HorizonDraft]) -> str:
    close = levels["close"]
    last = inp.bars[-1]
    limits = limit_prices(close, inp.board_market, inp.is_st)
    rules = inp.rules
    lines: List[str] = [
        f"交易日 {inp.trade_date.isoformat()} 收盘后，为 {inp.name or inp.code}（{inp.code}，"
        f"{BOARD_LABEL.get(inp.board_market, '主板')}{'，ST' if inp.is_st else ''}）"
        f"制定下一个交易日（{inp.next_trade_date.isoformat()}）的计划。",
        "",
        "## 账户",
    ]
    if inp.holding:
        h = inp.holding
        pnl = (close / h.cost - 1) * 100 if h.cost else 0.0
        lines.append(
            f"已持仓 {fmt_num(h.shares)} 股，成本 {fmt_price(h.cost)}，浮动盈亏 {pnl:+.2f}%，"
            f"买入日期 {h.buy_date.isoformat() if h.buy_date else '未填'}，计划周期 "
            f"{HORIZON_LABEL.get(h.horizon or '', '未指定')}。结论只能是 hold / add / reduce / clear。"
        )
    else:
        lines.append("未持仓。结论只能是 buy / wait / skip。")
    lines.append(
        f"止损幅度 {rules.stop_loss_pct:g}%（从买入价或成本起算，系统另行执行）；总资金 {fmt_num(inp.account.total_capital)} 元。"
    )
    lines += [
        "",
        "## 今日行情",
        f"收盘 {fmt_price(close)}，涨跌幅 {_pct_text(last.pct_chg)}，成交额 "
        f"{'—' if last.amount is None else f'{last.amount / 1e8:.2f} 亿'}，换手率 "
        f"{'—' if last.turnover is None else f'{last.turnover:.2f}%'}，涨跌停状态 {inp.limit_state or '未知'}。",
        f"明天涨停价 {fmt_price(limits['up'])}，跌停价 {fmt_price(limits['down'])}（涨跌幅限制 ±{limits['limit_pct']:g}%）。",
        "",
        f"## 最近 {min(PROMPT_BARS, len(inp.bars))} 个交易日（日期 收盘 涨跌幅 成交额亿）",
    ]
    for bar in inp.bars[-PROMPT_BARS:]:
        amount = "—" if bar.amount is None else f"{bar.amount / 1e8:.2f}"
        hi_lo = f" 高{bar.high:.2f} 低{bar.low:.2f}" if bar.has_ohlc else ""
        lines.append(f"{bar.date.isoformat()} {bar.close:.2f} {_pct_text(bar.pct_chg)} {amount}{hi_lo}")
    lines += [
        "",
        "## 均线与区间",
        f"MA5 {fmt_price(levels['ma5'])}，MA10 {fmt_price(levels['ma10'])}，MA20 {fmt_price(levels['ma20'])}；"
        f"{SHORT_WINDOW} 日高 {fmt_price(levels['short']['high'])} / 低 {fmt_price(levels['short']['low'])}；"
        f"{SWING_WINDOW} 日高 {fmt_price(levels['swing']['high'])} / 低 {fmt_price(levels['swing']['low'])}；"
        f"日均波动 {levels['avg_range']:.2f} 元。",
    ]
    if inp.boards:
        lines += ["", "## 所属板块（今日异动判定）"]
        for board in inp.boards[:3]:
            lines.append(
                f"- {board.get('name')}：{BOARD_STATE_TEXT.get(board.get('state'), board.get('state'))}，"
                f"当日 {_pct_text(board.get('change_pct'))}，超额 {_pct_text(board.get('excess_pct'))}"
                f"{'，' + board['reason'] if board.get('reason') else ''}"
            )
    lines += ["", "## 规则估算的参考价位（可采用、可调整）"]
    for horizon in HORIZONS:
        d = drafts[horizon]
        lines.append(
            f"- {HORIZON_LABEL[horizon]}：支撑 {fmt_price(d.support)}，压力 {fmt_price(d.pressure)}，"
            f"止盈 {fmt_price(d.take_profit_price)}"
        )
    lines += [
        "",
        "## 要求",
        "1. 分别给出短线（1–5 个交易日）和波段（2–4 周）两个周期的结论。",
        "2. 价格单位元，保留两位小数，必须在明天涨跌停价范围内；买入价 / 买入区间是回踩支撑的价位，"
        "invalid_price 是跌破就放弃（或趋势失效）的价位，breakout_price 是放量突破价（突破后不追高，等新的回踩）。",
        "3. 不要输出股数、仓位、金额。",
        "4. situation 用 2–3 句话写今天的情况（量价、强弱、板块），reason 写这个周期结论的原因；risks 写 1–3 条风险。",
        "5. 只输出下面格式的 JSON：",
        '{"situation": "…", "risks": ["…"], "short": {"verdict": "buy|wait|skip 或 hold|add|reduce|clear", '
        '"reason": "…", "entry_price": 0.0, "entry_zone": [0.0, 0.0], "breakout_price": 0.0, '
        '"invalid_price": 0.0, "take_profit_price": 0.0}, "swing": {…同上…}}',
    ]
    return "\n".join(lines)


def parse_reply(text: str) -> Optional[Dict[str, Any]]:
    """The JSON object of a model reply, or None when there is none."""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", str(text)).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start:end + 1])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def merge_model(draft: HorizonDraft, raw: Any, inp: PlanInput, close: float,
                avg_range: float) -> Tuple[HorizonDraft, bool]:
    """Apply the model's judgement of one horizon on a rule draft; returns (draft, changed).

    The verdict must be allowed for the stock's state; every price must lie inside the next day's limit range and
    keep its order (invalid < zone < breakout, take profit above the close), otherwise that field keeps the rule value.
    """
    if not isinstance(raw, dict):
        return draft, False
    limits = limit_prices(close, inp.board_market, inp.is_st)
    low_bound, high_bound = limits["down"], limits["up"]

    def in_range(value: Optional[float]) -> bool:
        return value is not None and low_bound is not None and high_bound is not None and low_bound <= value <= high_bound

    merged = HorizonDraft(**{**draft.__dict__})
    changed = False
    allowed = HELD_VERDICTS if inp.held else FLAT_VERDICTS
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict in allowed:
        merged.verdict = verdict
        changed = True
    reason = str(raw.get("reason") or "").strip()
    if reason:
        merged.reason = reason[:200]
        changed = True

    entry = _number(raw.get("entry_price"))
    if in_range(entry):
        merged.entry_price = round_price(entry)
        zone_raw = raw.get("entry_zone")
        zone: Optional[Tuple[float, float]] = None
        if isinstance(zone_raw, (list, tuple)) and len(zone_raw) == 2:
            lo, hi = _number(zone_raw[0]), _number(zone_raw[1])
            if in_range(lo) and in_range(hi) and lo < hi:
                zone = (round_price(lo), round_price(hi))
        if zone is None or not (zone[0] <= merged.entry_price <= zone[1]):
            zone = _zone(merged.entry_price, avg_range, close, low_bound)
            zone = (min(zone[0], merged.entry_price), max(zone[1], merged.entry_price))
        merged.entry_zone = zone
        changed = True
    elif merged.entry_price is not None and not in_range(merged.entry_price):
        merged.entry_price = None
        merged.entry_zone = None
    breakout = _number(raw.get("breakout_price"))
    if in_range(breakout) and (merged.entry_price is None or breakout > merged.entry_price):
        merged.breakout_price = round_price(breakout)
        changed = True
    invalid = _number(raw.get("invalid_price"))
    zone_floor = merged.entry_zone[0] if merged.entry_zone else merged.entry_price
    if in_range(invalid) and (zone_floor is None or invalid < zone_floor):
        merged.invalid_price = round_price(invalid)
        changed = True
    profit = _number(raw.get("take_profit_price"))
    if in_range(profit) and profit > close:
        merged.take_profit_price = round_price(profit)
        changed = True
    if merged.verdict in ("buy", "wait") and not inp.held and merged.entry_price is None:
        merged.entry_price = draft.entry_price
        merged.entry_zone = draft.entry_zone
        merged.invalid_price = merged.invalid_price or draft.invalid_price
    if merged.verdict == "skip" and not inp.held:
        merged.entry_price = merged.entry_zone = merged.invalid_price = None
    merged.from_model = changed
    return merged, changed


# ---------------------------------------------------------------------------
# rendering one horizon
# ---------------------------------------------------------------------------
def _outside(price: Optional[float], limits: Dict[str, Optional[float]]) -> bool:
    if price is None or limits["up"] is None or limits["down"] is None:
        return False
    return price > limits["up"] + 1e-9 or price < limits["down"] - 1e-9


def _cond(if_text: str, then_text: str, action: str, *, shares: Optional[int] = None,
          price: Optional[float] = None, limits: Optional[Dict[str, Optional[float]]] = None) -> Dict[str, Any]:
    return {
        "if_text": if_text, "then_text": then_text, "action": action, "shares": shares, "price": price,
        "outside_limit": _outside(price, limits) if limits else False,
    }


def _sell_row(shares_total: int, fraction: str, price: Optional[float], limits: Dict[str, Optional[float]],
              if_text: str, action: str = "reduce") -> Dict[str, Any]:
    qty = shares_total if action == "clear" else max(1, shares_total // 2)
    verb = "全部卖出" if action == "clear" else f"{fraction}，卖出"
    return _cond(if_text, f"{verb} {fmt_num(qty)} 股", action, shares=qty, price=price, limits=limits)


def render_horizon(
    horizon: str,
    draft: HorizonDraft,
    inp: PlanInput,
    levels: Dict[str, Any],
    sell_blocked: bool,
) -> Tuple[Dict[str, Any], str, List[str]]:
    """The ``HorizonPlan`` payload, the verdict before the max-holdings / below-lot downgrade, extra risks."""
    close = levels["close"]
    limits = limit_prices(close, inp.board_market, inp.is_st)
    rules = inp.rules
    account = inp.account
    hl = horizon_levels(horizon, levels)
    risks: List[str] = []
    verdict = draft.verdict
    raw_verdict = verdict
    reason = draft.reason or None
    support = draft.support if draft.support is not None else _round(hl["support"])
    plan: Dict[str, Any] = {
        "verdict": verdict, "headline": "", "reason": reason, "entry_price": None, "entry_zone": None,
        "breakout_price": draft.breakout_price, "invalid_price": None, "take_profit_price": draft.take_profit_price,
        "conditions": [], "sizing": None, "stop": None,
    }

    if inp.held:
        holding = inp.holding
        stop = compute_stop(holding.cost, rules, hl["support"], close)
        plan["stop"] = stop
        shares = holding.shares
        broke_stop = close <= stop["price"] + 1e-9
        if broke_stop:
            verdict = "clear"
            reason = f"收盘 {fmt_price(close)} 已跌破止损价 {fmt_price(stop['price'])}，按规则下一交易日全部卖出"
        if verdict in ("reduce", "clear") and sell_blocked:
            risks.append(
                f"T+1：这笔持仓的买入日期 {holding.buy_date.isoformat()} 不早于明天（{inp.next_trade_date.isoformat()}），"
                f"明天还不能卖出，止损和减仓条件要等到可以卖出的那天才能执行"
            )
            verdict = "hold"
        entry = draft.entry_price
        sizing = None
        if verdict == "add" and entry is not None:
            sizing = compute_sizing(
                price=entry, board_market=inp.board_market, is_st=inp.is_st, rules=rules, cash=account.cash,
                market_value=account.market_value, held_shares=shares, held_count=account.held_count,
                tech_support=hl["support"], close=close,
            )
            if sizing["status"] != "ok":
                reason = (reason + "；" if reason else "") + "按仓位规则已经没有加仓空间"
                sizing = None
        if verdict == "add" and sizing is None:
            verdict = "hold"
        raw_verdict = draft.verdict if not broke_stop else "clear"
        plan["sizing"] = sizing
        rows: List[Dict[str, Any]] = []
        pressure, profit = draft.breakout_price or draft.pressure, draft.take_profit_price
        if verdict == "clear":
            rows.append(_sell_row(shares, "", None, limits, "明天开盘", "clear"))
            if broke_stop:
                rows[0]["if_text"] = f"收盘 {fmt_price(close)} 已跌破止损价 {fmt_price(stop['price'])}"
                rows[0]["then_text"] = f"全部卖出 {fmt_num(shares)} 股，不再等待"
        else:
            rows.append(_sell_row(shares, "", stop["price"], limits,
                                  f"收盘跌破止损价 {fmt_price(stop['price'])}", "clear"))
            rows[-1]["then_text"] = f"下一交易日全部卖出 {fmt_num(shares)} 股"
            if hl["support"] > stop["price"] and verdict != "reduce":
                rows.append(_sell_row(shares, "减半", _round(hl["support"]), limits,
                                      f"收盘跌破技术支撑 {fmt_price(support)}", "reduce"))
            if verdict == "reduce":
                rows.insert(0, _sell_row(shares, "减半", None, limits, "明天开盘或盘中冲高", "reduce"))
            if pressure is not None:
                rows.append(_cond(f"放量站上 {fmt_price(pressure)} 元", "继续持有", "hold", price=pressure, limits=limits))
            if profit is not None:
                rows.append(_sell_row(shares, "减半", profit, limits,
                                      f"冲高至 {fmt_price(profit)} 元附近未放量", "reduce"))
            if verdict == "add" and sizing is not None:
                rows.insert(0, _cond(
                    f"回踩 {fmt_price(draft.entry_zone[0])}–{fmt_price(draft.entry_zone[1])} 元缩量企稳",
                    f"加仓 {fmt_num(sizing['shares'])} 股，按 {fmt_price(entry)} 元", "buy",
                    shares=sizing["shares"], price=entry, limits=limits,
                ))
        plan["conditions"] = rows
        plan["entry_price"] = entry if verdict == "add" else None
        plan["entry_zone"] = draft.entry_zone if verdict == "add" else None
        plan["take_profit_price"] = profit
        plan["invalid_price"] = None
        plan["verdict"] = verdict
        plan["reason"] = reason
        plan["headline"] = _held_headline(verdict, stop["price"], pressure, profit, shares, sizing, support)
        return plan, raw_verdict, risks

    # ---- not held --------------------------------------------------------------------------------
    if verdict in ("buy", "wait") and draft.entry_price is not None and draft.entry_zone is not None:
        entry, zone = draft.entry_price, draft.entry_zone
        sizing = compute_sizing(
            price=entry, board_market=inp.board_market, is_st=inp.is_st, rules=rules, cash=account.cash,
            market_value=account.market_value, held_shares=0, held_count=account.held_count,
            tech_support=support, close=close,
        )
        plan["sizing"] = sizing
        plan["stop"] = sizing["stop"] if sizing["stop"]["price"] else compute_stop(entry, rules, support, close)
        plan["entry_price"], plan["entry_zone"], plan["invalid_price"] = entry, zone, draft.invalid_price
        raw_verdict = verdict
        capital_missing = account.total_capital <= 0
        if verdict == "buy" and sizing["status"] == "max_holdings":
            verdict = "wait"
            reason = sizing["message"]
        elif verdict == "buy" and sizing["status"] == "below_lot" and not capital_missing:
            verdict = "skip"
            reason = sizing["message"]
        rows = []
        zone_text = f"回踩 {fmt_price(zone[0])}–{fmt_price(zone[1])} 元并缩量企稳"
        if sizing["status"] == "ok":
            buy_then = (f"按 {fmt_price(entry)} 元买入 {fmt_num(sizing['shares'])} 股"
                        f"（约 {fmt_num(sizing['amount'])} 元，占总资金 {sizing['capital_pct']:.1f}%）")
            buy_row = _cond(zone_text, buy_then, "buy", shares=sizing["shares"], price=entry, limits=limits)
        elif sizing["status"] == "max_holdings":
            buy_row = _cond(zone_text, "已持满最多持股数，不新开仓，是否换股见持仓股的换股判断", "wait",
                            price=entry, limits=limits)
        elif sizing["status"] == "below_lot":
            buy_row = _cond(zone_text, "按当前仓位规则买不到一手，不买", "skip", price=entry, limits=limits)
        else:
            buy_row = _cond(zone_text, f"按 {fmt_price(entry)} 元买入", "buy", price=entry, limits=limits)
        wait_row = _cond(
            f"未回踩，直接放量站上 {fmt_price(draft.breakout_price)} 元" if draft.breakout_price else "明天没有回踩",
            "不追高，等待新的回踩点", "wait", price=draft.breakout_price, limits=limits,
        )
        if inp.limit_state in ("up", "one_word_up"):
            rows += [_cond("明天继续涨停或高开", "不追，买不到也不勉强", "wait"), buy_row]
        elif verdict == "buy":
            rows += [buy_row, wait_row]
        elif verdict == "skip":
            rows.append(buy_row)
        else:
            rows += [wait_row, buy_row]
        if draft.invalid_price is not None:
            rows.append(_cond(f"收盘跌破 {fmt_price(draft.invalid_price)} 元", "放弃，不再关注这个买点", "skip",
                              price=draft.invalid_price, limits=limits))
        plan["conditions"] = rows
        plan["verdict"] = verdict
        plan["reason"] = reason
        plan["headline"] = _flat_headline(verdict, zone, entry, sizing, draft.breakout_price, reason)
        return plan, raw_verdict, risks

    plan["verdict"] = verdict = "skip" if verdict not in ("skip", "wait") else verdict
    raw_verdict = verdict  # no buy price: it cannot be a switch candidate either
    ref = f"{hl['ref_label']} {fmt_price(hl['ref_ma'])}"
    plan["conditions"] = [_cond(f"收盘重新站上 {fmt_price(hl['ref_ma'])} 元（{hl['ref_label']}）", "再评估是否买入", "wait",
                                price=_round(hl["ref_ma"]), limits=limits)]
    plan["reason"] = reason or f"收盘 {fmt_price(close)} 低于{ref}，先不买"
    plan["headline"] = f"先不买：{plan['reason']}"
    return plan, raw_verdict, risks


def _held_headline(verdict: str, stop: float, pressure: Optional[float], profit: Optional[float], shares: int,
                   sizing: Optional[Dict[str, Any]], support: Optional[float]) -> str:
    if verdict == "clear":
        return f"收盘跌破止损价 {fmt_price(stop)}，明天全部卖出 {fmt_num(shares)} 股"
    if verdict == "reduce":
        return f"趋势转弱，减半（卖出约 {fmt_num(max(1, shares // 2))} 股）；收盘跌破止损价 {fmt_price(stop)} 清仓"
    if verdict == "add" and sizing:
        return f"贴近支撑，可加仓 {fmt_num(sizing['shares'])} 股；收盘跌破止损价 {fmt_price(stop)} 清仓"
    tail = f"放量站上 {fmt_price(pressure)} 继续持有" if pressure else "趋势未破坏"
    return f"继续持有，收盘跌破止损价 {fmt_price(stop)} 清仓；{tail}"


def _flat_headline(verdict: str, zone: Tuple[float, float], entry: float, sizing: Dict[str, Any],
                   breakout: Optional[float], reason: Optional[str]) -> str:
    if verdict == "buy" and sizing["status"] == "ok":
        return (f"回踩 {fmt_price(zone[0])}–{fmt_price(zone[1])} 元缩量企稳，按 {fmt_price(entry)} 元买入 "
                f"{fmt_num(sizing['shares'])} 股")
    if verdict == "buy":
        return f"回踩 {fmt_price(zone[0])}–{fmt_price(zone[1])} 元缩量企稳后按 {fmt_price(entry)} 元买入"
    if verdict == "wait":
        tail = f"，放量站上 {fmt_price(breakout)} 不追高" if breakout else ""
        return f"先等待：回踩 {fmt_price(zone[0])}–{fmt_price(zone[1])} 元企稳再看{tail}"
    return f"不买：{reason}" if reason else "按当前规则不买"


# ---------------------------------------------------------------------------
# assembling the stock plan
# ---------------------------------------------------------------------------
def _boards_text(boards: Sequence[Dict[str, Any]]) -> str:
    parts = []
    for board in boards[:3]:
        state = BOARD_STATE_TEXT.get(board.get("state"), "")
        parts.append(f"{board.get('name')}（{state}，当日 {_pct_text(board.get('change_pct'))}）")
    return "、".join(parts)


def rule_situation(inp: PlanInput, levels: Dict[str, Any]) -> str:
    close = levels["close"]
    last = inp.bars[-1]
    pct = last.pct_chg
    if pct is None and len(inp.bars) >= 2 and inp.bars[-2].close:
        pct = (close / inp.bars[-2].close - 1) * 100
    parts = [f"{inp.name or inp.code} 收盘 {fmt_price(close)}（{_pct_text(pct)}）"]
    if last.amount:
        text = f"成交额 {last.amount / 1e8:.2f} 亿"
        prev = [b.amount for b in inp.bars[-6:-1] if b.amount]
        if len(prev) >= 3 and mean(prev) > 0:
            text += f"，是前 {len(prev)} 日均值的 {last.amount / mean(prev):.1f} 倍"
        parts.append(text)
    if last.turnover is not None:
        parts.append(f"换手率 {last.turnover:.2f}%")
    sentence = "，".join(parts) + "。"
    trend = []
    if levels["ma5"] is not None:
        trend.append(f"{'站上' if close >= levels['ma5'] else '跌破'} 5 日均线 {fmt_price(levels['ma5'])}")
    if levels["ma20"] is not None:
        trend.append(f"{'高于' if close >= levels['ma20'] else '低于'} 20 日均线 {fmt_price(levels['ma20'])}")
    if trend:
        sentence += "收盘" + "，".join(trend) + "。"
    if inp.limit_state in ("up", "one_word_up"):
        sentence += "收盘涨停。"
    elif inp.limit_state in ("down", "one_word_down"):
        sentence += "收盘跌停。"
    if inp.boards:
        sentence += f"所属板块：{_boards_text(inp.boards)}。"
    return sentence


def _stop_risks(inp: PlanInput, stop_pct: float) -> List[str]:
    limit = limit_pct(inp.board_market, inp.is_st)
    market = BOARD_LABEL.get(inp.board_market, "主板") + ("（ST）" if inp.is_st else "")
    if limit < stop_pct:
        return [f"{market}单日最大跌幅 {limit:g}%，小于止损幅度 {stop_pct:g}%，下跌可能连续跌停，到止损线时未必卖得出去"]
    if limit == stop_pct:
        return [f"{market}一个跌停约 {limit:g}%，止损可能在跌停当天无法成交，次日继续低开时实际亏损会超过 {stop_pct:g}%"]
    return [f"{market}单日最大跌幅 {limit:g}%，止损在一个交易日内就可能被跳空越过，实际亏损可能超过 {stop_pct:g}%"]


def _base_summary(inp: PlanInput, limits: Dict[str, Optional[float]], close: Optional[float],
                  change_pct: Optional[float]) -> Dict[str, Any]:
    return {
        "code": inp.code, "name": inp.name, "board_market": inp.board_market, "is_st": inp.is_st,
        "held": inp.held, "close": close, "change_pct": change_pct,
        "trade_date": inp.trade_date.isoformat(), "next_trade_date": inp.next_trade_date.isoformat(),
        "limit_pct": limits["limit_pct"], "next_limit_up": limits["up"], "next_limit_down": limits["down"],
    }


def failed_plan(inp: PlanInput, reason: str) -> PlanOutcome:
    """A ``failed`` StockPlan (the list shows the reason; the detail page has no horizons)."""
    close = inp.bars[-1].close if inp.bars else None
    limits = limit_prices(close, inp.board_market, inp.is_st)
    payload = {
        **_base_summary(inp, limits, close, inp.bars[-1].pct_chg if inp.bars else None),
        "verdict": None, "headline": "", "status": "failed", "failure_reason": reason, "situation": "",
        "horizons": {"short": None, "swing": None}, "default_horizon": _default_horizon(inp), "levels": [],
        "switch": None, "risks": [], "data_gaps": list(dict.fromkeys(inp.gaps)), "model": None,
    }
    return PlanOutcome(payload=payload, meta={"raw_verdicts": {}})


def suspended_plan(inp: PlanInput) -> PlanOutcome:
    """Suspended stock: no trade plan, only what to watch after it resumes (spec 5.6)."""
    close = inp.bars[-1].close if inp.bars else None
    limits = limit_prices(close, inp.board_market, inp.is_st)
    headline = "停牌，没有次日交易计划"
    reason = "停牌股票不生成交易计划，复牌后再看"
    horizon = {
        "verdict": "suspended", "headline": headline, "reason": reason, "entry_price": None, "entry_zone": None,
        "breakout_price": None, "invalid_price": None, "take_profit_price": None, "conditions": [],
        "sizing": None, "stop": None,
    }
    risks = ["复牌首日价格可能大幅跳空，复牌后先看开盘价相对停牌前收盘价的位置，再决定是否按规则处理"]
    if inp.held:
        risks.append("停牌期间无法卖出，止损规则在复牌后第一个能成交的交易日才能执行")
    levels = []
    if close:
        levels.append({"kind": "close", "price": close, "label": "停牌前收盘"})
        if inp.held:
            levels.append({"kind": "cost", "price": inp.holding.cost, "label": "持仓成本"})
    payload = {
        **_base_summary(inp, limits, close, inp.bars[-1].pct_chg if inp.bars else None),
        "verdict": "suspended", "headline": headline, "status": "ready", "failure_reason": None,
        "situation": f"{inp.name or inp.code} 停牌" + (f"，停牌前收盘 {fmt_price(close)}。" if close else "。"),
        "horizons": {"short": dict(horizon), "swing": dict(horizon)}, "default_horizon": _default_horizon(inp),
        "levels": levels, "switch": None, "risks": risks, "data_gaps": list(dict.fromkeys(inp.gaps)), "model": None,
    }
    return PlanOutcome(payload=payload, meta={"raw_verdicts": {"short": "suspended", "swing": "suspended"}})


def _default_horizon(inp: PlanInput) -> str:
    return inp.holding.horizon if inp.holding and inp.holding.horizon in HORIZONS else "short"


def _levels_list(inp: PlanInput, horizon_plan: Dict[str, Any], levels: Dict[str, Any], draft: HorizonDraft,
                 limits: Dict[str, Optional[float]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def add(kind: str, price: Optional[float], label: str) -> None:
        if price is not None and price > 0:
            out.append({"kind": kind, "price": round_price(price), "label": label})

    add("close", levels["close"], "收盘")
    if inp.held:
        add("cost", inp.holding.cost, "持仓成本")
    add("pressure", draft.pressure, "压力")
    if horizon_plan.get("entry_price") is not None:
        add("entry", horizon_plan["entry_price"], "买入价" if not inp.held else "加仓价")
    add("support", draft.support, "支撑")
    stop = horizon_plan.get("stop")
    if stop:
        add("stop", stop["price"], "止损")
    add("take_profit", horizon_plan.get("take_profit_price"), "止盈")
    add("limit_up", limits["up"], "明天涨停价")
    add("limit_down", limits["down"], "明天跌停价")
    return out


def build_plan(
    inp: PlanInput,
    llm: Optional[LlmFn] = None,
    model_label: str = "",
) -> PlanOutcome:
    """The ``StockPlan`` payload of one stock. Never raises for model problems; raises nothing for bad data either
    (returns a ``failed`` plan instead)."""
    if inp.suspended:
        return suspended_plan(inp)
    if not inp.bars or inp.bars[-1].date != inp.trade_date:
        have = inp.bars[-1].date.isoformat() if inp.bars else "无"
        return failed_plan(
            inp, f"没有 {inp.trade_date.isoformat()} 的本地行情（最近一天 {have}），可能停牌或数据还没入库",
        )
    if len(inp.bars) < MIN_BARS:
        return failed_plan(inp, f"本地只有 {len(inp.bars)} 个交易日的行情，不足 {MIN_BARS} 个，不生成计划")

    gaps = list(inp.gaps)
    levels = compute_levels(inp.bars)
    close = levels["close"]
    limits = limit_prices(close, inp.board_market, inp.is_st)
    if len(inp.bars) < RECOMMENDED_BARS:
        gaps.append(f"本地只有 {len(inp.bars)} 个交易日的行情，均线和高低点参考性不足")
    coverage = levels["swing"]["coverage"] if len(inp.bars) >= SWING_WINDOW else levels["short"]["coverage"]
    if coverage < OHLC_MIN_COVERAGE:
        gaps.append("缺少日线最高 / 最低价，支撑和压力按收盘价估算")
    if not inp.boards:
        gaps.append("没有这只股票所属板块的异动判定，计划里没有板块信号")

    drafts = {h: rule_draft(h, inp, levels) for h in HORIZONS}
    model_called = model_used = False
    model_error: Optional[str] = None
    situation = ""
    model_risks: List[str] = []
    if llm is not None:
        model_called = True
        try:
            reply = parse_reply(llm(build_prompt(inp, levels, drafts), SYSTEM_PROMPT))
            if reply is None:
                model_error = "模型没有返回可用的 JSON"
            else:
                for horizon in HORIZONS:
                    drafts[horizon], changed = merge_model(drafts[horizon], reply.get(horizon), inp, close,
                                                           levels["avg_range"])
                    model_used = model_used or changed
                if not model_used:
                    model_error = "模型的回复里没有可用的结论"
                situation = str(reply.get("situation") or "").strip()[:400]
                risks_raw = reply.get("risks")
                if isinstance(risks_raw, list):
                    model_risks = [str(item).strip()[:120] for item in risks_raw if str(item).strip()][:3]
        except Exception as exc:  # noqa: BLE001 - any model failure falls back to the rule plan
            logger.warning("[DailyPlan] model call failed for %s: %s", inp.code, exc)
            model_error = f"{type(exc).__name__}: {str(exc)[:100]}"
    if llm is None:
        gaps.append("没有配置分析模型，这份计划按规则生成")
    elif not model_used:
        gaps.append(f"模型这次没能给出结论（{model_error}），这份计划按规则生成")
        drafts = {h: rule_draft(h, inp, levels) for h in HORIZONS}

    sell_blocked = False
    if inp.holding is not None and inp.holding.buy_date is not None:
        sell_blocked = inp.holding.buy_date >= inp.next_trade_date
    horizons: Dict[str, Any] = {}
    raw_verdicts: Dict[str, str] = {}
    extra_risks: List[str] = []
    for horizon in HORIZONS:
        horizons[horizon], raw_verdicts[horizon], more = render_horizon(horizon, drafts[horizon], inp, levels, sell_blocked)
        extra_risks.extend(more)

    default = _default_horizon(inp)
    default_plan = horizons[default]
    risks: List[str] = []
    if inp.held or default_plan["verdict"] in ("buy", "wait", "add", "hold", "reduce", "clear"):
        risks.extend(_stop_risks(inp, inp.rules.stop_loss_pct))
    sizing = default_plan.get("sizing")
    if sizing and sizing["status"] == "ok":
        total = inp.account.total_capital
        pct = sizing["max_loss"] / total * 100 if total > 0 else 0.0
        risks.append(f"按止损价亏损约 {fmt_num(sizing['max_loss'])} 元（约占总资金 {pct:.1f}%）；"
                     f"如果卖出当天跌停，亏损约 {fmt_num(sizing['limit_down_loss'])} 元")
    if inp.holding is not None:
        holding = inp.holding
        stop = default_plan.get("stop")
        if stop:
            loss_at_limit = (holding.cost - round_price(stop["price"] * (1 - limits["limit_pct"] / 100))) * holding.shares
            risks.append(f"持仓按止损价卖出亏损约 {fmt_num((holding.cost - stop['price']) * holding.shares)} 元；"
                         f"止损日再跌停，亏损约 {fmt_num(loss_at_limit)} 元")
        if holding.buy_date is None:
            risks.append("没有填买入日期，请确认这笔持仓是否已满足 T+1，当天买入的股票次日才能卖出")
    if inp.limit_state in ("up", "one_word_up"):
        risks.append("收盘涨停，次日可能高开或一字板，追高风险大，也可能买不到")
    elif inp.limit_state in ("down", "one_word_down"):
        risks.append("收盘跌停，次日可能继续低开，卖单可能成交不了")
    risks.extend(extra_risks)
    risks.extend(model_risks)

    gap_list = list(dict.fromkeys(g for g in gaps if g))
    payload = {
        **_base_summary(inp, limits, close, inp.bars[-1].pct_chg if inp.bars[-1].pct_chg is not None else None),
        "verdict": default_plan["verdict"], "headline": default_plan["headline"], "status": "ready",
        "failure_reason": None, "situation": situation or rule_situation(inp, levels), "horizons": horizons,
        "default_horizon": default, "levels": _levels_list(inp, default_plan, levels, drafts[default], limits),
        "switch": None, "risks": list(dict.fromkeys(risks)), "data_gaps": gap_list,
        "model": {"model": model_label or "未命名模型", "fallback_from": None, "fallback_reason": None}
        if model_used else None,
    }
    meta = {
        "raw_verdicts": raw_verdicts,
        "tech_support": {h: drafts[h].support for h in HORIZONS},
        "board_strength": sum(1 for b in inp.boards if b.get("state") in STRONG_BOARD_STATES),
    }
    return PlanOutcome(payload=payload, meta=meta, model_called=model_called, model_error=model_error,
                       model_used=model_used)


# ---------------------------------------------------------------------------
# switch decision (spec 5.4)
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    code: str
    name: str
    payload: Dict[str, Any]
    raw_verdicts: Dict[str, str]
    source: str  # watchlist / picks
    order: int = 0
    board_strength: int = 0


def pick_candidate(candidates: Sequence[Candidate], horizon: str, held_codes: Sequence[str]) -> Optional[Candidate]:
    """Best buy / wait candidate for ``horizon``: buy before wait, watchlist before picks, then source order."""
    rank = {"buy": 0, "wait": 1}
    eligible = [
        c for c in candidates
        if c.code not in held_codes and c.payload.get("status") == "ready"
        and c.raw_verdicts.get(horizon) in rank
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda c: (rank[c.raw_verdicts[horizon]], -c.board_strength, 0 if c.source == "watchlist" else 1, c.order))
    return eligible[0]


def build_switch(
    held_payload: Dict[str, Any],
    holding: HoldingInfo,
    held_close: float,
    candidates: Sequence[Candidate],
    held_codes: Sequence[str],
    account: AccountCtx,
    rules: PositionRules,
    other_market_value: float,
) -> Dict[str, Any]:
    """``SwitchDecision`` for one held stock; the default is 「不换」, both conditions must hold to switch."""
    horizon = held_payload.get("default_horizon") or "short"
    held_plan = (held_payload.get("horizons") or {}).get(horizon) or {}
    held_verdict = held_plan.get("verdict")
    held_stop = (held_plan.get("stop") or {}).get("price")
    name = held_payload.get("name") or holding.code
    broken = held_verdict in ("clear", "reduce")
    cond1 = {
        "text": f"当前持仓 {name} 破位：收盘跌破止损价{'' if held_stop is None else ' ' + fmt_price(held_stop)}，或趋势转弱被判为减仓 / 清仓",
        "met": bool(broken),
    }
    candidate = pick_candidate(candidates, horizon, held_codes)
    if candidate is None:
        return {
            "decision": "keep", "summary": "不换：自选和选股里没有比当前持仓更好的候选",
            "candidate": None, "conditions": [cond1], "costs": [],
        }

    cand_plan = candidate.payload["horizons"][horizon]
    zone = cand_plan.get("entry_zone")
    cand_close = candidate.payload.get("close")
    if zone and cand_close is not None:
        met2: Optional[bool]
        if zone[0] <= cand_close <= zone[1]:
            met2 = True
        elif cand_close < zone[0]:
            met2 = False
        else:
            met2 = None
        zone_text = f"{fmt_price(zone[0])}–{fmt_price(zone[1])} 元"
    else:
        met2, zone_text = None, "买入区"
    cond2 = {"text": f"候选 {candidate.name} 回踩 {zone_text}并缩量企稳（要明天盘中确认）", "met": met2}
    decision = "switch" if cond1["met"] and met2 is True else "keep"
    if decision == "switch":
        summary = f"换股：当前持仓已破位，{candidate.name} 已回踩到买入区"
    elif not cond1["met"]:
        summary = f"不换：当前持仓没有破位，{candidate.name} 也没有明显优势，继续按持仓计划处理"
    else:
        summary = f"不换：当前持仓已破位，但 {candidate.name} 还没有回踩企稳，先按持仓计划处理，企稳后再看"

    reason_parts = [cand_plan.get("headline") or ""]
    if candidate.payload.get("situation"):
        reason_parts.append(str(candidate.payload["situation"])[:80])
    cand_verdict = candidate.raw_verdicts.get(horizon) or cand_plan.get("verdict")
    costs = [
        "T+1：卖出当前持仓后买入的新股，买入当天不能卖出，下一交易日起才能卖",
    ]
    if held_stop is not None:
        pnl = (held_close - holding.cost) * holding.shares
        costs.append(f"当前持仓止损价 {fmt_price(held_stop)}，按收盘 {fmt_price(held_close)} 卖出的盈亏约 "
                     f"{pnl:+,.0f} 元（成本 {fmt_price(holding.cost)}）")
    entry = cand_plan.get("entry_price")
    if entry:
        after = compute_sizing(
            price=entry, board_market=candidate.payload["board_market"], is_st=candidate.payload["is_st"],
            rules=rules, cash=account.cash + holding.market_value, market_value=other_market_value,
            held_shares=0, held_count=max(0, account.held_count - 1),
            tech_support=(cand_plan.get("stop") or {}).get("tech_support"), close=cand_close,
        )
        if after["status"] == "ok":
            costs.append(
                f"换入后：{candidate.name} 按 {fmt_price(entry)} 元买入 {fmt_num(after['shares'])} 股，止损价 "
                f"{fmt_price(after['stop']['price'])}，到止损最大亏损约 {fmt_num(after['max_loss'])} 元"
            )
        else:
            costs.append(f"换入后按当前仓位规则买不了 {candidate.name}：{after.get('message') or '股数不足'}")
    costs.append(f"最多持股 {rules.max_holdings} 只，卖出当前持仓之前不能同时买入候选")
    return {
        "decision": decision,
        "summary": summary,
        "candidate": {
            "code": candidate.code, "name": candidate.name,
            "reason": "；".join(p for p in reason_parts if p)[:200],
            "verdict": cand_verdict if cand_verdict in ("buy", "wait", "skip") else "wait",
        },
        "conditions": [cond1, cond2],
        "costs": costs,
    }
