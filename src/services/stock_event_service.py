# -*- coding: utf-8 -*-
"""Company events of A-shares: 业绩预告 / 快报、股东增减持、回购、限售解禁、股权质押、机构调研、
券商研报（盈利预测与评级）、龙虎榜，以及打板和风险名单：开盘啦涨停 / 炸板 / 跌停、同花顺 / 东方财富热股榜、
东方财富重点监控名单和严重异常波动.

Source: Tushare (all permitted on this account, sampled 2026-11-09). Every kind except 质押 can be
queried for the whole market by day, so one daily run stores the events of all stocks in about ten
calls (a first run backfills ``BACKFILL_DAYS``, 研报 ``RESEARCH_DAYS``); consumers then read ``stock_event``:

* screening — candidate context and fixed risk notes (``risk_notes``);
* the next-day plan — risks of held / watched stocks;
* ``GET /api/v1/stocks/{code}/events`` / ``/research`` / ``/capital`` — the web page and the assistant.

The two 东方财富 risk lists are not Tushare: ``refresh_risk_lists`` reads them (two requests) and runs
even without a Tushare token.

When the table has nothing recent for a stock (the daily run never ran, or failed), ``events_for``
fetches that stock alone (one call per kind; 龙虎榜 is only queryable by trade date, so it comes from
the daily run alone). See docs/design/a-stock-data-adoption.md section 3.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

BACKFILL_DAYS = 30
SHARE_FLOAT_AHEAD_DAYS = 30
RETENTION_DAYS = 120
PLEDGE_MIN_RATIO = 10.0  # only stocks with at least this pledge ratio are stored
PLEDGE_LOOKBACK_FRIDAYS = 3
CODE_REFRESH_HOURS = 24
RESEARCH_DAYS = 90            # consensus window of broker forecasts
DRAGON_TIGER_BACKFILL_DAYS = 10
SENTIMENT_BACKFILL_DAYS = 5
HOT_RANK_TOP_N = 100
HOT_REASON_MAX_CHARS = 300
PRICE_ANOMALY_RISK_DAYS = 10

KIND_LABELS = {
    "forecast": "业绩预告",
    "express": "业绩快报",
    "holder_trade": "股东增减持",
    "repurchase": "回购",
    "share_float": "限售解禁",
    "pledge": "股权质押",
    "survey": "机构调研",
    "research": "券商研报",
    "dragon_tiger": "龙虎榜",
    "limit_board": "涨停 / 炸板 / 跌停",
    "hot_rank": "热股榜",
    "risk_watch": "重点监控名单",
    "price_anomaly": "严重异常波动",
}
# Kinds fetched per stock from Tushare (``fetch_code``); the others come from market-wide daily lists.
CODE_FETCH_KINDS = ("forecast", "express", "holder_trade", "repurchase", "survey", "research", "share_float", "pledge")
ALL_KINDS = tuple(KIND_LABELS)

# Risk thresholds (deterministic; not left to the model)
UNLOCK_RISK_RATIO = 1.0       # % of float shares unlocking in the window
UNLOCK_RISK_DAYS = 30
REDUCE_RISK_RATIO = 0.5       # % of total shares sold by one holder
REDUCE_RISK_DAYS = 30
PLEDGE_RISK_RATIO = 50.0
BAD_FORECAST_TYPES = ("预减", "首亏", "续亏", "略减")
BAD_FORECAST_DAYS = 60


@dataclass(frozen=True)
class _KindSpec:
    api: str
    fields: str
    page_cap: int
    key_cols: Tuple[str, ...]
    date_param: str  # market-wide query parameter for one day
    backfill_days: Optional[int] = None  # first-run window when it differs from BACKFILL_DAYS


_DAY_KINDS: Dict[str, _KindSpec] = {
    "forecast": _KindSpec(
        "forecast",
        "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max,summary,change_reason",
        3500, ("ts_code", "ann_date", "end_date", "type"), "ann_date",
    ),
    "express": _KindSpec(
        "express", "ts_code,ann_date,end_date,revenue,n_income,yoy_net_profit,yoy_sales,perf_summary",
        2000, ("ts_code", "ann_date", "end_date"), "ann_date",
    ),
    "holder_trade": _KindSpec(
        "stk_holdertrade",
        "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_share,after_ratio,avg_price,"
        "begin_date,close_date",
        3000, ("ts_code", "ann_date", "holder_name", "in_de", "change_vol"), "ann_date",
    ),
    "repurchase": _KindSpec(
        "repurchase", "ts_code,ann_date,end_date,proc,exp_date,vol,amount,high_limit,low_limit",
        2000, ("ts_code", "ann_date", "proc", "amount"), "ann_date",
    ),
    "survey": _KindSpec(
        "stk_surv", "ts_code,name,surv_date,fund_visitors,rece_place,rece_mode,rece_org,org_type",
        400, ("ts_code", "surv_date", "rece_org"), "trade_date",
    ),
    "research": _KindSpec(
        "report_rc",
        "ts_code,name,report_date,report_title,report_type,classify,org_name,author_name,quarter,op_rt,np,eps,pe,"
        "roe,rating,max_price,min_price",
        3000, ("ts_code", "report_date", "org_name", "report_title", "quarter"), "report_date", RESEARCH_DAYS,
    ),
}
_SHARE_FLOAT = _KindSpec(
    "share_float", "ts_code,ann_date,float_date,float_share,float_ratio,holder_name,share_type",
    6000, ("ts_code", "float_date", "holder_name", "float_share"), "float_date",
)
_PLEDGE = _KindSpec(
    "pledge_stat", "ts_code,end_date,pledge_count,unrest_pledge,rest_pledge,total_share,pledge_ratio",
    3000, ("ts_code", "end_date"), "end_date",
)
_TOP_LIST = _KindSpec(
    "top_list", "trade_date,ts_code,name,close,pct_change,turnover_rate,amount,l_buy,l_sell,net_amount,reason",
    3000, ("ts_code", "reason"), "trade_date",
)
_TOP_INST = _KindSpec(
    "top_inst", "trade_date,ts_code,exalter,side,buy,sell,net_buy,reason",
    10000, ("ts_code", "reason", "side", "exalter", "buy", "sell"), "trade_date",
)
_KPL = _KindSpec(
    "kpl_list", "ts_code,name,trade_date,tag,lu_desc,theme,status,lu_time,open_time,pct_chg",
    8000, ("ts_code", "tag"), "trade_date",
)
KPL_TAGS = ("涨停", "炸板", "跌停")
_THS_HOT = _KindSpec("ths_hot", "trade_date,ts_code,ts_name,rank,rank_reason,concept", 2000, ("ts_code",), "trade_date")
_DC_HOT = _KindSpec("dc_hot", "trade_date,ts_code,ts_name,rank", 2000, ("ts_code",), "trade_date")


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _day(value: Any) -> Optional[date]:
    text = str(value or "").strip()[:10].replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def _code(ts_code: Any) -> str:
    return str(ts_code or "").split(".")[0].strip()


def _ts_code(code: str) -> str:
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return f"{code}.SH"
    if code.startswith(("4", "8", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _fmt(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "-"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _money(yuan: Optional[float]) -> str:
    if yuan is None:
        return "-"
    if abs(yuan) >= 1e8:
        return f"{_fmt(yuan / 1e8)} 亿元"
    return f"{_fmt(yuan / 1e4, 0)} 万元"


def _period(value: Any) -> str:
    d = _day(value)
    if d is None:
        return ""
    quarter = {3: "一季报", 6: "半年报", 9: "三季报", 12: "年报"}.get(d.month, "")
    return f"{d.year} 年{quarter}" if quarter else d.isoformat()


def _hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _record(
    code: str, kind: str, event_date: date, title: str, payload: Dict[str, Any], *, source: str = "tushare",
) -> Dict[str, Any]:
    clean = {k: v for k, v in payload.items() if v is not None and not (isinstance(v, float) and math.isnan(v))}
    return {"code": code, "kind": kind, "event_date": event_date, "title": title, "payload": clean,
            "payload_hash": _hash(clean), "source": source}


# ----------------------------------------------------------------------
# parsers: Tushare rows -> event records (pure)
# ----------------------------------------------------------------------
def parse_forecast(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        code, ann = _code(row.get("ts_code")), _day(row.get("ann_date"))
        if not code or ann is None:
            continue
        lo, hi = _num(row.get("p_change_min")), _num(row.get("p_change_max"))
        change = f"，净利润同比 {_fmt(lo)}%~{_fmt(hi)}%" if lo is not None or hi is not None else ""
        title = f"业绩预告（{_period(row.get('end_date'))}）：{row.get('type') or '未分类'}{change}"
        out.append(_record(code, "forecast", ann, title, dict(row)))
    return out


def parse_express(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        code, ann = _code(row.get("ts_code")), _day(row.get("ann_date"))
        if not code or ann is None:
            continue
        revenue, profit, yoy = _num(row.get("revenue")), _num(row.get("n_income")), _num(row.get("yoy_net_profit"))
        details = []
        if revenue is not None:
            details.append(f"营收 {_fmt(revenue / 1e8)} 亿元")
        if profit is not None:
            details.append(f"净利润 {_fmt(profit / 1e8)} 亿元" + (f"，同比 {_fmt(yoy)}%" if yoy is not None else ""))
        title = f"业绩快报（{_period(row.get('end_date'))}）" + (f"：{'，'.join(details)}" if details else "")
        out.append(_record(code, "express", ann, title, dict(row)))
    return out


_HOLDER_TYPES = {"G": "高管", "C": "公司股东", "P": "个人股东"}


def parse_holder_trades(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        code, ann = _code(row.get("ts_code")), _day(row.get("ann_date"))
        if not code or ann is None:
            continue
        action = "增持" if str(row.get("in_de") or "").upper() == "IN" else "减持"
        vol, ratio, price = _num(row.get("change_vol")), _num(row.get("change_ratio")), _num(row.get("avg_price"))
        who = f"{_HOLDER_TYPES.get(str(row.get('holder_type') or ''), '股东')}{row.get('holder_name') or ''}"
        detail = f" {_fmt(vol / 1e4)} 万股" if vol is not None else ""
        detail += f"（占总股本 {_fmt(ratio, 3)}%）" if ratio is not None else ""
        detail += f"，均价 {_fmt(price)} 元" if price is not None else ""
        out.append(_record(code, "holder_trade", ann, f"{who}{action}{detail}", dict(row)))
    return out


def parse_repurchases(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        code, ann = _code(row.get("ts_code")), _day(row.get("ann_date"))
        if not code or ann is None:
            continue
        amount, high = _num(row.get("amount")), _num(row.get("high_limit"))
        parts = [f"回购（{row.get('proc') or '进展'}）"]
        if amount is not None:
            parts.append(f"金额 {_fmt(amount / 1e8)} 亿元")
        if high is not None:
            parts.append(f"价格上限 {_fmt(high)} 元")
        out.append(_record(code, "repurchase", ann, "，".join(parts), dict(row)))
    return out


def parse_surveys(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One event per stock and survey day (Tushare returns one row per visiting institution)."""
    grouped: Dict[Tuple[str, date], List[Dict[str, Any]]] = {}
    for row in rows:
        code, day = _code(row.get("ts_code")), _day(row.get("surv_date"))
        if code and day is not None:
            grouped.setdefault((code, day), []).append(dict(row))
    out = []
    for (code, day), items in sorted(grouped.items()):
        orgs = sorted({str(item.get("rece_org") or "").strip() for item in items if item.get("rece_org")})
        modes = sorted({str(item.get("rece_mode") or "").strip() for item in items if item.get("rece_mode")})
        title = f"机构调研：{len(orgs) or len(items)} 家机构" + (f"（{'、'.join(modes)[:40]}）" if modes else "")
        out.append(_record(code, "survey", day, title, {"orgs": orgs[:30], "modes": modes, "rows": len(items)}))
    return out


def parse_share_floats(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One event per stock and unlock day, summed over holders."""
    grouped: Dict[Tuple[str, date], List[Dict[str, Any]]] = {}
    for row in rows:
        code, day = _code(row.get("ts_code")), _day(row.get("float_date"))
        if code and day is not None:
            grouped.setdefault((code, day), []).append(dict(row))
    out = []
    for (code, day), items in sorted(grouped.items()):
        shares = sum(_num(item.get("float_share")) or 0.0 for item in items)
        ratio = sum(_num(item.get("float_ratio")) or 0.0 for item in items)
        types = sorted({str(item.get("share_type") or "").strip() for item in items if item.get("share_type")})
        title = (f"{day.isoformat()} 限售解禁 {_fmt(shares / 1e4, 0)} 万股，占流通股 {_fmt(ratio)}%"
                 f"（{len(items)} 个股东" + (f"，{'、'.join(types)[:30]}" if types else "") + "）")
        out.append(_record(code, "share_float", day, title, {
            "float_share": shares, "float_ratio": round(ratio, 4), "holders": len(items), "share_types": types,
        }))
    return out


def parse_pledges(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        code, day, ratio = _code(row.get("ts_code")), _day(row.get("end_date")), _num(row.get("pledge_ratio"))
        if not code or day is None or ratio is None or ratio < PLEDGE_MIN_RATIO:
            continue
        title = f"股权质押比例 {_fmt(ratio)}%（{int(_num(row.get('pledge_count')) or 0)} 笔，统计截至 {day.isoformat()}）"
        out.append(_record(code, "pledge", day, title, dict(row)))
    return out


def parse_research(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One event per broker report; Tushare returns one row per forecast year (``quarter``)."""
    grouped: Dict[Tuple[str, date, str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        code, day = _code(row.get("ts_code")), _day(row.get("report_date"))
        org, title = str(row.get("org_name") or "").strip(), str(row.get("report_title") or "").strip()
        if code and day is not None and org:
            grouped.setdefault((code, day, org, title), []).append(dict(row))
    out = []
    for (code, day, org, title), items in sorted(grouped.items()):
        first = items[0]
        forecasts = []
        for item in sorted(items, key=lambda r: str(r.get("quarter") or "")):
            entry = {"quarter": str(item.get("quarter") or ""), "eps": _num(item.get("eps")),
                     "np": _num(item.get("np")), "op_rt": _num(item.get("op_rt")),
                     "pe": _num(item.get("pe")), "roe": _num(item.get("roe"))}
            if entry["quarter"] and any(entry[k] is not None for k in ("eps", "np", "op_rt")):
                forecasts.append({k: v for k, v in entry.items() if v is not None})
        rating = str(first.get("rating") or "").strip()
        high, low = _num(first.get("max_price")), _num(first.get("min_price"))
        parts = [f"{org}《{title or '研报'}》"]
        if rating:
            parts.append(f"评级{rating}")
        if high is not None:
            if low is not None and low != high:
                parts.append(f"目标价 {_fmt(low)}~{_fmt(high)} 元")
            else:
                parts.append(f"目标价 {_fmt(high)} 元")
        nearest = forecasts[0] if forecasts else None
        if nearest and nearest.get("eps") is not None:
            parts.append(f"{nearest['quarter'][:4]} 年 EPS {_fmt(nearest['eps'])}")
        out.append(_record(code, "research", day, "，".join(parts), {
            "org": org, "title": title, "authors": first.get("author_name"), "report_type": first.get("report_type"),
            "classify": first.get("classify"), "rating": rating or None, "max_price": high, "min_price": low,
            "forecasts": forecasts,
        }))
    return out


_MULTI_DAY_PREFIX = "连续"  # 「连续三个交易日…」accumulates three sessions; single-day reasons describe this one


def parse_dragon_tiger(list_rows: Iterable[Dict[str, Any]], inst_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One event per stock and trade date.

    ``top_list`` has one row per listing reason, ``top_inst`` one row per seat, side and reason; the same seat
    repeats across reasons and on both sides. The primary reason is the first single-day one (its amounts are
    that day's); seats of that reason are kept once each (same seat = same name, buy and sell, so several
    「机构专用」 seats stay separate).
    """
    by_stock: Dict[Tuple[str, date], List[Dict[str, Any]]] = {}
    for row in list_rows:
        code, day = _code(row.get("ts_code")), _day(row.get("trade_date"))
        if code and day is not None:
            by_stock.setdefault((code, day), []).append(dict(row))
    seats_by: Dict[Tuple[str, date, str], List[Dict[str, Any]]] = {}
    for row in inst_rows:
        code, day = _code(row.get("ts_code")), _day(row.get("trade_date"))
        if code and day is not None:
            seats_by.setdefault((code, day, str(row.get("reason") or "").strip()), []).append(dict(row))
    out = []
    for (code, day), rows in sorted(by_stock.items()):
        reasons = list(dict.fromkeys(str(r.get("reason") or "").strip() for r in rows if r.get("reason")))
        primary = next((r for r in rows if not str(r.get("reason") or "").startswith(_MULTI_DAY_PREFIX)), rows[0])
        reason = str(primary.get("reason") or "").strip()
        seats, seen = [], set()
        for seat in seats_by.get((code, day, reason), []):
            name = str(seat.get("exalter") or "").strip()
            buy, sell, net = _num(seat.get("buy")), _num(seat.get("sell")), _num(seat.get("net_buy"))
            key = (name, buy, sell)
            if not name or key in seen:
                continue
            seen.add(key)
            seats.append({"name": name, "buy": buy, "sell": sell,
                          "net": net if net is not None else (buy or 0.0) - (sell or 0.0)})
        seats.sort(key=lambda item: -abs(item["net"] or 0.0))
        inst = [item["net"] for item in seats if "机构专用" in item["name"]]
        connect = [item["net"] for item in seats if "股通专用" in item["name"]]
        net_amount = _num(primary.get("net_amount"))
        title = f"龙虎榜（{reason or '上榜'}）：净买入 {_money(net_amount)}"
        if inst:
            title += f"，机构净{'买入' if sum(inst) >= 0 else '卖出'} {_money(abs(sum(inst)))}（{len(inst)} 席）"
        if connect:
            title += f"，沪深股通净{'买入' if sum(connect) >= 0 else '卖出'} {_money(abs(sum(connect)))}"
        out.append(_record(code, "dragon_tiger", day, title, {
            "name": primary.get("name"), "close": _num(primary.get("close")),
            "pct_change": _num(primary.get("pct_change")), "turnover_rate": _num(primary.get("turnover_rate")),
            "amount": _num(primary.get("amount")), "l_buy": _num(primary.get("l_buy")),
            "l_sell": _num(primary.get("l_sell")), "net_amount": net_amount, "reason": reason, "reasons": reasons,
            "institution_net": sum(inst) if inst else None, "connect_net": sum(connect) if connect else None,
            "seats": seats[:10],
        }))
    return out


def parse_limit_board(rows: Iterable[Dict[str, Any]], day: date) -> List[Dict[str, Any]]:
    """开盘啦 ``kpl_list``: one event per stock (a stock is in one of 涨停 / 炸板 / 跌停 on a day)."""
    out, seen = [], set()
    for row in rows:
        code = _code(row.get("ts_code"))
        tag = str(row.get("tag") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        status, reason, theme = (str(row.get(k) or "").strip() for k in ("status", "lu_desc", "theme"))
        title = f"开盘啦{tag or '打板'}" + (f"（{status}）" if status else "")
        if reason:
            title += f"：{reason}"
        if theme:
            title += f"，题材 {theme}"
        out.append(_record(code, "limit_board", _day(row.get("trade_date")) or day, title, {
            "name": row.get("name"), "tag": tag or None, "status": status or None, "reason": reason or None,
            "theme": theme or None, "lu_time": row.get("lu_time"), "open_time": row.get("open_time"),
            "pct_chg": _num(row.get("pct_chg")),
        }))
    return out


def parse_hot_rank(ths_rows: Iterable[Dict[str, Any]], dc_rows: Iterable[Dict[str, Any]], day: date) -> List[Dict[str, Any]]:
    """同花顺热股榜 + 东方财富人气榜 of one day, merged per stock (top ``HOT_RANK_TOP_N`` of each)."""
    merged: Dict[str, Dict[str, Any]] = {}
    for source, rows in (("ths", ths_rows), ("dc", dc_rows)):
        for row in rows:
            code, rank = _code(row.get("ts_code")), _num(row.get("rank"))
            if not code or rank is None or rank > HOT_RANK_TOP_N:
                continue
            entry = merged.setdefault(code, {"name": row.get("ts_name")})
            if f"{source}_rank" in entry:
                continue
            entry[f"{source}_rank"] = int(rank)
            if source == "ths":
                reason = str(row.get("rank_reason") or "").strip()
                entry["ths_reason"] = reason[:HOT_REASON_MAX_CHARS] or None
                entry["concept"] = row.get("concept")
    out = []
    for code, payload in sorted(merged.items()):
        parts = []
        if payload.get("ths_rank"):
            parts.append(f"同花顺热股第 {payload['ths_rank']} 名")
        if payload.get("dc_rank"):
            parts.append(f"东方财富人气榜第 {payload['dc_rank']} 名")
        title = "，".join(parts)
        if payload.get("ths_reason"):
            title += f"（同花顺 AI 摘要：{payload['ths_reason'][:60]}）"
        out.append(_record(code, "hot_rank", day, title, payload))
    return out


def parse_risk_watch(items: Iterable[Any]) -> List[Dict[str, Any]]:
    """东方财富重点监控名单 (``sources.eastmoney_risk.MonitoredStock``)."""
    return [_record(item.code, "risk_watch", item.start,
                    f"东方财富重点监控名单（{item.start.isoformat()} 至 {item.end.isoformat()}）",
                    {"name": item.name, "start": item.start.isoformat(), "end": item.end.isoformat()},
                    source="eastmoney") for item in items]


def parse_price_anomalies(items: Iterable[Any]) -> List[Dict[str, Any]]:
    """东方财富严重异常波动 (``sources.eastmoney_risk.PriceAnomaly``)."""
    out = []
    for item in items:
        title = "严重异常波动"
        if item.days and item.deviation_pct is not None:
            title += f"：{item.days} 个交易日累计偏离 {_fmt(item.deviation_pct)}%"
        out.append(_record(item.code, "price_anomaly", item.day, title, {
            "name": item.name, "deviation_pct": item.deviation_pct, "days": item.days,
            "change_pct": item.change_pct, "rule": item.rule,
        }, source="eastmoney"))
    return out


def _stats(values: List[float]) -> Optional[Dict[str, Any]]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {"avg": round(sum(values) / len(values), 4), "median": round(median, 4),
            "min": ordered[0], "max": ordered[-1], "count": len(values)}


def research_summary(events: Sequence[Dict[str, Any]], as_of: date, *, days: int = RESEARCH_DAYS) -> Dict[str, Any]:
    """Consensus of broker reports within ``days``: each institution's latest report counts once.

    ``net_profit_yi`` is in 亿元 (``report_rc`` reports 万元).
    """
    since = as_of - timedelta(days=days)
    reports = [e for e in events if e.get("kind") == "research" and (_day(e.get("event_date")) or date.min) >= since]
    latest: Dict[str, Dict[str, Any]] = {}
    for event in sorted(reports, key=lambda e: str(e.get("event_date"))):
        org = str((event.get("payload") or {}).get("org") or "")
        if org:
            latest[org] = event
    ratings: Dict[str, int] = {}
    per_quarter: Dict[str, Dict[str, List[float]]] = {}
    targets: List[float] = []
    for event in latest.values():
        payload = event.get("payload") or {}
        if payload.get("rating"):
            ratings[payload["rating"]] = ratings.get(payload["rating"], 0) + 1
        if _num(payload.get("max_price")) is not None:
            targets.append(float(payload["max_price"]))
        for item in payload.get("forecasts") or []:
            bucket = per_quarter.setdefault(str(item.get("quarter")), {"eps": [], "np": []})
            for key in ("eps", "np"):
                if _num(item.get(key)) is not None:
                    bucket[key].append(float(item[key]))
    consensus = []
    for quarter in sorted(per_quarter):
        profit = _stats([v / 1e4 for v in per_quarter[quarter]["np"]])
        if profit:
            profit = {k: (round(v, 2) if k != "count" else v) for k, v in profit.items()}
        consensus.append({"quarter": quarter, "eps": _stats(per_quarter[quarter]["eps"]), "net_profit_yi": profit})
    return {
        "window_days": days, "reports": len(reports), "institutions": len(latest),
        "ratings": dict(sorted(ratings.items(), key=lambda kv: -kv[1])),
        "consensus": consensus, "target_price": _stats(targets),
    }


_PARSERS: Dict[str, Callable[[Iterable[Dict[str, Any]]], List[Dict[str, Any]]]] = {
    "forecast": parse_forecast,
    "express": parse_express,
    "holder_trade": parse_holder_trades,
    "repurchase": parse_repurchases,
    "survey": parse_surveys,
    "share_float": parse_share_floats,
    "pledge": parse_pledges,
    "research": parse_research,
}


# ----------------------------------------------------------------------
# risk notes (pure)
# ----------------------------------------------------------------------
def watch_active(event: Dict[str, Any], as_of: date) -> bool:
    payload = event.get("payload") or {}
    start, end = _day(payload.get("start") or event.get("event_date")), _day(payload.get("end"))
    return start is not None and end is not None and start <= as_of <= end


def risk_notes_from(events: Sequence[Dict[str, Any]], as_of: date) -> List[str]:
    """Fixed-rule risks from stored events; Chinese one-liners for screening and the next-day plan."""
    notes: List[str] = []
    for event in events:
        kind, day = event.get("kind"), _day(event.get("event_date"))
        payload = event.get("payload") or {}
        if day is None:
            continue
        if kind == "share_float" and as_of <= day <= as_of + timedelta(days=UNLOCK_RISK_DAYS):
            ratio = _num(payload.get("float_ratio")) or 0.0
            if ratio >= UNLOCK_RISK_RATIO:
                notes.append(f"{day.isoformat()} 将解禁占流通股 {_fmt(ratio)}% 的股份，存在抛压")
        elif kind == "holder_trade" and as_of - timedelta(days=REDUCE_RISK_DAYS) <= day <= as_of:
            ratio = _num(payload.get("change_ratio")) or 0.0
            if str(payload.get("in_de") or "").upper() == "DE" and ratio >= REDUCE_RISK_RATIO:
                notes.append(f"{day.isoformat()} 公告{payload.get('holder_name') or '股东'}减持占总股本 {_fmt(ratio, 3)}%")
        elif kind == "pledge":
            ratio = _num(payload.get("pledge_ratio")) or 0.0
            if ratio >= PLEDGE_RISK_RATIO:
                notes.append(f"股权质押比例 {_fmt(ratio)}%（截至 {day.isoformat()}），平仓风险高")
        elif kind == "forecast" and as_of - timedelta(days=BAD_FORECAST_DAYS) <= day <= as_of:
            kind_text = str(payload.get("type") or "")
            if kind_text in BAD_FORECAST_TYPES:
                notes.append(f"{day.isoformat()} 业绩预告{kind_text}")
        elif kind == "risk_watch" and watch_active(event, as_of):
            notes.append(f"在东方财富重点监控名单内（至 {payload.get('end')}），选股已排除")
        elif kind == "price_anomaly" and as_of - timedelta(days=PRICE_ANOMALY_RISK_DAYS) <= day <= as_of:
            notes.append(f"{day.isoformat()} {event.get('title') or '严重异常波动'}，按交易所规则可能停牌核查")
    # Only the latest pledge snapshot counts.
    return list(dict.fromkeys(notes))


# ----------------------------------------------------------------------
# service
# ----------------------------------------------------------------------
def query_rows(fetcher: Any, spec: _KindSpec, **params: Any) -> List[Dict[str, Any]]:
    """One Tushare interface as plain dict rows (keeps the partial rows when paging stopped early)."""
    from data_provider.tushare_market_data import IncompleteMarketDataError

    try:
        frame = fetcher._market_query_paged(
            spec.api, page_cap=spec.page_cap, key_cols=list(spec.key_cols), fields=spec.fields, **params,
        )
    except IncompleteMarketDataError as exc:
        frame = exc.partial
        logger.warning("[Tushare] %s 只取到部分数据: %s", spec.api, exc)
    if frame is None or getattr(frame, "empty", True):
        return []
    return frame.where(frame.notna(), None).to_dict("records")


class StockEventService:
    def __init__(self, repo: Any = None, fetcher: Any = None, *, today: Callable[[], date] = None) -> None:
        self._repo = repo
        self._fetcher = fetcher
        self._today = today or (lambda: datetime.now().date())

    @property
    def repo(self) -> Any:
        if self._repo is None:
            from src.repositories.stock_event_repo import StockEventRepository

            self._repo = StockEventRepository()
        return self._repo

    @property
    def fetcher(self) -> Any:
        if self._fetcher is None:
            from data_provider.base import DataFetchError
            from data_provider.tushare_fetcher import TushareFetcher

            fetcher = TushareFetcher()
            if not fetcher.is_available():
                raise DataFetchError("Tushare 未配置（缺少 TUSHARE_TOKEN），无法获取公司事件")
            self._fetcher = fetcher
        return self._fetcher

    # -- Tushare -------------------------------------------------------
    def _query(self, spec: _KindSpec, **params: Any) -> List[Dict[str, Any]]:
        return query_rows(self.fetcher, spec, **params)

    # -- daily whole-market ingest --------------------------------------
    def run_daily(self, *, backfill_days: int = BACKFILL_DAYS) -> Dict[str, Any]:
        """Store all stocks' events: missing announcement days (backfill), today again, unlock window, pledges."""
        today = self._today()
        summary: Dict[str, Any] = {"day": today.isoformat(), "kinds": {}}
        for kind, spec in _DAY_KINDS.items():
            window = max(backfill_days, spec.backfill_days or 0)
            done = self.repo.ingested_days(kind, since=today - timedelta(days=window))
            days = [today - timedelta(days=offset) for offset in range(window, -1, -1)]
            todo = [d for d in days if d == today or done.get(d) != "ok"]
            inserted = fetched = 0
            errors: List[str] = []
            for day in todo:
                try:
                    rows = self._query(spec, **{spec.date_param: day.strftime("%Y%m%d")})
                    records = _PARSERS[kind](rows)
                    inserted += self.repo.upsert(records)
                    fetched += len(records)
                    # Today is re-read next run: announcements keep arriving during the day.
                    self.repo.mark_ingested(kind, day, rows=len(records), status="partial" if day == today else "ok")
                except Exception as exc:  # noqa: BLE001 - one kind failing must not stop the others
                    errors.append(f"{day.isoformat()}: {exc}")
                    self.repo.mark_ingested(kind, day, rows=0, status="failed", error=str(exc))
                    if "无权限" in str(exc) or "未配置" in str(exc):
                        break
            summary["kinds"][kind] = {"days": len(todo), "fetched": fetched, "inserted": inserted, "errors": errors[:5]}
        summary["kinds"]["share_float"] = self._refresh_share_floats(today)
        summary["kinds"]["pledge"] = self._refresh_pledges(today)
        summary["kinds"]["dragon_tiger"] = self._refresh_dragon_tiger(
            today, backfill_days=min(backfill_days, DRAGON_TIGER_BACKFILL_DAYS))
        summary["kinds"]["limit_board"] = self._refresh_limit_board(
            today, backfill_days=min(backfill_days, SENTIMENT_BACKFILL_DAYS))
        summary["kinds"]["hot_rank"] = self._refresh_hot_rank(
            today, backfill_days=min(backfill_days, SENTIMENT_BACKFILL_DAYS))
        try:
            summary["pruned"] = self.repo.prune(before=today - timedelta(days=RETENTION_DAYS))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[公司事件] 清理过期事件失败: %s", exc)
        failed = [k for k, v in summary["kinds"].items() if v.get("errors") and not v.get("fetched")]
        summary["status"] = "failed" if len(failed) == len(summary["kinds"]) else ("partial" if failed else "ok")
        return summary

    def _refresh_share_floats(self, today: date) -> Dict[str, Any]:
        end = today + timedelta(days=SHARE_FLOAT_AHEAD_DAYS)
        try:
            rows = self._query(_SHARE_FLOAT, start_date=today.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
            records = parse_share_floats(rows)
            inserted = self.repo.upsert(records)
            self.repo.mark_ingested("share_float", today, rows=len(records), status="ok")
            return {"window": [today.isoformat(), end.isoformat()], "fetched": len(records), "inserted": inserted,
                    "errors": []}
        except Exception as exc:  # noqa: BLE001
            self.repo.mark_ingested("share_float", today, rows=0, status="failed", error=str(exc))
            return {"fetched": 0, "inserted": 0, "errors": [str(exc)]}

    def _refresh_pledges(self, today: date) -> Dict[str, Any]:
        """Weekly ChinaClear snapshot: the latest Friday that has data."""
        friday = today - timedelta(days=(today.weekday() - 4) % 7)
        errors: List[str] = []
        for _ in range(PLEDGE_LOOKBACK_FRIDAYS):
            if self.repo.ingested_days("pledge", since=friday).get(friday) == "ok":
                return {"end_date": friday.isoformat(), "fetched": 0, "inserted": 0, "errors": [], "cached": True}
            try:
                rows = self._query(_PLEDGE, end_date=friday.strftime("%Y%m%d"))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                break
            if rows:
                records = parse_pledges(rows)
                inserted = self.repo.upsert(records)
                self.repo.mark_ingested("pledge", friday, rows=len(records), status="ok")
                return {"end_date": friday.isoformat(), "fetched": len(records), "inserted": inserted, "errors": []}
            friday -= timedelta(days=7)
        return {"fetched": 0, "inserted": 0, "errors": errors}

    def _refresh_by_trade_date(
        self, kind: str, today: date, *, backfill_days: int, fetch_day: Callable[[str, date], List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """Market-wide lists published per trade date (weekdays), stored as one ingest row per day.

        Empty = a holiday, or not published yet (Tushare publishes these lists in the evening): such days
        and today are read again on the next run.
        """
        done = self.repo.ingested_days(kind, since=today - timedelta(days=backfill_days))
        days = [today - timedelta(days=offset) for offset in range(backfill_days, -1, -1)]
        todo = [d for d in days if d.weekday() < 5 and (d == today or done.get(d) != "ok")]
        inserted = fetched = 0
        errors: List[str] = []
        for day in todo:
            try:
                records = fetch_day(day.strftime("%Y%m%d"), day)
                inserted += self.repo.upsert(records)
                fetched += len(records)
                status = "ok" if records and day != today else "partial"
                self.repo.mark_ingested(kind, day, rows=len(records), status=status)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{day.isoformat()}: {exc}")
                self.repo.mark_ingested(kind, day, rows=0, status="failed", error=str(exc))
                if "无权限" in str(exc) or "未配置" in str(exc):
                    break
        return {"days": len(todo), "fetched": fetched, "inserted": inserted, "errors": errors[:5]}

    def _refresh_dragon_tiger(self, today: date, *, backfill_days: int) -> Dict[str, Any]:
        """龙虎榜: ``top_list`` + ``top_inst`` (2 calls a day; Tushare publishes around 20:00)."""

        def fetch_day(stamp: str, day: date) -> List[Dict[str, Any]]:
            listed = self._query(_TOP_LIST, trade_date=stamp)
            return parse_dragon_tiger(listed, self._query(_TOP_INST, trade_date=stamp) if listed else [])

        return self._refresh_by_trade_date("dragon_tiger", today, backfill_days=backfill_days, fetch_day=fetch_day)

    def _refresh_limit_board(self, today: date, *, backfill_days: int) -> Dict[str, Any]:
        """开盘啦涨停 / 炸板 / 跌停 (``kpl_list``, one call per tag)."""

        def fetch_day(stamp: str, day: date) -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            for tag in KPL_TAGS:
                rows.extend({**row, "tag": row.get("tag") or tag}
                            for row in self._query(_KPL, trade_date=stamp, tag=tag))
            return parse_limit_board(rows, day)

        return self._refresh_by_trade_date("limit_board", today, backfill_days=backfill_days, fetch_day=fetch_day)

    def _refresh_hot_rank(self, today: date, *, backfill_days: int) -> Dict[str, Any]:
        """同花顺热股榜 (``ths_hot``) + 东方财富人气榜 (``dc_hot``): the day's final snapshot (``is_new=Y``)."""

        def fetch_day(stamp: str, day: date) -> List[Dict[str, Any]]:
            ths = self._query(_THS_HOT, trade_date=stamp, market="热股", is_new="Y")
            dc = self._query(_DC_HOT, trade_date=stamp, market="A股市场", hot_type="人气榜", is_new="Y")
            return parse_hot_rank(ths, dc, day)

        return self._refresh_by_trade_date("hot_rank", today, backfill_days=backfill_days, fetch_day=fetch_day)

    # -- 东方财富风险名单 (no Tushare) --------------------------------------
    def refresh_risk_lists(self, *, timeout_sec: float = 10.0) -> Dict[str, Any]:
        """重点监控名单 + 严重异常波动, one request each; each list fails on its own."""
        from src.services.sources import eastmoney_risk

        today = self._today()
        summary: Dict[str, Any] = {}
        for kind, fetch, parse in (
            ("risk_watch", eastmoney_risk.fetch_monitor, parse_risk_watch),
            ("price_anomaly", eastmoney_risk.fetch_anomalies, parse_price_anomalies),
        ):
            try:
                records = parse(fetch(timeout_sec=timeout_sec))
                inserted = self.repo.upsert(records)
                self.repo.mark_ingested(kind, today, rows=len(records), status="ok")
                summary[kind] = {"status": "ok", "fetched": len(records), "inserted": inserted}
            except Exception as exc:  # noqa: BLE001
                self.repo.mark_ingested(kind, today, rows=0, status="failed", error=str(exc))
                summary[kind] = {"status": "failed", "error": str(exc)[:200]}
        statuses = {v["status"] for v in summary.values()}
        summary["status"] = "ok" if statuses == {"ok"} else ("failed" if statuses == {"failed"} else "partial")
        return summary

    def watched_codes(self, *, as_of: Optional[date] = None) -> Dict[str, str]:
        """Codes in an active 东方财富重点监控名单 window -> window end (stored list only, no request)."""
        day = as_of or self._today()
        events = self.repo.list_by_kind("risk_watch", since=day - timedelta(days=60), until=day)
        return {e["code"]: str((e.get("payload") or {}).get("end") or "") for e in events if watch_active(e, day)}

    # -- one stock ------------------------------------------------------
    def fetch_code(self, code: str, *, days: int = BACKFILL_DAYS) -> Dict[str, Any]:
        """Fetch one stock's events from Tushare (one call per kind) and store them."""
        today = self._today()
        start, end = (today - timedelta(days=days)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        ts_code = _ts_code(code)
        status: Dict[str, Any] = {}
        for kind, spec in _DAY_KINDS.items():
            params: Dict[str, Any] = {"ts_code": ts_code, "start_date": start, "end_date": end}
            status[kind] = self._fetch_code_kind(kind, spec, params)
        future = (today + timedelta(days=SHARE_FLOAT_AHEAD_DAYS)).strftime("%Y%m%d")
        status["share_float"] = self._fetch_code_kind(
            "share_float", _SHARE_FLOAT, {"ts_code": ts_code, "start_date": end, "end_date": future})
        status["pledge"] = self._fetch_code_kind("pledge", _PLEDGE, {"ts_code": ts_code})
        return status

    def _fetch_code_kind(self, kind: str, spec: _KindSpec, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rows = self._query(spec, **params)
            if kind == "pledge" and rows:  # all history: keep the latest snapshot only
                latest = max(str(r.get("end_date") or "") for r in rows)
                rows = [r for r in rows if str(r.get("end_date") or "") == latest]
            records = _PARSERS[kind](rows)
            return {"status": "ok", "fetched": len(records), "inserted": self.repo.upsert(records)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)[:200]}

    def _needs_code_fetch(self, code: str) -> bool:
        """True when neither the daily run (yesterday ingested) nor a recent per-code fetch covers ``code``."""
        yesterday = self._today() - timedelta(days=1)
        if self.repo.ingested_days("holder_trade", since=yesterday).get(yesterday) == "ok":
            return False
        # Market-wide lists (热股榜、风险名单…) also store rows for this code; only per-stock kinds count.
        last = self.repo.latest_fetch_for_code(code, kinds=CODE_FETCH_KINDS)
        return last is None or datetime.now() - last > timedelta(hours=CODE_REFRESH_HOURS)

    def events_for(
        self, code: str, *, kinds: Optional[Sequence[str]] = None, days: int = BACKFILL_DAYS, fetch: bool = True,
    ) -> Dict[str, Any]:
        """Stored events of ``code`` within ``days`` (plus future unlocks); fetches the stock when not covered."""
        today = self._today()
        sources: Dict[str, Any] = {}
        if fetch and self._needs_code_fetch(code):
            try:
                sources = self.fetch_code(code, days=max(days, BACKFILL_DAYS))
            except Exception as exc:  # noqa: BLE001 - stored events are still returned
                sources = {"tushare": {"status": "failed", "error": str(exc)[:200]}}
        wanted = [k for k in (kinds or ALL_KINDS) if k in KIND_LABELS]
        events = self.repo.list_for_code(code, since=today - timedelta(days=days),
                                         until=today + timedelta(days=SHARE_FLOAT_AHEAD_DAYS), kinds=wanted)
        # Pledge is a weekly snapshot: keep only the latest one.
        pledges = [e for e in events if e["kind"] == "pledge"]
        if len(pledges) > 1:
            latest = max(e["event_date"] for e in pledges)
            events = [e for e in events if e["kind"] != "pledge" or e["event_date"] == latest]
        return {
            "code": code, "as_of": today.isoformat(), "events": events,
            "risks": risk_notes_from(events, today), "fetch": sources,
        }

    def risk_notes(self, code: str, *, fetch: bool = False) -> List[str]:
        """Fixed-rule risks of ``code`` from stored events (no network unless ``fetch``)."""
        try:
            return self.events_for(code, kinds=("share_float", "holder_trade", "pledge", "forecast", "risk_watch",
                                                "price_anomaly"),
                                   days=BAD_FORECAST_DAYS, fetch=fetch)["risks"]
        except Exception as exc:  # noqa: BLE001 - risks are an add-on
            logger.debug("[公司事件] %s 风险读取失败: %s", code, exc)
            return []

    def status(self) -> List[Dict[str, Any]]:
        return self.repo.ingest_status()
