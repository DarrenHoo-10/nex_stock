# -*- coding: utf-8 -*-
"""Offline tests for AKShare news polling: fallback order, dedup, gaps, announcements, retention, cadence.

Only ``akshare`` is stubbed (``StubAkshare``); parsing, dedup, gap detection, persistence and
retention run for real against an in-memory SQLite.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Any, List, Tuple

import pandas as pd
import pytest
from sqlalchemy import select

import src.services.news_pool_service as pool
from src.repositories.intelligence_repo import IntelligenceRepository
from src.services.news_digest_service import get_news_health
from src.services.news_pool_service import NewsPoolService
from src.storage import DatabaseManager, IntelligenceItem, NewsFetchGap
from tests.news_fixtures import (
    NOW,
    at,
    brief_df,
    cninfo_fixture,
    cls_df,
    em_df,
    futu_df,
    install_stub,
    make_config,
    notice_df,
    sina_df,
)


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def stub(monkeypatch, db):
    return install_stub(monkeypatch)


def make_service(db, config=None, holdings=None, trading=True, sleeps=None) -> NewsPoolService:
    return NewsPoolService(
        IntelligenceRepository(db),
        config or make_config(),
        is_trading_day=lambda d: trading,
        holdings_provider=lambda: holdings or [],
        sleep_fn=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def all_items(db) -> List[IntelligenceItem]:
    with db.get_session() as session:
        rows = session.execute(select(IntelligenceItem).order_by(IntelligenceItem.id)).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def em_rows(*specs: Tuple[str, datetime]):
    return em_df([{"title": title, "at": when, "summary": f"{title} 详情"} for title, when in specs])


# ----------------------------------------------------------------------
# merge + dedup
# ----------------------------------------------------------------------
def test_cls_and_em_are_fetched_together_merged_and_stored_with_normalised_fields(db, stub):
    stub.flash["cls"] = cls_df([{"title": "", "content": "【央行宣布降准 0.5 个百分点】释放长期资金约 1 万亿元。", "at": at(9, 31)}])
    stub.flash["em"] = em_rows(("国家发改委召开新闻发布会", at(9, 20)), ("美股三大指数收涨", at(9, 10)))
    result = make_service(db).poll_flash(now=NOW)

    assert result["status"] == "ok" and result["inserted"] == 3
    assert stub.calls == ["cls:重点", "em"]  # no fallback source is touched while both primaries work
    rows = all_items(db)
    assert {r.source_name for r in rows} == {"akshare:cls", "akshare:em"}
    cls_row = next(r for r in rows if r.source_name == "akshare:cls")
    assert cls_row.title == "央行宣布降准 0.5 个百分点" and cls_row.source == "财联社电报"
    assert cls_row.item_kind == "flash" and cls_row.source_type == "akshare" and cls_row.market == "cn"
    assert cls_row.scope_type == "market" and cls_row.published_at == datetime(2026, 11, 9, 9, 31)
    assert cls_row.fetched_at == NOW and cls_row.classified_at is None and cls_row.source_id is None


def test_same_source_repeat_is_ignored_and_cross_source_near_duplicates_are_dropped(db, stub):
    stub.flash["cls"] = cls_df([{"title": "央行宣布下调存款准备金率 0.5 个百分点", "content": "详情", "at": at(9, 30)}])
    stub.flash["em"] = em_rows(
        ("【央行宣布下调存款准备金率0.5个百分点】", at(9, 33)),  # same headline, punctuation differs, 3 min later
        ("央行宣布下调存款准备金率 0.5 个百分点", at(13, 0)),  # same headline but hours away: a new event
        ("完全不同的消息", at(9, 34)),
    )
    service = make_service(db)
    first = service.poll_flash(now=NOW)
    assert first["inserted"] == 3 and first["near_duplicates"] == 1
    kept = {r.title for r in all_items(db)}
    assert "央行宣布下调存款准备金率 0.5 个百分点" in kept and len(all_items(db)) == 3
    assert next(r for r in all_items(db) if r.published_at == at(9, 30)).source_name == "akshare:cls"  # first source wins

    second = service.poll_flash(now=NOW + timedelta(minutes=10))
    assert second["inserted"] == 0 and second["same_source_duplicates"] >= 2
    assert len(all_items(db)) == 3


# ----------------------------------------------------------------------
# fallback order + failure isolation + timeout
# ----------------------------------------------------------------------
def test_fallbacks_are_tried_in_order_only_when_a_primary_fails_and_stop_at_the_first_success(db, stub):
    stub.flash["cls"] = cls_df([{"title": "财联社快讯", "content": "c", "at": at(9, 30)}])
    stub.flash["em"] = ConnectionError("em blocked")
    stub.flash["sina"] = TimeoutError("sina slow")
    stub.flash["futu"] = futu_df([{"title": "富途快讯", "at": at(9, 29)}])
    stub.flash["ths"] = futu_df([{"title": "同花顺快讯", "at": at(9, 28)}])
    result = make_service(db).poll_flash(now=NOW)

    assert stub.calls == ["cls:重点", "em", "sina", "futu"]  # ths is never reached
    assert result["status"] == "partial"
    assert result["sources"]["em"]["status"] == "failed" and "em blocked" in result["sources"]["em"]["error"]
    assert result["sources"]["sina"]["status"] == "failed"
    assert result["sources"]["futu"] == {"status": "ok", "count": 1, "fallback": True}
    assert {r.source_name for r in all_items(db)} == {"akshare:cls", "akshare:futu"}  # cls data survived em failing


def test_all_sources_failing_reports_failed_without_raising_and_records_the_errors(db, stub):
    for key in ("cls", "em", "sina", "futu", "ths"):
        stub.flash[key] = RuntimeError(f"{key} changed its format")
    service = make_service(db)
    result = service.poll_flash(now=NOW)
    assert result["status"] == "failed" and result["inserted"] == 0
    health = get_news_health(repo=service.repo, now=NOW, config=make_config())
    by_source = {s["source"]: s for s in health["sources"]}
    assert by_source["cls"]["last_status"] == "failed" and "changed its format" in by_source["cls"]["last_error"]
    assert by_source["cls"]["last_success_at"] is None and by_source["notice"]["last_status"] == "never"


def test_slow_source_times_out_then_is_skipped_while_its_call_is_still_running(db, stub):
    release = threading.Event()
    stub.flash["cls"] = lambda: (release.wait(10), cls_df([]))[1]
    stub.flash["em"] = em_rows(("东方财富快讯", at(9, 30)))
    stub.flash["sina"] = sina_df([{"content": "【新浪备用快讯】内容", "at": at(9, 29)}])
    service = make_service(db, make_config(news_fetch_timeout_sec=0.2))
    try:
        first = service.poll_flash(now=NOW)
        assert first["sources"]["cls"]["status"] == "failed" and "no response" in first["sources"]["cls"]["error"]
        assert first["sources"]["sina"]["status"] == "ok"  # the fallback covered the failed primary
        second = service.poll_flash(now=NOW + timedelta(minutes=10))
        assert second["sources"]["cls"]["status"] == "busy"  # previous call still running: skipped, not stacked
        assert stub.calls.count("cls:重点") == 1
        assert second["status"] == "partial"
    finally:
        release.set()


def test_overlapping_poll_rounds_do_not_run_twice(db, stub):
    assert pool._POLL_LOCK.acquire(blocking=False)
    try:
        assert make_service(db).poll_flash(now=NOW) == {"status": "skipped", "reason": "already_running"}
        assert stub.calls == []
    finally:
        pool._POLL_LOCK.release()


# ----------------------------------------------------------------------
# gap detection
# ----------------------------------------------------------------------
def test_a_batch_that_does_not_overlap_the_checkpoint_records_a_possibly_incomplete_window(db, stub):
    service = make_service(db)
    stub.flash["em"] = em_rows(("第一批-1", at(9, 0)), ("第一批-2", at(9, 10)))
    assert service.poll_flash(now=at(9, 12))["gaps"] == []  # first run: no checkpoint yet

    stub.flash["em"] = em_rows(("第二批-1", at(9, 12)), ("第二批-2", at(9, 20)))  # overlaps nothing: oldest 09:12 > 09:10
    result = service.poll_flash(now=at(9, 22))
    assert result["gaps"] == [{"source": "em", "from": "2026-11-09T09:10:00", "to": "2026-11-09T09:12:00", "covered": False}]

    health = get_news_health(repo=service.repo, now=at(9, 23), config=make_config())
    assert health["incomplete_windows"] is True
    assert health["open_gaps"][0]["source"] == "em"
    assert health["open_gaps"][0]["from"].startswith("2026-11-09T09:10:00") and health["open_gaps"][0]["from"].endswith("+08:00")

    stub.flash["em"] = em_rows(("第二批-2", at(9, 20)), ("第三批", at(9, 30)))  # overlaps the checkpoint (09:20)
    assert service.poll_flash(now=at(9, 32))["gaps"] == []
    assert len(service.repo.list_fetch_gaps()) == 1


def test_cls_with_the_important_filter_is_not_gap_checked_but_full_cls_is(db, stub):
    service = make_service(db)
    stub.flash["cls"] = cls_df([{"title": "旧", "content": "c", "at": at(9, 0)}])
    service.poll_flash(now=at(9, 5))
    stub.flash["cls"] = cls_df([{"title": "新", "content": "c", "at": at(9, 40)}])
    assert service.poll_flash(now=at(9, 41))["gaps"] == []  # 重点 is filtered client side: no continuity claim
    assert service.repo.get_fetch_state("cls")["gap_check"] == "filtered"

    full = make_service(db, make_config(news_akshare_cls_symbol="全部"))
    stub.flash["cls"] = cls_df([{"title": "更新", "content": "c", "at": at(10, 30)}])
    result = full.poll_flash(now=at(10, 31))
    assert [g["source"] for g in result["gaps"]] == ["cls"] and result["gaps"][0]["covered"] is False
    assert full.repo.get_fetch_state("cls")["gap_check"] == "full"


def test_gap_is_marked_covered_when_another_full_source_reaches_back_past_its_start(db, stub):
    service = make_service(db, make_config(news_akshare_cls_symbol="全部"))
    stub.flash["cls"] = cls_df([{"title": "旧", "content": "c", "at": at(9, 0)}])
    stub.flash["em"] = em_rows(("旧em", at(9, 0)))
    service.poll_flash(now=at(9, 5))

    stub.flash["cls"] = cls_df([{"title": "新", "content": "c", "at": at(9, 50)}])  # cls missed 09:00-09:50
    stub.flash["em"] = em_rows(("旧em", at(9, 0)), ("em补", at(9, 30)), ("em新", at(9, 49)))  # em still overlaps
    result = service.poll_flash(now=at(9, 51))
    assert result["gaps"] == [{"source": "cls", "from": "2026-11-09T09:00:00", "to": "2026-11-09T09:50:00", "covered": True}]
    health = get_news_health(repo=service.repo, now=at(9, 52), config=make_config())
    assert health["open_gaps"] == [] and health["incomplete_windows"] is False  # covered gaps are recorded, not open
    with db.get_session() as session:
        assert session.execute(select(NewsFetchGap)).scalars().one().covered is True


def test_fallback_source_is_judged_against_the_newest_item_already_in_the_pool(db, stub):
    service = make_service(db)
    stub.flash["cls"] = cls_df([{"title": "财联社", "content": "c", "at": at(9, 0)}])
    stub.flash["em"] = em_rows(("东方财富", at(9, 0)))
    service.poll_flash(now=at(9, 5))
    # em fails an hour later; sina's 20 rows start after everything the pool has -> a real hole
    stub.flash["em"] = RuntimeError("em down")
    stub.flash["sina"] = sina_df([{"content": "【新浪】晚一小时的消息", "at": at(10, 10)}])
    result = service.poll_flash(now=at(10, 12))
    assert [(g["source"], g["from"], g["covered"]) for g in result["gaps"]] == [("sina", "2026-11-09T09:00:00", False)]


# ----------------------------------------------------------------------
# cadence
# ----------------------------------------------------------------------
def _set_last_fetch(service: NewsPoolService, when: datetime) -> None:
    for key in ("cls", "em"):
        service.repo.save_fetch_state(key, last_fetch_at=when)


def test_trading_hours_use_the_short_interval_and_other_times_the_long_one(db, stub):
    service = make_service(db)
    now = at(10, 0)
    assert service.current_interval_minutes(now) == 10
    assert service.current_interval_minutes(at(7, 59)) == 30 and service.current_interval_minutes(at(15, 30)) == 30
    assert service.current_interval_minutes(at(15, 29)) == 10 and service.current_interval_minutes(at(8, 0)) == 10
    assert make_service(db, trading=False).current_interval_minutes(now) == 30  # holiday / weekend


def test_poll_tick_runs_only_when_the_current_period_interval_has_elapsed(db, stub):
    stub.flash["em"] = em_rows(("快讯", at(9, 59)))
    service = make_service(db)
    now = at(10, 0)

    assert service.poll_tick(now=now)["status"] != "skipped"  # never polled before
    _set_last_fetch(service, now - timedelta(minutes=5))
    assert service.poll_tick(now=now) == {"status": "skipped", "reason": "not_due", "interval_minutes": 10}
    _set_last_fetch(service, now - timedelta(minutes=10))
    assert service.poll_tick(now=now)["status"] != "skipped"

    off = at(18, 0)  # off hours: 30 minutes
    _set_last_fetch(service, off - timedelta(minutes=20))
    assert service.poll_tick(now=off)["reason"] == "not_due"
    _set_last_fetch(service, off - timedelta(minutes=30))
    assert service.poll_tick(now=off)["interval_minutes"] == 30

    holiday = make_service(db, trading=False)  # 10:00 on a non-trading day is "other time": 30 minutes
    _set_last_fetch(holiday, now - timedelta(minutes=20))
    assert holiday.poll_tick(now=now)["reason"] == "not_due"
    assert service.poll_tick(now=now, force=True)["status"] != "skipped"


# ----------------------------------------------------------------------
# announcements + 财经早餐
# ----------------------------------------------------------------------
def test_announcements_for_a_small_watchlist_are_fetched_per_code_with_a_pause_between_requests(db, stub):
    day = date(2026, 11, 9)
    stub.notice_by_code["600519"] = notice_df([{"code": "600519", "title": "回购公告", "day": day, "art": "A1"}])
    stub.notice_by_code["300750"] = notice_df([{"code": "300750", "title": "业绩预告", "day": date(2026, 11, 8), "art": "A2"}])
    stub.notice_by_code["601318"] = notice_df([{"code": "601318", "title": "持仓股公告", "day": day, "art": "A3"}])
    sleeps: List[float] = []
    service = make_service(db, holdings=[("cn", "601318"), ("us", "AAPL"), ("hk", "00700")], sleeps=sleeps)
    result = service.run_daily_fetch(day=day, now=at(8, 5))

    ann = result["announcements"]
    # 巨潮 is not stubbed for these codes, so every code falls back to 东方财富 (3 + 3 calls).
    assert ann["mode"] == "by_code" and ann["codes"] == 3 and ann["api_calls"] == 6 and ann["inserted"] == 3
    assert ann["sources"]["cninfo"]["status"] == "failed" and ann["sources"]["notice"]["status"] == "ok"
    assert stub.calls.count("notice_code:600519") == 1 and not any(c.startswith("notice_day") for c in stub.calls)
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)  # paced: no burst against the data centre
    assert stub.notice_calls[0] == {"security": "300750", "begin": "2026-11-08", "end": "2026-11-09"}  # sorted, yesterday..today
    rows = [r for r in all_items(db) if r.item_kind == "announcement"]
    assert sorted(r.scope_value for r in rows) == ["300750", "600519", "601318"]
    assert all(r.scope_type == "symbol" and r.news_scope == "stock" and r.published_at.hour == 0 for r in rows)
    assert result["status"] == "ok"

    again = service.run_daily_fetch(day=day, now=at(8, 6))  # same URLs: nothing new
    assert again["announcements"]["inserted"] == 0


def test_announcements_for_a_large_watchlist_use_the_market_listing_and_filter_locally(db, stub):
    day = date(2026, 11, 9)  # Monday: market mode covers the last days since the previous run
    codes = [f"60{i:04d}" for i in range(20)]
    stub.notice_by_day["20261109"] = notice_df([
        {"code": codes[0], "title": "命中自选", "day": day, "art": "M1"},
        {"code": "000001", "title": "不在自选", "day": day, "art": "M2"},
    ])
    stub.notice_by_day["20261108"] = notice_df([{"code": codes[1], "title": "周日公告", "day": date(2026, 11, 8), "art": "M3"}])
    config = make_config(stock_list=codes)
    result = make_service(db, config).run_daily_fetch(day=day, now=at(8, 5))
    ann = result["announcements"]
    assert ann["mode"] == "market" and ann["inserted"] == 2 and ann["api_calls"] == 2
    assert sorted(c for c in stub.calls if c.startswith("notice_day")) == ["notice_day:20261108", "notice_day:20261109"]
    assert {r.scope_value for r in all_items(db) if r.item_kind == "announcement"} == {codes[0], codes[1]}

    forced = make_service(db, make_config(stock_list=["600519"], news_announcement_mode="market"))
    assert forced._announcement_mode(1) == "market"
    assert make_service(db, make_config(news_announcement_mode="by_code"))._announcement_mode(99) == "by_code"
    assert make_service(db, make_config(news_announcement_by_code_max=2))._announcement_mode(3) == "market"


def test_one_failing_code_does_not_stop_the_others_and_repeated_failures_stop_the_run(db, stub):
    day = date(2026, 11, 9)
    config = make_config(stock_list=["600001", "600002", "600003", "600004", "600005"])
    stub.notice_by_code["600001"] = notice_df([{"code": "600001", "title": "公告一", "day": day, "art": "B1"}])
    stub.notice_by_code["600002"] = ConnectionError("blocked")
    stub.notice_by_code["600003"] = notice_df([{"code": "600003", "title": "公告三", "day": day, "art": "B3"}])
    partial = make_service(db, config).run_daily_fetch(day=day, now=at(8, 5))["announcements"]
    assert partial["status"] == "partial" and partial["inserted"] == 2 and any("600002" in e for e in partial["errors"])

    stub.notice_calls.clear()
    stub.calls.clear()
    for code in ("600001", "600002", "600003", "600004", "600005"):
        stub.notice_by_code[code] = ConnectionError("rate limited")
    failed = make_service(db, config).run_daily_fetch(day=day, now=at(8, 6))["announcements"]
    assert failed["status"] == "failed" and len(stub.notice_calls) == 3  # stops after 3 consecutive failures
    assert any("停止" in e for e in failed["errors"])


def test_cninfo_is_the_primary_announcement_source_per_code(db, stub):
    day = date(2026, 11, 9)
    stub.cninfo_by_code["600089"] = dict(cninfo_fixture(), hasMore=False)
    stub.cninfo_by_code["300750"] = {"announcements": [], "hasMore": False}
    service = make_service(db, make_config(stock_list=["600089", "300750"]))
    ann = service.run_daily_fetch(day=day, now=at(8, 5))["announcements"]

    assert ann["status"] == "ok" and ann["sources"] == {
        "cninfo": {"status": "ok", "calls": 2, "fetched": 3, "errors": []},
    }
    assert not any(c.startswith("notice") for c in stub.calls)  # 东方财富 is not touched while 巨潮 works
    assert stub.calls.count("cninfo_orgids") == 1  # orgId map is loaded once and cached
    # 300750's real orgId (GD165627) is irregular; the pattern guess would have been gssz0300750.
    by_code = {r["stock"].split(",")[0]: r for r in stub.cninfo_requests}
    assert by_code["300750"]["stock"] == "300750,GD165627" and by_code["600089"]["stock"] == "600089,gssh0600089"
    assert by_code["600089"]["seDate"] == "2026-11-08~2026-11-09"

    rows = sorted((r for r in all_items(db) if r.item_kind == "announcement"), key=lambda r: r.url)
    assert len(rows) == 3 and {r.source for r in rows} == {"巨潮资讯公告"}
    assert {r.source_name for r in rows} == {"akshare:cninfo"}
    assert all(r.scope_type == "symbol" and r.scope_value == "600089" and r.url.startswith(
        "https://static.cninfo.com.cn/finalpage/2026-10-30/") for r in rows)
    assert "特变电工股份有限公司2026年第三季度报告" in {r.title for r in rows}  # already starts with the name
    assert rows[0].published_at == datetime(2026, 10, 30, 0, 0)

    health = get_news_health(repo=service.repo, now=NOW, config=make_config())
    by_source = {s["source"]: s for s in health["sources"]}
    assert by_source["cninfo"]["label"] == "巨潮资讯公告" and by_source["cninfo"]["last_status"] == "ok"
    assert by_source["notice"]["last_status"] == "never"


def test_cninfo_failure_falls_back_to_east_money_for_that_code_only(db, stub):
    day = date(2026, 11, 9)
    stub.cninfo_by_code["600089"] = cninfo_fixture()
    stub.cninfo_by_code["600519"] = ConnectionError("cninfo reset")
    stub.notice_by_code["600519"] = notice_df([{"code": "600519", "title": "回购公告", "day": day, "art": "D1"}])
    service = make_service(db, make_config(stock_list=["600089", "600519"]))
    ann = service.run_daily_fetch(day=day, now=at(8, 5))["announcements"]

    assert ann["status"] == "ok" and ann["errors"] == []  # every code was covered by some source
    assert ann["sources"]["cninfo"]["status"] == "partial" and "cninfo reset" in ann["sources"]["cninfo"]["errors"][0]
    assert ann["sources"]["notice"] == {"status": "ok", "calls": 1, "fetched": 1, "errors": []}
    assert stub.calls.count("notice_code:600519") == 1 and "notice_code:600089" not in stub.calls
    health = {s["source"]: s for s in get_news_health(repo=service.repo, now=NOW, config=make_config())["sources"]}
    assert health["cninfo"]["last_status"] == "partial" and health["notice"]["last_status"] == "ok"

    # Next run starts from the day either source last succeeded.
    assert service._announcement_window_begin(date(2026, 11, 10)) == date(2026, 11, 9)


def test_orgid_map_failure_uses_the_code_pattern_and_market_mode_falls_back_to_cninfo(db, stub):
    day = date(2026, 11, 9)
    stub.cninfo_orgid_map = ConnectionError("map down")
    stub.cninfo_by_code["600089"] = cninfo_fixture()
    stub.notice_by_day["20261109"] = RuntimeError("em listing blocked")
    stub.notice_by_day["20261108"] = RuntimeError("em listing blocked")
    config = make_config(stock_list=["600089"], news_announcement_mode="market")
    ann = make_service(db, config).run_daily_fetch(day=day, now=at(8, 5))["announcements"]

    assert ann["mode"] == "market" and ann["inserted"] == 3 and ann["status"] == "ok"
    assert ann["sources"]["notice"]["status"] == "failed" and ann["sources"]["cninfo"]["status"] == "ok"
    assert stub.cninfo_requests[0]["stock"] == "600089,gssh0600089"


def test_no_watchlist_and_no_holdings_skips_announcements_but_still_fetches_the_brief(db, stub):
    stub.brief = brief_df([{"title": "财经早餐 11月9日", "summary": "隔夜美股收涨，A 股今日关注……", "at": at(7, 20), "url": "https://finance.eastmoney.com/a/zc.html"}])
    result = make_service(db, make_config(stock_list=[])).run_daily_fetch(day=date(2026, 11, 9), now=at(8, 5))
    assert result["announcements"]["status"] == "empty" and result["announcements"]["reason"] == "no_codes"
    assert result["morning_brief"] == {"status": "ok", "fetched": 1, "inserted": 1}
    assert not any(c.startswith("notice") for c in stub.calls)
    brief = next(r for r in all_items(db) if r.item_kind == "morning_brief")
    assert brief.source == "东方财富财经早餐" and brief.published_at == at(7, 20)


def test_morning_brief_failure_is_isolated_from_announcements(db, stub):
    stub.brief = RuntimeError("cjzc changed")
    day = date(2026, 11, 9)
    stub.notice_by_code["600519"] = notice_df([{"code": "600519", "title": "公告", "day": day, "art": "C1"}])
    result = make_service(db, make_config(stock_list=["600519"])).run_daily_fetch(day=day, now=at(8, 5))
    assert result["morning_brief"]["status"] == "failed" and result["announcements"]["inserted"] == 1
    assert result["status"] == "partial"


# ----------------------------------------------------------------------
# tiered retention
# ----------------------------------------------------------------------
def test_flash_is_kept_three_days_announcements_thirty_and_legacy_sources_follow_their_own_key(db, stub):
    repo = IntelligenceRepository(db)
    now = datetime(2026, 11, 20, 12, 0)

    def put(**fields: Any) -> None:
        base = dict(source_type="akshare", source_name="akshare:em", url="u", title="t", market="cn",
                    scope_type="market", scope_value="__none__", fetched_at=now)
        base.update(fields)
        with db.session_scope() as session:
            session.add(IntelligenceItem(**base))

    put(url="flash-new", title="新快讯", item_kind="flash", published_at=now - timedelta(days=1))
    put(url="flash-old", title="旧快讯", item_kind="flash", published_at=now - timedelta(days=4))
    put(url="brief-old", title="旧早餐", item_kind="morning_brief", published_at=now - timedelta(days=4))
    put(url="ann-10", title="十天前公告", item_kind="announcement", scope_type="symbol", scope_value="600519",
        published_at=now - timedelta(days=10))
    put(url="ann-40", title="四十天前公告", item_kind="announcement", scope_type="symbol", scope_value="600519",
        published_at=now - timedelta(days=40))
    put(url="rss-old", title="旧 RSS", source_type="rss", source_name="rss-src", item_kind=None,
        published_at=now - timedelta(days=5), fetched_at=now - timedelta(days=5))

    deleted = repo.apply_news_retention(flash_days=3, company_days=30, now=now)
    assert deleted["flash"] == 2 and deleted["company"] == 1
    titles = {r.title for r in all_items(db)}
    assert titles == {"新快讯", "十天前公告", "旧 RSS"}  # akshare tiers never touch rss rows

    # the legacy key (NEWS_INTEL_RETENTION_DAYS) handles rss / atom / newsnow only, never akshare rows
    with db.session_scope() as session:
        for row in session.execute(select(IntelligenceItem)).scalars():
            row.fetched_at = datetime.now() - timedelta(days=400)
    assert repo.apply_retention(30) == 1
    assert {r.title for r in all_items(db)} == {"新快讯", "十天前公告"}


def test_poll_applies_retention_and_drops_stale_gap_records(db, stub):
    service = make_service(db)
    service.repo.add_fetch_gap("em", at(1, 0, day=date(2026, 11, 1)), at(2, 0, day=date(2026, 11, 1)), covered=False,
                               detected_at=at(2, 0, day=date(2026, 11, 1)))
    stub.flash["em"] = em_rows(("今天", at(9, 0)))
    result = service.poll_flash(now=NOW)
    assert result["retention"]["gaps"] == 1 and service.repo.list_fetch_gaps() == []


# ----------------------------------------------------------------------
# schema migration
# ----------------------------------------------------------------------
def test_existing_databases_get_the_nullable_classification_columns_and_news_state_tables(tmp_path):
    from sqlalchemy import inspect, text

    from src.storage import _INTELLIGENCE_ITEM_NEWS_COLUMN_SQL

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=url)
    with manager.session_scope() as session:
        session.add(IntelligenceItem(
            source_type="rss", source_name="legacy", title="旧资讯", url="https://example.test/legacy",
            scope_type="market", scope_value="__none__", market="cn",
        ))
    with manager._engine.begin() as connection:  # simulate a database created before the columns existed
        for column in _INTELLIGENCE_ITEM_NEWS_COLUMN_SQL:
            connection.execute(text(f"ALTER TABLE intelligence_items DROP COLUMN {column}"))
        connection.execute(text("DROP TABLE news_fetch_state"))
        connection.execute(text("DROP TABLE news_fetch_gap"))
    DatabaseManager.reset_instance()
    try:
        migrated = DatabaseManager(db_url=url)
        columns = {c["name"] for c in inspect(migrated._engine).get_columns("intelligence_items")}
        assert set(_INTELLIGENCE_ITEM_NEWS_COLUMN_SQL) <= columns
        tables = set(inspect(migrated._engine).get_table_names())
        assert {"news_fetch_state", "news_fetch_gap"} <= tables
        [row] = all_items(migrated)
        assert row.title == "旧资讯" and row.importance is None and row.classified_at is None and row.item_kind is None
        # legacy rows stay readable through the existing list API
        assert IntelligenceRepository(migrated).list_items()[1] == 1
    finally:
        DatabaseManager.reset_instance()
