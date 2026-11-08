# -*- coding: utf-8 -*-
"""Position rules, A-share trading constraints and the deterministic share sizing of the next-day plan.

Pure functions only (no I/O): the model decides *whether* and *at what price* to buy, the shares always come
from here (docs/design/core-product-spec.md 5.3). Every number the rules need comes from ``PositionRules``
(``POSITION_*`` config); nothing about the user's risk appetite is hard-coded.

Trading constraints (spec 5.6):

* price limit: main board 10%, ChiNext (300/301) and STAR (688/689) 20%, BSE (8xx / 4xx / 92x) 30%. ST stocks on the
  main board are 5%; ST stocks on the other boards keep their board's limit (the exchanges do not shrink it),
* lot: main board / ChiNext / BSE trade in 100 shares; the STAR board needs at least 200 shares, above which
  any whole number of shares is allowed (selling may always be odd lots),
* prices are rounded half-up to 0.01.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from math import floor
from typing import Any, Dict, List, Optional

BOARD_MAIN = "main"
BOARD_CHINEXT = "chinext"
BOARD_STAR = "star"
BOARD_BSE = "bse"

LIMIT_PCT = {BOARD_MAIN: 10.0, BOARD_CHINEXT: 20.0, BOARD_STAR: 20.0, BOARD_BSE: 30.0}
ST_MAIN_LIMIT_PCT = 5.0
# exchange_board column of market_stock_daily -> board market of the plan API
_EXCHANGE_BOARD_MAP = {"main": BOARD_MAIN, "gem": BOARD_CHINEXT, "chinext": BOARD_CHINEXT,
                       "star": BOARD_STAR, "bse": BOARD_BSE}

STOP_MODES = ("fixed", "nearer")
HORIZONS = ("short", "swing")


def round_price(value: float) -> float:
    """Round half-up to 0.01 (exchange tick); avoids binary float surprises like 2.675 -> 2.67."""
    # round(.., 6) first: 42.3 * 0.95 is 40.184999999999995 in binary floats but 40.185 on the exchange's decimals
    return float(Decimal(str(round(float(value), 6))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round_money(value: float) -> float:
    return round_price(value)


def fmt_num(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def fmt_price(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:,.2f}"


def fmt_shares(value: float) -> str:
    """Shares with up to two decimals, trailing zeros trimmed (2,127.66 / 2,100)."""
    text = f"{value:,.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------------
# board / limits / lots
# ---------------------------------------------------------------------------
def classify_board(code: str, exchange_board: Optional[str] = None) -> str:
    """Board market of a 6-digit A-share code. The code decides; ``exchange_board`` only fills in unknown ones."""
    text = str(code or "").strip()
    if text.startswith(("688", "689")):
        return BOARD_STAR
    if text.startswith(("300", "301")):
        return BOARD_CHINEXT
    if text.startswith(("8", "4", "92")):
        return BOARD_BSE
    if text.startswith(("60", "000", "001", "002", "003")):
        return BOARD_MAIN
    return _EXCHANGE_BOARD_MAP.get(str(exchange_board or "").strip().lower(), BOARD_MAIN)


def limit_pct(board_market: str, is_st: bool = False) -> float:
    if is_st and board_market == BOARD_MAIN:
        return ST_MAIN_LIMIT_PCT
    return LIMIT_PCT.get(board_market, LIMIT_PCT[BOARD_MAIN])


def limit_prices(close: Optional[float], board_market: str, is_st: bool = False) -> Dict[str, Optional[float]]:
    """Next session's limit-up / limit-down price from the last close."""
    pct = limit_pct(board_market, is_st)
    if close is None or close <= 0:
        return {"limit_pct": pct, "up": None, "down": None}
    return {
        "limit_pct": pct,
        "up": round_price(close * (1 + pct / 100)),
        "down": round_price(close * (1 - pct / 100)),
    }


def lot_size(board_market: str) -> int:
    """Smallest order (and the buy increment, except on the STAR board where it is 1 share above the minimum)."""
    return 200 if board_market == BOARD_STAR else 100


def floor_to_lot(raw_shares: float, board_market: str) -> int:
    """Largest tradable buy quantity not above ``raw_shares`` (0 when it is below one lot)."""
    raw = max(0.0, raw_shares)
    lot = lot_size(board_market)
    if board_market == BOARD_STAR:
        return floor(raw) if raw >= lot else 0
    return int(raw // lot) * lot


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PositionRules:
    stop_loss_pct: float = 10.0
    max_total_pct: float = 100.0
    max_holdings: int = 1
    max_single_pct: float = 100.0
    max_single_pct_derived: bool = True
    stop_mode: str = "fixed"

    @classmethod
    def from_config(cls, config: Any) -> "PositionRules":
        """Rules from the ``POSITION_*`` config fields; invalid values fall back to the documented defaults."""
        def number(name: str, default: float, low: float, high: float) -> float:
            try:
                value = float(getattr(config, name, default))
            except (TypeError, ValueError):
                return default
            return value if low < value <= high else default

        stop = number("position_stop_loss_pct", 10.0, 0.0, 99.9)
        total = number("position_max_total_pct", 100.0, 0.0, 100.0)
        try:
            holdings = int(getattr(config, "position_max_holdings", 1))
        except (TypeError, ValueError):
            holdings = 1
        holdings = max(1, holdings)
        explicit = getattr(config, "position_max_single_pct", None)
        single: Optional[float] = None
        if explicit not in (None, ""):
            try:
                parsed = float(explicit)
                single = parsed if 0 < parsed <= 100 else None
            except (TypeError, ValueError):
                single = None
        mode = str(getattr(config, "position_stop_mode", "fixed") or "fixed").strip().lower()
        return cls(
            stop_loss_pct=stop,
            max_total_pct=total,
            max_holdings=holdings,
            max_single_pct=single if single is not None else round(total / holdings, 4),
            max_single_pct_derived=single is None,
            stop_mode=mode if mode in STOP_MODES else "fixed",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stop_loss_pct": self.stop_loss_pct,
            "max_total_pct": self.max_total_pct,
            "max_holdings": self.max_holdings,
            "max_single_pct": self.max_single_pct,
            "max_single_pct_derived": self.max_single_pct_derived,
            "stop_mode": self.stop_mode,
        }


# ---------------------------------------------------------------------------
# stop price
# ---------------------------------------------------------------------------
def compute_stop(
    base_price: float,
    rules: PositionRules,
    tech_support: Optional[float] = None,
    close: Optional[float] = None,
) -> Dict[str, Any]:
    """StopInfo for a position bought (or held) at ``base_price``.

    ``fixed``: base x (1 - stop%). ``nearer``: the nearer (higher) of the fixed stop and the technical support when
    the support lies below the base price. The technical support is otherwise only a hint: a support below the stop
    means the stop fires first, a support above it that was already broken (``close`` below it) means the shape
    weakened.
    """
    factor = 1 - rules.stop_loss_pct / 100
    fixed_stop = round_price(base_price * factor)
    fixed_formula = f"{fmt_price(base_price)} × {factor:.2f}"
    support = round_price(tech_support) if tech_support is not None and tech_support > 0 else None

    mode = rules.stop_mode
    price = fixed_stop
    formula = fixed_formula
    warning: Optional[str] = None
    if mode == "nearer":
        if support is not None and support < base_price and support > fixed_stop:
            price = support
            formula = f"取较近者：技术支撑 {fmt_price(support)}（固定比例 {fixed_formula} = {fmt_price(fixed_stop)}）"
        elif support is not None and support < base_price:
            formula = f"取较近者：固定比例 {fixed_formula}（技术支撑 {fmt_price(support)} 更远）"
        elif support is not None:
            formula = f"取较近者：技术支撑 {fmt_price(support)} 不低于买入价，忽略，按固定比例 {fixed_formula}"
        else:
            formula = f"取较近者：没有可用的技术支撑，按固定比例 {fixed_formula}"
    if support is not None:
        if support < price:
            warning = f"止损会在支撑之前触发：止损价 {fmt_price(price)} 高于技术支撑 {fmt_price(support)}"
        elif close is not None and close < support:
            warning = f"收盘 {fmt_price(close)} 已跌破技术支撑 {fmt_price(support)}，技术形态转弱，是否提前处理由你决定"
    return {"price": price, "mode": mode, "formula": formula, "tech_support": support, "warning": warning}


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------
def compute_sizing(
    *,
    price: Optional[float],
    board_market: str,
    is_st: bool,
    rules: PositionRules,
    cash: float,
    market_value: float,
    held_shares: int = 0,
    held_count: int = 0,
    tech_support: Optional[float] = None,
    close: Optional[float] = None,
) -> Dict[str, Any]:
    """Shares to buy at ``price`` (spec 5.3), as a ``Sizing`` payload with the 「怎么算的」 steps.

    ``held_count`` is the number of distinct holdings and ``held_shares`` what is already held of this stock; a new
    stock is blocked (``max_holdings``) once the holdings are full, adding to a held stock is not.
    """
    lot = lot_size(board_market)
    limit = limit_pct(board_market, is_st)
    total_capital = cash + market_value
    zero_stop = {"price": 0.0, "mode": rules.stop_mode, "formula": "", "tech_support": None, "warning": None}

    if price is None or price <= 0:
        return {
            "status": "no_price", "message": "这个周期还没有可用的买入价，暂时算不出股数。", "price": 0.0, "lot": lot,
            "by_single": 0.0, "by_total": 0.0, "by_cash": 0.0, "raw_shares": 0.0, "shares": 0, "amount": 0.0,
            "capital_pct": 0.0, "post_position_pct": _pct(market_value, total_capital), "stop": zero_stop,
            "max_loss": 0.0, "limit_down_price": 0.0, "limit_down_loss": 0.0, "steps": [],
        }

    price = round_price(price)
    stop = compute_stop(price, rules, tech_support, close)
    limit_down_price = round_price(stop["price"] * (1 - limit / 100))
    by_single = max(0.0, total_capital * rules.max_single_pct / 100 / price - held_shares)
    by_total = max(0.0, (total_capital * rules.max_total_pct / 100 - market_value) / price)
    by_cash = max(0.0, cash / price)
    raw = min(by_single, by_total, by_cash)

    steps: List[str] = [
        f"总资金 {fmt_num(total_capital)} 元 = 现金 {fmt_num(cash)} + 持仓市值 {fmt_num(market_value)}",
        f"按单只上限：{fmt_num(total_capital)} × {fmt_num(rules.max_single_pct, 2).rstrip('0').rstrip('.')}%"
        f" ÷ {fmt_price(price)} − 已持 {fmt_num(held_shares)} 股 = {fmt_shares(by_single)} 股"
        + ("（单只上限由总仓位 ÷ 最多持股数推出）" if rules.max_single_pct_derived else ""),
        f"按总仓位：({fmt_num(total_capital)} × {fmt_num(rules.max_total_pct, 2).rstrip('0').rstrip('.')}%"
        f" − 持仓市值 {fmt_num(market_value)}) ÷ {fmt_price(price)} = {fmt_shares(by_total)} 股",
        f"按现金：{fmt_num(cash)} ÷ {fmt_price(price)} = {fmt_shares(by_cash)} 股",
    ]

    base = {
        "price": price, "lot": lot,
        "by_single": round(by_single, 2), "by_total": round(by_total, 2), "by_cash": round(by_cash, 2),
        "raw_shares": round(raw, 2), "stop": stop, "limit_down_price": limit_down_price,
    }

    if held_shares <= 0 and held_count >= rules.max_holdings:
        steps = [f"已持有 {held_count} 只（最多 {rules.max_holdings} 只），按规则不再新开仓，买入改为看持仓股的换股判断"]
        return {
            **base, "status": "max_holdings",
            "message": f"已持有 {held_count}/{rules.max_holdings} 只，达到最多持股数，不新开仓；是否换股见换股判断。",
            "shares": 0, "amount": 0.0, "capital_pct": 0.0, "post_position_pct": _pct(market_value, total_capital),
            "max_loss": 0.0, "limit_down_loss": 0.0, "steps": steps,
        }

    shares = floor_to_lot(raw, board_market)
    unit = (f"科创板最少 {lot} 股，达到后按 1 股递增" if board_market == BOARD_STAR else f"每手 {lot} 股向下取整")
    if shares <= 0:
        steps.append(f"取最小值 {fmt_shares(raw)} 股，{unit} = 0 股，不足一手")
        if total_capital <= 0:
            message = "还没有录入资金，先在「我的股票」里录入现金和持仓，才能算出股数。"
        else:
            message = (f"按当前规则最多买 {fmt_shares(raw)} 股，不足一手（{fmt_num(lot)} 股 × {fmt_price(price)} = "
                       f"{fmt_num(lot * price)} 元），不建议买入。")
        return {
            **base, "status": "below_lot", "message": message, "shares": 0, "amount": 0.0, "capital_pct": 0.0,
            "post_position_pct": _pct(market_value, total_capital), "max_loss": 0.0, "limit_down_loss": 0.0,
            "steps": steps,
        }

    amount = round_money(shares * price)
    capital_pct = _pct(amount, total_capital)
    post_pct = _pct(market_value + amount, total_capital)
    max_loss = round_money((price - stop["price"]) * shares)
    limit_down_loss = round_money((price - limit_down_price) * shares)
    steps.append(f"取最小值 {fmt_shares(raw)} 股，{unit} = {fmt_num(shares)} 股")
    steps.append(
        f"金额 {fmt_num(shares)} × {fmt_price(price)} = {fmt_num(amount)} 元，占总资金 {capital_pct:.1f}%，"
        f"买后总仓位 {post_pct:.1f}%"
    )
    steps.append(
        f"止损价 {stop['formula']} = {fmt_price(stop['price'])}，到止损亏损 {fmt_num(max_loss)} 元；"
        f"止损日再跌停（{limit:g}%）成交价约 {fmt_price(limit_down_price)}，亏损约 {fmt_num(limit_down_loss)} 元"
    )
    return {
        **base, "status": "ok", "message": None, "shares": int(shares), "amount": amount, "capital_pct": capital_pct,
        "post_position_pct": post_pct, "max_loss": max_loss, "limit_down_loss": limit_down_loss, "steps": steps,
    }


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 2) if whole > 0 else 0.0
