# -*- coding: utf-8 -*-
"""Capital data of one A-share stock, read on demand (docs/design/a-stock-data-adoption.md section 5).

* 两融 — Tushare ``margin_detail`` for this stock (one call; not stored, cached in-process for 30 minutes);
* 主力资金 — 新浪个股历史资金流 (``src/services/sources/sina_fund_flow.py``; one call, cached for 30 minutes);
* 龙虎榜 — ``stock_event`` kind ``dragon_tiger``, stored by the daily company-event run (no call here);
* 北向 — Tushare ``moneyflow_hsgt`` for the market (one call, cached in-process for an hour).

Each section fails on its own: a broken source gives ``{"status": "failed", "error": ...}`` for that
section and the others are still returned. A failure is remembered for two minutes, an empty answer is cached like
any other, and a non-primary user over their on-demand fetch limit (``USER_ONDEMAND_FETCH_MAX_PER_HOUR``) gets
``{"status": "rate_limited"}`` for the sections that would have hit an external source (``src/services/on_demand_fetch.py``).
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
SECTION_CACHE_SECONDS = 1800  # margin / fund flow of one stock: published once a day
SECTION_FAILURE_SECONDS = 120  # a failing source is not asked again on every request
_SECTION_CACHE_MAX = 2000
RATE_LIMITED_NOTE = "按需取数次数已达上限，稍后再试"

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
# (section, code, days) -> (expires_at, "ok" | "error", value | message)
_section_cache: Dict[tuple, tuple] = {}


def reset_cache_for_tests() -> None:
    with _northbound_lock:
        _northbound_cache.clear()
        _section_cache.clear()


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
        user_id: Optional[str] = None,
    ) -> None:
        self._fetcher = fetcher
        self._events = events
        self._today = today or (lambda: datetime.now().date())
        self._clock = clock
        # The user whose request may trigger external fetches (None = primary user / system: not rate limited).
        self._user_id = user_id

    @property
    def events(self) -> StockEventService:
        if self._events is None:
            self._events = StockEventService(fetcher=self._fetcher, today=self._today, user_id=self._user_id)
        return self._events

    @property
    def fetcher(self) -> Any:
        return self._fetcher if self._fetcher is not None else self.events.fetcher

    def _cached_section(
        self, name: str, code: str, days: int, load: Callable[[], Dict[str, Any]], limited: Dict[str, Any],
    ) -> Dict[str, Any]:
        """``load()`` at most once per ``SECTION_CACHE_SECONDS`` per (section, code, days); failures are remembered briefly.

        A cache or failure hit costs no on-demand permit; a real fetch takes one (``limited`` is returned, uncached, when
        the user is over the limit).
        """
        from src.services.on_demand_fetch import allow_on_demand_fetch

        key = (name, code, days)
        now = self._clock()
        with _northbound_lock:
            hit = _section_cache.get(key)
        if hit is not None and hit[0] > now:
            if hit[1] == "error":
                raise RuntimeError(hit[2])
            return hit[2]
        if not allow_on_demand_fetch(self._user_id):
            return limited
        try:
            value = load()
        except Exception as exc:  # noqa: BLE001 - remembered, then re-raised for the section to report
            with _northbound_lock:
                _section_cache[key] = (now + SECTION_FAILURE_SECONDS, "error", str(exc)[:200])
            raise
        with _northbound_lock:
            if len(_section_cache) >= _SECTION_CACHE_MAX:
                for stale in [k for k, v in _section_cache.items() if v[0] <= now]:
                    _section_cache.pop(stale, None)
                if len(_section_cache) >= _SECTION_CACHE_MAX:
                    _section_cache.clear()
            _section_cache[key] = (now + SECTION_CACHE_SECONDS, "ok", value)
        return value

    def margin(self, code: str, *, days: int = 20) -> Dict[str, Any]:
        from src.services.stock_event_service import _ts_code

        def load() -> Dict[str, Any]:
            today = self._today()
            start = today - timedelta(days=days * 2 + 10)  # calendar window covering ``days`` trading days
            rows = query_rows(self.fetcher, _MARGIN, ts_code=_ts_code(code), start_date=start.strftime("%Y%m%d"),
                              end_date=today.strftime("%Y%m%d"))
            return margin_from(rows, days=days)

        return self._cached_section(
            "margin", code, days, load,
            {"status": "rate_limited", "rows": [], "summary": {}, "note": RATE_LIMITED_NOTE},
        )

    def northbound(self, *, days: int = 5) -> Dict[str, Any]:
        today = self._today()
        with _northbound_lock:
            cached = _northbound_cache.get(days)
            if cached and cached[0] == today and self._clock() - cached[1] < NORTHBOUND_CACHE_SECONDS:
                return cached[2]
        from src.services.on_demand_fetch import allow_on_demand_fetch

        if not allow_on_demand_fetch(self._user_id):
            return {"status": "rate_limited", "rows": [], "unit": "百万元", "note": RATE_LIMITED_NOTE}
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

        def load() -> Dict[str, Any]:
            rows = sina_fund_flow.fetch(code, days=days, timeout_sec=SOURCE_TIMEOUT_SECONDS)
            items = [{"date": r.day.isoformat(), "close": r.close, "change_pct": r.change_pct,
                      "main_net": r.main_net, "total_net": r.total_net, "main_ratio_pct": r.main_ratio} for r in rows]
            main = [i["main_net"] for i in items[:5] if i["main_net"] is not None]
            return {"status": "ok" if items else "empty", "source": "新浪财经", "rows": items,
                    "summary": {"main_net_5d_yi": _yi(sum(main)) if main else None,
                                "main_net_latest_yi": _yi(items[0]["main_net"]) if items else None}}

        return self._cached_section(
            "fund_flow", code, days, load, {"status": "rate_limited", "rows": [], "note": RATE_LIMITED_NOTE},
        )

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
