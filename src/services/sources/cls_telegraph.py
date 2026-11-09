# -*- coding: utf-8 -*-
"""财联社电报 direct: the second implementation of the ``cls`` flash source (after AkShare).

``GET https://www.cls.cn/v1/roll/get_roll_list`` with ``appName=CailianpressWeb, os=web, sv=7.7.5,
last_time, refresh_type=1, rn`` and ``sign = md5(sha1(query string sorted by key))`` — computed
locally, no key (a-stock-data §5.2; sample in tests/fixtures/flash). ``level`` A / B are the red
(重点) items.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from src.services.sources import SourceError

ROLL_URL = "https://www.cls.cn/v1/roll/get_roll_list"
IMPORTANT_LEVELS = ("A", "B")
_CN = ZoneInfo("Asia/Shanghai")
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Referer": "https://www.cls.cn/telegraph",
}


@dataclass
class Telegraph:
    item_id: str
    title: str
    content: str
    published_at: Optional[datetime]  # naive China time
    level: str


def _session() -> Any:
    import requests

    return requests


def sign(params: Dict[str, Any]) -> str:
    query = urlencode(sorted((k, v) for k, v in params.items() if k != "sign"))
    return hashlib.md5(hashlib.sha1(query.encode("utf-8")).hexdigest().encode("utf-8")).hexdigest()


def signed_params(*, rn: int, last_time: Optional[int] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
                              "last_time": int(last_time if last_time is not None else time.time()),
                              "refresh_type": 1, "rn": int(rn)}
    params["sign"] = sign(params)
    return params


def parse(payload: Any, *, important_only: bool = False) -> List[Telegraph]:
    if not isinstance(payload, dict) or payload.get("errno") not in (0, "0"):
        raise SourceError(f"财联社电报返回错误：{payload.get('errno') if isinstance(payload, dict) else payload!r:.60}")
    rows = (payload.get("data") or {}).get("roll_data")
    if not isinstance(rows, list):
        raise SourceError("财联社电报的返回结构变了（没有 data.roll_data）")
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("is_ad"):
            continue
        level = str(row.get("level") or "").strip()
        if important_only and level not in IMPORTANT_LEVELS:
            continue
        content = str(row.get("content") or row.get("brief") or "").strip()
        title = str(row.get("title") or "").strip()
        if not content and not title:
            continue
        try:
            published = datetime.fromtimestamp(int(row.get("ctime")), _CN).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            published = None
        out.append(Telegraph(item_id=str(row.get("id") or ""), title=title, content=content,
                             published_at=published, level=level))
    return out


def fetch(*, timeout_sec: float, rn: int = 50, important_only: bool = False) -> List[Telegraph]:
    response = _session().get(ROLL_URL, params=signed_params(rn=rn), headers=_HEADERS, timeout=timeout_sec)
    response.raise_for_status()
    return parse(response.json(), important_only=important_only)
