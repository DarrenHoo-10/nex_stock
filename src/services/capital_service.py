# -*- coding: utf-8 -*-
"""Capital data of one A-share stock, read on demand (docs/design/a-stock-data-adoption.md section 5).

* 两融 — Tushare ``margin_detail`` for this stock (one call; not stored);
* 主力资金 — 新浪个股历史资金流 (``src/services/sources/sina_fund_flow.py``; one call);
* 龙虎榜 — ``stock_event`` kind ``dragon_tiger``, stored by the daily company-event run (no call here);
* 北向 — Tushare ``moneyflow_hsgt`` for the market (one call, cached in-process for an hour).

Each section fails on its own: a broken source gives ``{"status": "failed", "error": ...}`` for that
section and the others are still returned.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from src.services.stock_event_service import StockEventService, _KindSpec, _num, query_rows

logger = logging.getLogger(__name__)

NORTHBOUND_CACHE_SECONDS = 3600
SOURCE_TIMEOUT_SECONDS = 8.0

_MARGIN = _KindSpec(
    "margin_detail", "trade_date,ts_code,rzye,rqye,rzmre,rzche,rqyl,rzrqye", 1000, ("ts_code", "trade_date"),
    "trade_date",
)
_HSGT = _KindSpec(
    "moneyflow_hsgt", "trade_date,hgt,sgt,north_money,ggt_ss,ggt_sz,south_money", 1000, ("trade_date",),
    "trade_date",
)
NORTHBOUND_NOTE = (
    "单位百万元，来自 Tushare moneyflow_hsgt。2024 年 8 月起交易所盘后只公布北向成交额，不再公布净买入，"
    "这里的数值量级与成交额一致，不能当作净买入看。"
)

_northbound_cache: Dict[int, Any] = {}
_northbound_lock = threading.Lock()


def reset_cache_for_tests() -> None:
    with _northbound_lock:
        _northbound_cache.clear()


def _iso(value: Any) -> str:
    text = str(value or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 and text.isdigit() else text


def _yi(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value / 1e8, 2)


def margin_from(rows: List[Dict[str, Any]], *, days: int) -> Dict[str, Any]:
    """融资融券 rows (yuan) -> newest-first days plus a summary in 亿元."""
    ordered = sorted((r for r in rows if r.get("trade_date")), key=lambda r: str(r["trade_date"]), reverse=True)[:days]
    if not ordered:
        return {"status": "empty", "rows": [], "summary": {},
                "note": "没有两融数据：这只股票不是两融标的，或最近没有公布"}
    items = []
    for row in ordered:
        buy, repay = _num(row.get("rzmre")), _num(row.get("rzche"))
        items.append({
            "date": _iso(row.get("trade_date")), "financing_balance": _num(row.get("rzye")),
            "financing_buy": buy, "financing_repay": repay,
            "financing_net_buy": None if buy is None or repay is None else buy - repay,
            "short_balance": _num(row.get("rqye")), "short_volume": _num(row.get("rqyl")),
            "total_balance": _num(row.get("rzrqye")),
        })
    latest, oldest = items[0], items[-1]
    change = None
    if latest["financing_balance"] is not None and oldest["financing_balance"]:
        change = round((latest["financing_balance"] / oldest["financing_balance"] - 1) * 100, 2)
    recent = [i["financing_net_buy"] for i in items[:5] if i["financing_net_buy"] is not None]
    return {
        "status": "ok", "rows": items,
        "summary": {
            "date": latest["date"], "financing_balance_yi": _yi(latest["financing_balance"]),
            "short_balance_yi": _yi(latest["short_balance"]),
            "financing_balance_change_pct": change, "window_days": len(items),
            "financing_net_buy_5d_yi": _yi(sum(recent)) if recent else None,
        },
    }


def northbound_from(rows: List[Dict[str, Any]], *, days: int) -> Dict[str, Any]:
    ordered = sorted((r for r in rows if r.get("trade_date")), key=lambda r: str(r["trade_date"]), reverse=True)[:days]
    items = [{
        "date": _iso(row.get("trade_date")), "north_money": _num(row.get("north_money")),
        "hgt": _num(row.get("hgt")), "sgt": _num(row.get("sgt")),
        "south_money": _num(row.get("south_money")),
    } for row in ordered]
    return {"status": "ok" if items else "empty", "rows": items, "unit": "百万元", "note": NORTHBOUND_NOTE}


class CapitalService:
    def __init__(
        self,
        fetcher: Any = None,
        events: Optional[StockEventService] = None,
        *,
        today: Optional[Callable[[], date]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._events = events
        self._today = today or (lambda: datetime.now().date())
        self._clock = clock

    @property
    def events(self) -> StockEventService:
        if self._events is None:
            self._events = StockEventService(fetcher=self._fetcher, today=self._today)
        return self._events

    @property
    def fetcher(self) -> Any:
        return self._fetcher if self._fetcher is not None else self.events.fetcher

    def margin(self, code: str, *, days: int = 20) -> Dict[str, Any]:
        from src.services.stock_event_service import _ts_code

        today = self._today()
        start = today - timedelta(days=days * 2 + 10)  # calendar window covering ``days`` trading days
        rows = query_rows(self.fetcher, _MARGIN, ts_code=_ts_code(code), start_date=start.strftime("%Y%m%d"),
                          end_date=today.strftime("%Y%m%d"))
        return margin_from(rows, days=days)

    def northbound(self, *, days: int = 5) -> Dict[str, Any]:
        today = self._today()
        with _northbound_lock:
            cached = _northbound_cache.get(days)
            if cached and cached[0] == today and self._clock() - cached[1] < NORTHBOUND_CACHE_SECONDS:
                return cached[2]
        start = today - timedelta(days=days * 2 + 10)
        rows = query_rows(self.fetcher, _HSGT, start_date=start.strftime("%Y%m%d"), end_date=today.strftime("%Y%m%d"))
        result = northbound_from(rows, days=days)
        with _northbound_lock:
            _northbound_cache[days] = (today, self._clock(), result)
        return result

    def fund_flow(self, code: str, *, days: int = 10) -> Dict[str, Any]:
        from src.services.sources import sina_fund_flow

        if sina_fund_flow.symbol(code) is None:
            return {"status": "not_supported", "rows": [], "note": "新浪资金流只覆盖沪深股票"}
        rows = sina_fund_flow.fetch(code, days=days, timeout_sec=SOURCE_TIMEOUT_SECONDS)
        items = [{"date": r.day.isoformat(), "close": r.close, "change_pct": r.change_pct,
                  "main_net": r.main_net, "total_net": r.total_net, "main_ratio_pct": r.main_ratio} for r in rows]
        main = [i["main_net"] for i in items[:5] if i["main_net"] is not None]
        return {"status": "ok" if items else "empty", "source": "新浪财经", "rows": items,
                "summary": {"main_net_5d_yi": _yi(sum(main)) if main else None,
                            "main_net_latest_yi": _yi(items[0]["main_net"]) if items else None}}

    def dragon_tiger(self, code: str, *, days: int = 30) -> List[Dict[str, Any]]:
        return self.events.events_for(code, kinds=("dragon_tiger",), days=days, fetch=False)["events"]

    def overview(self, code: str, *, days: int = 20) -> Dict[str, Any]:
        def section(name: str, call: Callable[[], Any]) -> Any:
            try:
                return call()
            except Exception as exc:  # noqa: BLE001 - one source failing keeps the others
                logger.warning("[资金面] %s %s 获取失败: %s", code, name, exc)
                return {"status": "failed", "error": str(exc)[:200]}

        dragon = section("dragon_tiger", lambda: self.dragon_tiger(code, days=max(days, 30)))
        return {
            "stock_code": code, "as_of": self._today().isoformat(),
            "margin": section("margin", lambda: self.margin(code, days=days)),
            "fund_flow": section("fund_flow", lambda: self.fund_flow(code, days=min(days, 20))),
            "dragon_tiger": dragon if isinstance(dragon, list) else [],
            "dragon_tiger_status": dragon if isinstance(dragon, dict) else {"status": "ok" if dragon else "empty"},
            "northbound": section("northbound", lambda: self.northbound(days=5)),
        }
