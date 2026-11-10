# -*- coding: utf-8 -*-
"""Investor Q&A with listed companies: 巨潮互动易 (深市) and 上证e互动 (沪市).

Tushare's ``irm_qa_sz`` / ``irm_qa_sh`` are not permitted on this account (sampled 2026-11-09), so both
official platforms are read directly (a-stock-data §10.1 / §10.3, samples in tests/fixtures/investor_qa):

* 互动易: ``POST /newircs/index/queryKeyboardInfo`` (form ``keyWord=code``) gives the company ``secid``;
  ``POST /newircs/company/question`` takes its parameters **in the query string with an empty body**
  (a form body answers HTTP 400). Only 深市 companies; 沪市 ones return nothing.
* 上证e互动: HTML. A company's feed (``POST /ajax/userfeeds.do``) needs its uid, found in the company list
  (``POST /allcompany.do``, 32 companies per page sorted by code) by doubling then binary search and
  cached for a day. Times are absolute (``2026年11月04日 09:11``) or relative within a day (``昨天 18:22``).
  Only about the last month is available. 北交所 companies are on neither platform.

Only answered questions are returned.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.services.sources import SourceError

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

IRM_BASE = "https://irm.cninfo.com.cn"
SSE_BASE = "https://sns.sseinfo.com"
IRM_PAGE_SIZE = 30
SSE_PAGE_SIZE = 20
MAX_PAGES = 3
CACHE_SECONDS = 24 * 3600


@dataclass
class QaItem:
    code: str
    name: str
    question: str
    answer: str
    asked_at: Optional[datetime]  # naive China time
    answered_at: Optional[datetime]
    item_id: str
    platform: str  # irm / sse_e


def _session() -> Any:
    """``requests`` module (separate function so tests can substitute a stub)."""
    import requests

    return requests


def route(code: str) -> Optional[str]:
    """Platform of an A-share code: ``irm`` (深市), ``sse_e`` (沪市) or ``None`` (北交所 / other)."""
    if not re.fullmatch(r"\d{6}", code or ""):
        return None
    if code.startswith(("60", "68", "900")):
        return "sse_e"
    if code.startswith(("00", "30", "20")):
        return "irm"
    return None


def _text(fragment: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(fragment or "")))).strip()


def _from_millis(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(value) / 1000, CN_TZ).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ----------------------------------------------------------------------
# 巨潮互动易
# ----------------------------------------------------------------------
_irm_lock = threading.Lock()
_irm_secids: Dict[str, Tuple[str, float]] = {}


def irm_secid(code: str, *, timeout_sec: float, now: Any = time.monotonic) -> str:
    with _irm_lock:
        cached = _irm_secids.get(code)
    if cached and now() - cached[1] < CACHE_SECONDS:
        return cached[0]
    response = _session().post(f"{IRM_BASE}/newircs/index/queryKeyboardInfo", data={"keyWord": code},
                               headers={"User-Agent": _UA}, timeout=timeout_sec)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceError("互动易公司查询的返回结构变了（没有 data 列表）")
    match = next((r for r in rows if isinstance(r, dict) and r.get("stockCode") == code and r.get("secid")), None)
    if match is None:
        raise SourceError(f"互动易没有找到 {code}")
    with _irm_lock:
        _irm_secids[code] = (str(match["secid"]), now())
    return str(match["secid"])


def parse_irm(payload: Any, code: str) -> List[QaItem]:
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceError("互动易问答的返回结构变了（没有 rows 列表）")
    items = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("stockCode") or "") != code:
            continue
        answer = _text(row.get("attachedContent"))
        question = _text(row.get("mainContent"))
        if not answer or not question:
            continue
        items.append(QaItem(
            code=code, name=_text(row.get("companyShortName")), question=question, answer=answer,
            asked_at=_from_millis(row.get("pubDate")),
            answered_at=_from_millis(row.get("attachedPubDate") or row.get("updateDate")),
            item_id=str(row.get("indexId") or ""), platform="irm",
        ))
    return items


def fetch_irm(code: str, *, since: datetime, timeout_sec: float) -> List[QaItem]:
    secid = irm_secid(code, timeout_sec=timeout_sec)
    items: List[QaItem] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"_t": 1, "stockcode": code, "orgId": secid, "pageSize": IRM_PAGE_SIZE, "pageNum": page,
                  "keyWord": "", "startDay": "", "endDay": ""}
        response = _session().post(f"{IRM_BASE}/newircs/company/question", params=params,
                                   headers={"User-Agent": _UA}, timeout=timeout_sec)
        response.raise_for_status()
        payload = response.json()
        page_items = parse_irm(payload, code)
        items.extend(page_items)
        rows = payload.get("rows") or []
        asked = [_from_millis(r.get("pubDate")) for r in rows if isinstance(r, dict)]
        oldest = min((a for a in asked if a), default=None)
        if len(rows) < IRM_PAGE_SIZE or (oldest is not None and oldest < since):
            break
    return [item for item in items if (item.answered_at or item.asked_at or since) >= since]


# ----------------------------------------------------------------------
# 上证e互动
# ----------------------------------------------------------------------
_COMPANY_END = "没有任何上市公司的信息"
_EMPTY_NOTE = re.compile(r'class="m_feed_note"[^>]*>[^<]*(暂无|暂时没有)[^<]*<')
_sse_lock = threading.Lock()
_sse_pages: Dict[int, List[Tuple[str, str]]] = {}
_sse_uids: Dict[str, str] = {}
_sse_loaded_at = [0.0]


def reset_caches_for_tests() -> None:
    with _sse_lock:
        _sse_pages.clear()
        _sse_uids.clear()
        _sse_loaded_at[0] = 0.0
    with _irm_lock:
        _irm_secids.clear()


def parse_company_page(payload: Any, page: int) -> List[Tuple[str, str]]:
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str):
        raise SourceError(f"上证e互动公司列表第 {page} 页的返回结构变了（没有 content）")
    pairs = [(code, uid) for uid, code in
             re.findall(r"uid=['\"]?(\d+)['\"]?[^>]*>\s*<img[^>]*company/(\d{6})\.png", content)]
    if not pairs and (page == 1 or _COMPANY_END not in content):
        raise SourceError(f"上证e互动公司列表第 {page} 页解析出 0 家公司，页面格式可能已变")
    return pairs


def _company_page(page: int, timeout_sec: float) -> List[Tuple[str, str]]:
    with _sse_lock:
        if time.monotonic() - _sse_loaded_at[0] > CACHE_SECONDS:
            _sse_pages.clear()
            _sse_uids.clear()
            _sse_loaded_at[0] = time.monotonic()
        if page in _sse_pages:
            return _sse_pages[page]
    response = _session().post(f"{SSE_BASE}/allcompany.do", data={"code": "0", "order": "2", "areaId": "0",
                                                                  "page": page},
                               headers={"User-Agent": _UA, "Referer": SSE_BASE + "/"}, timeout=timeout_sec)
    response.raise_for_status()
    pairs = parse_company_page(json.loads(response.content.decode("utf-8", "replace")), page)
    with _sse_lock:
        _sse_pages[page] = pairs
        _sse_uids.update(dict(pairs))
    return pairs


def sse_uid(code: str, *, timeout_sec: float) -> str:
    """Company uid: the list is sorted by code, so double the page number, then binary search."""
    with _sse_lock:
        if code in _sse_uids:
            return _sse_uids[code]
    low, high = 1, 1
    while True:
        pairs = _company_page(high, timeout_sec)
        if not pairs or pairs[-1][0] >= code or high > 512:
            break
        low, high = high, high * 2
    while low <= high:
        mid = (low + high) // 2
        pairs = _company_page(mid, timeout_sec)
        if not pairs or code < pairs[0][0]:
            high = mid - 1
        elif code > pairs[-1][0]:
            low = mid + 1
        else:
            break
    with _sse_lock:
        if code in _sse_uids:
            return _sse_uids[code]
    raise SourceError(f"上证e互动没有 {code}（不在上交所，或已退市）")


_ABS_TIME = re.compile(r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*日?\s*(\d{1,2}):(\d{2})")
_DAY_WORD_TIME = re.compile(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{2})$")
_MONTH_DAY_TIME = re.compile(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$")
_AGO_TIME = re.compile(r"^(\d+)\s*(秒|分钟|小时|天)前$")
_CLOCK_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_DAY_WORD = {"今天": 0, "昨天": 1, "前天": 2}
_AGO_UNIT = {"秒": "seconds", "分钟": "minutes", "小时": "hours", "天": "days"}


def parse_sse_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """The site's time text as naive China time (``None`` when not recognised)."""
    text = (text or "").strip()
    now = now or datetime.now(CN_TZ).replace(tzinfo=None)
    try:
        match = _ABS_TIME.search(text)
        if match:
            year, month, day, hour, minute = (int(g) for g in match.groups())
            return datetime(year, month, day, hour, minute)
        match = _DAY_WORD_TIME.match(text)
        if match:
            when = now - timedelta(days=_DAY_WORD[match.group(1)])
            return when.replace(hour=int(match.group(2)), minute=int(match.group(3)), second=0, microsecond=0)
        match = _MONTH_DAY_TIME.match(text)
        if match:
            month, day, hour, minute = (int(g) for g in match.groups())
            when = datetime(now.year, month, day, hour, minute)
            if when - now > timedelta(days=1):  # in January, 12-31 is last year
                when = when.replace(year=now.year - 1)
            return when
        match = _AGO_TIME.match(text)
        if match:
            return now - timedelta(**{_AGO_UNIT[match.group(2)]: int(match.group(1))})
        if text in ("刚刚", "刚才"):
            return now
        match = _CLOCK_TIME.match(text)
        if match:
            return now.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0)
    except (ValueError, OverflowError):
        return None
    return None


def parse_sse_feed(text: str, *, now: Optional[datetime] = None) -> List[QaItem]:
    """Answered items of a feed page (an explicit 暂无 note means none; anything else unparsable raises)."""
    items: List[QaItem] = []
    chunks = re.split(r'<div class="m_feed_item[^"]*" id="item-', text)[1:]
    for chunk in chunks:
        numbered = re.match(r"(\d+)", chunk)
        if not numbered:
            raise SourceError("上证e互动条目 id 不是数字，页面结构可能已变")
        item_id = numbered.group(1)
        ask_part, _, answer_part = chunk.partition('class="m_feed_detail m_qa"')
        question = re.search(r'<div class="m_feed_txt"[^>]*>\s*<a[^>]*>:(.*?)\((\d{6})\)</a>(.*?)</div>', ask_part, re.S)
        ask_time = re.search(r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', ask_part)
        if not question or not ask_time:
            raise SourceError(f"上证e互动第 {item_id} 条结构改变，无法解析问题或时间")
        if not answer_part:
            continue
        body = re.search(r'<div class="m_feed_txt"[^>]*>(.*?)</div>', answer_part, re.S)
        when = re.search(r'<div class="m_feed_from"[^>]*>\s*<span>([^<]+)</span>', answer_part)
        if not body or not when:
            raise SourceError(f"上证e互动第 {item_id} 条有回复块但解析不出回复内容或时间")
        answered_at = parse_sse_time(when.group(1), now)
        asked_at = parse_sse_time(ask_time.group(1), now)
        if answered_at is None or asked_at is None:
            raise SourceError(f"上证e互动第 {item_id} 条的时间认不出：{ask_time.group(1)!r} / {when.group(1)!r}")
        items.append(QaItem(
            code=question.group(2), name=_text(question.group(1)), question=_text(question.group(3)),
            answer=_text(body.group(1)), asked_at=asked_at, answered_at=answered_at, item_id=item_id,
            platform="sse_e",
        ))
    if not chunks and not _EMPTY_NOTE.search(text):
        raise SourceError("上证e互动的页面既没有问答也没有「暂无」提示，结构可能已变")
    return items


def fetch_sse(code: str, *, since: datetime, timeout_sec: float) -> List[QaItem]:
    uid = sse_uid(code, timeout_sec=timeout_sec)
    items: List[QaItem] = []
    for page in range(1, MAX_PAGES + 1):
        response = _session().post(
            f"{SSE_BASE}/ajax/userfeeds.do",
            data={"typeCode": "company", "type": 11, "pageSize": SSE_PAGE_SIZE, "uid": uid, "page": page},
            headers={"User-Agent": _UA, "Referer": SSE_BASE + "/"}, timeout=timeout_sec,
        )
        response.raise_for_status()
        page_items = parse_sse_feed(response.content.decode("utf-8", "replace"))
        if any(item.code != code for item in page_items):
            raise SourceError("上证e互动返回了其他公司的问答，uid 映射可能已变")
        items.extend(page_items)
        if len(page_items) < SSE_PAGE_SIZE or min(i.answered_at for i in page_items) < since:
            break
    return [item for item in items if item.answered_at and item.answered_at >= since]


def fetch_answered(code: str, *, since: datetime, timeout_sec: float) -> List[QaItem]:
    """Answered Q&A of ``code`` since ``since`` from the platform that covers it."""
    platform = route(code)
    if platform == "irm":
        return fetch_irm(code, since=since, timeout_sec=timeout_sec)
    if platform == "sse_e":
        return fetch_sse(code, since=since, timeout_sec=timeout_sec)
    return []
