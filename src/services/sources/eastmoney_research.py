"""One bounded page of company research from the East Money report index."""
from datetime import date
import re

import requests

from data_provider import eastmoney_gate
from src.services.sources import SourceError

LIMIT = 30


def fetch(code: str, since: date, until: date) -> list[dict]:
    params = {
        "code": code, "qType": 0, "pageNo": 1, "pageSize": LIMIT,
        "beginTime": since.isoformat(), "endTime": until.isoformat(),
        "industryCode": "*", "industry": "*", "rating": "*", "ratingChange": "*",
        "fields": "", "orgCode": "", "rcode": "", "p": 1, "pageNum": 1, "pageNumber": 1,
    }
    try:
        response = eastmoney_gate.get(
            "https://reportapi.eastmoney.com/report/list", params=params, timeout=(3, 8),
            headers={"Referer": "https://data.eastmoney.com/"}, allow_redirects=False,
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SourceError("东方财富研报暂时无法读取") from exc

    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list) or (not rows and str(body.get("hits")) != "0"):
        raise SourceError("东方财富研报列表格式异常")
    reports = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("stockCode") != code:
            raise SourceError("东方财富研报股票代码不匹配")
        info = str(row.get("infoCode") or "")
        title = str(row.get("title") or "").strip()
        try:
            day = date.fromisoformat(str(row["publishDate"])[:10])
        except (KeyError, ValueError) as exc:
            raise SourceError("东方财富研报日期无效") from exc
        if not title or not re.fullmatch(r"AP[0-9]+", info):
            raise SourceError("东方财富研报缺少标题或编号")
        if not since <= day <= until:
            continue
        pages = str(row.get("attachPages") or "")
        payload = {"title": title, "org": row.get("orgSName") or row.get("orgName"),
                   "rating": row.get("sRatingName") or row.get("emRatingName"), "info_code": info}
        if pages.isascii() and pages.isdigit() and int(pages) > 0:
            payload.update(pages=int(pages), pdf_url=f"https://pdf.dfcfw.com/pdf/H3_{info}_1.pdf")
        reports[info] = {"kind": "research", "source": "eastmoney", "event_date": day.isoformat(),
                         "title": title, "payload": payload}
    return list(reports.values())
