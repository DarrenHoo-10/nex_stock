# -*- coding: utf-8 -*-
"""Macro and index data from Tushare (docs/design/a-stock-data-adoption.md section 11).

All interfaces were sampled once on 2026-11-09 and are permitted on this account, so Tushare is used
instead of the official web sources listed in a-stock-data:

* PMI ``cn_pmi`` (制造业 ``pmi010000``、非制造业商务活动 ``pmi020100``、综合产出 ``pmi030000``);
* 货币供应量 ``cn_m`` (亿元, %); 社会融资规模增量 ``sf_month`` (亿元; 存量 ``stk_endval`` 万亿元);
* LPR ``shibor_lpr`` (%); 主要指数估值 ``index_dailybasic`` (PE / PE TTM / PB, 市值元);
* 指数成分和权重 ``index_weight`` (monthly snapshot of ``MACRO_INDEX_CODES``).

Everything is stored in ``macro_series`` (one row per series and period, value as JSON) by
``refresh`` — run from the daily news job and skipped per series when it was fetched in the last
``REFRESH_HOURS`` — and read by ``GET /api/v1/data/macro`` and
``GET /api/v1/data/indices/{code}/constituents``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from src.services.stock_event_service import _KindSpec, _num, query_rows

logger = logging.getLogger(__name__)

REFRESH_HOURS = 20
MONTHS_BACK = 13
LPR_DAYS_BACK = 400
VALUATION_DAYS_BACK = 7
WEIGHT_DAYS_BACK = 45
# 沪深300、中证500、中证1000、上证50、创业板指、科创50
MACRO_INDEX_CODES = ("000300.SH", "000905.SH", "000852.SH", "000016.SH", "399006.SZ", "000688.SH")
INDEX_NAMES = {
    "000001.SH": "上证指数", "399001.SZ": "深证成指", "000300.SH": "沪深300", "399300.SZ": "沪深300",
    "000905.SH": "中证500", "399905.SZ": "中证500", "000852.SH": "中证1000", "000016.SH": "上证50",
    "399006.SZ": "创业板指", "000688.SH": "科创50", "399005.SZ": "中小100", "399016.SZ": "深证创新",
    "399673.SZ": "创业板50", "000005.SH": "上证商业", "000006.SH": "上证地产",
}

_PMI = _KindSpec("cn_pmi", "month,pmi010000,pmi020100,pmi030000", 2000, ("month",), "m")
_MONEY = _KindSpec("cn_m", "month,m0,m0_yoy,m1,m1_yoy,m2,m2_yoy", 2000, ("month",), "m")
_SOCIAL = _KindSpec("sf_month", "month,inc_month,inc_cumval,stk_endval", 2000, ("month",), "m")
_LPR = _KindSpec("shibor_lpr", "date,1y,5y", 4000, ("date",), "date")
_VALUATION = _KindSpec("index_dailybasic", "ts_code,trade_date,total_mv,float_mv,turnover_rate,pe,pe_ttm,pb",
                       3000, ("ts_code", "trade_date"), "trade_date")
_WEIGHT = _KindSpec("index_weight", "index_code,con_code,trade_date,weight", 7000,
                    ("index_code", "con_code", "trade_date"), "trade_date")

SERIES_LABELS = {
    "pmi": "PMI",
    "money": "货币供应量",
    "social_financing": "社会融资规模",
    "lpr": "LPR",
    "index_valuation": "主要指数估值",
    "index_weight": "指数成分和权重",
}


def _clean(row: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    return {k: _num(row.get(k)) for k in keys if _num(row.get(k)) is not None}


def pmi_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        month = str(row.get("month") or "")
        value = {"manufacturing": _num(row.get("pmi010000")), "non_manufacturing": _num(row.get("pmi020100")),
                 "composite": _num(row.get("pmi030000"))}
        if month and any(v is not None for v in value.values()):
            out.append({"period": month, "value": {k: v for k, v in value.items() if v is not None}})
    return out


def money_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"period": str(r["month"]), "value": _clean(r, ("m0", "m0_yoy", "m1", "m1_yoy", "m2", "m2_yoy"))}
            for r in rows if r.get("month")]


def social_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"period": str(r["month"]), "value": _clean(r, ("inc_month", "inc_cumval", "stk_endval"))}
            for r in rows if r.get("month")]


def lpr_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"period": str(r["date"]), "value": {"lpr_1y": _num(r.get("1y")), "lpr_5y": _num(r.get("5y"))}}
            for r in rows if r.get("date") and (_num(r.get("1y")) is not None or _num(r.get("5y")) is not None)]


def valuation_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per trade date with every index of that day."""
    by_day: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        day, code = str(row.get("trade_date") or ""), str(row.get("ts_code") or "")
        if not day or not code:
            continue
        values = _clean(row, ("pe", "pe_ttm", "pb", "turnover_rate", "total_mv", "float_mv"))
        if values:
            by_day.setdefault(day, {})[code] = {"name": INDEX_NAMES.get(code, code), **values}
    return [{"period": day, "value": {"indices": indices}} for day, indices in sorted(by_day.items())]


def weight_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Latest snapshot per index: ``index_weight:<code>`` series, period = snapshot date."""
    by_key: Dict[tuple, List[Dict[str, Any]]] = {}
    for row in rows:
        code, day = str(row.get("index_code") or ""), str(row.get("trade_date") or "")
        if code and day and row.get("con_code") and _num(row.get("weight")) is not None:
            by_key.setdefault((code, day), []).append(row)
    latest: Dict[str, str] = {}
    for code, day in by_key:
        latest[code] = max(latest.get(code, ""), day)
    out = []
    for code, day in sorted(latest.items()):
        members = sorted(by_key[(code, day)], key=lambda r: -float(r["weight"]))
        out.append({"series": f"index_weight:{code}", "period": day, "value": {
            "index_code": code, "name": INDEX_NAMES.get(code, code),
            "constituents": [[str(r["con_code"]).split(".")[0], round(float(r["weight"]), 4)] for r in members],
        }})
    return out


class MacroService:
    def __init__(self, repo: Any = None, fetcher: Any = None, *, now: Optional[Callable[[], datetime]] = None) -> None:
        self._repo = repo
        self._fetcher = fetcher
        self._now = now or datetime.now

    @property
    def repo(self) -> Any:
        if self._repo is None:
            from src.repositories.macro_repo import MacroRepository

            self._repo = MacroRepository()
        return self._repo

    @property
    def fetcher(self) -> Any:
        if self._fetcher is None:
            from data_provider.base import DataFetchError
            from data_provider.tushare_fetcher import TushareFetcher

            fetcher = TushareFetcher()
            if not fetcher.is_available():
                raise DataFetchError("Tushare 未配置（缺少 TUSHARE_TOKEN），无法获取宏观和指数数据")
            self._fetcher = fetcher
        return self._fetcher

    # -- refresh --------------------------------------------------------
    def refresh(self, *, force: bool = False) -> Dict[str, Any]:
        now = self._now()
        today = now.date()
        month_start = (today.replace(day=1) - timedelta(days=MONTHS_BACK * 31)).strftime("%Y%m")
        month_end = today.strftime("%Y%m")
        jobs: Dict[str, Callable[[], List[Dict[str, Any]]]] = {
            "pmi": lambda: pmi_rows(self._q(_PMI, start_m=month_start, end_m=month_end)),
            "money": lambda: money_rows(self._q(_MONEY, start_m=month_start, end_m=month_end)),
            "social_financing": lambda: social_rows(self._q(_SOCIAL, start_m=month_start, end_m=month_end)),
            "lpr": lambda: lpr_rows(self._q(_LPR, start_date=_ymd(today - timedelta(days=LPR_DAYS_BACK)),
                                            end_date=_ymd(today))),
            "index_valuation": lambda: self._valuations(today),
            "index_weight": lambda: self._weights(today),
        }
        summary: Dict[str, Any] = {}
        for series, job in jobs.items():
            last = self.repo.last_fetched(series)
            if not force and last is not None and now - last < timedelta(hours=REFRESH_HOURS):
                summary[series] = {"status": "cached"}
                continue
            try:
                rows = job()
                stored = self.repo.upsert(series, rows, fetched_at=now)
                summary[series] = {"status": "ok" if rows else "empty", "rows": len(rows), "stored": stored}
            except Exception as exc:  # noqa: BLE001 - one series failing keeps the others
                logger.warning("[宏观] %s 获取失败: %s", series, exc)
                summary[series] = {"status": "failed", "error": str(exc)[:200]}
                if "未配置" in str(exc):
                    break
        statuses = {v["status"] for v in summary.values()}
        summary["status"] = "failed" if statuses == {"failed"} else ("partial" if "failed" in statuses else "ok")
        return summary

    def _q(self, spec: _KindSpec, **params: Any) -> List[Dict[str, Any]]:
        return query_rows(self.fetcher, spec, **params)

    def _valuations(self, today: date) -> List[Dict[str, Any]]:
        start = today - timedelta(days=VALUATION_DAYS_BACK)
        rows: List[Dict[str, Any]] = []
        day = start
        while day <= today:
            if day.weekday() < 5:
                rows.extend(self._q(_VALUATION, trade_date=_ymd(day)))
            day += timedelta(days=1)
        return valuation_rows(rows)

    def _weights(self, today: date) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start = _ymd(today - timedelta(days=WEIGHT_DAYS_BACK))
        for code in MACRO_INDEX_CODES:
            rows.extend(self._q(_WEIGHT, index_code=code, start_date=start, end_date=_ymd(today)))
        return weight_rows(rows)

    # -- read -----------------------------------------------------------
    def overview(self, *, months: int = MONTHS_BACK) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for series in ("pmi", "money", "social_financing", "lpr"):
            rows = self.repo.latest(series, limit=months)
            out[series] = [{"period": r["period"], **r["value"]} for r in rows]
        valuation = self.repo.latest("index_valuation", limit=1)
        out["index_valuation"] = ({"trade_date": valuation[0]["period"], "indices": valuation[0]["value"]["indices"]}
                                  if valuation else None)
        out["fetched_at"] = {s: (v.isoformat(timespec="seconds") if v else None)
                             for s, v in ((s, self.repo.last_fetched(s)) for s in SERIES_LABELS)}
        out["units"] = {
            "pmi": "指数（50 为荣枯线）", "money": "余额亿元，同比 %", "social_financing": "增量亿元，存量万亿元",
            "lpr": "%", "index_valuation": "PE / PB 倍，市值元，换手率 %",
        }
        return out

    def constituents(self, index_code: str) -> Optional[Dict[str, Any]]:
        rows = self.repo.latest(f"index_weight:{index_code}", limit=1)
        if not rows:
            return None
        return {"trade_date": rows[0]["period"], **rows[0]["value"]}


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
