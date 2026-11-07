# -*- coding: utf-8 -*-
"""Offline tests for board anomaly signals (docs/design/core-product-spec.md 3.2 / 3.3).

Every scenario seeds raw ``market_stock_daily`` rows, runs the real metric computation and then the
signal service, so thresholds, protection rules and the three-valued data-gap logic are exercised
end to end.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from src.services.board_signal_service import (
    BoardSignalService,
    t_and,
    t_at_least,
    t_not,
    t_or,
)
from src.services.board_thresholds import (
    DEFAULT_THRESHOLDS,
    describe_thresholds,
    effective_thresholds,
    overridden_keys,
    parse_overrides,
    resolve_thresholds,
    validate_overrides,
)
from src.storage import DatabaseManager
from tests.board_fixtures import World, thresholds


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def build(db, days: int, plan: Dict[int, Dict[str, Any]], target: str = "I0") -> World:
    """Seed ``days`` sessions; ``plan[i]`` scripts day ``i`` of the target board.

    Keys: ``ret`` / ``amt`` (target return, turnover multiplier), ``returns`` (extra board returns),
    ``overrides`` (per-stock row fields), ``bench`` (benchmark return).
    """
    world = World(db, days=days)
    for index, day in enumerate(world.days):
        spec = plan.get(index, {})
        returns = {target: spec.get("ret", 0.0)}
        returns.update(spec.get("returns", {}))
        amounts = {target: spec.get("amt", 1.0)}
        amounts.update(spec.get("amounts", {}))
        overrides = spec.get("overrides")
        if callable(overrides):  # lets a plan refer to the world's deterministic stock codes
            overrides = overrides(world)
        world.seed_day(day, returns=returns, amounts=amounts, overrides=overrides, benchmark=spec.get("bench", 0.0))
    return world


def limit_up(world: World, key: str, count: int, streak: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    return {
        code: {"limit_state": "up", "touched_limit_up": True, "consecutive_up": streak or 1}
        for code in world.members[key][:count]
    }


def conditions(signal: Dict[str, Any], state: str) -> Dict[str, Dict[str, Any]]:
    return {c["key"]: c for c in signal["evidence"]["evaluations"][state]["conditions"]}


# ----------------------------------------------------------------------
# three-valued logic
# ----------------------------------------------------------------------
def test_three_valued_logic():
    assert t_and(True, None) is None and t_and(False, None) is False and t_and(True, True) is True
    assert t_or(False, None) is None and t_or(True, None) is True and t_or(False, False) is False
    assert t_not(None) is None and t_not(True) is False
    assert t_at_least([True, None, False], 2) is None
    assert t_at_least([True, True, None], 2) is True
    assert t_at_least([True, False, False], 2) is False


# ----------------------------------------------------------------------
# first launch
# ----------------------------------------------------------------------
def test_first_launch_with_evidence_and_snapshot_of_inputs(db):
    world = build(db, 11, {10: {"ret": 3.0, "amt": 2.0}})
    signal = world.judge(world.days[10], "I0")
    assert signal["signal_type"] == "first_launch"
    assert signal["flags"]["tags"] == []
    assert signal["driver"] == "unknown"  # 2.0x is below the strong-volume threshold: no driver claim
    assert "放量启动" in signal["reason"] and "2.0" in signal["reason"]
    evidence = signal["evidence"]
    assert evidence["state"] == "first_launch" and evidence["evaluations"]["first_launch"]["result"] is True
    conds = conditions(signal, "first_launch")
    assert conds["strong_day"]["met"] is True
    assert conds["amount_ratio"]["actual"] == pytest.approx(2.0) and conds["amount_ratio"]["threshold"] == 1.5
    assert conds["breadth"]["actual"] == pytest.approx(100.0) and conds["breadth"]["threshold"] == 60.0
    assert "limit_up_effect" not in conds  # industry boards do not use limit-up conditions
    assert evidence["inputs"]["excess_ret"] == pytest.approx(3.0)
    assert evidence["thresholds_used"]["amount_ratio_mild"] == 1.5
    assert any("成交额" in line for line in evidence["lines"])
    assert evidence["data_gaps"] == []


def test_first_launch_respects_threshold_overrides(db):
    world = build(db, 11, {10: {"ret": 3.0, "amt": 2.0}})
    signal = world.judge(world.days[10], "I0", thresholds(amount_ratio_mild=2.5, amount_ratio_strong=3.0))
    assert signal["signal_type"] == "none"
    cond = conditions(signal, "first_launch")["amount_ratio"]
    assert cond["met"] is False and cond["threshold"] == 2.5
    assert signal["evidence"]["thresholds_used"]["amount_ratio_mild"] == 2.5


def test_first_launch_capital_driver_needs_strong_volume(db):
    world = build(db, 11, {10: {"ret": 3.0, "amt": 3.0}})
    signal = world.judge(world.days[10], "I0")
    assert signal["signal_type"] == "first_launch"
    assert signal["driver"] == "capital" and "资金推动" in signal["evidence"]["driver"]["text"]


def test_short_history_is_insufficient_not_unmet(db):
    world = build(db, 5, {4: {"ret": 3.0, "amt": 2.0}})
    signal = world.judge(world.days[4], "I0")
    assert signal["signal_type"] == "insufficient_history"
    evaluation = signal["evidence"]["evaluations"]["first_launch"]
    assert evaluation["result"] is None  # undetermined, not False
    assert "first_launch" in signal["evidence"]["undetermined"]
    assert signal["evidence"]["data_gaps"] and "历史数据不足" in signal["reason"]
    assert conditions(signal, "first_launch")["amount_ratio"]["met"] is None


def test_quiet_board_with_short_history_stays_none(db):
    world = build(db, 5, {})
    signal = world.judge(world.days[4], "I0")
    assert signal["signal_type"] == "none" and signal["flags"]["tags"] == []  # not notable: nothing to report


def test_prior_strong_day_blocks_first_launch(db):
    world = build(db, 13, {9: {"ret": 3.0, "amt": 2.0}, 12: {"ret": 3.0, "amt": 2.0}})
    signal = world.judge(world.days[12], "I0")
    assert signal["signal_type"] != "first_launch"
    assert conditions(signal, "first_launch")["no_prior_strong_day"]["met"] is False


# ----------------------------------------------------------------------
# strengthening / mainline
# ----------------------------------------------------------------------
def test_strengthening(db):
    plan = {10: {"ret": 3.0, "amt": 2.0}, 11: {"ret": 2.5, "amt": 2.0}, 12: {"ret": 2.0, "amt": 2.0}}
    world = build(db, 13, plan)
    signal = world.judge(world.days[12], "I0")
    assert signal["signal_type"] == "strengthening"
    assert "连续 3 日跑赢基准" in signal["reason"]
    conds = conditions(signal, "strengthening")
    assert conds["strong_days"]["actual"] == 3 and conds["outperform_days"]["met"] is True
    assert conds["share_not_falling"]["met"] is True and conds["rank_5d"]["met"] is True
    assert signal["flags"]["tags"] == []  # industries are never tagged as mainline


def test_strengthening_threshold_override_changes_the_outcome(db):
    plan = {10: {"ret": 3.0, "amt": 2.0}, 11: {"ret": 2.5, "amt": 2.0}, 12: {"ret": 2.0, "amt": 2.0}}
    world = build(db, 13, plan)
    signal = world.judge(world.days[12], "I0", thresholds(confirm={"days": 4, "strong_days": 3}))
    assert signal["signal_type"] != "strengthening"  # day 9 did not outperform
    assert conditions(signal, "strengthening")["outperform_days"]["met"] is False


def test_concept_mainline_tag(db):
    plan = {i: {"ret": 3.0, "amt": 2.0} for i in range(8, 13)}
    plan[11]["overrides"] = lambda w: limit_up(w, "C0", 5, streak=2)
    plan[12]["overrides"] = lambda w: limit_up(w, "C0", 5, streak=3)
    world = build(db, 13, plan, target="C0")
    signal = world.judge(world.days[12], "C0")
    assert signal["signal_type"] == "strengthening"
    assert "mainline" in signal["flags"]["tags"]
    # a higher bar on consecutive boards removes the tag but not the state
    stricter = world.judge(world.days[12], "C0", thresholds(mainline={"max_consecutive": 5}))
    assert stricter["signal_type"] == "strengthening" and "mainline" not in stricter["flags"]["tags"]


# ----------------------------------------------------------------------
# divergence / ebbing
# ----------------------------------------------------------------------
def test_concept_divergence_with_support_tag(db):
    plan = {i: {"ret": 3.0, "amt": 2.0} for i in range(8, 13)}
    plan[11]["overrides"] = lambda w: limit_up(w, "C0", 5, streak=2)
    plan[12]["overrides"] = lambda w: limit_up(w, "C0", 5, streak=3)

    def day13(world):  # one limit-up stock, the rest down; turnover not below the previous day
        codes = world.members["C0"]
        rows = {codes[0]: {"pct_chg": 10.0, "limit_state": "up", "touched_limit_up": True, "consecutive_up": 4}}
        rows.update({c: {"pct_chg": -2.0} for c in codes[1:]})
        return rows

    plan[13] = {"ret": 0.0, "amt": 2.0, "overrides": day13}
    world = build(db, 14, plan, target="C0")
    signal = world.judge(world.days[13], "C0")
    assert signal["signal_type"] == "divergence"
    assert "supported" in signal["flags"]["tags"]
    conds = conditions(signal, "divergence")
    assert conds["was_strong"]["met"] is True
    assert conds["limit_up_drop"]["met"] is True and conds["excess_or_breadth"]["met"] is True
    assert conds["weakness_conditions"]["actual"] >= 2


def test_industry_divergence_volume_stall(db):
    plan = {i: {"ret": 3.0, "amt": 2.0} for i in range(10, 13)}
    plan[13] = {"ret": -0.3, "amt": 4.0}  # 2.5x the previous 5-day mean: volume up, price not
    world = build(db, 14, plan)
    signal = world.judge(world.days[13], "I0")
    assert signal["signal_type"] == "divergence"
    assert "supported" not in signal["flags"]["tags"]  # industries have no limit-up based tags
    assert "放量滞涨" in signal["reason"]


def test_ebbing_needs_two_consecutive_days_and_beats_divergence(db):
    plan = {i: {"ret": 3.0, "amt": 2.0} for i in range(10, 13)}
    plan[13] = {"ret": -1.0, "amt": 4.0}
    plan[14] = {"ret": -1.0, "amt": 0.8}
    world = build(db, 15, plan)
    one_day = world.judge(world.days[13], "I0")
    assert one_day["signal_type"] == "divergence"  # first weak day: still a divergence
    signal = world.judge(world.days[14], "I0")
    assert signal["signal_type"] == "ebbing"
    conds = conditions(signal, "ebbing")
    assert conds["recently_strong"]["met"] is True
    assert conds["decay_day_0"]["met"] is True and conds["decay_day_1"]["met"] is True


# ----------------------------------------------------------------------
# abnormal drop
# ----------------------------------------------------------------------
def test_abnormal_drop_industry(db):
    world = build(db, 18, {17: {"ret": -3.0, "amt": 2.0}})
    signal = world.judge(world.days[17], "I0")
    assert signal["signal_type"] == "abnormal_drop"
    assert "放量下跌" in signal["reason"]
    assert conditions(signal, "abnormal_drop")["weak_day"]["met"] is True


def test_abnormal_drop_concept_needs_limit_down_or_deep_losers(db):
    quiet = build(db, 18, {17: {"ret": -3.0, "amt": 2.0}}, target="C0")
    assert quiet.judge(quiet.days[17], "C0")["signal_type"] == "none"  # a plain -3% day is not enough


def test_abnormal_drop_concept_deep_losers(db):
    # three of five stocks down 8% (60% > 30%): the deep-loser branch fires
    plan = {17: {"ret": -3.0, "amt": 2.0, "overrides": lambda w: {c: {"pct_chg": -8.0} for c in w.members["C0"][:3]}}}
    deep = build(db, 18, plan, target="C0")
    signal = deep.judge(deep.days[17], "C0")
    assert signal["signal_type"] == "abnormal_drop"
    assert conditions(signal, "abnormal_drop")["limit_down_or_drop7"]["met"] is True


# ----------------------------------------------------------------------
# protection rules
# ----------------------------------------------------------------------
def _broad_plan(last: int) -> Dict[int, Dict[str, Any]]:
    others = {f"I{i}": 2.0 + 0.1 * i for i in range(1, 6)}
    others.update({f"C{i}": 2.0 + 0.1 * i for i in range(1, 6)})
    others.update({"I0": 2.2})
    return {last: {"ret": 3.0, "amt": 2.0, "returns": others}}


def test_broad_rally_protection_drops_limit_up_and_breadth_conditions(db):
    last = 13
    world = build(db, 14, _broad_plan(last), target="C0")
    signal = world.judge(world.days[last], "C0")
    assert signal["evidence"]["broad_rally"] is True
    assert signal["signal_type"] == "first_launch"  # no limit-ups, yet reported: rank decides on a broad rally
    conds = conditions(signal, "first_launch")
    assert conds["limit_up_effect"]["skipped"] == "普涨日保护" and conds["breadth"]["skipped"] == "普涨日保护"
    batch = signal["_batch"]
    assert batch.broad_rally is True


def test_same_board_without_broad_rally_needs_limit_ups(db):
    last = 13
    plan = {last: {"ret": 3.0, "amt": 2.0}}
    world = build(db, 14, plan, target="C0")
    signal = world.judge(world.days[last], "C0")
    assert signal["evidence"]["broad_rally"] is False
    assert signal["signal_type"] == "none"
    assert conditions(signal, "first_launch")["limit_up_effect"]["met"] is False


def test_single_stock_driven_rise_is_not_reported(db):
    def overrides(world):
        codes = world.members["I0"]
        rows = {codes[0]: {"pct_chg": 20.0}}
        rows.update({c: {"pct_chg": 0.5} for c in codes[1:]})
        return rows

    world = build(db, 14, {13: {"ret": 0.0, "amt": 2.0, "overrides": overrides}})
    signal = world.judge(world.days[13], "I0")
    assert signal["signal_type"] == "none"
    assert signal["flags"]["tags"] == ["single_stock"] and signal["flags"]["blocked_by"] == "single_stock"
    assert "单只股票" in signal["reason"]
    assert conditions(signal, "first_launch")["not_single_stock"]["met"] is False
    assert world.board_daily(world.days[13], "I0")["top_contrib_share"] > 50
    # a looser threshold lets the same data through
    allowed = world.judge(world.days[13], "I0", thresholds(single_stock_share_pct=95))
    assert allowed["signal_type"] == "first_launch" and allowed["flags"]["tags"] == []


def test_new_concept_cooling_period(db):
    world = World(db, days=14)
    world.add_board("C", 7, first_seen=world.days[2], name="新概念", member_from=world.days[2])
    returns_last = {"C7": 3.0}
    for index, day in enumerate(world.days):
        last = index == 13
        world.seed_day(
            day,
            returns=returns_last if last else None,
            amounts={"C7": 2.0} if last else None,
            overrides=limit_up(world, "C7", 3) if last else None,
        )
    th = thresholds(cooling_days=15)
    signal = world.judge(world.days[13], "C7", th)
    assert signal["signal_type"] == "insufficient_history"
    assert signal["flags"]["tags"] == ["cooling"] and signal["flags"]["blocked_by"] == "cooling"
    assert "冷却期" in signal["reason"]
    assert signal["evidence"]["board"]["age_known"] is True
    # once the cooling period is over the same data is a normal first launch
    released = world.judge(world.days[13], "C7", thresholds(cooling_days=5))
    assert released["signal_type"] == "first_launch" and "cooling" not in released["flags"]["tags"]


# ----------------------------------------------------------------------
# data gaps and excluded boards
# ----------------------------------------------------------------------
def test_missing_key_inputs_are_reported_as_insufficient_with_gaps(db):
    world = World(db, days=8)
    for index, day in enumerate(world.days):
        world.seed_day(day, float_mv=None if index == 7 else 1e10)
    signal = world.judge(world.days[7], "I0")
    assert signal["signal_type"] == "insufficient_history"
    assert signal["flags"]["core_missing"] is True
    assert any("流通市值" in gap for gap in signal["evidence"]["data_gaps"])
    assert signal["_batch"].core_missing == 12


def test_excluded_boards_are_not_judged(db):
    world = build(db, 11, {10: {"ret": 3.0, "amt": 2.0}})
    signal = world.judge(world.days[10], "I0", thresholds(min_members=6))
    assert signal["signal_type"] == "none" and signal["flags"]["excluded"] == "too_few"
    assert "不参与判定" in signal["reason"]
    assert signal["_batch"].not_evaluated == {"too_few": 12}


def test_board_without_members_is_reported_as_not_judged(db):
    from src.storage import BoardMembership

    world = World(db, days=6)
    world.add_board("C", 6, name="无成分")
    with db.session_scope() as session:
        session.query(BoardMembership).filter(BoardMembership.board_id == world.board_ids["C6"]).delete()
    for day in world.days:
        world.seed_day(day)
    signal = world.judge(world.days[5], "C6")
    assert signal["flags"]["excluded"] == "no_membership" and "暂无成分数据" in signal["reason"]


def test_missing_benchmark_makes_excess_unavailable(db):
    world = World(db, days=8)
    for index, day in enumerate(world.days):
        world.seed_day(day, returns={"I0": 3.0}, benchmark=None)
    signal = world.judge(world.days[7], "I0")
    assert signal["signal_type"] == "insufficient_history"
    assert any("基准指数" in gap for gap in signal["evidence"]["data_gaps"])


# ----------------------------------------------------------------------
# merge groups
# ----------------------------------------------------------------------
def test_merged_concepts_show_one_representative(db):
    world = World(db, days=14)
    world.per_board = 1
    world.add_board("C", 6, name="近似概念")
    world.share_members("C4", "C6", 5)  # C6 = C4 plus one own stock
    shared = limit_up(world, "C4", 3)
    own = world.members["C6"][0]
    for index, day in enumerate(world.days):
        last = index == 13
        overrides = dict(shared, **{own: {"pct_chg": 4.0}}) if last else None
        world.seed_day(day, returns={"C4": 3.0, "C6": 3.0} if last else None,
                       amounts={"C4": 2.0, "C6": 2.0} if last else None, overrides=overrides)
    signal_c6 = world.judge(world.days[13], "C6")
    by_board = {s["board_id"]: s for s in signal_c6["_batch"].signals}
    sig4 = by_board[world.board_ids["C4"]]
    assert signal_c6["signal_type"] == "first_launch"  # the stronger of the pair is shown
    assert signal_c6["flags"]["merge"]["role"] == "representative"
    assert signal_c6["flags"]["merge"]["members"] == [world.board_refs["C4"], world.board_refs["C6"]]
    assert sig4["flags"]["merge"]["role"] == "member"
    assert sig4["flags"]["merge"]["representative"] == world.board_refs["C6"]
    assert "merged" in signal_c6["flags"]["tags"] and "merged" not in sig4["flags"]["tags"]


# ----------------------------------------------------------------------
# determinism / thresholds module
# ----------------------------------------------------------------------
def test_signal_evaluation_is_deterministic(db):
    world = build(db, 11, {10: {"ret": 3.0, "amt": 2.0}})
    world.compute_all()
    service = world.signals()
    first = service.evaluate_trade_date(world.days[10])
    second = service.evaluate_trade_date(world.days[10])
    assert first.signals == second.signals
    assert first.counts == second.counts and first.counts["first_launch"] == 1


def test_signal_service_uses_config_overrides_and_reports_snapshot(db, monkeypatch):
    from types import SimpleNamespace

    import src.config as config_module

    monkeypatch.setattr(config_module, "get_config",
                        lambda: SimpleNamespace(board_anomaly_thresholds='{"amount_ratio_mild": 2.0}'))
    world = World(db, days=2)
    service = BoardSignalService(repo=world.repo())
    assert service.thresholds["amount_ratio_mild"] == 2.0
    snapshot = service.thresholds_snapshot()
    assert snapshot["overridden"] == ["amount_ratio_mild"] and snapshot["values"]["amount_ratio_strong"] == 2.5
    assert snapshot["calc_version"]


def test_threshold_defaults_match_the_spec_table():
    d = DEFAULT_THRESHOLDS
    assert d["amount_ratio_mild"] == 1.5 and d["amount_ratio_strong"] == 2.5
    assert d["rank_strong_top_pct"] == 10 and d["rank_weak_bottom_pct"] == 10
    assert d["excess_floor"] == {"industry": 1.5, "concept": 2.5}
    assert d["breadth_pct"] == {"strong": 70, "weak": 30}
    assert d["limit_up_effect"] == 3
    assert d["mainline"] == {"limit_up": 5, "days": 2, "max_consecutive": 3, "min_days": 5}
    assert d["break_rate_warn"] == 40 and d["break_min_touched"] == 5
    assert d["confirm"] == {"days": 3, "strong_days": 2}
    assert d["ebb_peak_pct"] == 50 and d["broad_rally_pct"] == 2
    assert d["min_members"] == 10 and d["broad_max_members"] == 800
    assert d["single_stock_share_pct"] == 50 and d["cooling_days"] == 10 and d["merge_jaccard"] == 0.6


def test_threshold_validation_and_overrides():
    assert validate_overrides({}) == []
    assert validate_overrides({"amount_ratio_mild": 2.0, "excess_floor": {"concept": 3}}) == []
    errors = validate_overrides({
        "amount_ratio_mild": 0.5, "foo": 1, "excess_floor": {"bogus": 1}, "limit_up_effect": 2.5,
        "break_rate_warn": True, "rank_strong_top_pct": "10", "mainline": 5, "min_members": 1000,
    })
    text = "；".join(errors)
    assert "amount_ratio_mild 必须大于 1" in text and "未知阈值 foo" in text and "excess_floor 没有字段 bogus" in text
    assert "limit_up_effect 必须为整数" in text and "break_rate_warn 必须为数值" in text
    assert "rank_strong_top_pct 必须为数值" in text and "mainline 必须是对象" in text
    assert "min_members 不能大于 500" in text
    assert "amount_ratio_strong 不能小于 amount_ratio_mild" in validate_overrides({"amount_ratio_mild": 4.0})
    assert any("breadth_pct" in e for e in validate_overrides({"breadth_pct": {"weak": 80}}))
    assert any("confirm" in e for e in validate_overrides({"confirm": {"strong_days": 5}}))
    assert validate_overrides({"amount_ratio_mild": float("nan")}) != []


def test_effective_thresholds_partial_dict_override_and_description():
    effective = effective_thresholds({"excess_floor": {"concept": 3.0}, "min_members": 20.0, "bogus": 1, "break_rate_warn": "x"})
    assert effective["excess_floor"] == {"industry": 1.5, "concept": 3.0}
    assert effective["min_members"] == 20 and isinstance(effective["min_members"], int)
    assert effective["break_rate_warn"] == 40  # invalid value ignored
    assert overridden_keys(effective) == {"excess_floor", "min_members"}
    items = {i["key"]: i for i in describe_thresholds(effective)}
    assert items["excess_floor"]["overridden"] is True and items["excess_floor"]["default"] == {"industry": 1.5, "concept": 2.5}
    assert items["amount_ratio_mild"]["overridden"] is False
    assert items["amount_ratio_mild"]["evidence"] == "medium" and items["rank_weak_bottom_pct"]["evidence"] == "inferred"
    assert items["amount_ratio_mild"]["display"] == "成交额 ≥ 近 5 日均值 1.5 倍"
    assert items["excess_floor"]["display"] == "行业 +1.5%，概念 +3%"
    assert all(i["evidence"] in ("medium", "low", "inferred") for i in items.values())


def test_stored_threshold_text_degrades_to_defaults():
    assert parse_overrides("") == ({}, [])
    assert parse_overrides(None) == ({}, [])
    overrides, warnings = parse_overrides("{not json")
    assert overrides == {} and warnings
    overrides, warnings = parse_overrides("[1, 2]")
    assert overrides == {} and warnings
    effective, warnings = resolve_thresholds('{"amount_ratio_mild": 0.2}')
    assert effective == effective_thresholds({}) and warnings and "被忽略" in warnings[0]
    effective, warnings = resolve_thresholds('{"amount_ratio_mild": 2.0}')
    assert effective["amount_ratio_mild"] == 2.0 and warnings == []
