# -*- coding: utf-8 -*-
"""Board driver ``news``: classified news linked to a board in the trade-date window, headline copied into evidence.

The real chain runs end to end on an in-memory DB: seeded market data -> metrics -> signals (default
news loader = ``build_news_digest`` over stored ``intelligence_items``) -> pipeline persistence ->
``BoardAnomalyService`` detail payload.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, List, Optional

import pytest
from sqlalchemy import delete

from api.v1.schemas.boards import BoardDetail
from src.services.board_anomaly_service import BoardAnomalyService
from src.services.board_signal_service import BoardSignalService
from src.storage import DatabaseManager, IntelligenceItem
from tests.board_fixtures import World, thresholds
from tests.test_board_anomaly_service import run_pipeline, service
from tests.test_board_signals import build


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def launch_world(db, amt: float = 2.0) -> World:
    """11 sessions, I0 launches on the last one (2026-11-06, a Friday); the previous session is 11-05."""
    return build(db, 11, {10: {"ret": 3.0, "amt": amt}})


def put_news(db, title: str, when: datetime, boards: List[int], *, importance: Optional[int] = 5,
             breaking: bool = False, source: str = "财联社电报") -> None:
    with db.session_scope() as session:
        session.add(IntelligenceItem(
            source_type="akshare", source_name="akshare:cls", title=title, summary="详情", url=f"https://example.test/{title}",
            source=source, published_at=when, fetched_at=when, scope_type="market", scope_value="__none__", market="cn",
            item_kind="flash", news_scope="board", importance=importance, is_breaking=breaking,
            board_ids=json.dumps(boards), board_names=json.dumps(["板块"], ensure_ascii=False),
            classified_at=when if importance is not None else None,
        ))


def judge(world: World, **kwargs: Any) -> dict:
    return world.judge(world.days[-1], "I0")


def test_without_news_the_driver_is_unchanged(db):
    world = launch_world(db)
    signal = judge(world)
    assert signal["signal_type"] == "first_launch" and signal["driver"] == "unknown"
    assert "news" not in signal["evidence"]["driver"]


def test_important_board_news_in_the_window_makes_the_driver_news_and_copies_the_headline(db):
    world = launch_world(db)
    i0 = world.board_ids["I0"]
    put_news(db, "工信部发布半导体产业扶持新政", datetime(2026, 11, 6, 9, 31), [i0], breaking=True)
    put_news(db, "某龙头公司发布涨价函", datetime(2026, 11, 5, 16, 20), [i0], importance=4)  # after the previous close
    put_news(db, "重要度不够的消息", datetime(2026, 11, 6, 10, 0), [i0], importance=3)
    put_news(db, "其他板块的重要消息", datetime(2026, 11, 6, 10, 0), [world.board_ids["I1"]])
    put_news(db, "窗口之前的消息", datetime(2026, 11, 5, 14, 59), [i0])  # before the previous close (15:00)
    put_news(db, "收盘后的消息", datetime(2026, 11, 6, 17, 1), [i0])  # after D 17:00
    put_news(db, "尚未分类的消息", datetime(2026, 11, 6, 11, 0), [i0], importance=None)

    signal = judge(world)
    assert signal["signal_type"] == "first_launch" and signal["driver"] == "news"
    driver = signal["evidence"]["driver"]
    assert driver["kind"] == "news"
    assert driver["text"].startswith("消息驱动：工信部发布半导体产业扶持新政")  # breaking first
    assert "某龙头公司发布涨价函" in driver["text"]
    for excluded in ("重要度不够", "其他板块", "窗口之前", "收盘后", "尚未分类"):
        assert excluded not in driver["text"]
    assert [n["title"] for n in driver["news"]] == ["工信部发布半导体产业扶持新政", "某龙头公司发布涨价函"]
    assert driver["news"][0]["is_breaking"] is True and driver["news"][0]["importance"] == 5
    assert driver["news"][0]["published_at"] == "2026-11-06T09:31:00+08:00" and driver["news"][0]["source"] == "财联社电报"
    assert driver["news_window"] == ["2026-11-05T15:00:00", "2026-11-06T17:00:00"]
    assert any(line.startswith("消息面：") and "工信部发布半导体产业扶持新政" in line for line in signal["evidence"]["lines"])


def test_news_driver_keeps_the_capital_note_and_threshold_is_configurable(db):
    world = launch_world(db, amt=3.0)  # strong volume: the capital driver would apply
    i0 = world.board_ids["I0"]
    put_news(db, "三级重要消息", datetime(2026, 11, 6, 9, 31), [i0], importance=3)
    assert judge(world)["driver"] == "capital"  # default threshold 4: importance 3 is not enough

    world.compute_all(thresholds())
    low = BoardSignalService(repo=world.repo(), thresholds=thresholds(), calendar_fn=None, news_min_importance=3)
    signal = {s["board_id"]: s for s in low.evaluate_trade_date(world.days[-1]).signals}[i0]
    assert signal["driver"] == "news" and "三级重要消息" in signal["evidence"]["driver"]["text"]
    assert "同时成交额放大至 3.0 倍" in signal["evidence"]["driver"]["text"]


def test_boards_without_a_state_are_not_touched_and_a_failing_loader_never_breaks_judgment(db):
    world = launch_world(db)
    for key in ("I1", "C0"):
        put_news(db, f"{key} 相关重要消息", datetime(2026, 11, 6, 10, 0), [world.board_ids[key]])
    world.compute_all(thresholds())
    batch = world.signals().evaluate_trade_date(world.days[-1])
    by_board = {s["board_id"]: s for s in batch.signals}
    for key in ("I1", "C0"):
        assert by_board[world.board_ids[key]]["signal_type"] in ("none", "insufficient_history")
        assert by_board[world.board_ids[key]]["driver"] == "unknown"

    def broken(since, until, min_importance):
        raise RuntimeError("news store unavailable")

    resilient = BoardSignalService(repo=world.repo(), thresholds=thresholds(), calendar_fn=None, news_loader=broken)
    batch = resilient.evaluate_trade_date(world.days[-1])
    launch = {s["board_id"]: s for s in batch.signals}[world.board_ids["I0"]]
    assert launch["signal_type"] == "first_launch" and launch["driver"] == "unknown"
    assert any("消息面数据读取失败" in w for w in batch.warnings)


def test_window_uses_the_previous_session_close_across_a_weekend(db):
    world = launch_world(db)
    svc = BoardSignalService(repo=world.repo(), thresholds=thresholds(), calendar_fn=None)
    since, until = svc.news_window(date(2026, 11, 9), [date(2026, 11, 9), date(2026, 11, 6), date(2026, 11, 5)])
    assert (since, until) == (datetime(2026, 11, 6, 15, 0), datetime(2026, 11, 9, 17, 0))  # Monday looks back to Friday 15:00
    assert svc.news_window(date(2026, 11, 9), [date(2026, 11, 9)])[0] == datetime(2026, 11, 8, 15, 0)  # no history: D-1


def test_pipeline_persists_news_driver_and_the_detail_stays_readable_after_the_news_is_purged(db):
    world = launch_world(db)
    put_news(db, "国务院印发算力基础设施行动计划", datetime(2026, 11, 6, 9, 31), [world.board_ids["I0"]], breaking=True)
    result = run_pipeline(world)
    assert result["status"] in ("succeeded", "partial")

    detail = BoardDetail.model_validate(service(world).get_board_detail(world.board_refs["I0"], None))
    assert detail.driver_kind == "news"
    assert detail.drivers.startswith("消息驱动：国务院印发算力基础设施行动计划")
    assert any("消息面：国务院印发算力基础设施行动计划" in line for line in detail.evidence)

    # news is purged after a few days; the copied headline keeps the judgment explainable
    with db.session_scope() as session:
        session.execute(delete(IntelligenceItem))
    again = BoardDetail.model_validate(service(world).get_board_detail(world.board_refs["I0"], None))
    assert again.driver_kind == "news" and "国务院印发算力基础设施行动计划" in again.drivers
