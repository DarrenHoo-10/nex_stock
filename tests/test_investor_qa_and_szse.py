# -*- coding: utf-8 -*-
"""Investor Q&A (互动易 / 上证e互动) and 深交所 announcements: parsing, news-pool wiring, API (no network).

Fixtures are single samples taken on 2026-11-09 (tests/fixtures/investor_qa, tests/fixtures/szse),
with investor names replaced.
"""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from src.services.sources import SourceError, investor_qa, szse_disclosure

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 9, 10, 0)


def _json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Response:
    def __init__(self, payload=None, text=None):
        self._payload, self._text = payload, text

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    @property
    def content(self):
        return (self._text if self._text is not None else json.dumps(self._payload)).encode("utf-8")


# ----------------------------------------------------------------------
# 互动易
# ----------------------------------------------------------------------
def test_route_by_board():
    assert investor_qa.route("002773") == "irm" and investor_qa.route("300750") == "irm"
    assert investor_qa.route("600089") == "sse_e" and investor_qa.route("688981") == "sse_e"
    assert investor_qa.route("920002") is None and investor_qa.route("AAPL") is None


def test_irm_parse_real_sample():
    items = investor_qa.parse_irm(_json("investor_qa/irm_questions_002773.json"), "002773")
    assert items and all(i.platform == "irm" and i.code == "002773" and i.question and i.answer for i in items)
    first = items[0]
    assert first.name == "康弘药业" and first.item_id == "2377611940555259904"
    assert first.asked_at and first.answered_at and first.answered_at >= first.asked_at
    with pytest.raises(SourceError):
        investor_qa.parse_irm({"unexpected": True}, "002773")


def test_irm_fetch_sends_parameters_in_the_query_string(monkeypatch):
    investor_qa.reset_caches_for_tests()
    calls = []

    class Http:
        def post(self, url, data=None, params=None, **kwargs):
            calls.append((url.rsplit("/", 1)[-1], data, params))
            if url.endswith("queryKeyboardInfo"):
                return _Response(_json("investor_qa/irm_keyboard_002773.json"))
            return _Response(_json("investor_qa/irm_questions_002773.json"))

    monkeypatch.setattr(investor_qa, "_session", lambda: Http())
    items = investor_qa.fetch_irm("002773", since=datetime(2026, 1, 1), timeout_sec=5)
    assert items
    assert calls[0] == ("queryKeyboardInfo", {"keyWord": "002773"}, None)
    name, body, params = calls[1]
    assert name == "question" and body is None and params["orgId"] == "9900022951" and params["stockcode"] == "002773"
    investor_qa.fetch_irm("002773", since=datetime(2026, 1, 1), timeout_sec=5)
    assert sum(1 for c in calls if c[0] == "queryKeyboardInfo") == 1  # secid cached
    assert investor_qa.fetch_irm("002773", since=datetime(2099, 1, 1), timeout_sec=5) == []


# ----------------------------------------------------------------------
# 上证e互动
# ----------------------------------------------------------------------
def test_sse_feed_parsing_real_samples():
    items = investor_qa.parse_sse_feed(_text("investor_qa/sse_feeds_market.html"), now=NOW)
    assert len(items) == 3 and all(i.platform == "sse_e" and i.answer for i in items)
    first = items[0]
    assert first.code == "603382" and first.item_id == "1803581"
    assert first.asked_at == datetime(2026, 11, 4, 9, 11) and first.answered_at == datetime(2026, 11, 6, 18, 21)
    assert investor_qa.parse_sse_feed(_text("investor_qa/sse_userfeeds_empty_600000.html"), now=NOW) == []
    with pytest.raises(SourceError):
        investor_qa.parse_sse_feed("<html>changed</html>", now=NOW)


@pytest.mark.parametrize(("text", "expected"), [
    ("2026年11月04日 09:11", datetime(2026, 11, 4, 9, 11)),
    ("昨天 18:22", datetime(2026, 11, 8, 18, 22)),
    ("今天 08:05", datetime(2026, 11, 9, 8, 5)),
    ("11-07 12:00", datetime(2026, 11, 7, 12, 0)),
    ("3小时前", datetime(2026, 11, 9, 7, 0)),
    ("刚刚", NOW),
    ("09:30", datetime(2026, 11, 9, 9, 30)),
    ("某天", None),
])
def test_sse_time_formats(text, expected):
    assert investor_qa.parse_sse_time(text, NOW) == expected


def test_sse_uid_binary_search_over_the_company_list(monkeypatch):
    investor_qa.reset_caches_for_tests()
    real = investor_qa.parse_company_page(_json("investor_qa/sse_allcompany_page1.json"), 1)
    assert len(real) == 32 and real[0] == ("600000", "65")

    # Synthetic list in the real page format: page n holds codes 600000+32(n-1) ... ; 20 pages.
    def page_payload(page):
        if page > 20:
            return {"content": "<p>没有任何上市公司的信息</p>"}
        cells = "".join(
            f"<a href='x' uid='{9000 + (page - 1) * 32 + i}'><img src='https://x/company/{600000 + (page - 1) * 32 + i}.png'></a>"
            for i in range(32))
        return {"content": cells}

    requested = []

    class Http:
        def post(self, url, data=None, **kwargs):
            requested.append(int(data["page"]))
            return _Response(page_payload(int(data["page"])))

    monkeypatch.setattr(investor_qa, "_session", lambda: Http())
    assert investor_qa.sse_uid("600500", timeout_sec=5) == str(9000 + 500)
    assert len(requested) <= 12
    assert investor_qa.sse_uid("600501", timeout_sec=5) == str(9000 + 501)  # same page, cached
    with pytest.raises(SourceError):
        investor_qa.sse_uid("609999", timeout_sec=5)


# ----------------------------------------------------------------------
# 深交所公告
# ----------------------------------------------------------------------
def test_szse_announcements_real_sample():
    items = szse_disclosure.parse(_json("szse/ann_list_002773.json"), "002773")
    assert len(items) == 3
    assert items[0].url.startswith("https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-11-06/")
    assert items[0].published_at == datetime(2026, 11, 6, 0, 0) and items[0].name == "康弘药业"
    assert szse_disclosure.covers("002773") and not szse_disclosure.covers("600089")
    with pytest.raises(SourceError):
        szse_disclosure.parse({"oops": 1}, "002773")


# ----------------------------------------------------------------------
# news pool wiring
# ----------------------------------------------------------------------
@pytest.fixture
def pool(monkeypatch):
    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_pool_service import NewsPoolService
    from src.storage import DatabaseManager
    from tests.news_fixtures import install_stub, make_config

    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    stub = install_stub(monkeypatch)
    service = NewsPoolService(IntelligenceRepository(db), make_config(stock_list=["002773", "600089", "920002"]),
                              is_trading_day=lambda d: True, holdings_provider=lambda: [], sleep_fn=lambda s: None)
    yield service, stub
    DatabaseManager.reset_instance()


def test_daily_job_stores_answered_qa_per_platform(pool, monkeypatch):
    service, _stub = pool
    asked = []

    def fake_fetch(code, *, since, timeout_sec):
        asked.append(code)
        if code == "600089":
            raise SourceError("页面结构可能已变")
        return investor_qa.parse_irm(_json("investor_qa/irm_questions_002773.json"), code)

    monkeypatch.setattr(investor_qa, "fetch_answered", fake_fetch)
    result = service.run_daily_fetch(day=date(2026, 11, 9), now=NOW)
    qa = result["qa"]
    assert asked == ["002773", "600089"] and qa["skipped"] == ["920002"]  # 北交所: no platform
    assert qa["status"] == "partial" and qa["sources"]["irm"]["status"] == "ok"
    assert qa["sources"]["sse_e"]["status"] == "failed"
    rows = service.repo.list_stock_news(code="002773", since=datetime(2026, 1, 1), kinds=("qa",))
    assert rows and rows[0].source == "巨潮互动易" and rows[0].summary.startswith("问：")
    assert json.loads(rows[0].raw_payload)["answer"]
    health = {s["source"]: s for s in __import__("src.services.news_digest_service", fromlist=["x"]).get_news_health(
        repo=service.repo, now=NOW, config=service.config)["sources"]}
    assert health["irm"]["last_status"] == "ok" and health["sse_e"]["last_status"] == "failed"


def test_szse_is_the_third_announcement_source(pool, monkeypatch):
    service, stub = pool
    stub.cninfo_by_code["002773"] = ConnectionError("cninfo down")
    stub.notice_by_code["002773"] = ConnectionError("eastmoney down")
    monkeypatch.setattr(szse_disclosure, "fetch_by_code",
                        lambda code, begin, end, *, timeout_sec: szse_disclosure.parse(
                            _json("szse/ann_list_002773.json"), code))
    monkeypatch.setattr(investor_qa, "fetch_answered", lambda *a, **k: [])
    service.config.stock_list = ["002773"]
    ann = service.run_daily_fetch(day=date(2026, 11, 9), now=NOW)["announcements"]
    assert ann["status"] == "ok" and ann["sources"]["szse"] == {"status": "ok", "calls": 1, "fetched": 3, "errors": []}
    rows = [r for r in service.repo.list_stock_news(code="002773", since=datetime(2026, 1, 1), kinds=("announcement",))]
    assert len(rows) == 3 and {r.source for r in rows} == {"深交所公告"}


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


def _primary():
    """The acting user the open stock routes receive for the primary user (CLI / local mode)."""
    from api.deps import CurrentUser
    from src.config import get_primary_user_id

    uid = get_primary_user_id()
    return CurrentUser(user_id=uid, handle=uid, display_name=uid, role="admin", is_admin=True)

def test_qa_endpoint_refreshes_then_reads_stored_items(pool, monkeypatch):
    from fastapi import HTTPException

    from api.v1.endpoints.stocks import get_stock_qa
    from src.services import news_pool_service

    service, _stub = pool
    monkeypatch.setattr(news_pool_service, "china_now", lambda: NOW)
    monkeypatch.setattr(news_pool_service, "IntelligenceRepository", lambda: service.repo, raising=False)
    import src.repositories.intelligence_repo as repo_module
    monkeypatch.setattr(repo_module, "IntelligenceRepository", lambda *a, **k: service.repo)
    fetches = []
    monkeypatch.setattr(investor_qa, "fetch_answered", lambda code, **k: fetches.append(code) or investor_qa.parse_irm(
        _json("investor_qa/irm_questions_002773.json"), code))
    monkeypatch.setattr(news_pool_service.NewsPoolService, "__init__",
                        lambda self, repo=None, config=None, **k: (setattr(self, "repo", service.repo),
                                                                    setattr(self, "config", service.config),
                                                                    setattr(self, "_sleep", lambda s: None),
                                                                    setattr(self, "_events_runner", None),
                                                                    None)[-1])
    first = get_stock_qa("002773", days=60, user=_primary())
    assert first.platform == "irm" and first.items and first.fetch["status"] == "ok" and fetches == ["002773"]
    assert first.items[0].platform_label == "巨潮互动易" and first.items[0].answer
    again = get_stock_qa("002773", days=60, user=_primary())
    assert again.fetch == {} and fetches == ["002773"]  # fetched within 6 h
    bse = get_stock_qa("920002", days=30, user=_primary())
    assert bse.platform is None and bse.items == [] and "北交所" in bse.coverage
    with pytest.raises(HTTPException):
        get_stock_qa("sh000001", days=30, user=_primary())
