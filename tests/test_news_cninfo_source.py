# -*- coding: utf-8 -*-
"""巨潮 announcement parsing and orgId lookup (recorded responses, no network)."""

from datetime import date

import pytest

from src.services import news_cninfo_source as cninfo
from src.services.news_akshare_source import NewsSourceError, reset_inflight_for_tests
from tests.news_fixtures import StubAkshare, cninfo_fixture


@pytest.fixture
def stub(monkeypatch):
    stub = StubAkshare()
    monkeypatch.setattr(cninfo, "_session", lambda: stub.http)
    cninfo.reset_cache_for_tests()
    reset_inflight_for_tests()
    return stub


def test_parse_real_response_into_stock_scope_records():
    records = cninfo.parse_announcements(cninfo_fixture(), "600089")
    assert [r.title for r in records][:2] == [
        "特变电工股份有限公司2026年前三季度发电量情况公告", "特变电工股份有限公司2026年第三季度报告",
    ]
    first = records[0]
    assert first.url == "https://static.cninfo.com.cn/finalpage/2026-10-30/1225643210.PDF"
    assert first.kind == "announcement" and first.scope_type == "symbol" and first.scope_value == "600089"
    assert first.stock_codes == ["600089"] and first.source_key == "cninfo" and first.source_label == "巨潮资讯公告"
    assert first.raw_payload["announcement_id"] == "1225643210"


def test_parse_strips_highlights_prefixes_short_titles_and_skips_other_codes():
    payload = {"announcements": [
        {"secCode": "300750", "secName": "宁德时代", "announcementTitle": "关于<em>回购</em>股份的公告",
         "announcementTime": 1793289600000, "adjunctUrl": "finalpage/2026-10-30/1.PDF", "announcementId": "1"},
        {"secCode": "300751", "secName": "迈为股份", "announcementTitle": "别的公司", "adjunctUrl": "x.PDF"},
        {"secCode": "300750", "secName": "宁德时代", "announcementTitle": "无链接公告"},
        {"secCode": "300750", "secName": "宁德时代", "announcementTitle": "只有编号", "announcementId": "9"},
    ]}
    records = cninfo.parse_announcements(payload, "300750")
    assert [r.title for r in records] == ["宁德时代：关于回购股份的公告", "宁德时代：只有编号"]
    assert records[1].url == "https://www.cninfo.com.cn/new/disclosure/detail?annoId=9"
    assert records[1].published_at is None


def test_orgid_comes_from_the_cached_official_map_with_pattern_fallback(stub):
    assert cninfo.org_id("300750", timeout_sec=5) == "GD165627"
    assert cninfo.org_id("920002", timeout_sec=5) == "gfbj0873843"
    assert cninfo.org_id("688981", timeout_sec=5) == "gssh0688981"  # not in the sample map: pattern
    assert stub.calls.count("cninfo_orgids") == 1

    cninfo.reset_cache_for_tests()
    stub.cninfo_orgid_map = ConnectionError("down")
    assert cninfo.org_id("000001", timeout_sec=5) == "gssz0000001"
    assert cninfo.org_id("830799", timeout_sec=5) == "gfbj0830799"


def test_fetch_pages_until_has_more_is_false_and_caps_pages(stub):
    pages = []

    def page(data):
        pages.append(int(data["pageNum"]))
        payload = cninfo_fixture()
        payload["hasMore"] = True
        return payload

    stub.cninfo_by_code["600089"] = page
    records = cninfo.fetch_notices_by_code("600089", date(2026, 10, 1), date(2026, 11, 9), timeout_sec=5)
    assert pages == [1, 2, 3] and len(records) == 9
    assert stub.cninfo_requests[0]["seDate"] == "2026-10-01~2026-11-09"
    assert stub.cninfo_requests[0]["pageSize"] == "30"

    with pytest.raises(NewsSourceError):
        cninfo.fetch_notices_by_code("AAPL", date(2026, 11, 8), date(2026, 11, 9), timeout_sec=5)
