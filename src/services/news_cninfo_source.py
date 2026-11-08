# -*- coding: utf-8 -*-
"""巨潮资讯 (cninfo.com.cn) announcements for the intelligence pool.

巨潮 is the statutory disclosure site for SSE / SZSE / BSE listed companies, so it is the primary
announcement source; ``news_pool_service`` falls back to the 东方财富 listing (AKShare) per code when it
fails. Both produce the same ``NewsRecord`` shape (``kind=announcement``, ``scope_type=symbol``).

Endpoints (public, no token; the paid 深证信 webapi.cninfo.com.cn is not used):

* ``POST /new/hisAnnouncement/query``: announcements of one stock in a date range, 30 per page.
  ``stock`` must be ``<code>,<orgId>``.
* ``GET /new/data/szse_stock.json``: code -> orgId for all A shares. Most orgIds do not follow the
  ``gssh0<code>`` pattern (sampled 2026-11-09: 4641 of 6263, e.g. 300750 -> ``GD165627``), and a wrong
  orgId silently returns no announcements, so the map is required. It is cached for a day; the pattern
  is only a fallback when the map cannot be loaded or lacks the code.

AKShare's ``stock_zh_a_disclosure_report_cninfo`` wraps the same query but downloads the whole map on
every call and sets no timeout, so this module calls the endpoints directly.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from src.services.news_akshare_source import (
    CN_TZ,
    KIND_ANNOUNCEMENT,
    SOURCE_CNINFO,
    NewsRecord,
    NewsSourceError,
    call_with_timeout,
    normalize_a_share_code,
)

logger = logging.getLogger(__name__)

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
ORGID_MAP_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
PDF_BASE_URL = "https://static.cninfo.com.cn/"
PAGE_SIZE = 30
MAX_PAGES = 3  # 90 announcements per code and window is far beyond any daily window
ORGID_CACHE_SECONDS = 24 * 3600
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Referer": "https://www.cninfo.com.cn/new/disclosure",
    "Origin": "https://www.cninfo.com.cn",
    "X-Requested-With": "XMLHttpRequest",
}
_HIGHLIGHT_RE = re.compile(r"</?em>", re.IGNORECASE)

_orgid_lock = threading.Lock()
_orgid_cache: Dict[str, Any] = {"loaded_at": 0.0, "map": {}}


def _session() -> Any:
    """``requests`` module (separate function so tests can substitute a stub)."""
    import requests

    return requests


def reset_cache_for_tests() -> None:
    with _orgid_lock:
        _orgid_cache["loaded_at"] = 0.0
        _orgid_cache["map"] = {}


def _pattern_org_id(code: str) -> str:
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return f"gssh0{code}"
    if code.startswith(("4", "8", "92")):
        return f"gfbj0{code}"
    return f"gssz0{code}"


def parse_orgid_map(payload: Any) -> Dict[str, str]:
    rows = payload.get("stockList") if isinstance(payload, dict) else None
    return {
        str(row["code"]): str(row["orgId"])
        for row in rows or []
        if isinstance(row, dict) and row.get("code") and row.get("orgId")
    }


def org_id(code: str, *, timeout_sec: float, now: Callable[[], float] = time.monotonic) -> str:
    """orgId of a 6-digit code from the cached official map (pattern fallback, logged)."""
    with _orgid_lock:
        fresh = _orgid_cache["map"] and now() - _orgid_cache["loaded_at"] < ORGID_CACHE_SECONDS
        mapping: Dict[str, str] = _orgid_cache["map"]
    if not fresh:
        try:
            response = _session().get(ORGID_MAP_URL, headers=_HEADERS, timeout=timeout_sec)
            response.raise_for_status()
            loaded = parse_orgid_map(response.json())
            if loaded:
                with _orgid_lock:
                    _orgid_cache["map"] = loaded
                    _orgid_cache["loaded_at"] = now()
                mapping = loaded
        except Exception as exc:  # noqa: BLE001 - keep the old map / the pattern
            logger.warning("[News] 巨潮 orgId 映射表获取失败，沿用%s: %s", "缓存" if mapping else "代码规则", exc)
    found = mapping.get(code)
    if found:
        return found
    if mapping:
        logger.info("[News] 巨潮 orgId 映射表里没有 %s，按代码规则推断", code)
    return _pattern_org_id(code)


def _published_at(value: Any) -> Optional[datetime]:
    """``announcementTime`` is epoch milliseconds; returns naive China time."""
    try:
        millis = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(millis / 1000, CN_TZ).replace(tzinfo=None)


def parse_announcements(payload: Any, code: str) -> List[NewsRecord]:
    """One query page -> stock-scope records for ``code`` (other codes in the page are ignored)."""
    records: List[NewsRecord] = []
    rows = payload.get("announcements") if isinstance(payload, dict) else None
    for row in rows or []:
        if not isinstance(row, dict) or normalize_a_share_code(row.get("secCode")) != code:
            continue
        headline = _HIGHLIGHT_RE.sub("", str(row.get("announcementTitle") or "")).strip()
        if not headline:
            continue
        name = str(row.get("secName") or "").strip()
        adjunct = str(row.get("adjunctUrl") or "").strip()
        announcement_id = str(row.get("announcementId") or "").strip()
        if adjunct:
            url = PDF_BASE_URL + adjunct.lstrip("/")
        elif announcement_id:
            url = f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={announcement_id}"
        else:
            continue
        category = str(row.get("announcementTypeName") or "").strip()
        title = headline if name and headline.startswith(name) else (f"{name}：{headline}" if name else headline)
        records.append(NewsRecord(
            title=title[:300],
            summary=f"{name}（{code}）公告" + (f"，类型：{category}" if category else "") + "，来源：巨潮资讯",
            url=url[:1000],
            published_at=_published_at(row.get("announcementTime")),
            source_key=SOURCE_CNINFO,
            kind=KIND_ANNOUNCEMENT,
            scope_type="symbol",
            scope_value=code,
            stock_codes=[code],
            raw_payload={"name": name, "category": category, "announcement_id": announcement_id},
        ))
    return records


def fetch_notices_by_code(code: str, begin: date, end: date, *, timeout_sec: float) -> List[NewsRecord]:
    """Announcements of one A-share code over ``begin..end`` (inclusive), newest first."""
    normalized = normalize_a_share_code(code)
    if not normalized:
        raise NewsSourceError(f"cninfo: not an A-share code: {code}")

    def _call() -> List[NewsRecord]:
        request_timeout = max(1.0, min(float(timeout_sec), 15.0))
        stock = f"{normalized},{org_id(normalized, timeout_sec=request_timeout)}"
        records: List[NewsRecord] = []
        for page in range(1, MAX_PAGES + 1):
            response = _session().post(
                QUERY_URL,
                data={
                    "pageNum": str(page), "pageSize": str(PAGE_SIZE), "column": "szse", "tabName": "fulltext",
                    "plate": "", "stock": stock, "searchkey": "", "secid": "", "category": "", "trade": "",
                    "seDate": f"{begin.isoformat()}~{end.isoformat()}", "sortName": "", "sortType": "",
                    "isHLtitle": "true",
                },
                headers=_HEADERS,
                timeout=request_timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise NewsSourceError("cninfo: unexpected response")
            records.extend(parse_announcements(payload, normalized))
            if not payload.get("hasMore"):
                break
        return records

    return call_with_timeout(SOURCE_CNINFO, _call, timeout_sec)
