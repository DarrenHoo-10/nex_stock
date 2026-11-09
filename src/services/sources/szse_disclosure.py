# -*- coding: utf-8 -*-
"""深交所官方公告列表: the third announcement source for 深市 stocks (after 巨潮 and 东方财富).

``POST https://www.szse.cn/api/disc/announcement/annList`` with a JSON body
``{"seDate": [begin, end], "channelCode": ["listedNotice_disc"], "pageSize", "pageNum", "stock": [code]}``
(a-stock-data's backup source; sample in tests/fixtures/szse). PDFs live under
``https://disc.static.szse.cn/download`` + ``attachPath``. 上交所 has no equivalent public API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, List, Optional

from src.services.sources import SourceError

ANN_LIST_URL = "https://www.szse.cn/api/disc/announcement/annList"
PDF_BASE = "https://disc.static.szse.cn/download"
PAGE_SIZE = 30
MAX_PAGES = 3
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Content-Type": "application/json",
    "Referer": "https://www.szse.cn/disclosure/listed/notice/index.html",
}


@dataclass
class SzseAnnouncement:
    code: str
    name: str
    title: str
    url: str
    published_at: Optional[datetime]


def _session() -> Any:
    import requests

    return requests


def covers(code: str) -> bool:
    return bool(re.fullmatch(r"(00|30|20)\d{4}", code or ""))


def parse(payload: Any, code: str) -> List[SzseAnnouncement]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceError("深交所公告列表的返回结构变了（没有 data 列表）")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        codes = row.get("secCode") if isinstance(row.get("secCode"), list) else [row.get("secCode")]
        if code not in [str(c) for c in codes]:
            continue
        names = row.get("secName") if isinstance(row.get("secName"), list) else [row.get("secName")]
        title = re.sub(r"<[^>]+>", "", str(row.get("title") or "")).strip()
        path = str(row.get("attachPath") or "").strip()
        if not title or not path:
            continue
        try:
            published = datetime.strptime(str(row.get("publishTime") or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            published = None
        out.append(SzseAnnouncement(code=code, name=str(names[0] or "") if names else "", title=title,
                                    url=PDF_BASE + path, published_at=published))
    return out


def fetch_by_code(code: str, begin: date, end: date, *, timeout_sec: float) -> List[SzseAnnouncement]:
    if not covers(code):
        raise SourceError(f"深交所公告只覆盖深市股票：{code}")
    items: List[SzseAnnouncement] = []
    for page in range(1, MAX_PAGES + 1):
        body = {"seDate": [begin.isoformat(), end.isoformat()], "channelCode": ["listedNotice_disc"],
                "pageSize": PAGE_SIZE, "pageNum": page, "stock": [code]}
        response = _session().post(ANN_LIST_URL, json=body, headers=_HEADERS, timeout=timeout_sec)
        response.raise_for_status()
        payload = response.json()
        page_items = parse(payload, code)
        items.extend(page_items)
        if len(payload.get("data") or []) < PAGE_SIZE:
            break
    return items
