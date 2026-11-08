# -*- coding: utf-8 -*-
"""Shared offline fixtures for the news pool tests (no network: ``ak.*`` and the 巨潮 HTTP calls are stubs)."""

from __future__ import annotations

from datetime import date, datetime, time as dtime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import json
from pathlib import Path

import pandas as pd

from src.services import news_akshare_source as ak_source
from src.services import news_cninfo_source as cninfo_source

CNINFO_FIXTURES = Path(__file__).parent / "fixtures" / "cninfo"

NOW = datetime(2026, 11, 9, 10, 0, 0)  # a Monday, inside the 08:00-15:30 trading window


def make_config(**overrides: Any) -> SimpleNamespace:
    values: Dict[str, Any] = dict(
        news_intel_auto_fetch_enabled=True,
        news_poll_interval_minutes=10,
        news_poll_offhours_interval_minutes=30,
        news_akshare_cls_symbol="重点",
        news_flash_retention_days=3,
        news_company_retention_days=30,
        news_daily_fetch_time="08:05",
        news_dedup_window_minutes=30,
        news_fetch_timeout_sec=5.0,
        news_announcement_mode="auto",
        news_announcement_by_code_max=12,
        news_classify_enabled=True,
        news_classify_batch_size=20,
        news_classify_max_items_per_run=60,
        news_classify_max_attempts=3,
        news_board_driver_min_importance=4,
        stock_list=["600519", "300750"],
        litellm_model="openai/primary-model",
        litellm_light_model="",
        litellm_fallback_models=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def cls_df(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    """rows: ``{"title", "content", "at": datetime}`` -> stock_info_global_cls layout."""
    return pd.DataFrame({
        "标题": [r.get("title", "") for r in rows],
        "内容": [r["content"] for r in rows],
        "发布日期": [r["at"].date() for r in rows],
        "发布时间": [r["at"].time() for r in rows],
    })


def em_df(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame({
        "标题": [r["title"] for r in rows],
        "摘要": [r.get("summary", "") for r in rows],
        "发布时间": [r["at"].strftime("%Y-%m-%d %H:%M:%S") for r in rows],
        "链接": [r.get("url", f"https://finance.eastmoney.com/a/{abs(hash(r['title'])) % 10**9}.html") for r in rows],
    })


def sina_df(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame({
        "时间": [r["at"].strftime("%Y-%m-%d %H:%M:%S") for r in rows],
        "内容": [r["content"] for r in rows],
    })


def futu_df(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame({
        "标题": [r["title"] for r in rows],
        "内容": [r.get("content", "") for r in rows],
        "发布时间": [r["at"].strftime("%Y-%m-%d %H:%M:%S") for r in rows],
        "链接": [r.get("url", "https://news.futunn.com/flash/" + str(i)) for i, r in enumerate(rows)],
    })


def notice_df(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame({
        "代码": [r["code"] for r in rows],
        "名称": [r.get("name", "名称" + r["code"]) for r in rows],
        "公告标题": [r["title"] for r in rows],
        "公告类型": [r.get("category", "重大事项") for r in rows],
        "公告日期": [r["day"] for r in rows],
        "网址": [f"https://data.eastmoney.com/notices/detail/{r['code']}/{r['art']}.html" for r in rows],
    })


def brief_df(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame({
        "标题": [r["title"] for r in rows],
        "摘要": [r["summary"] for r in rows],
        "发布时间": [r["at"].strftime("%Y-%m-%d %H:%M:%S") for r in rows],
        "链接": [r["url"] for r in rows],
    })


class StubAkshare:
    """Scriptable replacement for the ``akshare`` module: every call is recorded, nothing touches the network."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.flash: Dict[str, Any] = {}  # source key -> DataFrame | Exception | callable
        self.brief: Any = pd.DataFrame()
        self.notice_by_code: Dict[str, Any] = {}
        self.notice_by_day: Dict[str, Any] = {}
        self.notice_calls: List[Dict[str, Any]] = []
        # 巨潮: code -> query payload (dict) | Exception; codes not listed fail, so by-code runs fall back to
        # 东方财富 unless a test opts in.
        self.cninfo_by_code: Dict[str, Any] = {}
        self.cninfo_requests: List[Dict[str, Any]] = []
        self.cninfo_orgid_map: Any = json.loads((CNINFO_FIXTURES / "szse_stock_sample.json").read_text(encoding="utf-8"))
        self.http = _StubCninfoHttp(self)

    def _flash(self, key: str, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(key + (f":{kwargs['symbol']}" if "symbol" in kwargs else ""))
        outcome = self.flash.get(key, pd.DataFrame())
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stock_info_global_cls(self, symbol: str = "全部") -> pd.DataFrame:
        return self._flash("cls", symbol=symbol)

    def stock_info_global_em(self) -> pd.DataFrame:
        return self._flash("em")

    def stock_info_global_sina(self) -> pd.DataFrame:
        return self._flash("sina")

    def stock_info_global_futu(self) -> pd.DataFrame:
        return self._flash("futu")

    def stock_info_global_ths(self) -> pd.DataFrame:
        return self._flash("ths")

    def stock_info_cjzc_em(self) -> pd.DataFrame:
        self.calls.append("cjzc")
        if isinstance(self.brief, Exception):
            raise self.brief
        return self.brief

    def stock_individual_notice_report(self, security: str, symbol: str = "全部", begin_date: Optional[str] = None,
                                       end_date: Optional[str] = None) -> pd.DataFrame:
        self.calls.append(f"notice_code:{security}")
        self.notice_calls.append({"security": security, "begin": begin_date, "end": end_date})
        outcome = self.notice_by_code.get(security, pd.DataFrame())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def stock_notice_report(self, symbol: str = "全部", date: str = "20220511") -> pd.DataFrame:
        self.calls.append(f"notice_day:{date}")
        self.notice_calls.append({"day": date})
        outcome = self.notice_by_day.get(date, pd.DataFrame())
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _StubResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _StubCninfoHttp:
    """Stands in for ``requests`` inside ``news_cninfo_source``."""

    def __init__(self, stub: "StubAkshare") -> None:
        self._stub = stub

    def get(self, url: str, **_kwargs: Any) -> _StubResponse:
        self._stub.calls.append("cninfo_orgids")
        if isinstance(self._stub.cninfo_orgid_map, Exception):
            raise self._stub.cninfo_orgid_map
        return _StubResponse(self._stub.cninfo_orgid_map)

    def post(self, url: str, data: Dict[str, Any], **_kwargs: Any) -> _StubResponse:
        code = str(data.get("stock", "")).split(",")[0]
        self._stub.calls.append(f"cninfo:{code}")
        self._stub.cninfo_requests.append(dict(data))
        outcome = self._stub.cninfo_by_code.get(code, ConnectionError("cninfo not stubbed"))
        if callable(outcome):
            outcome = outcome(data)
        if isinstance(outcome, Exception):
            raise outcome
        return _StubResponse(outcome)


def cninfo_fixture(name: str = "his_announcement_600089.json") -> Dict[str, Any]:
    """Real 巨潮 query response (trimmed), sampled once on 2026-11-09."""
    return json.loads((CNINFO_FIXTURES / name).read_text(encoding="utf-8"))


def install_stub(monkeypatch: Any, stub: Optional[StubAkshare] = None) -> StubAkshare:
    stub = stub or StubAkshare()
    monkeypatch.setattr(ak_source, "_akshare", lambda: stub)
    monkeypatch.setattr(cninfo_source, "_session", lambda: stub.http)
    ak_source.reset_inflight_for_tests()
    cninfo_source.reset_cache_for_tests()
    return stub


def at(hour: int, minute: int = 0, second: int = 0, day: date = date(2026, 11, 9)) -> datetime:
    return datetime.combine(day, dtime(hour, minute, second))
