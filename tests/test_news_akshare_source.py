# -*- coding: utf-8 -*-
"""Offline tests for the AKShare news source parsers and the timeout / skip-if-running wrapper.

``ak.*`` is replaced by ``StubAkshare`` returning fixture DataFrames shaped like AKShare 1.19.3.
"""

from __future__ import annotations

import threading
from datetime import date, datetime

import pandas as pd
import pytest

from src.services import news_akshare_source as src
from tests.news_fixtures import (
    StubAkshare,
    at,
    brief_df,
    cls_df,
    em_df,
    futu_df,
    install_stub,
    notice_df,
    sina_df,
)


def test_cls_rows_without_title_get_a_derived_title_and_a_stable_synthetic_url():
    df = cls_df([
        {"title": "", "content": "【国务院发布算力基础设施新规】要求加快智算中心建设。", "at": at(9, 31, 5)},
        {"title": "", "content": "美联储官员表示将维持利率不变，市场预期降息推迟。后续仍需观察", "at": at(9, 30, 0)},
        {"title": "央行开展逆回购", "content": "央行今日开展 500 亿元逆回购操作", "at": at(9, 29, 0)},
    ])
    records = src.parse_cls(df)
    assert [r.title for r in records] == [
        "国务院发布算力基础设施新规",
        "美联储官员表示将维持利率不变，市场预期降息推迟",
        "央行开展逆回购",
    ]
    first = records[0]
    assert first.published_at == datetime(2026, 11, 9, 9, 31, 5)  # China time, naive
    assert first.url.startswith(src.NO_URL_PREFIX) and first.kind == "flash"
    assert first.source_name == "akshare:cls" and first.source_label == "财联社电报"
    assert first.scope_type == "market" and first.scope_value is None
    # the same row parses to the same key (re-polls must dedup), a different row to another one
    assert src.parse_cls(df)[0].url == first.url and records[1].url != first.url


def test_em_sina_futu_ths_and_brief_parsers_normalise_title_content_url_and_time():
    em = src.parse_em(em_df([{"title": "A 股收盘三大指数集体上涨", "summary": "沪指涨 1%", "at": at(15, 1), "url": "https://finance.eastmoney.com/a/1.html"}]))
    assert em[0].url == "https://finance.eastmoney.com/a/1.html" and em[0].summary == "沪指涨 1%"
    assert em[0].published_at == at(15, 1)

    sina = src.parse_sina(sina_df([{"content": "【工信部：支持人工智能产业发展】工信部今日表示……", "at": at(11, 2)}]))
    assert sina[0].title == "工信部：支持人工智能产业发展" and sina[0].source_key == "sina"
    assert sina[0].url.startswith(src.NO_URL_PREFIX)

    futu = src.parse_futu(futu_df([{"title": "英伟达盘后大涨", "content": "财报超预期", "at": at(5, 0)}]))
    assert futu[0].source_key == "futu" and futu[0].url.startswith("https://news.futunn.com")

    ths = src.parse_ths(futu_df([{"title": "同花顺快讯", "content": "正文", "at": at(10, 5)}]))
    assert ths[0].source_key == "ths"

    brief = src.parse_cjzc(brief_df([{"title": "财经早餐 11月9日", "summary": "隔夜美股收涨", "at": at(7, 30), "url": "https://finance.eastmoney.com/a/b.html"}]))
    assert brief[0].kind == "morning_brief" and brief[0].source_key == "cjzc"


def test_unusable_rows_are_dropped_and_empty_frames_parse_to_nothing():
    df = cls_df([{"title": "", "content": "", "at": at(9, 0)}, {"title": "有效", "content": "x", "at": at(9, 1)}])
    assert [r.title for r in src.parse_cls(df)] == ["有效"]
    assert src.parse_cls(pd.DataFrame()) == [] and src.parse_em(None) == []
    # unparsable time keeps the item with published_at None (stored by fetch time)
    odd = em_df([{"title": "时间坏了", "at": at(9, 0)}])
    odd.loc[0, "发布时间"] = "not-a-time"
    assert src.parse_em(odd)[0].published_at is None


def test_notice_parser_filters_to_wanted_codes_and_marks_them_as_stock_scope():
    df = notice_df([
        {"code": "600519", "title": "关于回购股份的公告", "day": date(2026, 11, 9), "art": "AN1"},
        {"code": "000001", "title": "平安银行公告", "day": date(2026, 11, 9), "art": "AN2"},
        {"code": "300750", "title": "宁德时代业绩预告", "day": date(2026, 11, 8), "art": "AN3", "category": "财务报告"},
    ])
    records = src.parse_notices(df, ["sh600519", "300750.SZ"])
    assert sorted(r.scope_value for r in records) == ["300750", "600519"]
    moutai = next(r for r in records if r.scope_value == "600519")
    assert moutai.kind == "announcement" and moutai.scope_type == "symbol" and moutai.stock_codes == ["600519"]
    assert moutai.title == "名称600519：关于回购股份的公告"
    assert moutai.published_at == datetime(2026, 11, 9)  # announcements only carry a date
    assert moutai.url.endswith("AN1.html")
    assert len(src.parse_notices(df)) == 3  # no filter: everything


def test_normalize_a_share_code():
    assert src.normalize_a_share_code("sh600519") == "600519"
    assert src.normalize_a_share_code("600519.SH") == "600519"
    assert src.normalize_a_share_code("300750") == "300750"
    assert src.normalize_a_share_code("hk00700") == "" and src.normalize_a_share_code("AAPL") == ""


def test_fetch_flash_uses_the_configured_cls_symbol_and_rejects_unknown_sources(monkeypatch):
    stub = install_stub(monkeypatch)
    stub.flash["cls"] = cls_df([{"title": "t", "content": "c", "at": at(9, 0)}])
    assert len(src.fetch_flash("cls", timeout_sec=5, cls_symbol="全部")) == 1
    assert stub.calls == ["cls:全部"]
    with pytest.raises(src.NewsSourceError):
        src.fetch_flash("nope", timeout_sec=5)


def test_call_with_timeout_times_out_and_skips_a_source_whose_previous_call_is_running():
    src.reset_inflight_for_tests()
    release = threading.Event()
    try:
        with pytest.raises(src.NewsSourceTimeout):
            src.call_with_timeout("slow", lambda: release.wait(5), 0.2)
        # the abandoned worker is still running: the next round must skip, not pile up threads
        with pytest.raises(src.NewsSourceBusy):
            src.call_with_timeout("slow", lambda: "never runs", 0.2)
        assert src.call_with_timeout("other", lambda: 42, 1) == 42  # other sources are unaffected
    finally:
        release.set()
    for _ in range(50):  # once the worker finishes, the source is usable again
        try:
            assert src.call_with_timeout("slow", lambda: "ok", 1) == "ok"
            break
        except src.NewsSourceBusy:
            threading.Event().wait(0.05)
    else:  # pragma: no cover
        pytest.fail("worker never finished")


def test_call_with_timeout_reraises_the_callers_exception():
    src.reset_inflight_for_tests()

    def boom():
        raise ConnectionError("upstream down")

    with pytest.raises(ConnectionError):
        src.call_with_timeout("boom", boom, 1)


def test_stub_never_reaches_the_real_akshare(monkeypatch):
    stub = install_stub(monkeypatch)
    assert isinstance(src._akshare(), StubAkshare) and stub.calls == []


def test_morning_brief_keeps_only_recent_issues(monkeypatch):
    # The real endpoint returns hundreds of past issues (about 400 when sampled); only recent ones are kept.
    stub = install_stub(monkeypatch)
    stub.brief = brief_df([
        {"title": "财经早餐 11月9日", "summary": "今日", "at": at(6, 0), "url": "https://finance.eastmoney.com/a/9.html"},
        {"title": "财经早餐 11月8日", "summary": "昨日", "at": at(6, 0, day=date(2026, 11, 8)), "url": "https://finance.eastmoney.com/a/8.html"},
        {"title": "财经早餐 3月13日", "summary": "很早", "at": at(6, 0, day=date(2026, 3, 13)), "url": "https://finance.eastmoney.com/a/3.html"},
    ])
    kept = src.fetch_morning_brief(timeout_sec=5, now=at(8, 10))
    assert [r.title for r in kept] == ["财经早餐 11月9日", "财经早餐 11月8日"]


def test_cls_defaults_to_full_scope(monkeypatch):
    stub = install_stub(monkeypatch)
    src.fetch_flash("cls", timeout_sec=5)
    assert stub.calls == ["cls:全部"]
