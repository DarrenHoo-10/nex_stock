# -*- coding: utf-8 -*-
"""Screening candidate news: stored news pool first, readable on-topic web results only (no network)."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.config import Config
from src.search_service import SearchResponse, SearchResult, SearchService
from src.services import screening_service
from src.storage import DatabaseManager, IntelligenceItem

NOW = datetime(2026, 11, 8, 22, 0)

# Real titles / snippets stored by the 2026-11-08 capital_heat run (证券之星 pages returned by
# Tavily with GBK text decoded as another code page).
GARBLED_TITLE = "116չETF̻ݶ200ݣزֹر繤¡ܡԴ_ƱƵ_֤ȯ֮"
GARBLED_SNIPPET = "ȨϢٱר\n\n֤ȯ֮Ϣ116գETF̻516230·ݶΪ7.55ڷݣǰһ200.0ݡ"
GARBLED_NAV = "ҳ\n Ѷ\n Ʊ\n ƾ\n \n ר\n \n \n ר\n \n\n  Ʊ |  ض б ¹ ҵ ƴ Ȩ"


def _result(title, snippet="", url=None, **kwargs):
    return SearchResult(title=title, snippet=snippet, url=url or f"https://example.invalid/{abs(hash(title))}",
                        source="example.invalid", **kwargs)


# ---------------------------------------------------------------------------
# shared admission filter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("item", [
    _result(GARBLED_TITLE, GARBLED_SNIPPET),
    _result("ֱɷݣ600038116ʽ7084.00Ԫ_ƱƵ_֤ȯ֮", GARBLED_NAV),
    _result("è‚¡ç¥¨ æœ€æ–°æ¶ˆæ¯ ç‰¹å˜ç”µå·¥"),
    _result("特变电工 � 业绩 � 快报 �"),
])
def test_garbled_text_is_detected(item):
    assert SearchService._has_garbled_text_signal(item)


@pytest.mark.parametrize("item", [
    _result("特变电工股份有限公司2025年半年度业绩快报公告", "证券代码：600089 · 公告编号：临2025-053"),
    _result("Novo Nordisk Stock Posts 16% Profit Drop", "A 16% collapse in quarterly net income — €3.2bn"),
    _result("Газпром нефть увеличила добычу", "Компания сообщила о росте добычи на 5%"),
    _result("أرامكو السعودية تعلن نتائج الربع الثالث", "ارتفعت الأرباح بنسبة ٥٪"),
    _result("Résultats trimestriels : hausse de 12 % du chiffre d’affaires", "« une année record »"),
    _result("Cổ phiếu ngân hàng tăng mạnh trong phiên sáng", "Chỉ số VN-Index tăng điểm"),
])
def test_readable_text_in_any_language_is_kept(item):
    assert not SearchService._has_garbled_text_signal(item)


def test_admission_filter_drops_garbled_results_even_from_ranked_hits():
    response = SearchResponse(query="q", provider="Tavily", results=[
        _result(GARBLED_TITLE, GARBLED_SNIPPET, relevance_category="direct", relevance_score=5),
        _result("特变电工发布业绩快报", "特变电工(600089)公告", relevance_category="direct", relevance_score=5),
    ])
    filtered = SearchService._filter_ranked_news_for_context(response, log_scope="test")
    assert [item.title for item in filtered.results] == ["特变电工发布业绩快报"]


# ---------------------------------------------------------------------------
# news pool
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'news.db'}")
    from src.services import news_pool_service

    monkeypatch.setattr(news_pool_service, "china_now", lambda: NOW)
    yield manager
    DatabaseManager.reset_instance()
    Config.reset_instance()


def _store(db, title, *, hours_ago=1, kind="flash", codes=None, scope=("market", None), summary="", source="财联社电报"):
    with db.get_session() as session:
        session.add(IntelligenceItem(
            source_name="akshare:cls", source_type="akshare", source=source, title=title, summary=summary,
            url=f"https://example.invalid/pool/{abs(hash(title))}", published_at=NOW - timedelta(hours=hours_ago),
            fetched_at=NOW - timedelta(hours=hours_ago), item_kind=kind, scope_type=scope[0],
            scope_value=scope[1] or "__all__", stock_codes=json.dumps(codes) if codes else None,
        ))
        session.commit()


def test_repository_matches_linked_code_announcement_scope_or_named_title(db):
    from src.repositories.intelligence_repo import IntelligenceRepository

    _store(db, "电网投资提速", codes=["600089"], hours_ago=1)
    _store(db, "特变电工中标特高压项目", hours_ago=2)
    _store(db, "关于回购股份的公告", kind="announcement", scope=("symbol", "600089"), codes=["600089"], hours_ago=3)
    _store(db, "特变电工旧闻", hours_ago=24 * 40)  # outside the window
    _store(db, "宁德时代发布新品", codes=["300750"], hours_ago=1)
    _store(db, "代码 6000890 不应匹配", hours_ago=1)

    repo = IntelligenceRepository(db)
    since = NOW - timedelta(days=30)
    titles = [row.title for row in repo.list_stock_news(code="600089", name="特变电工", since=since)]
    assert titles == ["电网投资提速", "特变电工中标特高压项目", "关于回购股份的公告"]
    announcements = repo.list_stock_news(code="600089", name="特变电工", since=since, kinds=("announcement",))
    assert [row.title for row in announcements] == ["关于回购股份的公告"]
    assert repo.list_stock_news(code="60008%", name="", since=since) == []


# ---------------------------------------------------------------------------
# screening candidate news / events
# ---------------------------------------------------------------------------

class _Search:
    is_available = True

    def __init__(self, results, success=True):
        self.calls = []
        self._response = SearchResponse(query="q", provider="Tavily", results=results, success=success)

    def search_stock_news(self, code, name, max_results=5):
        self.calls.append(("news", code, name, max_results))
        return self._response

    def search_stock_events(self, code, name):
        self.calls.append(("events", code, name))
        return self._response


def test_web_news_keeps_only_readable_results_that_name_the_stock(db, monkeypatch):
    search = _Search([
        _result(GARBLED_TITLE, GARBLED_SNIPPET),
        _result("Sai Life Sciences shares rally 9% after Q2 results", "net profit rises 17%"),
        _result("康弘药业(002773)获批临床", "康弘药业公告称……"),
        _result("医药板块午后走强", "康弘药业等多股涨超3%"),
        _result("编号 0027731 的公告", "与本股无关"),
    ])
    monkeypatch.setattr(screening_service, "_get_dsa_search_service", lambda: search)

    news = screening_service.search_dsa_stock_news("002773", "康弘药业", max_results=3)

    assert [item["title"] for item in news["results"]] == ["康弘药业(002773)获批临床", "医药板块午后走强"]
    assert news["dropped"] == {"garbled": 1, "off_topic": 2}
    assert news["success"] is True and news["provider"] == "Tavily"
    assert search.calls == [("news", "002773", "康弘药业", 6)]


def test_stored_pool_comes_first_and_skips_web_search_when_enough(db, monkeypatch):
    for index in range(3):
        _store(db, f"特变电工快讯{index}", codes=["600089"], hours_ago=index + 1)
    search = _Search([_result("特变电工(600089)网页新闻")])
    monkeypatch.setattr(screening_service, "_get_dsa_search_service", lambda: search)

    news = screening_service.search_dsa_stock_news("600089", "特变电工", max_results=3)

    assert news["provider"] == "news_pool" and news["success"] is True
    assert [item["title"] for item in news["results"]] == ["特变电工快讯0", "特变电工快讯1", "特变电工快讯2"]
    assert news["results"][0]["source"] == "财联社电报"
    assert news["results"][0]["published_date"] == "2026-11-08T21:00:00"
    assert search.calls == []


def test_pool_is_topped_up_by_web_and_survives_web_failure(db, monkeypatch):
    _store(db, "*ST华微收到问询函", hours_ago=1)
    search = _Search([_result("ST华微(600360)股价异动", "")])
    monkeypatch.setattr(screening_service, "_get_dsa_search_service", lambda: search)
    news = screening_service.search_dsa_stock_news("600360", "*ST华微", max_results=3)
    assert [item["title"] for item in news["results"]] == ["*ST华微收到问询函", "ST华微(600360)股价异动"]
    assert news["provider"] == "news_pool+Tavily"

    monkeypatch.setattr(screening_service, "_get_dsa_search_service", lambda: _Search([], success=False))
    failed_web = screening_service.search_dsa_stock_news("600360", "*ST华微", max_results=3)
    assert failed_web["success"] is True and failed_web["error"] is None
    assert [item["title"] for item in failed_web["results"]] == ["*ST华微收到问询函"]

    monkeypatch.setattr(screening_service, "_get_dsa_search_service", lambda: SimpleNamespace(is_available=False))
    offline = screening_service.search_dsa_stock_news("600360", "*ST华微", max_results=3)
    assert offline["provider"] == "news_pool" and len(offline["results"]) == 1
    assert screening_service.search_dsa_stock_news("000001", "平安银行", max_results=3)["success"] is False


def test_events_use_stored_announcements_and_filter_event_search(db, monkeypatch):
    _store(db, "特变电工关于回购股份的公告", kind="announcement", scope=("symbol", "600089"), codes=["600089"])
    _store(db, "特变电工快讯", codes=["600089"])  # a flash is news, not an event
    search = _Search([_result(GARBLED_TITLE), _result("特变电工2025年半年度业绩快报公告")])
    monkeypatch.setattr(screening_service, "_get_dsa_search_service", lambda: search)

    events = screening_service.search_dsa_stock_events("600089", "特变电工", max_results=3)

    assert [item["title"] for item in events["results"]] == [
        "特变电工关于回购股份的公告", "特变电工2025年半年度业绩快报公告",
    ]
    assert search.calls == [("events", "600089", "特变电工")]
