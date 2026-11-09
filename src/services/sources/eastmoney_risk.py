# -*- coding: utf-8 -*-
"""东方财富风险名单: 重点监控名单 and 严重异常波动 (a-stock-data §8.4 / §8.5; samples in tests/fixtures/eastmoney).

* ``stock_monitor.json`` — the whole list in one file: ``STKCODE``, ``STKNAME``, ``MARKET`` ("1" 沪 /
  "0" 深 / "B" 北), ``VALIDATESTARTDATE`` / ``VALIDATEENDDATE`` (about a two-week window). It also lists
  funds (ETF / LOF); only A-share stock codes are kept.
* ``price-anomaly/list`` — stocks whose cumulative deviation reached the exchanges' severe-anomaly rules.
  The fixed ``team/product/client/version/name/user`` parameters are required (otherwise
  ``{"result": 1001, "msg": "unknow team"}``). Fields: ``c`` code, ``n`` name, ``x`` cumulative deviation
  (%), ``d`` days, ``a`` today's change (%), ``e`` rule code, ``s`` board.

Both hosts are East Money, so every request goes through ``data_provider.eastmoney_gate`` (installed
on import of ``data_provider``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, List, Optional

from src.services.sources import SourceError

MONITOR_URL = "https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json"
ANOMALY_URL = "https://dycalchis.eastmoney.com/price-anomaly/list"
_FIXED = {"team": "h5", "product": "EastMoney", "client": "WAP", "version": "9001", "name": "WAP", "user": "123"}
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Referer": "https://vipmoney.eastmoney.com/",
}
_STOCK_CODE = re.compile(r"(00|30|60|68|92)\d{4}")


@dataclass
class MonitoredStock:
    code: str
    name: str
    start: date
    end: date


@dataclass
class PriceAnomaly:
    code: str
    name: str
    day: date
    deviation_pct: Optional[float]
    days: Optional[int]
    change_pct: Optional[float]
    rule: Optional[int]


def _session() -> Any:
    import data_provider  # noqa: F401 - installs the East Money gate on requests
    import requests

    return requests


def _day(value: Any) -> Optional[date]:
    text = str(value or "").strip().replace("-", "")[:8]
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_monitor(payload: Any) -> List[MonitoredStock]:
    if not isinstance(payload, list):
        raise SourceError("东方财富重点监控名单的返回结构变了（不是列表）")
    out = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        code = str(row.get("STKCODE") or "").strip()
        start, end = _day(row.get("VALIDATESTARTDATE")), _day(row.get("VALIDATEENDDATE"))
        if not _STOCK_CODE.fullmatch(code) or start is None or end is None:
            continue
        out.append(MonitoredStock(code=code, name=str(row.get("STKNAME") or "").strip(), start=start, end=end))
    if payload and not out and not any(isinstance(r, dict) and r.get("STKCODE") for r in payload):
        raise SourceError("东方财富重点监控名单的返回结构变了（没有 STKCODE）")
    return out


def parse_anomalies(payload: Any) -> List[PriceAnomaly]:
    if not isinstance(payload, dict) or "result" not in payload:
        raise SourceError("东方财富异常波动列表的返回结构变了")
    if payload.get("result") != 0:
        raise SourceError(f"东方财富异常波动列表返回错误：{payload.get('result')} {payload.get('msg') or ''}".strip())
    day = _day(payload.get("date"))
    rows = payload.get("data") or []
    if day is None or not isinstance(rows, list):
        raise SourceError("东方财富异常波动列表的返回结构变了（没有 date / data）")
    out = []
    for row in rows:
        code = str((row or {}).get("c") or "").strip()
        if not _STOCK_CODE.fullmatch(code):
            continue
        days, rule = _num(row.get("d")), _num(row.get("e"))
        out.append(PriceAnomaly(
            code=code, name=str(row.get("n") or "").strip(), day=day, deviation_pct=_num(row.get("x")),
            days=None if days is None else int(days), change_pct=_num(row.get("a")),
            rule=None if rule is None else int(rule),
        ))
    return out


def fetch_monitor(*, timeout_sec: float) -> List[MonitoredStock]:
    response = _session().get(MONITOR_URL, headers=_HEADERS, timeout=timeout_sec)
    response.raise_for_status()
    return parse_monitor(response.json())


def fetch_anomalies(*, timeout_sec: float, page_size: int = 200) -> List[PriceAnomaly]:
    response = _session().get(ANOMALY_URL, params={**_FIXED, "pageSize": page_size, "pageNo": 1},
                              headers=_HEADERS, timeout=timeout_sec)
    response.raise_for_status()
    return parse_anomalies(response.json())
