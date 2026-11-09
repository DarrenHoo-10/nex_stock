# -*- coding: utf-8 -*-
"""Offline tests for the digest contract: keys, ordering, filters, time zones, morning brief and health."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional

import pytest

from src.repositories.intelligence_repo import IntelligenceRepository
from src.services.news_digest_service import build_news_digest, get_morning_brief, get_news_health
from src.storage import DatabaseManager, IntelligenceItem
from tests.news_fixtures import at, make_config

DIGEST_KEYS = {
    "id", "title", "summary", "source", "published_at", "scope", "importance", "is_breaking", "board_ids",
    "board_names", "us_tickers", "codes", "url", "kind",
}


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def put(db, title: str, when: Optional[datetime], *, importance: Optional[int] = None, breaking: Optional[bool] = None,
        boards: Optional[List[int]] = None, codes: Optional[List[str]] = None, us: Optional[List[str]] = None,
        kind: Optional[str] = "flash", source_type: str = "akshare", scope: Optional[str] = None,
        scope_type: str = "market", scope_value: str = "__none__", url: Optional[str] = None, summary: str = "摘要",
        fetched_at: Optional[datetime] = None) -> int:
    classified = importance is not None
    with db.session_scope() as session:
        row = IntelligenceItem(
            source_type=source_type, source_name=f"{source_type}:x", title=title, summary=summary,
            url=url or f"https://example.test/{title}", source="来源", published_at=when,
            fetched_at=fetched_at or at(12), scope_type=scope_type, scope_value=scope_value, market="cn",
            item_kind=kind, news_scope=scope, importance=importance, is_breaking=breaking,
            board_ids=json.dumps(boards) if boards is not None else None,
            board_names=json.dumps([f"板块{b}" for b in boards], ensure_ascii=False) if boards else None,
            us_tickers=json.dumps(us) if us else None,
            stock_codes=json.dumps(codes) if codes else None,
            classified_at=at(12) if classified else None,
        )
        session.add(row)
        session.flush()
        return row.id


SINCE = at(0)
UNTIL = at(23, 59)


def titles(items: List[dict]) -> List[str]:
    return [i["title"] for i in items]


def test_every_item_has_exactly_the_contract_keys_and_a_china_tz_iso_time(db):
    put(db, "央行降准", at(9, 30), importance=5, breaking=True, boards=[3], codes=["600519"], us=["NVDA"],
        scope="board", url="https://finance.example.test/a")
    [item] = build_news_digest(SINCE, UNTIL)
    assert set(item) == DIGEST_KEYS
    assert item["published_at"] == "2026-11-09T09:30:00+08:00"
    assert (item["scope"], item["importance"], item["is_breaking"]) == ("board", 5, True)
    assert item["board_ids"] == [3] and item["board_names"] == ["板块3"] and item["us_tickers"] == ["NVDA"]
    assert item["codes"] == ["600519"] and item["url"] == "https://finance.example.test/a" and item["kind"] == "flash"
    assert item["source"] == "来源" and item["summary"] == "摘要"


def test_sorted_by_breaking_then_importance_then_newest_with_unclassified_last(db):
    put(db, "普通-重要度3-早", at(9, 0), importance=3, breaking=False)
    put(db, "普通-重要度5", at(8, 0), importance=5, breaking=False)
    put(db, "突发-重要度3", at(7, 0), importance=3, breaking=True)
    put(db, "普通-重要度3-晚", at(10, 0), importance=3, breaking=False)
    put(db, "未分类-最新", at(11, 0))
    put(db, "未分类-较早", at(6, 0))
    put(db, "普通-重要度1", at(5, 0), importance=1, breaking=False)
    assert titles(build_news_digest(SINCE, UNTIL)) == [
        "突发-重要度3", "普通-重要度5", "普通-重要度3-晚", "普通-重要度3-早", "普通-重要度1", "未分类-最新", "未分类-较早",
    ]
    unclassified = [i for i in build_news_digest(SINCE, UNTIL) if i["importance"] is None]
    assert all(i["is_breaking"] is False and i["board_ids"] == [] and i["us_tickers"] == [] for i in unclassified)
    assert titles(build_news_digest(SINCE, UNTIL, limit=2)) == ["突发-重要度3", "普通-重要度5"]
    assert build_news_digest(SINCE, UNTIL, limit=0) == []


def test_filters_by_time_codes_boards_and_min_importance(db):
    put(db, "板块1重要", at(9, 0), importance=4, breaking=False, boards=[1])
    put(db, "板块1不重要", at(9, 1), importance=2, breaking=False, boards=[1])
    put(db, "板块2", at(9, 2), importance=5, breaking=False, boards=[2])
    put(db, "茅台消息", at(9, 3), importance=3, breaking=False, codes=["600519"])
    put(db, "昨天的消息", at(9, 0, day=date(2026, 11, 8)), importance=5, breaking=False, boards=[1])
    put(db, "未分类", at(9, 4))
    put(db, "宁德公告", at(0, 0), kind="announcement", scope_type="symbol", scope_value="300750", importance=3,
        breaking=False, codes=["300750"])

    assert titles(build_news_digest(SINCE, UNTIL, board_ids=[1])) == ["板块1重要", "板块1不重要"]
    assert titles(build_news_digest(SINCE, UNTIL, board_ids=[1, 2], min_importance=4)) == ["板块2", "板块1重要"]
    assert titles(build_news_digest(SINCE, UNTIL, codes=["sh600519"])) == ["茅台消息"]  # code spellings normalised
    assert titles(build_news_digest(SINCE, UNTIL, codes=["300750"])) == ["宁德公告"]  # symbol scope + codes both match
    assert "未分类" not in titles(build_news_digest(SINCE, UNTIL, min_importance=1))  # unclassified never meet a threshold
    assert "昨天的消息" in titles(build_news_digest(at(0, day=date(2026, 11, 8)), UNTIL))
    assert titles(build_news_digest(at(9, 1), at(9, 2))) == ["板块2", "宁德公告", "板块1不重要"]  # edges inclusive; the announcement matches by its date
    assert build_news_digest(UNTIL, SINCE) == []  # until before since
    assert titles(build_news_digest(at(9, 3), None, codes=None)) is not None  # until defaults to now


def test_announcements_are_matched_by_calendar_day_not_by_time_of_day(db):
    put(db, "今日公告", at(0, 0), kind="announcement", scope_type="symbol", scope_value="600519", importance=3, breaking=False)
    put(db, "昨日公告", at(0, 0, day=date(2026, 11, 8)), kind="announcement", scope_type="symbol", scope_value="600519",
        importance=3, breaking=False)
    # window "since yesterday's close": the evening announcement dated yesterday and today's are both relevant
    since = datetime(2026, 11, 8, 15, 0)
    assert sorted(titles(build_news_digest(since, at(17), codes=["600519"]))) == ["今日公告", "昨日公告"]
    assert titles(build_news_digest(at(8, 45), at(17), codes=["600519"])) == ["今日公告"]  # a 08:45 push window still sees today's


def test_time_zones_are_normalised_akshare_is_china_time_and_legacy_sources_are_utc(db):
    put(db, "akshare 09:30 中国时间", at(9, 30), importance=3, breaking=False)
    # newsnow / rss rows are stored as naive UTC: 02:30 UTC == 10:30 China time
    put(db, "newsnow 10:30 中国时间", datetime(2026, 11, 9, 2, 30), source_type="newsnow", kind=None, importance=3, breaking=False)
    items = build_news_digest(at(9, 0), at(11, 0))
    assert {i["title"]: i["published_at"] for i in items} == {
        "akshare 09:30 中国时间": "2026-11-09T09:30:00+08:00",
        "newsnow 10:30 中国时间": "2026-11-09T10:30:00+08:00",
    }
    assert [i for i in items if i["title"].startswith("newsnow")][0]["kind"] == "flash"
    # aware boundaries in another zone work the same way
    utc_since = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)  # 09:00 China
    assert len(build_news_digest(utc_since, datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc))) == 2
    assert build_news_digest(datetime(2026, 11, 9, 1, 45, tzinfo=timezone.utc), datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc))[0]["title"].startswith("newsnow")


def test_url_less_items_and_scope_fallbacks(db):
    put(db, "无链接", at(9, 0), url="no-url:intel:cls:abc123")
    put(db, "个股资讯", at(9, 1), scope_type="symbol", scope_value="000001", kind="announcement")
    items = {i["title"]: i for i in build_news_digest(SINCE, UNTIL)}
    assert items["无链接"]["url"] == "" and items["无链接"]["scope"] == "market"
    assert items["个股资讯"]["scope"] == "stock" and items["个股资讯"]["codes"] == ["000001"]


def test_morning_brief_returns_the_newest_brief_of_that_day_with_full_content(db):
    long_text = "隔夜美股收涨。" * 100
    put(db, "财经早餐 11月9日", at(7, 20), kind="morning_brief", summary=long_text, url="https://finance.example.test/zc9")
    put(db, "财经早餐 11月8日", at(7, 20, day=date(2026, 11, 8)), kind="morning_brief", summary="旧")
    put(db, "普通快讯", at(7, 25), summary="x")
    brief = get_morning_brief(date(2026, 11, 9))
    assert brief is not None and set(brief) == DIGEST_KEYS | {"content"}
    assert brief["title"] == "财经早餐 11月9日" and brief["kind"] == "morning_brief" and brief["content"] == long_text
    assert brief["published_at"] == "2026-11-09T07:20:00+08:00" and brief["url"] == "https://finance.example.test/zc9"
    assert get_morning_brief(date(2026, 11, 7)) is None
    put(db, "财经早餐 11月9日（更新）", at(7, 50), kind="morning_brief", summary="更新版")
    assert get_morning_brief(date(2026, 11, 9))["title"] == "财经早餐 11月9日（更新）"


def test_health_reports_per_source_status_open_gaps_and_pending_classification(db):
    repo = IntelligenceRepository(db)
    repo.save_fetch_state("cls", last_fetch_at=at(9, 50), last_success_at=at(9, 40), last_status="failed",
                          last_error="TimeoutError: cls: no response within 30s", last_count=20, checkpoint_at=at(9, 30),
                          gap_check="filtered")
    repo.save_fetch_state("em", last_fetch_at=at(9, 50), last_success_at=at(9, 50), last_status="ok", last_count=200,
                          checkpoint_at=at(9, 49), gap_check="full")
    repo.add_fetch_gap("em", at(9, 10), at(9, 20), covered=False, detected_at=at(9, 22))
    repo.add_fetch_gap("cls", at(8, 10), at(8, 20), covered=True, detected_at=at(8, 22))
    repo.add_fetch_gap("em", at(8, 0, day=date(2026, 10, 1)), at(8, 5, day=date(2026, 10, 1)), covered=False,
                       detected_at=at(8, 5, day=date(2026, 10, 1)))  # older than the retention window
    put(db, "待分类", at(9, 0))

    health = get_news_health(repo=repo, now=at(10, 0), config=make_config())
    by_source = {s["source"]: s for s in health["sources"]}
    assert by_source["cls"]["last_status"] == "failed" and "no response" in by_source["cls"]["last_error"]
    assert by_source["cls"]["last_success_at"] == "2026-11-09T09:40:00+08:00" and by_source["cls"]["gap_check"] == "filtered"
    assert by_source["em"]["last_status"] == "ok" and by_source["em"]["last_error"] is None
    assert by_source["sina"]["last_status"] == "never" and by_source["sina"]["last_success_at"] is None
    assert {"cls", "em", "sina", "futu", "ths", "cninfo", "notice", "szse", "irm", "sse_e", "cjzc", "wscn", "major"} == set(by_source)
    assert [(g["source"], g["from"], g["to"]) for g in health["open_gaps"]] == [
        ("em", "2026-11-09T09:10:00+08:00", "2026-11-09T09:20:00+08:00")
    ]  # covered and expired gaps are not "open"
    assert health["incomplete_windows"] is True and health["unclassified_pending"] == 1
    assert health["checked_at"].endswith("+08:00")
