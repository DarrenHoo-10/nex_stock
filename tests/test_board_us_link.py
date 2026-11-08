# -*- coding: utf-8 -*-
"""17:00 board pipeline ``us_link`` step: evidence on mapped boards, non-fatal, schema shape unchanged (offline)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from src.services.board_anomaly_service import BoardAnomalyService
from src.services.board_pipeline import run_board_pipeline
from src.services.board_us_link import board_run_as_of, build_us_link_evidence
from src.services.us_leaders import UsLeader, UsMoveThresholds
from src.services.us_overnight_service import UsMove, UsOvernightService
from src.storage import BoardCatalog, DatabaseManager
from tests.board_fixtures import World, thresholds
from tests.test_board_anomaly_service import BoardDetail, BoardAnomalyListResponse, launch_world
from tests.test_us_overnight_service import (
    FakeTushare,
    FakeYfinance,
    closes_for,
    sessions,
    tushare_frame,
)

CN = ZoneInfo("Asia/Shanghai")
NVDA = UsLeader("NVDA", "英伟达", "stock", ("半导体", "CPO", "算力"))   # 算力 is not in the catalog
AMD = UsLeader("AMD", "超威", "stock", ("CPO",))                        # stays below the threshold
SPX = UsLeader("SPX", "标普500指数", "index", ())                        # market-wide context, no boards
LEADERS = [NVDA, AMD, SPX]


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def rename(db, world: World, key: str, name: str) -> None:
    with db.session_scope() as session:
        session.execute(update(BoardCatalog).where(BoardCatalog.id == world.board_ids[key]).values(name=name))


def make_world(db) -> World:
    world = launch_world(db)  # I0 launches on the last day (state first_launch)
    rename(db, world, "I0", "半导体")
    rename(db, world, "C0", "CPO概念")
    return world


def us_service(db, world: World, **overrides: Any) -> UsOvernightService:
    # the board run of Fri 2026-11-06 17:00 sees Thursday's US session (closed 05:00 Beijing time on Friday)
    days = sessions(date(2026, 11, 5))
    frames = {
        "NVDA": tushare_frame(days, closes_for(days, 104.4, step=0.0), pct_last=4.4),
        "AMD": tushare_frame(days, closes_for(days, 103.0, step=0.0), pct_last=3.0),
        "SPX": tushare_frame(days, closes_for(days, 5000.0, step=5.0)),
    }
    kwargs = dict(
        leaders=LEADERS, thresholds=UsMoveThresholds(4.0, 1.5), db=db,
        tushare_fetcher=FakeTushare(frames), yfinance_fetcher=FakeYfinance({}),
    )
    kwargs.update(overrides)
    return UsOvernightService(object(), **kwargs)


def run(world: World, **kwargs: Any) -> Dict[str, Any]:
    th = thresholds()
    return run_board_pipeline(
        world.days[-1], repo=world.repo(), skip_ingest=True,
        metrics_service=world.metrics(th), signal_service=world.signals(th), **kwargs,
    )


def signal_of(world: World, result: Dict[str, Any], key: str) -> Dict[str, Any]:
    return next(s for s in world.repo().get_signals(result["run_id"]) if s["board_id"] == world.board_ids[key])


def detail(world: World, key: str) -> Dict[str, Any]:
    payload = BoardAnomalyService(repo=world.repo(), calendar_fn=None).get_board_detail(world.board_refs[key], None)
    BoardDetail.model_validate(payload)  # same schema as before: nothing new in the response shape
    return payload


# ----------------------------------------------------------------------
def test_step_attaches_us_link_to_boards_mapped_from_crossed_leaders_only(db):
    world = make_world(db)
    result = run(world, us_service=us_service(db, world))
    assert result["status"] == "succeeded"  # an unresolved board name is only noted, it does not degrade the run
    step = result["steps"][-1]
    assert step["key"] == "us_link"
    assert step["status"] == "succeeded", step["detail"]
    assert "1 个超过阈值" in step["detail"] and "NVDA" in step["detail"] and "2 个板块" in step["detail"]
    assert step["data"]["unresolved_boards"] == ["算力"]
    assert step["data"]["us"]["session"] == "2026-11-05" and step["data"]["us"]["crossed"] == ["NVDA"]

    industry = signal_of(world, result, "I0")["evidence"]["us_link"]
    concept = signal_of(world, result, "C0")["evidence"]["us_link"]
    assert industry["session"] == "2026-11-05"
    assert [leader["symbol"] for leader in industry["leaders"]] == ["NVDA"]  # AMD stayed below its threshold
    assert industry["leaders"][0]["pct_chg"] == pytest.approx(4.4) and industry["leaders"][0]["direction"] == "up"
    assert "英伟达(NVDA) +4.40%" in industry["text"] and concept["lines"][0].startswith("隔夜美股：")
    assert "us_link" not in signal_of(world, result, "I1")["evidence"]  # unmapped board untouched


def test_non_fatal_failure_is_a_warning_and_keeps_signals(db):
    world = make_world(db)

    class Exploding:
        leaders = LEADERS

        def refresh(self, *args, **kwargs):
            raise RuntimeError("yfinance exploded")

    result = run(world, us_service=Exploding())
    assert result["status"] == "partial" and result["error"] is None
    step = result["steps"][-1]
    assert step["key"] == "us_link" and step["status"] == "failed" and "yfinance exploded" in step["detail"]
    assert [s["status"] for s in result["steps"][:-1]] == ["skipped", "skipped", "skipped", "succeeded", "succeeded"]
    assert signal_of(world, result, "I0")["signal_type"] == "first_launch"
    assert "us_link" not in signal_of(world, result, "I0")["evidence"]


def test_all_sources_failing_marks_the_step_partial(db):
    world = make_world(db)
    offline = us_service(
        db, world, tushare_fetcher=FakeTushare({}), yfinance_fetcher=FakeYfinance({}, errors={s.yfinance_symbol: RuntimeError("down") for s in LEADERS}),
    )
    result = run(world, us_service=offline)
    step = result["steps"][-1]
    assert step["status"] == "partial" and "没有取到已收盘的美股日线" in step["detail"]
    assert signal_of(world, result, "I0")["signal_type"] == "first_launch"


def test_empty_leaders_skips_the_step(db):
    world = make_world(db)
    result = run(world, us_service=us_service(db, world, leaders=[]))
    assert result["steps"][-1]["status"] == "skipped" and result["status"] == "succeeded"


def test_step_is_not_run_unless_requested_or_live(db):
    world = make_world(db)
    result = run(world)  # skip_ingest + default: the existing five steps only
    assert [s["key"] for s in result["steps"]] == ["previous", "membership", "ingest", "metrics", "signals"]


def test_anomaly_service_surfaces_us_link_without_changing_states_or_shape(db):
    world = make_world(db)
    first = run(world)
    before = detail(world, "I0")
    assert "隔夜美股" not in before["drivers"] and all("隔夜美股" not in line for line in before["evidence"])

    forced = run(world, force=True, us_service=us_service(db, world))
    assert forced["run_id"] > first["run_id"]
    after = detail(world, "I0")
    assert "英伟达(NVDA) +4.40%" in after["drivers"] and after["drivers"].startswith(before["drivers"])
    assert after["evidence"][: len(before["evidence"])] == before["evidence"]
    assert any(line.startswith("隔夜美股：") and "NVDA" in line for line in after["evidence"][len(before["evidence"]):])
    # state rules stay price-only
    for field in ("state", "tags", "reason", "verdict_text", "driver_kind", "metrics", "can_pick", "data_gaps"):
        assert after[field] == before[field], field
    assert set(after) == set(before)

    listing = BoardAnomalyService(repo=world.repo(), calendar_fn=None).list_anomalies(None, None, None)
    BoardAnomalyListResponse.model_validate(listing)
    assert detail(world, "I1")["drivers"] == "原因未明：未接入消息 / 业绩数据，仅按行情判断"  # unmapped board unchanged


def test_board_run_reference_time_and_evidence_payload():
    nominal = board_run_as_of(date(2026, 11, 6), now=datetime(2026, 11, 8, 12, 0, tzinfo=CN))
    assert (nominal.year, nominal.month, nominal.day, nominal.hour) == (2026, 11, 6, 17)
    early = datetime(2026, 11, 6, 10, 0, tzinfo=CN)
    assert board_run_as_of(date(2026, 11, 6), now=early) == early  # a manual morning run uses the actual time
    moves = [
        UsMove("NVDA", "英伟达", "stock", date(2026, 11, 5), 104.4, 100.0, 4.4, 1.0, 4.0, True),
        UsMove("AVGO", "博通", "stock", date(2026, 11, 5), 90.0, 100.0, -10.0, -2.0, 4.0, True),
    ]
    payload = build_us_link_evidence(date(2026, 11, 5), moves)
    assert [leader["symbol"] for leader in payload["leaders"]] == ["AVGO", "NVDA"]  # biggest move first
    assert payload["text"] == "隔夜美股联动：博通(AVGO) -10.00%；英伟达(NVDA) +4.40%"
