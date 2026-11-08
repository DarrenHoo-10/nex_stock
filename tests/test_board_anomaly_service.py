# -*- coding: utf-8 -*-
"""BoardAnomalyService read side: payloads validate against the API schemas (no network)."""

from __future__ import annotations

import json
import sys
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.deps import get_board_anomaly_service, get_system_config_service
from api.middlewares.error_handler import add_error_handlers
from api.v1.endpoints import boards as boards_endpoint
from api.v1.schemas.boards import BoardAnomalyListResponse, BoardDetail, ThresholdListResponse
from src.repositories.board_analysis_repo import BoardAnalysisRepository
from src.services.board_anomaly_service import BoardAnomalyService
from src.services.board_pipeline import run_board_pipeline
from src.storage import DatabaseManager
from tests.board_fixtures import World, thresholds
from tests.test_board_signals import _broad_plan, build, limit_up


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def file_db(tmp_path):
    """Endpoint handlers run in worker threads: an in-memory SQLite is per-thread, a file is shared."""
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'boards.db'}")
    yield manager
    DatabaseManager.reset_instance()


def run_pipeline(world: World, day: date = None, **kwargs: Any) -> Dict[str, Any]:
    th = kwargs.pop("th", None) or thresholds()
    return run_board_pipeline(
        day or world.days[-1], repo=world.repo(), skip_ingest=True,
        metrics_service=world.metrics(th), signal_service=world.signals(th), **kwargs,
    )


def service(world: World) -> BoardAnomalyService:
    return BoardAnomalyService(repo=world.repo(), calendar_fn=None)


def launch_world(db) -> World:
    return build(db, 11, {10: {"ret": 3.0, "amt": 2.0}})


# ----------------------------------------------------------------------
# list
# ----------------------------------------------------------------------
def test_list_payload_validates_and_maps_columns(db):
    world = launch_world(db)
    run_pipeline(world)
    payload = service(world).list_anomalies(None, None, None)
    parsed = BoardAnomalyListResponse.model_validate(payload)
    assert parsed.trade_date == world.days[-1].isoformat()
    market = parsed.market
    assert market.index_name == "中证全指" and market.index_change_pct == 0.0
    assert market.turnover_yi == pytest.approx(65.0)  # 55 stocks x 1e8 + 5 target stocks x 2e8 yuan
    assert market.up_count == 30 + 5 and market.down_count == 20
    assert market.limit_up_count == 0 and market.broad_rally is False
    # the weakest board of each type is "notable" but cannot be classified with 11 sessions of history
    assert [(b.id, b.state) for b in parsed.boards][0] == (world.board_refs["I0"], "first_launch")
    assert {(b.id, b.state) for b in parsed.boards[1:]} == {
        (world.board_refs["I1"], "insufficient_history"), (world.board_refs["C1"], "insufficient_history")}
    row = parsed.boards[0]
    assert row.name == "indI0" and row.board_type == "industry" and row.state == "first_launch"
    assert row.change_pct == pytest.approx(3.0) and row.excess_pct == pytest.approx(3.0)
    assert row.amount_ratio == pytest.approx(2.0) and row.breadth_pct == pytest.approx(100.0)
    assert row.limit_up_count is None and row.broken_rate_pct is None  # industries: no limit-up columns
    assert row.change_5d_pct == pytest.approx(3.0)
    assert "放量启动" in row.reason
    assert parsed.history_note and "板块历史积累 11 天" in parsed.history_note and "退潮需 16 天" in parsed.history_note


def test_list_filters_and_ordering(db):
    world = build(db, 18, {17: {"ret": 3.0, "amt": 2.0, "returns": {"I1": -3.0}, "amounts": {"I1": 2.0}}})
    run_pipeline(world)
    svc = service(world)
    full = BoardAnomalyListResponse.model_validate(svc.list_anomalies(None, None, None))
    states = [(b.id, b.state) for b in full.boards]
    assert states == [(world.board_refs["I0"], "first_launch"), (world.board_refs["I1"], "abnormal_drop")]
    only_drop = svc.list_anomalies(None, None, "abnormal_drop")
    assert [b["id"] for b in only_drop["boards"]] == [world.board_refs["I1"]]
    assert svc.list_anomalies(None, "concept", None)["boards"] == []
    assert [b["id"] for b in svc.list_anomalies(None, "industry", None)["boards"]] == [
        world.board_refs["I0"], world.board_refs["I1"]]
    none_rows = svc.list_anomalies(None, "industry", "none")["boards"]
    assert world.board_refs["I0"] not in [b["id"] for b in none_rows] and len(none_rows) == 4
    assert none_rows[0]["excess_pct"] >= none_rows[-1]["excess_pct"]


def test_trade_date_selection_prefers_latest_completed_run(db):
    world = launch_world(db)
    first = run_pipeline(world, world.days[9])
    second = run_pipeline(world)
    svc = service(world)
    assert svc.list_anomalies(None, None, None)["trade_date"] == world.days[10].isoformat()
    assert svc.list_anomalies(world.days[9], None, None)["trade_date"] == world.days[9].isoformat()
    # a failed or still running newer run is never served
    world.repo().update_run(second["run_id"], status="failed")
    assert svc.list_anomalies(None, None, None)["trade_date"] == world.days[9].isoformat()
    assert first["status"] == "succeeded"
    forced = run_pipeline(world, force=True)
    assert forced["run_id"] > second["run_id"]
    assert svc.list_anomalies(None, None, None)["trade_date"] == world.days[10].isoformat()


def test_empty_state_has_valid_payload_and_note(db):
    svc = BoardAnomalyService(repo=BoardAnalysisRepository(db), calendar_fn=None)
    payload = svc.list_anomalies(None, None, None)
    parsed = BoardAnomalyListResponse.model_validate(payload)
    assert parsed.boards == [] and parsed.market.up_count is None and parsed.market.turnover_yi is None
    assert "暂无板块数据" in parsed.history_note
    dated = BoardAnomalyListResponse.model_validate(svc.list_anomalies(date(2026, 11, 6), None, None))
    assert dated.trade_date == "2026-11-06" and "2026-11-06" in dated.history_note
    assert svc.get_board_detail("industry:BK1000", None) is None  # unknown board -> 404 at the endpoint


def test_broad_rally_summary_and_note(db):
    last = 13
    world = build(db, 14, _broad_plan(last), target="C0")
    run_pipeline(world)
    parsed = BoardAnomalyListResponse.model_validate(service(world).list_anomalies(None, None, None))
    assert parsed.market.broad_rally is True and "普涨日保护" in parsed.market.note
    assert [b.id for b in parsed.boards] == [world.board_refs["C0"]]
    assert parsed.boards[0].state == "first_launch" and parsed.boards[0].limit_up_count == 0


def test_partial_run_surfaces_data_gaps_in_the_note(db):
    world = World(db, days=9)
    for index, day in enumerate(world.days):
        world.seed_day(day, float_mv=None if index == 8 else 1e10, benchmark=None if index == 8 else 0.0)
    run_pipeline(world)
    parsed = BoardAnomalyListResponse.model_validate(service(world).list_anomalies(None, None, None))
    assert parsed.boards == []  # key inputs missing: not listed one by one ...
    assert "12 个板块因当日关键数据缺失无法判定" in parsed.history_note  # ... but never silently dropped
    assert "benchmark_missing" in parsed.market.note and parsed.market.index_change_pct is None


def test_backfilled_membership_period_is_called_out(db):
    world = World(db, days=7, end=date(2024, 12, 18))
    for day in world.days:
        world.seed_day(day)
    run_pipeline(world)
    parsed = BoardAnomalyListResponse.model_validate(service(world).list_anomalies(None, None, None))
    assert "倒推" in parsed.history_note
    detail = BoardDetail.model_validate(service(world).get_board_detail(world.board_refs["I0"], None))
    assert any("幸存者偏差" in gap for gap in detail.data_gaps)


def test_single_stock_board_is_listed_with_its_tag_and_blocks_picking(db):
    def overrides(world):
        codes = world.members["I0"]
        rows = {codes[0]: {"pct_chg": 20.0}}
        rows.update({c: {"pct_chg": 0.5} for c in codes[1:]})
        return rows

    world = build(db, 14, {13: {"ret": 0.0, "amt": 2.0, "overrides": overrides}})
    run_pipeline(world)
    svc = service(world)
    parsed = BoardAnomalyListResponse.model_validate(svc.list_anomalies(None, None, None))
    [row] = [b for b in parsed.boards if b.id == world.board_refs["I0"]]
    assert row.state == "none" and row.tags == ["single_stock"] and "单只股票" in row.reason
    detail = BoardDetail.model_validate(svc.get_board_detail(world.board_refs["I0"], None))
    assert detail.can_pick is False


def test_merged_concept_group_lists_only_its_representative(db):
    world = World(db, days=14)
    world.per_board = 1
    world.add_board("C", 6, name="近似概念")
    world.share_members("C4", "C6", 5)
    shared = limit_up(world, "C4", 3)
    own = world.members["C6"][0]
    for index, day in enumerate(world.days):
        last = index == 13
        world.seed_day(day, returns={"C4": 3.0, "C6": 3.0} if last else None,
                       amounts={"C4": 2.0, "C6": 2.0} if last else None,
                       overrides=dict(shared, **{own: {"pct_chg": 4.0}}) if last else None)
    run_pipeline(world)
    svc = service(world)
    parsed = BoardAnomalyListResponse.model_validate(svc.list_anomalies(None, None, None))
    listed = {b.id: b for b in parsed.boards}
    assert listed[world.board_refs["C6"]].tags == ["merged"] and listed[world.board_refs["C6"]].state == "first_launch"
    assert world.board_refs["C4"] not in listed  # the duplicate concept is not listed separately
    hidden = BoardDetail.model_validate(svc.get_board_detail(world.board_refs["C4"], None))
    assert any("已合并展示" in gap for gap in hidden.data_gaps)


# ----------------------------------------------------------------------
# detail
# ----------------------------------------------------------------------
def test_detail_payload(db):
    world = launch_world(db)
    run_pipeline(world)
    payload = service(world).get_board_detail(world.board_refs["I0"], None)
    detail = BoardDetail.model_validate(payload)
    assert detail.id == world.board_refs["I0"] and detail.state == "first_launch"
    assert detail.verdict_text.startswith("首次启动：放量启动")
    assert detail.driver_kind == "unknown" and detail.drivers.startswith("原因未明")
    labels = [m.label for m in detail.metrics]
    assert labels == ["成交额倍数", "上涨家数占比", "当日超额", "5 日涨幅", "涨停家数", "炸板率"]
    values = {m.label: m for m in detail.metrics}
    assert values["成交额倍数"].value == "2.0 倍" and values["上涨家数占比"].value == "100.0%"
    assert values["当日超额"].value == "+3.00%" and values["涨停家数"].hint == "行业板块不使用涨停类条件"
    series = detail.excess_series
    assert len(series) == 8 and series[-1].trade_date == world.days[-1].isoformat()
    assert [p.trade_date for p in series] == sorted(p.trade_date for p in series)  # oldest first for the chart
    assert series[-1].excess_pct == pytest.approx(3.0) and series[0].excess_pct == pytest.approx(0.0)
    roles = [r.role for r in detail.representatives]
    assert roles[:2] == ["leader", "turnover"] and set(roles) <= {"leader", "turnover", "follower"}
    assert len({r.code for r in detail.representatives}) == len(detail.representatives)
    leader = detail.representatives[0]
    assert leader.name.startswith("N10") and leader.change_pct == pytest.approx(3.0) and leader.close == pytest.approx(10.3)
    assert detail.evidence and detail.can_pick is True
    assert detail.limit_up_count is None and detail.tags == []
    assert isinstance(detail.data_gaps, list)


def test_detail_lookup_by_ref_code_and_trade_date(db):
    world = launch_world(db)
    run_pipeline(world)
    run_pipeline(world, world.days[9])
    svc = service(world)
    assert svc.get_board_detail("industry:BK1000", None)["id"] == world.board_refs["I0"]
    assert svc.get_board_detail("BK1000.DC", None)["id"] == world.board_refs["I0"]
    older = BoardDetail.model_validate(svc.get_board_detail(world.board_refs["I0"], world.days[9]))
    assert older.state == "none" and older.verdict_text == "未达到异动条件"
    assert svc.get_board_detail("industry:NOPE", None) is None


def test_concept_detail_metrics_include_limit_up_block(db):
    plan = {i: {"ret": 3.0, "amt": 2.0} for i in range(8, 13)}
    plan[11]["overrides"] = lambda w: limit_up(w, "C0", 5, streak=2)
    plan[12]["overrides"] = lambda w: limit_up(w, "C0", 5, streak=3)
    world = build(db, 13, plan, target="C0")
    run_pipeline(world)
    detail = BoardDetail.model_validate(service(world).get_board_detail(world.board_refs["C0"], None))
    assert detail.state == "strengthening" and "mainline" in detail.tags
    values = {m.label: m for m in detail.metrics}
    assert values["涨停家数"].value == "5 家" and "最高 3 连板" in values["涨停家数"].hint
    assert values["炸板率"].value == "0.0%" and "触及涨停 5 家" in values["炸板率"].hint
    assert detail.limit_up_count == 5 and detail.broken_rate_pct == 0.0


def test_detail_without_a_run_still_resolves_known_boards(db):
    world = launch_world(db)
    world.compute_all()
    detail = BoardDetail.model_validate(service(world).get_board_detail(world.board_refs["I0"], None))
    assert detail.state == "none" and detail.can_pick is False
    assert any("暂无板块判定结果" in gap for gap in detail.data_gaps)


def test_detail_reports_quality_gaps(db):
    world = World(db, days=6)
    for day in world.days:
        world.seed_day(day, float_mv=None)
    run_pipeline(world)
    detail = BoardDetail.model_validate(service(world).get_board_detail(world.board_refs["I0"], None))
    assert detail.state == "insufficient_history" and detail.can_pick is False
    assert any("流通市值" in gap for gap in detail.data_gaps)
    assert detail.metrics[2].value == "—" and detail.excess_series[-1].excess_pct is None


# ----------------------------------------------------------------------
# thresholds
# ----------------------------------------------------------------------
def test_threshold_definitions_and_validation(db, monkeypatch):
    import src.config as config_module

    svc = BoardAnomalyService(repo=BoardAnalysisRepository(db))
    monkeypatch.setattr(config_module, "get_config", lambda: SimpleNamespace(board_anomaly_thresholds=""))
    items = ThresholdListResponse.model_validate({"items": svc.get_threshold_definitions()}).items
    by_key = {i.key: i for i in items}
    assert by_key["amount_ratio_mild"].value == 1.5 and by_key["amount_ratio_mild"].overridden is False
    assert by_key["excess_floor"].value == {"industry": 1.5, "concept": 2.5}
    monkeypatch.setattr(
        config_module, "get_config",
        lambda: SimpleNamespace(board_anomaly_thresholds=json.dumps({"amount_ratio_mild": 2.0, "excess_floor": {"concept": 3}})),
    )
    items = ThresholdListResponse.model_validate({"items": svc.get_threshold_definitions()}).items
    by_key = {i.key: i for i in items}
    assert by_key["amount_ratio_mild"].value == 2.0 and by_key["amount_ratio_mild"].overridden is True
    assert by_key["amount_ratio_mild"].default == 1.5
    assert by_key["excess_floor"].value == {"industry": 1.5, "concept": 3.0} and by_key["excess_floor"].overridden is True
    assert by_key["amount_ratio_strong"].overridden is False
    assert svc.validate_threshold_overrides({"amount_ratio_mild": 2.0}) == []
    assert svc.validate_threshold_overrides({"amount_ratio_mild": 0.5, "foo": 1}) != []


# ----------------------------------------------------------------------
# through the real endpoints
# ----------------------------------------------------------------------
class _Config:
    def __init__(self):
        self.updates = []

    def get_config(self, include_schema=True, mask_token="******"):
        return {"config_version": "v1", "items": []}

    def update(self, config_version, items, mask_token="******", reload_now=True):
        self.updates.append(items)
        return {}


def test_endpoints_serve_the_real_service(file_db):
    world = launch_world(file_db)
    run_pipeline(world)
    app = FastAPI()
    app.include_router(boards_endpoint.router, prefix="/api/v1/boards")
    add_error_handlers(app)
    config = _Config()
    app.dependency_overrides[get_board_anomaly_service] = lambda: service(world)
    app.dependency_overrides[get_system_config_service] = lambda: config
    client = TestClient(app)

    response = client.get("/api/v1/boards/anomalies?board_type=industry&state=first_launch")
    assert response.status_code == 200
    assert [b["id"] for b in response.json()["boards"]] == [world.board_refs["I0"]]
    detail = client.get(f"/api/v1/boards/{world.board_refs['I0']}")
    assert detail.status_code == 200 and detail.json()["state"] == "first_launch"
    assert client.get("/api/v1/boards/industry:NOPE").status_code == 404
    assert client.get("/api/v1/boards/thresholds").status_code == 200
    bad = client.put("/api/v1/boards/thresholds", json={"overrides": {"amount_ratio_mild": 0.5}})
    assert bad.status_code == 400 and config.updates == []
    good = client.put("/api/v1/boards/thresholds", json={"overrides": {"amount_ratio_mild": 2.0}})
    assert good.status_code == 200
    assert json.loads(config.updates[0][0]["value"]) == {"amount_ratio_mild": 2.0}
