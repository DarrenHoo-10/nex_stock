# -*- coding: utf-8 -*-
"""Offline tests for board / market metric computation (docs/design/core-backend-design.md 2.6, 2.7)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from src.repositories.market_snapshot_repo import MarketSnapshotRepository
from src.services.board_common import MEMBERSHIP_OBSERVED_FROM, session_axis
from src.services.board_metrics_service import (
    CALC_VERSION,
    compound_pct,
    jaccard,
    midrank_percentile,
)
from src.services.market_snapshot_service import DC_MEMBER_START_DATE
from src.storage import BoardCatalog, BoardMembership, DatabaseManager
from tests.board_fixtures import END_DATE, World, thresholds, weekdays


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def _quality(row):
    return json.loads(row["quality"])


def _add_members(db, world, key, codes, valid_from=None):
    with db.session_scope() as session:
        for code in codes:
            session.add(BoardMembership(
                board_id=world.board_ids[key], stock_code=code, valid_from=valid_from or world.days[0],
                valid_to=None, origin="observed",
            ))
    world.members[key] = world.members[key] + list(codes)


# ----------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------
def test_helpers():
    assert midrank_percentile([1, 2, 3, 4], 4) == pytest.approx(87.5)
    assert midrank_percentile([1, 2, 3, 4], 1) == pytest.approx(12.5)
    assert midrank_percentile([5, 5], 5) == pytest.approx(50.0)
    assert compound_pct([10, -10]) == pytest.approx(-1.0)
    assert jaccard({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(0.5)
    assert jaccard(set(), {"a"}) == 0.0
    assert MEMBERSHIP_OBSERVED_FROM == DC_MEMBER_START_DATE


def test_session_axis_uses_calendar_gaps_or_falls_back():
    ingested = [date(2026, 11, 6), date(2026, 11, 4), date(2026, 11, 3)]  # 11-05 failed to ingest
    calendar = lambda s, e: [d for d in weekdays(date(2026, 11, 6), 30) if s <= d <= e]  # noqa: E731
    assert session_axis(date(2026, 11, 6), 4, ingested, calendar) == [
        date(2026, 11, 6), date(2026, 11, 5), date(2026, 11, 4), date(2026, 11, 3)]
    assert session_axis(date(2026, 11, 6), 4, ingested, None) == ingested
    assert session_axis(date(2026, 11, 6), 4, ingested, lambda s, e: None) == ingested
    # a calendar that does not list an ingested date is not trusted
    odd = lambda s, e: [date(2026, 11, 6), date(2026, 11, 5)]  # noqa: E731
    assert session_axis(date(2026, 11, 6), 4, ingested, odd) == ingested


# ----------------------------------------------------------------------
# board formulas and exclusions
# ----------------------------------------------------------------------
def test_board_metrics_formulas_and_exclusions(db):
    world = World(db, days=7)
    last, prev = world.days[-1], world.days[-2]
    extras = ["190001", "190002", "190003", "190004", "190005", "190006"]
    _add_members(db, world, "I0", extras)
    for day in world.days[:-1]:
        world.seed_day(day, extra=[
            {"code": c, "pct_chg": 0.5, "amount": 1e8} for c in extras
        ] if day != prev else [
            {"code": c, "pct_chg": 0.5, "amount": 1e8} for c in extras[:-1]
        ] + [{"code": "190006", "is_suspended": True, "close": None, "pct_chg": None, "amount": None}])
    first = world.members["I0"][:5]
    overrides = {
        first[0]: {"pct_chg": 10.0, "float_mv": 1e10, "amount": 4e8, "limit_state": "up"},
        first[1]: {"pct_chg": -2.0, "float_mv": 3e10, "amount": 1e8},
        first[2]: {"pct_chg": 1.0, "float_mv": 2e10, "amount": 2e8},
        first[3]: {"pct_chg": 0.0, "float_mv": 1e10, "amount": 1e8},
        first[4]: {"pct_chg": 4.0, "float_mv": 4e10, "amount": 3e8},
    }
    world.seed_day(last, overrides=overrides, extra=[
        {"code": "190001", "is_st": True, "pct_chg": 5.0},
        {"code": "190002", "is_suspended": True, "close": None, "pct_chg": None, "amount": None},
        {"code": "190003", "list_date": last - timedelta(days=1), "pct_chg": 30.0},
        {"code": "190004", "name": "某某退", "pct_chg": -3.0},
        {"code": "190005", "pct_chg": 0.5, "close": None},  # missing data
        {"code": "190006", "pct_chg": 8.0},  # suspended on the previous session -> resuming today
    ])
    world.compute_all()
    row = world.board_daily(last, "I0")
    weights = {
        first[0]: 1e10 / 1.10, first[1]: 3e10 / 0.98, first[2]: 2e10 / 1.01, first[3]: 1e10, first[4]: 4e10 / 1.04,
    }
    pcts = {first[0]: 10.0, first[1]: -2.0, first[2]: 1.0, first[3]: 0.0, first[4]: 4.0}
    expected_cap = sum(weights[c] * pcts[c] for c in first) / sum(weights.values())

    assert row["member_count"] == 11
    assert row["valid_member_count"] == 5
    assert row["excluded_reason"] is None
    assert row["ret_eq"] == pytest.approx(2.6)
    assert row["ret_cap"] == pytest.approx(expected_cap)
    assert row["excess_ret"] == pytest.approx(expected_cap)  # benchmark 0.0
    assert row["amount"] == pytest.approx(11e8)
    assert row["up_count"] == 3 and row["down_count"] == 1
    assert row["up_ratio"] == pytest.approx(60.0)
    assert row["drop7_ratio"] == pytest.approx(0.0)
    assert row["leader_code"] == first[0]
    assert row["top_amount_code"] == first[0]
    contrib4 = weights[first[4]] * 4.0 / sum(weights.values())  # large cap beats the biggest mover
    assert row["top_contrib_code"] == first[4]
    assert row["top_contrib_share"] == pytest.approx(contrib4 / expected_cap * 100.0)
    assert row["limit_up_count"] == 1 and row["limit_down_count"] == 0 and row["one_word_count"] == 0
    assert row["calc_version"] == CALC_VERSION
    quality = _quality(row)
    assert quality["exclusions"] == {
        "st": 1, "suspended": 1, "new_listing": 1, "delisting": 1, "missing_data": 1, "resumed": 1,
    }
    assert quality["member_origin"] == "observed"
    assert quality["age_known"] is False  # first seen on the first ingested day: true age unknown
    assert quality["top_contributors"][0]["code"] == first[4]


def test_stock_classification_rules(db):
    world = World(db, days=7)
    service = world.metrics()
    day = world.days[-1]
    for d in world.days:
        world.seed_day(d)
    axis = world.days[::-1]
    base = {"is_st": False, "is_suspended": False, "close": 10.0, "pct_chg": 1.0, "exchange_board": "main",
            "list_date": date(2010, 1, 1), "name": "正常股"}
    rows = [
        dict(base, code="000001"),
        dict(base, code="000002", is_st=True, name="*ST甲"),
        dict(base, code="000003", name="退市乙"),
        dict(base, code="000004", name="丙退"),
        dict(base, code="000005", list_date=axis[3]),  # 4th session of listing on the main board: still no limit
        dict(base, code="000006", list_date=axis[5]),  # 6th session: normal
        dict(base, code="688001", exchange_board="star", list_date=axis[4]),  # 5th session
        dict(base, code="920001", exchange_board="bse", list_date=axis[0]),  # first day
        dict(base, code="920002", exchange_board="bse", list_date=axis[1]),  # BSE: only the first day is unlimited
        dict(base, code="000007", pct_chg=None),
        dict(base, code="000008", close=None),
        dict(base, code="000009", list_date=None),  # unknown listing date is not guessed
    ]
    states = service._classify_stocks(day, rows, axis, set(axis))
    reasons = {code: reason for code, (_row, reason) in states.items()}
    assert reasons == {
        "000001": None, "000002": "st", "000003": "delisting", "000004": "delisting",
        "000005": "new_listing", "000006": None, "688001": "new_listing", "920001": "new_listing",
        "920002": None, "000007": "missing_data", "000008": "missing_data", "000009": None,
    }


def test_missing_inputs_are_null_never_zero(db):
    world = World(db, days=7)
    for day in world.days:
        world.seed_day(day, float_mv=None)  # daily_basic missing: no market cap, no weights
    world.compute_all()
    last = world.days[-1]
    row = world.board_daily(last, "I0")
    assert row["ret_eq"] == pytest.approx(0.0)  # plain mean exists (and happens to be 0.0 here)
    assert row["ret_cap"] is None
    assert row["excess_ret"] is None and row["rank_pct"] is None and row["excess_vs_median"] is None
    assert row["top_contrib_code"] is None and row["top_contrib_share"] is None
    assert "float_mv_missing" in _quality(row)["flags"]
    peer = world.board_daily(last, "I5")
    assert peer["ret_eq"] == pytest.approx(0.5) and peer["ret_cap"] is None
    # multi-day fields cannot be built from missing daily returns either
    assert row["ret_5d"] is None and row["excess_5d"] is None


def test_limit_fields_need_known_state_and_break_rate_has_sample_floor(db):
    world = World(db, days=7)
    for day in world.days[:-1]:
        world.seed_day(day)
    last = world.days[-1]
    codes = world.members["C0"]
    three_touched = {
        codes[0]: {"limit_state": "up", "touched_limit_up": True, "consecutive_up": 3},
        codes[1]: {"limit_state": "one_word_up", "touched_limit_up": True, "consecutive_up": 2},
        codes[2]: {"limit_state": "none", "touched_limit_up": True},
        codes[3]: {"limit_state": "down", "touched_limit_up": False},
    }
    world.seed_day(last, overrides=three_touched)
    world.compute_all()
    row = world.board_daily(last, "C0")
    assert row["limit_up_count"] == 2 and row["one_word_count"] == 1 and row["limit_down_count"] == 1
    assert row["touched_up_count"] == 3
    assert row["max_consecutive"] == 3
    assert row["break_rate"] is None  # fewer than 5 touched: not computed
    assert "break_rate_sample_low" in _quality(row)["notes"]

    five_touched = {
        codes[0]: {"limit_state": "up", "touched_limit_up": True, "consecutive_up": 1},
        codes[1]: {"limit_state": "up", "touched_limit_up": True, "consecutive_up": 1},
        codes[2]: {"limit_state": "up", "touched_limit_up": True, "consecutive_up": 1},
        codes[3]: {"limit_state": "none", "touched_limit_up": True},
        codes[4]: {"limit_state": "none", "touched_limit_up": True},
    }
    world.seed_day(last, overrides=five_touched)
    world.compute_all()
    row = world.board_daily(last, "C0")
    assert row["touched_up_count"] == 5 and row["break_rate"] == pytest.approx(40.0)

    # the sample floor is a threshold: lowering it computes the rate for 3 touched stocks
    world.seed_day(last, overrides={codes[0]: {"limit_state": "up", "touched_limit_up": True, "consecutive_up": 1},
                                    codes[1]: {"limit_state": "none", "touched_limit_up": True},
                                    codes[2]: {"limit_state": "none", "touched_limit_up": True}})
    world.compute_all(thresholds(break_min_touched=3))
    assert world.board_daily(last, "C0")["break_rate"] == pytest.approx(200.0 / 3)


def test_unknown_limit_state_is_null_not_zero(db):
    world = World(db, days=6)
    for day in world.days[:-1]:
        world.seed_day(day)
    last = world.days[-1]
    codes = world.members["C0"]
    world.seed_day(last, overrides={c: {"limit_state": None, "touched_limit_up": None} for c in codes})
    world.compute_all()
    row = world.board_daily(last, "C0")
    assert row["limit_up_count"] is None and row["touched_up_count"] is None and row["max_consecutive"] is None
    flags = _quality(row)["flags"]
    assert "limit_state_missing" in flags and "touched_limit_up_missing" in flags
    # other boards of the day keep their (known) zeros
    assert world.board_daily(last, "C1")["limit_up_count"] == 0


def test_rank_percentile_median_and_peer_pool_floor(db):
    world = World(db, days=7)
    for day in world.days[:-1]:
        world.seed_day(day)
    last = world.days[-1]
    world.seed_day(last, returns={"I0": 3.0}, benchmark=0.5)
    world.compute_all()
    target = world.board_daily(last, "I0")
    assert target["excess_ret"] == pytest.approx(2.5)
    assert target["rank_pct"] == pytest.approx((6 - 0.5) / 6 * 100)
    assert world.board_daily(last, "I5")["rank_pct"] == pytest.approx((5 - 0.5) / 6 * 100)
    assert world.board_daily(last, "I1")["rank_pct"] == pytest.approx(0.5 / 6 * 100)
    # median of the six daily returns [3.0, -0.3, -0.1, 0.1, 0.3, 0.5] is 0.2
    assert target["excess_vs_median"] == pytest.approx(2.8)
    # concept pool is ranked independently of the industry pool
    assert world.board_daily(last, "C0")["rank_pct"] == pytest.approx((3 - 0.5) / 6 * 100)


def test_peer_pool_below_floor_has_no_rank(db):
    world = World(db, days=4)
    with db.session_scope() as session:  # keep only 3 industry boards
        for key in ("I3", "I4", "I5"):
            session.query(BoardMembership).filter(BoardMembership.board_id == world.board_ids[key]).delete()
            session.query(BoardCatalog).filter(BoardCatalog.id == world.board_ids[key]).delete()
    for day in world.days:
        world.seed_day(day)
    world.compute_all()
    row = world.board_daily(world.days[-1], "I0")
    assert row["ret_cap"] is not None
    assert row["rank_pct"] is None and row["excess_vs_median"] is None
    assert any(f.startswith("peer_pool_small") for f in _quality(row)["flags"])
    # the concept pool still has six boards
    assert world.board_daily(world.days[-1], "C0")["rank_pct"] is not None


def test_five_day_amount_and_peak_fields(db):
    world = World(db, days=12)
    returns = [0.0, 0.0, 1.0, 2.0, -1.0, 3.0, 2.0, 1.0, 0.5, 0.2, 0.4, 0.6]
    multipliers = [1, 1, 5, 2, 2, 2, 2, 2, 1, 1, 1, 4]
    for index, day in enumerate(world.days):
        world.seed_day(day, returns={"I0": returns[index]}, amounts={"I0": multipliers[index]}, benchmark=0.5)
    world.compute_all()
    last = world.days[-1]
    row = world.board_daily(last, "I0")
    window = returns[-5:]
    expected_5d = compound_pct(window)
    assert row["ret_5d"] == pytest.approx(expected_5d)
    assert row["excess_5d"] == pytest.approx(expected_5d - compound_pct([0.5] * 5))
    unit = 5 * 1e8
    assert row["amount"] == pytest.approx(unit * 4)
    assert row["amount_ma5_prev"] == pytest.approx(unit * sum(multipliers[-6:-1]) / 5)
    assert row["amount_ratio_5d"] == pytest.approx(4 / (sum(multipliers[-6:-1]) / 5))
    assert row["amount_peak_20d"] == pytest.approx(unit * 5)  # the day-2 spike, 12 sessions available
    assert row["amount_share"] == pytest.approx(row["amount"] / (unit * 4 + 11 * unit))
    assert row["rank_5d_pct"] is not None

    early = world.board_daily(world.days[3], "I0")  # only 4 sessions of history
    assert early["ret_5d"] is None and early["amount_ma5_prev"] is None and early["amount_peak_20d"] is None
    assert "history_short:ret_5d" in _quality(early)["flags"]


def test_missing_session_in_window_nulls_multi_day_fields(db):
    world = World(db, days=9)
    skipped = world.days[5]
    for day in world.days:
        if day != skipped:
            world.seed_day(day, returns={"I0": 1.0})
    calendar = lambda s, e: [d for d in weekdays(END_DATE, 40) if s <= d <= e]  # noqa: E731
    world.metrics(calendar_fn=calendar).compute_range([d for d in world.days if d != skipped])
    row = world.board_daily(world.days[-1], "I0")
    assert row["ret_5d"] is None and row["amount_ma5_prev"] is None
    quality = _quality(row)
    assert skipped.isoformat() in quality["missing_sessions"]
    assert "ret_5d_incomplete" in quality["flags"]


def test_recompute_is_idempotent_and_preserves_vendor_columns(db):
    world = World(db, days=7)
    for day in world.days:
        world.seed_day(day, returns={"I0": 1.0})
    MarketSnapshotRepository(db).upsert_board_vendor(
        world.days[-1], [{"board_id": world.board_ids["I0"], "vendor_ret": 1.23, "vendor_amount": 9.9e9}]
    )
    world.compute_all()
    first = world.board_daily(world.days[-1], "I0")
    world.compute_all()
    second = world.board_daily(world.days[-1], "I0")
    first.pop("computed_at"), second.pop("computed_at")
    assert first == second
    assert second["vendor_ret"] == pytest.approx(1.23) and second["vendor_amount"] == pytest.approx(9.9e9)
    repo = world.repo()
    assert repo.count_computed_board_rows(world.days[-1], CALC_VERSION) == 12
    assert repo.count_computed_board_rows(world.days[-1], "0") == 0


def test_too_few_broad_and_cooling_exclusions(db):
    world = World(db, days=8)
    last = world.days[-1]
    world.per_board = 60
    world.add_board("C", 6, name="宽基概念")
    world.per_board = 5
    world.add_board("C", 7, first_seen=world.days[-3], name="新概念")  # first seen after the baseline
    for day in world.days:
        world.seed_day(day)
    th = thresholds(broad_max_members=50)
    world.compute_all(th)
    broad = world.board_daily(last, "C6")
    assert broad["excluded_reason"] == "broad" and broad["member_count"] == 60 and broad["ret_cap"] is None
    cooling = world.board_daily(last, "C7")
    assert cooling["excluded_reason"] == "cooling"
    assert cooling["ret_cap"] is not None and cooling["rank_pct"] is not None  # recorded and ranked
    assert _quality(cooling)["age_known"] is True and _quality(cooling)["age_days"] == 3
    assert world.board_daily(last, "C0")["excluded_reason"] is None  # first seen at the baseline: age unknown, not new

    world.compute_all(thresholds(min_members=6, broad_max_members=50))
    too_few = world.board_daily(last, "I0")
    assert too_few["excluded_reason"] == "too_few" and too_few["valid_member_count"] == 5 and too_few["ret_cap"] is None

    world.compute_all(thresholds(cooling_days=0, broad_max_members=50))
    assert world.board_daily(last, "C7")["excluded_reason"] is None


def test_board_without_membership_is_flagged_not_zero(db):
    world = World(db, days=6)
    world.add_board("C", 6, name="无成分")
    with db.session_scope() as session:
        session.query(BoardMembership).filter(BoardMembership.board_id == world.board_ids["C6"]).delete()
    for day in world.days:
        world.seed_day(day)
    world.compute_all()
    row = world.board_daily(world.days[-1], "C6")
    assert row["member_count"] == 0 and row["excluded_reason"] == "too_few"
    assert row["ret_cap"] is None and row["ret_eq"] is None and row["amount"] is None
    assert "no_membership" in _quality(row)["flags"]


def test_membership_is_resolved_as_of_the_trade_date(db):
    world = World(db, days=7)
    for day in world.days:
        world.seed_day(day)
    # a stock that left the board before the last day must not count on that day
    leaver = world.members["I1"][0]
    with db.session_scope() as session:
        session.query(BoardMembership).filter(
            BoardMembership.board_id == world.board_ids["I1"], BoardMembership.stock_code == leaver
        ).update({"valid_to": world.days[-3]})
    world.compute_all()
    assert world.board_daily(world.days[-3], "I1")["member_count"] == 5
    assert world.board_daily(world.days[-1], "I1")["member_count"] == 4


def test_market_daily_metrics(db):
    world = World(db, days=6)
    for day in world.days:
        world.seed_day(day)
    world.compute_all()
    last = world.days[-1]
    market = world.repo().get_market_daily(last)
    assert market["benchmark_ret"] == 0.0 and market["benchmark_code"] == "000985.CSI"  # ingestion value kept
    assert market["total_amount"] == pytest.approx(60 * 1e8)
    assert market["up_count"] == 30 and market["down_count"] == 20
    assert market["limit_up_count"] == 0 and market["limit_down_count"] == 0  # known state: a real zero
    assert market["touched_up_count"] == 0 and market["break_rate"] is None
    assert market["all_a_ret"] == pytest.approx(5.0 / 60)
    quality = market["quality"]["metrics"]
    assert quality["calc_version"] == CALC_VERSION and quality["valid_stock_count"] == 60
    assert "benchmark_missing" not in quality["flags"]


def test_market_daily_flags_missing_benchmark_and_limit_data(db):
    world = World(db, days=6)
    for day in world.days:
        world.seed_day(day, benchmark=None, overrides={c: {"limit_state": None} for codes in world.members.values() for c in codes})
    world.compute_all()
    market = world.repo().get_market_daily(world.days[-1])
    assert market["benchmark_ret"] is None
    assert market["limit_up_count"] is None and market["limit_down_count"] is None
    flags = market["quality"]["metrics"]["flags"]
    assert "benchmark_missing" in flags and "limit_state_missing" in flags
    assert world.board_daily(world.days[-1], "I0")["excess_ret"] is None


def test_concept_merge_groups(db):
    world = World(db, days=6)
    world.per_board = 1
    world.add_board("C", 6, name="近似概念")
    world.share_members("C4", "C6", 5)  # 5 shared of 6 -> Jaccard 5/6
    for day in world.days:
        world.seed_day(day)
    world.compute_all()
    report = world.metrics().compute_trade_date(world.days[-1])
    assert report.merge_groups == 1
    boards = {b["id"]: b for b in world.repo().list_catalog(["concept"])}
    group = boards[world.board_ids["C6"]]["merge_group"]
    assert group == world.board_refs["C6"]  # anchored on the larger board (6 members vs 5)
    assert boards[world.board_ids["C4"]]["merge_group"] == group
    assert boards[world.board_ids["C1"]]["merge_group"] is None
    assert boards[world.board_ids["C4"]]["merge_computed_on"] == world.days[-1]

    # a lower threshold never merges unrelated boards, a stricter one splits the pair
    world.metrics(thresholds(merge_jaccard=0.95)).compute_trade_date(world.days[-1])
    boards = {b["id"]: b for b in world.repo().list_catalog(["concept"])}
    assert boards[world.board_ids["C6"]]["merge_group"] is None


def test_ensure_history_computes_missing_earlier_days_once(db):
    world = World(db, days=8)
    for day in world.days:
        world.seed_day(day)
    service = world.metrics()
    computed = service.ensure_history(world.days[-1])
    assert computed == world.days[:-1]
    assert service.ensure_history(world.days[-1]) == []  # already at the current version
    assert world.board_daily(world.days[-2], "I0")["calc_version"] == CALC_VERSION


def test_compute_trade_date_without_stock_rows(db):
    world = World(db, days=3)
    report = world.metrics().compute_trade_date(date(2026, 11, 9))
    assert report.status == "no_data"
