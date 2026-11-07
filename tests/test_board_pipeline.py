# -*- coding: utf-8 -*-
"""Offline tests for the board pipeline entry (previous -> membership -> ingest -> metrics -> signals).

Most tests replace the Tushare-facing ingestion service with a recording fake; metrics, signals, run
bookkeeping and persistence are real. The end-to-end tests at the bottom drive the real ingestion
service against an in-memory fake of the Tushare SDK (``TushareFetcher._api``) through two 17:00 runs.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest
from sqlalchemy import select

import data_provider.tushare_fetcher as tushare_fetcher_module
from data_provider.tushare_fetcher import TushareFetcher
from src.repositories.board_analysis_repo import BoardAnalysisRepository
from src.repositories.market_snapshot_repo import MarketSnapshotRepository
from src.services.board_metrics_service import BoardMetricsService
from src.services.board_pipeline import run_board_pipeline
from src.services.board_signal_service import BoardSignalService
from src.services.market_snapshot_service import MarketSnapshotService
from src.storage import BoardDaily, BoardSignal, DailyRun, DatabaseManager, MarketStockDaily, utc_naive_now
from tests.board_fixtures import World, thresholds
from tests.test_market_snapshot import (
    D1,
    D2,
    D3,
    _EVENING_D2,
    _EVENING_DAILY_COLS,
    _board_members,
    _concat,
    _evening_daily,
    build_evening_api,
)


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


class FakeIngest:
    """Records what the pipeline asks the ingestion service for."""

    def __init__(self, status: str = "complete", missing: Optional[List[str]] = None, error: Optional[Exception] = None,
                 previous_status: str = "skipped", previous_error: Optional[Exception] = None,
                 on_previous: Optional[Callable[[date], None]] = None,
                 membership: Optional[Dict[str, Any]] = None, derived: Optional[List[str]] = None):
        self.status = status
        self.missing = missing or []
        self.error = error
        self.previous_status = previous_status
        self.previous_error = previous_error
        self.on_previous = on_previous
        self.membership = membership if membership is not None else {"status": "skipped", "reason": "已折叠"}
        self.derived = derived or []
        self.ingest_calls: List[tuple] = []
        self.member_calls: List[date] = []

    def ingest_trade_date(self, trade_date, *, force=False, allow_derived=False, boards=True):
        self.ingest_calls.append((trade_date, allow_derived, boards))
        is_previous = boards  # the pipeline completes D-1 with boards and ingests D without
        error = self.previous_error if is_previous else self.error
        if error is not None:
            raise error
        status = self.previous_status if is_previous else self.status
        if is_previous and self.on_previous is not None and status != "skipped":
            self.on_previous(trade_date)
        report = SimpleNamespace(
            status=status, stock_rows=60, missing=[] if is_previous else self.missing, incomplete=[],
            derived=[] if is_previous else self.derived,
        )
        report.to_dict = lambda: {"status": status, "stock_rows": 60, "missing": report.missing}
        return report

    def refresh_membership_for_day(self, trade_date):
        self.member_calls.append(trade_date)
        return self.membership


def seeded_world(db, days: int = 11) -> World:
    world = World(db, days=days)
    for index, day in enumerate(world.days):
        last = index == days - 1
        world.seed_day(day, returns={"I0": 3.0} if last else None, amounts={"I0": 2.0} if last else None)
    return world


def calendar_of(world: World) -> Callable[[date, date], List[date]]:
    return lambda start, end: [d for d in world.days if start <= d <= end]


def run(world: World, day: Optional[date] = None, **kwargs: Any) -> Dict[str, Any]:
    th = thresholds()
    repo = world.repo()
    return run_board_pipeline(
        day or world.days[-1], repo=repo, calendar_fn=calendar_of(world),
        metrics_service=world.metrics(th), signal_service=world.signals(th), **kwargs,
    )


def runs(db) -> List[DailyRun]:
    with db.get_session() as session:
        rows = session.execute(select(DailyRun).order_by(DailyRun.id)).scalars().all()
        for row in rows:
            session.expunge(row)
    return rows


def signal_rows(db, run_id: int) -> List[BoardSignal]:
    with db.get_session() as session:
        rows = session.execute(select(BoardSignal).where(BoardSignal.run_id == run_id)).scalars().all()
        for row in rows:
            session.expunge(row)
    return rows


STEP_KEYS = ["previous", "membership", "ingest", "metrics", "signals"]


def test_full_run_writes_steps_thresholds_and_one_signal_per_board(db):
    world = seeded_world(db)
    ingest = FakeIngest()
    result = run(world, ingest_service=ingest)
    assert result["status"] == "succeeded" and result["error"] is None
    assert [s["key"] for s in result["steps"]] == STEP_KEYS
    # D-1 is already final and its members are folded: both are "skipped" no-ops, the rest ran
    assert [s["status"] for s in result["steps"]] == ["skipped", "skipped", "succeeded", "succeeded", "succeeded"]
    prev, day = world.days[-2], world.days[-1]
    # D-1 is completed with boards (derivation allowed); D is ingested without boards (dc_* come at 20:30)
    assert ingest.ingest_calls == [(prev, True, True), (day, True, False)]
    assert ingest.member_calls == [prev]

    [stored] = runs(db)
    assert stored.id == result["run_id"] and stored.status == "succeeded" and stored.finished_at is not None
    steps = json.loads(stored.steps)
    assert [s["key"] for s in steps] == STEP_KEYS
    assert all({"label", "status", "finished_at", "detail"} <= set(s) for s in steps)
    snapshot = json.loads(stored.thresholds_snapshot)
    assert snapshot["values"]["amount_ratio_mild"] == 1.5 and snapshot["values"]["min_members"] == 3
    assert snapshot["overridden"] == ["min_members"] and snapshot["calc_version"]

    signals = signal_rows(db, stored.id)
    assert len(signals) == 12
    by_board = {s.board_id: s for s in signals}
    target = by_board[world.board_ids["I0"]]
    assert target.signal_type == "first_launch" and target.reason
    assert json.loads(target.evidence)["state"] == "first_launch"
    assert json.loads(target.flags)["tags"] == [] and target.driver == "unknown"
    signals_step = steps[-1]["data"]
    assert signals_step["counts"]["first_launch"] == 1 and signals_step["history_sessions"] == 11
    assert signals_step["requirements"]["ebbing"] == 16


def test_previous_session_comes_from_the_calendar_then_from_local_history(db):
    world = seeded_world(db)
    ingest = FakeIngest()
    run(world, ingest_service=ingest)
    assert ingest.member_calls == [world.days[-2]]

    # no calendar: the newest ingested date before D is used
    again = FakeIngest()
    th = thresholds()
    run_board_pipeline(world.days[-1], repo=world.repo(), calendar_fn=None, force=True, ingest_service=again,
                       metrics_service=world.metrics(th), signal_service=world.signals(th))
    assert again.member_calls == [world.days[-2]]


def test_cold_start_without_calendar_or_history_skips_the_previous_steps(db):
    th = thresholds()
    # nothing before D at all (cold start without a calendar): both steps say so instead of guessing
    cold = World(db, days=1)
    cold.seed_day(cold.days[0])
    result = run_board_pipeline(cold.days[0], repo=cold.repo(), calendar_fn=None, ingest_service=FakeIngest(),
                                metrics_service=cold.metrics(th), signal_service=cold.signals(th))
    assert [s["status"] for s in result["steps"][:2]] == ["skipped", "skipped"]
    assert "无法确定上一交易日" in result["steps"][0]["detail"]


def test_previous_day_completion_recomputes_its_metrics_but_never_its_signals(db):
    world = seeded_world(db)
    prev, day = world.days[-2], world.days[-1]
    first = run(world, day=prev, ingest_service=FakeIngest(), skip_ingest=True)  # D-1's own (provisional) run
    assert first["status"] == "succeeded"
    before = {r.board_id: (r.signal_type, r.reason, r.evidence, r.flags) for r in signal_rows(db, first["run_id"])}
    assert world.board_daily(prev, "I1")["limit_up_count"] == 0

    def complete_previous(completed: date) -> None:
        # the real limit_list_d arrives: one I1 member actually closed at the limit
        code = world.members["I1"][0]
        with db.session_scope() as session:
            session.query(MarketStockDaily).filter(
                MarketStockDaily.trade_date == completed, MarketStockDaily.code == code
            ).update({"limit_state": "up", "consecutive_up": 2})

    ingest = FakeIngest(previous_status="complete", on_previous=complete_previous)
    second = run(world, day=day, ingest_service=ingest)
    assert second["status"] == "succeeded"
    previous_step, _membership, _ingest, metrics_step, _signals = second["steps"]
    assert previous_step["status"] == "succeeded"
    assert metrics_step["data"]["previous_recomputed"] == prev.isoformat() and "已重算上一交易日" in metrics_step["detail"]

    # D-1's facts-only metrics follow the completed data ...
    assert world.board_daily(prev, "I1")["limit_up_count"] == 1
    assert world.board_daily(prev, "I1")["max_consecutive"] == 2
    # ... while the judgments of D-1's run stay exactly as they were made
    after = {r.board_id: (r.signal_type, r.reason, r.evidence, r.flags) for r in signal_rows(db, first["run_id"])}
    assert after == before
    assert len(signal_rows(db, second["run_id"])) == 12  # D's run has its own rows
    assert world.repo().latest_completed_run(prev)["id"] == first["run_id"]


def test_previous_day_metrics_are_left_alone_when_nothing_was_completed(db):
    world = seeded_world(db)
    run(world, ingest_service=FakeIngest(previous_status="skipped"))
    result = world.repo().latest_completed_run(world.days[-1])
    metrics_step = next(s for s in result["steps"] if s["key"] == "metrics")
    assert metrics_step["data"]["previous_recomputed"] is None and "已重算" not in metrics_step["detail"]


def test_derived_ingest_is_reported_but_is_not_a_warning(db):
    world = seeded_world(db)
    result = run(world, ingest_service=FakeIngest(status="provisional", derived=["daily_basic", "limit_list_d"]))
    ingest_step = next(s for s in result["steps"] if s["key"] == "ingest")
    assert ingest_step["status"] == "succeeded"
    assert "daily_basic" in ingest_step["detail"] and "推算" in ingest_step["detail"]
    assert result["status"] == "succeeded"


def test_membership_step_reports_incomplete_and_failed_folds(db):
    world = seeded_world(db)
    incomplete = run(world, ingest_service=FakeIngest(
        membership={"status": "incomplete", "reason": "成分条数 3 低于上一份 8 的 50%", "rows": 3, "baseline": 8}))
    step = next(s for s in incomplete["steps"] if s["key"] == "membership")
    assert incomplete["status"] == "partial" and step["status"] == "partial"
    assert "保留上一日成分" in step["detail"] and step["data"]["baseline"] == 8
    assert next(s for s in incomplete["steps"] if s["key"] == "signals")["status"] == "succeeded"

    failed = run(world, ingest_service=FakeIngest(membership={"status": "error", "error": "timeout"}), force=True)
    step = next(s for s in failed["steps"] if s["key"] == "membership")
    assert failed["status"] == "partial" and step["status"] == "failed" and "timeout" in step["detail"]

    folded = run(world, ingest_service=FakeIngest(
        membership={"status": "ok", "rows": 80000, "boards": 700, "opened": 30, "closed": 20}), force=True)
    step = next(s for s in folded["steps"] if s["key"] == "membership")
    assert step["status"] == "succeeded" and "80000" in step["detail"]
    # folded membership changes D-1's inputs: its metrics are recomputed
    assert next(s for s in folded["steps"] if s["key"] == "metrics")["data"]["previous_recomputed"] == world.days[-2].isoformat()


def test_second_run_is_skipped_unless_forced(db):
    world = seeded_world(db)
    ingest = FakeIngest()
    first = run(world, ingest_service=ingest)
    again = run(world, ingest_service=ingest)
    assert again["status"] == "skipped" and again["run_id"] == first["run_id"]
    assert len(ingest.ingest_calls) == 2 and len(runs(db)) == 1  # nothing was requested the second time

    forced = run(world, ingest_service=ingest, force=True)
    assert forced["status"] == "succeeded" and forced["run_id"] != first["run_id"]
    assert len(runs(db)) == 2
    old, new = signal_rows(db, first["run_id"]), signal_rows(db, forced["run_id"])
    assert len(old) == len(new) == 12  # the older run keeps its own signals
    key = lambda rows: sorted((r.board_id, r.signal_type, r.reason, r.evidence) for r in rows)  # noqa: E731
    assert key(old) == key(new)  # same inputs, same verdicts
    latest = world.repo().latest_completed_run(world.days[-1])
    assert latest["id"] == forced["run_id"]
    # unique (run_id, board_id) holds and a repeated write inside a run replaces instead of duplicating
    world.repo().replace_signals(forced["run_id"], world.days[-1], [
        {"board_id": world.board_ids["I0"], "signal_type": "none", "flags": {}, "evidence": {}, "reason": ""}])
    assert len(signal_rows(db, forced["run_id"])) == 1
    assert len(signal_rows(db, first["run_id"])) == 12


def test_partial_ingest_marks_run_partial_and_rerun_is_allowed(db):
    world = seeded_world(db)
    ingest = FakeIngest(status="partial", missing=["daily_basic"])
    first = run(world, ingest_service=ingest)
    assert first["status"] == "partial"
    ingest_step = next(s for s in first["steps"] if s["key"] == "ingest")
    assert ingest_step["status"] == "partial" and "daily_basic" in ingest_step["detail"]
    second = run(world, ingest_service=FakeIngest())  # data completed later: a partial run does not block
    assert second["status"] == "succeeded" and second["run_id"] != first["run_id"]


def test_ingest_failure_with_local_data_continues_as_partial(db):
    world = seeded_world(db)
    result = run(world, ingest_service=FakeIngest(error=RuntimeError("tushare down")))
    assert result["status"] == "partial"
    ingest_step = next(s for s in result["steps"] if s["key"] == "ingest")
    assert ingest_step["status"] == "failed" and "tushare down" in ingest_step["detail"]
    assert [s["key"] for s in result["steps"]][-2:] == ["metrics", "signals"]


def test_failed_previous_completion_does_not_stop_today(db):
    world = seeded_world(db)
    result = run(world, ingest_service=FakeIngest(previous_error=RuntimeError("dc_index down")))
    assert result["status"] == "partial"
    previous = result["steps"][0]
    assert previous["key"] == "previous" and previous["status"] == "failed" and "dc_index down" in previous["detail"]
    assert [s["status"] for s in result["steps"][2:]] == ["succeeded"] * 3  # D itself is unaffected


def test_no_market_data_fails_the_run_without_signals(db):
    world = World(db, days=3)  # catalog exists, no stock rows at all
    day = date(2026, 11, 9)
    ingest = FakeIngest(status="no_data")
    result = run_board_pipeline(day, ingest_service=ingest, repo=world.repo(), calendar_fn=None,
                                metrics_service=world.metrics(), signal_service=world.signals())
    assert result["status"] == "failed" and "没有行情数据" in result["error"]
    [stored] = runs(db)
    assert stored.status == "failed" and stored.error and stored.finished_at is not None
    assert signal_rows(db, stored.id) == []
    assert world.repo().latest_completed_run(day) is None  # failed runs are never served


def test_skip_ingest_uses_local_data_and_makes_no_external_call(db):
    world = seeded_world(db)
    ingest = FakeIngest()
    result = run(world, ingest_service=ingest, skip_ingest=True)
    assert result["status"] == "succeeded"
    assert ingest.ingest_calls == [] and ingest.member_calls == []
    assert [s["status"] for s in result["steps"][:3]] == ["skipped", "skipped", "skipped"]


def test_skip_members_only_skips_the_membership_step(db):
    world = seeded_world(db)
    ingest = FakeIngest()
    result = run(world, ingest_service=ingest, skip_members=True)
    assert ingest.member_calls == [] and len(ingest.ingest_calls) == 2
    assert [s["status"] for s in result["steps"][:2]] == ["skipped", "skipped"]


def test_stale_running_run_is_failed_and_fresh_one_blocks(db):
    world = seeded_world(db)
    day = world.days[-1]
    with db.session_scope() as session:
        session.add(DailyRun(trade_date=day, status="running", started_at=utc_naive_now() - timedelta(minutes=5)))
    blocked = run(world, ingest_service=FakeIngest())
    assert blocked["status"] == "skipped" and "in progress" in blocked["reason"]
    with db.session_scope() as session:
        session.query(DailyRun).update({"started_at": utc_naive_now() - timedelta(hours=3)})
    result = run(world, ingest_service=FakeIngest())
    assert result["status"] == "succeeded"
    stored = runs(db)
    assert [r.status for r in stored] == ["failed", "succeeded"]
    assert "超时" in stored[0].error


def test_core_data_gap_in_metrics_is_reported_as_partial(db):
    world = World(db, days=9)
    for index, day in enumerate(world.days):
        world.seed_day(day, float_mv=None if index == 8 else 1e10, benchmark=None if index == 8 else 0.0)
    result = run(world, ingest_service=FakeIngest())
    assert result["status"] == "partial"
    metrics = next(s for s in result["steps"] if s["key"] == "metrics")
    assert metrics["status"] == "partial" and "benchmark_missing" in metrics["detail"]
    signals = next(s for s in result["steps"] if s["key"] == "signals")
    assert signals["status"] == "partial" and signals["data"]["core_missing"] == 12


def test_recompute_days_rebuilds_history_with_the_current_version(db):
    world = seeded_world(db)
    world.compute_all()
    with db.session_scope() as session:  # simulate rows written by an older formula
        session.query(BoardDaily).update({"calc_version": "0", "ret_cap": 99.0})
    result = run(world, ingest_service=FakeIngest(), skip_ingest=True, recompute_days=10)
    assert result["status"] == "succeeded"
    assert world.board_daily(world.days[-3], "I0")["ret_cap"] != 99.0
    assert world.board_daily(world.days[-3], "I0")["calc_version"] != "0"


# ----------------------------------------------------------------------
# End to end: the real ingestion service against a fake Tushare SDK, two consecutive 17:00 runs
# ----------------------------------------------------------------------
@pytest.fixture()
def evening_env(db, monkeypatch):
    monkeypatch.setattr(tushare_fetcher_module, "get_config", lambda: SimpleNamespace(tushare_token=None))
    monkeypatch.setattr(TushareFetcher, "_market_retry_base_delay", 0.0)
    monkeypatch.setattr(TushareFetcher, "_market_rate_limit_wait", 0.0)
    fetcher = TushareFetcher(rate_limit_per_minute=100000)
    api = build_evening_api()
    fetcher._api = api
    sessions = [D1, D2, D3]
    calendar_fn = lambda start, end: [d for d in sessions if start <= d <= end]  # noqa: E731
    config = SimpleNamespace(
        market_snapshot_retention_days=250, market_snapshot_backfill_days=60,
        market_benchmark_code="000985.CSI", market_snapshot_requests_per_minute=10000,
    )
    service = MarketSnapshotService(
        fetcher=fetcher, repo=MarketSnapshotRepository(db), config=config, calendar_fn=calendar_fn
    )
    repo = BoardAnalysisRepository(db)
    th = thresholds()
    metrics = BoardMetricsService(repo=repo, thresholds=th, calendar_fn=calendar_fn)
    signals = BoardSignalService(repo=repo, thresholds=th, calendar_fn=calendar_fn)

    def go(day: date, **kwargs: Any) -> Dict[str, Any]:
        return run_board_pipeline(
            day, ingest_service=service, repo=repo, metrics_service=metrics, signal_service=signals,
            calendar_fn=calendar_fn, **kwargs,
        )

    return SimpleNamespace(api=api, service=service, repo=repo, go=go, db=db)


def _stocks(db, day: date) -> Dict[str, MarketStockDaily]:
    with db.get_session() as session:
        rows = session.execute(select(MarketStockDaily).where(MarketStockDaily.trade_date == day)).scalars().all()
        for r in rows:
            session.expunge(r)
    return {r.code: r for r in rows}


def _publish_evening_of_d2(api) -> None:
    """What the vendor publishes between D2 17:00 and D3 17:00."""
    _concat(api, "daily_basic", ["ts_code", "trade_date", "turnover_rate", "circ_mv", "limit_status"],
            [("600519.SH", "20261103", 0.5, 1010.0, 1), ("300750.SZ", "20261103", 7.7, 5000.0, 2),  # 10x the cap
             ("000001.SZ", "20261103", 1.0, 400.0, 1), ("600001.SH", "20261103", 2.0, 100.0, 2),
             ("688001.SH", "20261103", 3.0, 300.0, 5), ("000333.SZ", "20261103", 1.5, 800.0, 3),
             ("002001.SZ", "20261103", 1.0, 200.0, 4), ("002002.SZ", "20261103", 1.0, 200.0, 2),
             ("002003.SZ", "20261103", 1.0, 200.0, 1)])
    _concat(api, "limit_list_d", ["trade_date", "ts_code", "name", "open_times", "limit_times", "limit"],
            [("20261103", "300750.SZ", "宁德时代", 2, 3, "U"), ("20261103", "000001.SZ", "平安银行", 3, 1, "Z"),
             ("20261103", "000333.SZ", "美的集团", 0, 1, "U"), ("20261103", "002002.SZ", "鸿达兴业", 1, 1, "U")])
    _concat(api, "dc_index", ["ts_code", "trade_date", "name", "idx_type", "level", "total_mv"],
            [("BK0477.DC", "20261103", "酿酒行业", "行业板块", "1", 100.0),
             ("BK1184.DC", "20261103", "人形机器人", "概念板块", None, 200.0),
             ("BK3000.DC", "20261103", "新概念", "概念板块", None, 50.0)])
    _concat(api, "dc_daily", ["ts_code", "trade_date", "pct_change", "amount"],
            [("BK0477.DC", "20261103", 1.5, 1e10), ("BK1184.DC", "20261103", 3.5, 2e10),
             ("BK3000.DC", "20261103", 4.5, 3e9)])
    _board_members(api, "BK1184.DC", "20261103", ["600519.SH", "300750.SZ", "000001.SZ", "688001.SH"])
    _board_members(api, "BK0477.DC", "20261103", ["600519.SH", "000333.SZ", "002001.SZ", "002002.SZ"])
    _board_members(api, "BK3000.DC", "20261103", ["300750.SZ", "000333.SZ", "002002.SZ"])
    # D3's 17:00 publications
    _concat(api, "daily", _EVENING_DAILY_COLS, _evening_daily("20261104", _EVENING_D2))
    _concat(api, "stk_limit", ["ts_code", "trade_date", "up_limit", "down_limit"],
            [(c, "20261104", v[6], v[7]) for c, v in _EVENING_D2.items()])
    _concat(api, "index_daily", ["ts_code", "trade_date", "close", "pct_chg"], [("000985.CSI", "20261104", 5020.0, 0.1)])
    _concat(api, "suspend_d", ["ts_code", "trade_date", "suspend_type", "suspend_timing"],
            [("000002.SZ", "20261104", "S", None)])


def test_two_evening_runs_derive_today_and_complete_yesterday(evening_env):
    env = evening_env
    api, db = env.api, env.db
    _board_members(api, "BK1184.DC", "20261102", ["600519.SH", "300750.SZ", "000001.SZ", "688001.SH"])
    _board_members(api, "BK0477.DC", "20261102", ["600519.SH", "000333.SZ", "002001.SZ", "002002.SZ"])

    # --- D2, 17:00: daily / stk_limit / suspend_d only ------------------------------------------------
    first = env.go(D2)
    steps = {s["key"]: s for s in first["steps"]}
    assert list(steps) == STEP_KEYS
    assert steps["previous"]["status"] == "succeeded"  # cold start: D1 ingested in full, with real data
    assert steps["membership"]["status"] == "succeeded" and steps["membership"]["data"]["rows"] == 8
    assert steps["ingest"]["status"] == "succeeded" and "推算" in steps["ingest"]["detail"]
    assert steps["ingest"]["data"]["derived"] == ["daily_basic", "limit_list_d"]
    assert steps["metrics"]["status"] == "succeeded" and steps["signals"]["status"] in ("succeeded", "partial")
    assert all(kw["trade_date"] == "20261102" for kw in api.calls_of("dc_index"))  # D2's boards: not requested yet

    d2_before = _stocks(db, D2)
    assert d2_before["300750"].consecutive_up == 3 and d2_before["300750"].open_times is None
    assert d2_before["300750"].float_mv == pytest.approx(5.0e6 * 1.2)
    quality_before = env.repo.get_market_daily(D2)["quality"]
    assert quality_before["ingestion"]["limit_source"] == "derived"
    assert quality_before["metrics"]["input_sources"] == {"basic": "derived", "limit": "derived"}
    board_ids = {b["code"]: b["id"] for b in env.repo.list_catalog()}
    concept_before = env.repo.get_board_daily_range([D2], [board_ids["BK1184.DC"]])[(D2, board_ids["BK1184.DC"])]
    assert concept_before["limit_up_count"] == 1 and concept_before["max_consecutive"] == 3
    signals_before = sorted(
        (s["board_id"], s["signal_type"], json.dumps(s["evidence"], sort_keys=True), s["reason"])
        for s in env.repo.get_signals(first["run_id"])
    )
    assert signals_before

    # --- between 17:00 and 20:30: D2's evening publications --------------------------------------------
    _publish_evening_of_d2(api)
    calls_before = len(api.calls)

    # --- D3, 17:00 ------------------------------------------------------------------------------------
    second = env.go(D3)
    steps = {s["key"]: s for s in second["steps"]}
    assert steps["previous"]["status"] == "succeeded" and "已按实际数据重写" in steps["previous"]["detail"]
    assert steps["previous"]["data"]["status"] == "complete" and steps["previous"]["data"]["derived"] == []
    assert steps["membership"]["status"] == "succeeded" and steps["membership"]["data"]["boards"] == 3
    assert steps["ingest"]["data"]["derived"] == ["daily_basic", "limit_list_d"]
    assert steps["metrics"]["data"]["previous_recomputed"] == D2.isoformat()

    # D2's derived fields were replaced by the real ones
    d2_after = _stocks(db, D2)
    assert d2_after["300750"].open_times == 2 and d2_after["300750"].turnover_rate == pytest.approx(7.7)
    assert d2_after["300750"].float_mv == pytest.approx(5000.0e4)
    assert d2_after["000001"].open_times == 3
    q_after = env.repo.get_market_daily(D2)["quality"]
    assert q_after["ingestion"]["status"] == "complete" and q_after["ingestion"]["limit_source"] == "limit_list_d"
    assert q_after["ingestion"]["boards"] == "done" and q_after["ingestion"]["membership"]["status"] == "ok"
    assert q_after["metrics"]["input_sources"] == {"basic": "daily_basic", "limit": "limit_list_d"}

    # ... and D2's metrics were recomputed from them (the real cap re-weights the concept board)
    concept_after = env.repo.get_board_daily_range([D2], [board_ids["BK1184.DC"]])[(D2, board_ids["BK1184.DC"])]
    assert concept_after["ret_cap"] != pytest.approx(concept_before["ret_cap"])
    assert concept_after["limit_up_count"] == 1 and concept_after["max_consecutive"] == 3

    # D2's judgments are untouched: same rows, same evidence, still attached to the first run
    signals_after = sorted(
        (s["board_id"], s["signal_type"], json.dumps(s["evidence"], sort_keys=True), s["reason"])
        for s in env.repo.get_signals(first["run_id"])
    )
    assert signals_after == signals_before
    assert env.repo.latest_completed_run(D2)["id"] == first["run_id"]

    # D3 is derived from the completed D2: the real streak (3) continues, and the new concept board exists
    d3 = _stocks(db, D3)
    assert d3["300750"].consecutive_up == 4
    assert d3["300750"].float_mv == pytest.approx(5000.0e4 * 1.2)  # real D2 cap x (1 + 20%)
    catalog = {b["code"]: b for b in env.repo.list_catalog()}
    assert catalog["BK3000.DC"]["first_seen_date"] == D2  # appeared in dc_index(D2): visible from D3's run on
    assert env.repo.get_signals(second["run_id"])

    # --- a forced re-run of D3 requests no membership / completion again --------------------------------
    api.calls.clear()
    again = env.go(D3, force=True)
    again_steps = {s["key"]: s for s in again["steps"]}
    assert again_steps["previous"]["status"] == "skipped" and again_steps["membership"]["status"] == "skipped"
    assert api.calls_of("dc_member") == [] and api.calls_of("dc_index") == []
    assert {kw["trade_date"] for kw in api.calls_of("daily")} == {"20261104"}
    assert calls_before > 0
