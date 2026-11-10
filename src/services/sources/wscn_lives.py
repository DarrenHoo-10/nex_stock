# -*- coding: utf-8 -*-
"""华尔街见闻 A 股快讯 (a-stock-data §5.4; sample in tests/fixtures/flash): a fallback flash source
outside the East Money / AkShare risk surface.

``GET https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=a-stock-channel&limit=N&cursor=``;
``code == 20000`` on success, items carry ``display_time`` (epoch seconds), ``content_text``, ``title``
(often empty), ``uri`` and ``score`` (2 = 重要).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from src.services.sources import SourceError

LIVES_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
CHANNEL = "a-stock-channel"
IMPORTANT_SCORE = 2
_CN = ZoneInfo("Asia/Shanghai")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
}


@dataclass
class Live:
    item_id: str
    title: str
    text: str
    url: str
    published_at: Optional[datetime]  # naive China time
    important: bool


def _session() -> Any:
    import requests

    return requests


def parse(payload: Any) -> List[Live]:
    if not isinstance(payload, dict) or payload.get("code") != 20000:
        raise SourceError(f"华尔街见闻快讯返回错误：{payload.get('code') if isinstance(payload, dict) else payload!r:.60}")
    items = (payload.get("data") or {}).get("items")
    if not isinstance(items, list):
        raise SourceError("华尔街见闻快讯的返回结构变了（没有 data.items）")
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content_text") or "").strip()
        title = str(item.get("title") or "").strip()
        if not text and not title:
            continue
        try:
            published = datetime.fromtimestamp(int(item.get("display_time")), _CN).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            published = None
        score = item.get("score")
        out.append(Live(item_id=str(item.get("id") or ""), title=title, text=text,
                        url=str(item.get("uri") or "").strip(), published_at=published,
                        important=isinstance(score, int) and score >= IMPORTANT_SCORE))
    return out


def fetch(*, timeout_sec: float, limit: int = 100) -> List[Live]:
    response = _session().get(LIVES_URL, params={"channel": CHANNEL, "limit": int(limit), "cursor": ""},
                              headers=_HEADERS, timeout=timeout_sec)
    response.raise_for_status()
    return parse(response.json())
