# -*- coding: utf-8 -*-
"""Position rules, A-share limits / lots and the deterministic share sizing (spec 5.3 / 5.5 / 5.6)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.daily_plan_sizing import (
    PositionRules,
    classify_board,
    compute_sizing,
    compute_stop,
    floor_to_lot,
    limit_pct,
    limit_prices,
    lot_size,
    round_price,
)

RULES = PositionRules()  # defaults: stop 10%, total 100%, 1 holding, single derived = 100%


def _size(price, **kwargs):
    args = dict(board_market="main", is_st=False, rules=RULES, cash=100_000.0, market_value=0.0)
    args.update(kwargs)
    return compute_sizing(price=price, **args)


def test_spec_example_empty_account_buy_at_47():
    sizing = _size(47.0, tech_support=46.9)
    assert sizing["status"] == "ok"
    assert sizing["shares"] == 2100
    assert sizing["amount"] == 98_700
    assert sizing["capital_pct"] == 98.7
    assert sizing["post_position_pct"] == 98.7
    assert sizing["stop"]["price"] == 42.3
    assert sizing["stop"]["mode"] == "fixed"
    assert sizing["stop"]["formula"] == "47.00 × 0.90"
    assert sizing["max_loss"] == 9_870
    assert sizing["lot"] == 100
    assert sizing["by_single"] == sizing["by_total"] == sizing["by_cash"] == 2127.66
    assert sizing["raw_shares"] == 2127.66
    # main board limit down on the stop day: 42.30 x 0.9 = 38.07, loss (47 - 38.07) x 2100
    assert sizing["limit_down_price"] == 38.07
    assert sizing["limit_down_loss"] == 18_753
    steps = "\n".join(sizing["steps"])
    assert "2,127.66" in steps and "2,100 股" in steps and "98,700" in steps and "9,870" in steps


def test_below_lot_does_not_pad_the_quantity():
    sizing = _size(1500.0)
    assert sizing["status"] == "below_lot"
    assert sizing["shares"] == 0 and sizing["amount"] == 0 and sizing["max_loss"] == 0
    assert "不足一手" in sizing["message"] and "不建议买入" in sizing["message"]


def test_no_capital_recorded_explains_instead_of_blaming_the_stock():
    sizing = _size(47.0, cash=0.0)
    assert sizing["status"] == "below_lot"
    assert "还没有录入资金" in sizing["message"]


def test_no_price():
    sizing = _size(None)
    assert sizing["status"] == "no_price" and sizing["shares"] == 0


@pytest.mark.parametrize(
    "raw,expected",
    [(2000.9, 2000), (250.7, 250), (200.0, 200), (199.9, 0), (0.0, 0), (-5.0, 0)],
)
def test_star_board_needs_200_then_one_share_steps(raw, expected):
    assert floor_to_lot(raw, "star") == expected


def test_main_board_rounds_down_to_100():
    assert floor_to_lot(2127.66, "main") == 2100
    assert floor_to_lot(99.0, "main") == 0
    assert floor_to_lot(2127.66, "chinext") == 2100
    assert floor_to_lot(2127.66, "bse") == 2100


def test_star_sizing_and_lot_in_the_payload():
    sizing = _size(50.0, board_market="star", cash=100_000.0)
    assert sizing["lot"] == 200 and sizing["shares"] == 2000
    thin = _size(50.0, board_market="star", cash=9_000.0)  # 180 shares: below the 200 minimum
    assert thin["status"] == "below_lot" and thin["lot"] == 200
    odd = _size(50.0, board_market="star", cash=10_300.0)  # 206 shares: any whole number above the minimum
    assert odd["shares"] == 206
    assert "最少 200" in "\n".join(odd["steps"])


@pytest.mark.parametrize(
    "code,exchange,expected",
    [("600519", None, "main"), ("000001", "main", "main"), ("002475", None, "main"), ("300750", None, "chinext"),
     ("301269", None, "chinext"), ("688981", None, "star"), ("689009", None, "star"), ("830799", None, "bse"),
     ("920748", None, "bse"), ("430047", None, "bse"), ("999999", "gem", "chinext")],
)
def test_classify_board(code, exchange, expected):
    assert classify_board(code, exchange) == expected


def test_limit_percentages_and_prices():
    assert limit_pct("main") == 10 and limit_pct("chinext") == 20 and limit_pct("star") == 20
    assert limit_pct("bse") == 30 and limit_pct("main", is_st=True) == 5
    assert limit_pct("chinext", is_st=True) == 20  # exchanges keep the board's limit for ST on ChiNext / STAR
    assert lot_size("main") == 100 and lot_size("star") == 200
    main = limit_prices(48.35, "main")
    assert (main["up"], main["down"]) == (53.19, 43.52)  # 48.35 x 1.1 = 53.185 -> half-up
    assert limit_prices(10.0, "main", is_st=True) == {"limit_pct": 5.0, "up": 10.5, "down": 9.5}
    assert limit_prices(None, "main")["up"] is None


def test_prices_round_half_up_not_bankers():
    assert round_price(2.675) == 2.68
    assert round_price(42.3 * 0.95) == 40.19  # 40.184999999999995 in binary floats
    assert round_price(0.005) == 0.01


def test_limit_down_uses_the_boards_limit():
    sizing = _size(47.0, board_market="chinext")
    assert sizing["limit_down_price"] == round_price(42.3 * 0.8)
    st = _size(47.0, is_st=True)
    assert st["limit_down_price"] == round_price(42.3 * 0.95)


def test_single_stock_cap_binds_before_total():
    rules = PositionRules(max_single_pct=50.0, max_single_pct_derived=False)
    sizing = _size(47.0, rules=rules)
    assert sizing["by_single"] == round(100_000 * 0.5 / 47, 2) and sizing["shares"] == 1000
    assert "单只上限" in sizing["steps"][1] and "50%" in sizing["steps"][1]


def test_held_shares_and_market_value_reduce_the_room():
    sizing = _size(47.0, cash=76_500.0, market_value=23_500.0, held_shares=500, held_count=1)
    assert sizing["status"] == "ok"  # adding to a held stock is not a new holding
    assert sizing["by_single"] == round(100_000 / 47 - 500, 2)
    assert sizing["by_total"] == round(76_500 / 47, 2)
    assert sizing["shares"] == 1600
    assert sizing["post_position_pct"] == round((23_500 + 1600 * 47) / 100_000 * 100, 2)


def test_total_cap_limits_the_position():
    rules = PositionRules(max_total_pct=60.0, max_single_pct=60.0)
    sizing = _size(47.0, rules=rules)
    assert sizing["shares"] == 1200 and sizing["by_total"] == round(60_000 / 47, 2)


def test_max_holdings_blocks_a_new_stock_only():
    blocked = _size(47.0, cash=0.0, market_value=100_000.0, held_count=1)
    assert blocked["status"] == "max_holdings" and blocked["shares"] == 0
    assert "1/1" in blocked["message"] and "换股" in blocked["message"]
    allowed = _size(47.0, rules=PositionRules(max_holdings=2, max_single_pct=50.0, max_single_pct_derived=False),
                    cash=50_000.0, market_value=50_000.0, held_count=1)
    assert allowed["status"] == "ok" and allowed["shares"] == 1000


def test_stop_fixed_keeps_the_percentage_and_only_warns():
    below = compute_stop(47.0, RULES, tech_support=40.0)
    assert below["price"] == 42.3 and "止损会在支撑之前触发" in below["warning"]
    broken = compute_stop(47.0, RULES, tech_support=46.9, close=46.0)
    assert broken["price"] == 42.3 and "技术形态转弱" in broken["warning"]
    quiet = compute_stop(47.0, RULES, tech_support=46.9, close=48.0)
    assert quiet["price"] == 42.3 and quiet["warning"] is None and quiet["tech_support"] == 46.9


def test_stop_nearer_takes_the_support_when_it_is_closer():
    rules = PositionRules(stop_mode="nearer")
    closer = compute_stop(47.0, rules, tech_support=45.0)
    assert closer["price"] == 45.0 and closer["mode"] == "nearer" and "技术支撑" in closer["formula"]
    farther = compute_stop(47.0, rules, tech_support=40.0)
    assert farther["price"] == 42.3 and "更远" in farther["formula"]
    ignored = compute_stop(47.0, rules, tech_support=48.0)  # a support above the buy price is meaningless
    assert ignored["price"] == 42.3
    none = compute_stop(47.0, rules, tech_support=None)
    assert none["price"] == 42.3 and "没有可用的技术支撑" in none["formula"]
    sizing = _size(47.0, rules=rules, tech_support=45.0)
    assert sizing["stop"]["price"] == 45.0 and sizing["max_loss"] == round((47 - 45) * sizing["shares"], 2)


def test_rules_from_config_derive_the_single_cap():
    derived = PositionRules.from_config(SimpleNamespace(
        position_stop_loss_pct=8, position_max_total_pct=80, position_max_holdings=2, position_max_single_pct=None,
        position_stop_mode="nearer"))
    assert derived.max_single_pct == 40 and derived.max_single_pct_derived is True
    assert derived.stop_loss_pct == 8 and derived.stop_mode == "nearer" and derived.max_holdings == 2
    explicit = PositionRules.from_config(SimpleNamespace(position_max_single_pct=30))
    assert explicit.max_single_pct == 30 and explicit.max_single_pct_derived is False
    assert explicit.to_dict()["max_single_pct_derived"] is False


def test_rules_from_config_fall_back_to_documented_defaults_on_bad_values():
    rules = PositionRules.from_config(SimpleNamespace(
        position_stop_loss_pct=0, position_max_total_pct=150, position_max_holdings="x", position_max_single_pct=500,
        position_stop_mode="loose"))
    assert (rules.stop_loss_pct, rules.max_total_pct, rules.max_holdings) == (10.0, 100.0, 1)
    assert rules.max_single_pct == 100 and rules.max_single_pct_derived is True and rules.stop_mode == "fixed"
    assert PositionRules.from_config(SimpleNamespace()).to_dict() == PositionRules().to_dict()


def test_config_parsers_for_position_keys():
    from src.config import _parse_optional_pct, _parse_stop_mode

    assert _parse_optional_pct("", field_name="X") is None
    assert _parse_optional_pct("35.5", field_name="X") == 35.5
    assert _parse_optional_pct("0", field_name="X") is None and _parse_optional_pct("abc", field_name="X") is None
    assert _parse_optional_pct("101", field_name="X") is None
    assert _parse_stop_mode(None) == "fixed" and _parse_stop_mode(" Nearer ") == "nearer"
    assert _parse_stop_mode("other") == "fixed"
