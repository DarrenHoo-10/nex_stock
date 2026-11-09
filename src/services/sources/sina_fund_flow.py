# -*- coding: utf-8 -*-
"""新浪财经个股历史资金流: the last backup for a stock's main-force flow (after AkShare / 东财, Tushare, 妙想).

``GET https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs``
with ``daima=sh600089`` (a-stock-data's backup source; one sample in tests/fixtures/sina). One row per
trading day, newest first; amounts in yuan, ratios as fractions:

* ``r0_net`` — 主力净流入 (the column of that name on Sina's 历史资金流向 page);
* ``netamount`` — 全部净流入; ``changeratio`` — 涨跌幅; ``turnover`` — 换手率 (in 0.01 %).

Only 沪深 stocks: Sina has no 北交所 rows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from src.services.sources import SourceError

FLOW_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


@dataclass
class FlowDay:
    day: date
    close: Optional[float]
    change_pct: Optional[float]
    main_net: Optional[float]   # yuan
    total_net: Optional[float]  # yuan
    main_ratio: Optional[float]  # % of turnover


_last: Dict[str, Any] = {}
_last_lock = threading.Lock()


def _remember(ok: bool, error: str = "") -> None:
    with _last_lock:
        _last.clear()
        _last.update({"ok": ok, "at": datetime.now().isoformat(timespec="seconds"), "error": error[:200]})


def last_status() -> Dict[str, Any]:
    """Result of this process's latest request (empty before the first one); shown in 数据状态."""
    with _last_lock:
        return dict(_last)


def _session() -> Any:
    import requests

    return requests


def symbol(code: str) -> Optional[str]:
    """``sh600089`` / ``sz002773``; None for codes Sina does not cover (北交所, non-A-shares)."""
    if re.fullmatch(r"(60|68)\d{4}", code or ""):
        return f"sh{code}"
    if re.fullmatch(r"(00|30)\d{4}", code or ""):
        return f"sz{code}"
    return None


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse(payload: Any) -> List[FlowDay]:
    if payload is None:  # Sina answers ``null`` for codes it does not know
        return []
    if not isinstance(payload, list):
        raise SourceError("新浪资金流的返回结构变了（不是列表）")
    out = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(str(row.get("opendate") or ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        change, ratio = _num(row.get("changeratio")), _num(row.get("r0_ratio"))
        out.append(FlowDay(
            day=day, close=_num(row.get("trade")),
            change_pct=None if change is None else round(change * 100, 2),
            main_net=_num(row.get("r0_net")), total_net=_num(row.get("netamount")),
            main_ratio=None if ratio is None else round(ratio * 100, 2),
        ))
    if payload and not out:
        raise SourceError("新浪资金流的返回结构变了（没有可识别的日期行）")
    return sorted(out, key=lambda item: item.day, reverse=True)


def fetch(code: str, *, days: int = 10, timeout_sec: float) -> List[FlowDay]:
    sym = symbol(code)
    if sym is None:
        raise SourceError(f"新浪资金流只覆盖沪深股票：{code}")
    try:
        response = _session().get(
            FLOW_URL, params={"page": 1, "num": max(1, int(days)), "sort": "opendate", "asc": 0, "daima": sym},
            headers=_HEADERS, timeout=timeout_sec,
        )
        response.raise_for_status()
        text = response.content.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text) if text else None
        except ValueError as exc:
            raise SourceError(f"新浪资金流返回的不是 JSON：{text[:60]}") from exc
        rows = parse(payload)
    except Exception as exc:
        _remember(False, str(exc))
        raise
    _remember(True)
    return rows


def capital_flow(code: str, *, timeout_sec: float) -> Dict[str, Any]:
    """The ``get_capital_flow`` result shape used by ``DataFetcherManager``'s supplement chain."""
    result: Dict[str, Any] = {"status": "not_supported", "stock_flow": {}, "source_chain": [], "errors": []}
    rows = fetch(code, days=10, timeout_sec=timeout_sec)
    if not rows or rows[0].main_net is None:
        result["errors"].append("sina: empty fund-flow series")
        return result

    def window(size: int) -> Optional[float]:
        values = [row.main_net for row in rows[:size]]
        if len(values) < size or any(v is None for v in values):
            return None
        return float(sum(values))

    result["stock_flow"] = {"main_net_inflow": rows[0].main_net, "inflow_5d": window(5), "inflow_10d": window(10)}
    result["status"] = "partial"
    result["source_chain"].append(f"sina:moneyflow:{rows[0].day.strftime('%Y%m%d')}")
    return result
