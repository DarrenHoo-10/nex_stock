# -*- coding: utf-8 -*-
"""Broker research, 龙虎榜, 两融 / 北向 and 新浪 fund flow: parsing, daily ingest, API, capital-flow backup (no network).

Fixtures are single samples taken on 2026-11-09: Tushare top_list / top_inst (002086, 2026-11-06),
report_rc (600089), margin_detail (600089), moneyflow_hsgt (tests/fixtures/tushare) and 新浪
个股历史资金流 (sh600089, tests/fixtures/sina).
"""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from src.repositories.stock_event_repo import StockEventRepository
from src.services import capital_service
from src.services.capital_service import CapitalService, margin_from
from src.services.sources import SourceError, sina_fund_flow
from src.services.stock_event_service import (
    StockEventService,
    parse_dragon_tiger,
    parse_research,
    research_summary,
)
from src.storage import DatabaseManager

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 9)  # a Monday


def _json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


TOP_LIST = _json("tushare/top_list_002086_20261106.json")
TOP_INST = _json("tushare/top_inst_002086_20261106.json")
REPORT_RC = _json("tushare/report_rc_600089.json")


class FakeTushare:
    def __init__(self, by_api):
        self.by_api = by_api
        self.calls = []

    def _market_query_paged(self, api, *, page_cap, key_cols, fields, **params):
        self.calls.append((api, dict(params)))
        outcome = self.by_api.get(api, [])
        if callable(outcome):
            outcome = outcome(params)
        if isinstance(outcome, Exception):
            raise outcome
        return pd.DataFrame(outcome)


@pytest.fixture
def db(tmp_path):
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'p4.db').as_posix()}")
    capital_service.reset_cache_for_tests()
    yield manager
    DatabaseManager.reset_instance()


# ----------------------------------------------------------------------
# 龙虎榜
# ----------------------------------------------------------------------
def test_dragon_tiger_real_sample_one_event_per_stock_day():
    events = parse_dragon_tiger(TOP_LIST, TOP_INST)
    assert len(events) == 1
    event = events[0]
    payload = event["payload"]
    assert event["code"] == "002086" and event["kind"] == "dragon_tiger" and event["event_date"] == date(2026, 11, 6)
    # The 3-day reason accumulates three sessions; the first single-day reason is primary.
    assert payload["reason"] == "日换手率达到20%的前5只证券" and len(payload["reasons"]) == 3
    assert payload["net_amount"] == pytest.approx(496527.22)
    seats = payload["seats"]
    keys = [(s["name"], s["buy"], s["sell"]) for s in seats]
    assert len(keys) == len(set(keys)) and len(seats) <= 10
    assert payload["connect_net"] == pytest.approx(41196808.11) and "institution_net" not in payload
    assert event["title"] == "龙虎榜（日换手率达到20%的前5只证券）：净买入 50 万元，沪深股通净买入 4120 万元"


def test_dragon_tiger_keeps_separate_institution_seats():
    listed = [{"trade_date": "20261106", "ts_code": "600089.SH", "reason": "日涨幅偏离值达到7%的前5只证券",
               "net_amount": 2.5e8}]
    seats = [
        {"trade_date": "20261106", "ts_code": "600089.SH", "exalter": "机构专用", "side": "0", "buy": 1.2e8,
         "sell": 0.0, "net_buy": 1.2e8, "reason": "日涨幅偏离值达到7%的前5只证券"},
        {"trade_date": "20261106", "ts_code": "600089.SH", "exalter": "机构专用", "side": "0", "buy": 0.5e8,
         "sell": 0.1e8, "net_buy": 0.4e8, "reason": "日涨幅偏离值达到7%的前5只证券"},
        # The same seat again on the sell side list: counted once.
        {"trade_date": "20261106", "ts_code": "600089.SH", "exalter": "机构专用", "side": "1", "buy": 0.5e8,
         "sell": 0.1e8, "net_buy": 0.4e8, "reason": "日涨幅偏离值达到7%的前5只证券"},
    ]
    payload = parse_dragon_tiger(listed, seats)[0]
    assert len(payload["payload"]["seats"]) == 2
    assert payload["payload"]["institution_net"] == pytest.approx(1.6e8)
    assert "机构净买入 1.6 亿元（2 席）" in payload["title"] and "净买入 2.5 亿元" in payload["title"]


def test_daily_run_ingests_dragon_tiger_by_trade_date(db):
    def by_day(rows):
        return lambda params: [r for r in rows if r["trade_date"] == params["trade_date"]]

    fake = FakeTushare({"top_list": by_day(TOP_LIST), "top_inst": by_day(TOP_INST)})
    service = StockEventService(StockEventRepository(db), fake, today=lambda: TODAY)
    summary = service.run_daily(backfill_days=10)
    dragon = summary["kinds"]["dragon_tiger"]
    asked = [p["trade_date"] for api, p in fake.calls if api == "top_list"]
    assert "20261108" not in asked and "20261107" not in asked  # weekend skipped
    assert sum(1 for api, p in fake.calls if api == "top_inst") == 1  # seats only for days with a list
    assert dragon["fetched"] == 1 and dragon["errors"] == []
    stored = service.events_for("002086", kinds=("dragon_tiger",), days=10, fetch=False)["events"]
    assert len(stored) == 1 and stored[0]["payload"]["reason"].startswith("日换手率")

    # Next run: only the trading day that had a list counts as done; empty days and today are read again.
    fake.calls.clear()
    service.run_daily(backfill_days=10)
    asked = [p["trade_date"] for api, p in fake.calls if api == "top_list"]
    assert "20261106" not in asked and "20261109" in asked


# ----------------------------------------------------------------------
# 券商研报
# ----------------------------------------------------------------------
def test_research_real_sample_and_consensus():
    events = parse_research(REPORT_RC)
    assert len(events) == 2 and all(len(e["payload"]["forecasts"]) == 3 for e in events)
    huatai = next(e for e in events if e["payload"]["org"] == "华泰证券")
    assert huatai["title"] == "华泰证券《特变电工：Q3扣非归母净利润同比稳增长》，评级买入，目标价 21.76 元，2026 年 EPS 1.04"
    summary = research_summary(events, TODAY)
    assert summary["institutions"] == 2 and summary["ratings"] == {"买入": 2}
    first = summary["consensus"][0]
    assert first["quarter"] == "2026Q4" and first["eps"]["avg"] == pytest.approx(1.12)
    assert first["net_profit_yi"]["min"] == pytest.approx(51.97)  # 519700 万元
    assert summary["target_price"]["count"] == 1


def test_research_consensus_counts_each_institution_once():
    older = [dict(r, report_date="20260901", report_title="旧报告", eps=0.5) for r in REPORT_RC if r["org_name"] == "华泰证券"]
    events = parse_research(REPORT_RC + older)
    summary = research_summary(events, TODAY)
    assert summary["reports"] == 3 and summary["institutions"] == 2
    assert summary["consensus"][0]["eps"]["min"] == pytest.approx(1.04)  # the older 0.5 is superseded
    assert research_summary(events, TODAY, days=30)["reports"] == 2


def test_daily_run_backfills_research_for_the_consensus_window(db):
    fake = FakeTushare({"report_rc": lambda p: [r for r in REPORT_RC if r["report_date"] == p["report_date"]]})
    service = StockEventService(StockEventRepository(db), fake, today=lambda: TODAY)
    service.run_daily(backfill_days=30)
    asked = [p["report_date"] for api, p in fake.calls if api == "report_rc"]
    assert len(asked) == 91 and asked[0] == "20260811"
    assert len([api for api, _ in fake.calls if api == "stk_holdertrade"]) == 31


# ----------------------------------------------------------------------
# 新浪资金流
# ----------------------------------------------------------------------
def test_sina_fund_flow_real_sample():
    rows = sina_fund_flow.parse(_json("sina/fund_flow_sh600089.json"))
    assert len(rows) == 5 and rows[0].day == date(2026, 11, 6)
    assert rows[0].main_net == pytest.approx(138721670.37) and rows[0].change_pct == pytest.approx(2.22)
    assert sina_fund_flow.parse(None) == []
    with pytest.raises(SourceError):
        sina_fund_flow.parse({"error": "changed"})
    assert sina_fund_flow.symbol("600089") == "sh600089" and sina_fund_flow.symbol("002773") == "sz002773"
    assert sina_fund_flow.symbol("920002") is None and sina_fund_flow.symbol("AAPL") is None


class _Response:
    def __init__(self, text):
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        return None


def test_sina_capital_flow_shape(monkeypatch):
    sent = {}

    class Http:
        def get(self, url, params=None, **kwargs):
            sent.update(params)
            return _Response((FIXTURES / "sina/fund_flow_sh600089.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(sina_fund_flow, "_session", lambda: Http())
    result = sina_fund_flow.capital_flow("600089", timeout_sec=3)
    assert sent["daima"] == "sh600089"
    flow = result["stock_flow"]
    assert flow["main_net_inflow"] == pytest.approx(138721670.37)
    assert flow["inflow_5d"] is not None and flow["inflow_10d"] is None  # only 5 days in the sample
    assert result["source_chain"] == ["sina:moneyflow:20261106"]


def test_sina_status_row_reflects_the_latest_request(monkeypatch):
    from src.services.data_capability_service import DataCapabilityService

    monkeypatch.setattr(sina_fund_flow, "_last", {})
    assert DataCapabilityService._sina_fund_flow_status() == ("unknown", ["runtime_probe_not_performed"])
    with pytest.raises(ConnectionError):
        sina_fund_flow.fetch("600089", timeout_sec=1)  # conftest: offline
    status, warnings = DataCapabilityService._sina_fund_flow_status()
    assert status == "unavailable" and "network disabled" in warnings[0]
    monkeypatch.setattr(sina_fund_flow, "_session", lambda: SimpleNamespace(get=lambda *a, **k: _Response("null")))
    assert sina_fund_flow.fetch("600089", timeout_sec=1) == []
    assert DataCapabilityService._sina_fund_flow_status() == ("ok", [])


def test_sina_is_the_last_capital_flow_backup(monkeypatch):
    from data_provider.base import DataFetcherManager

    calls = []
    monkeypatch.setattr(sina_fund_flow, "capital_flow", lambda code, *, timeout_sec: calls.append(code) or {
        "status": "partial", "stock_flow": {"main_net_inflow": 1.0e8, "inflow_5d": 2.0e8, "inflow_10d": None},
        "source_chain": ["sina:moneyflow:20261106"], "errors": []})
    cfg = SimpleNamespace(enable_fundamental_pipeline=True, fundamental_cache_ttl_seconds=0,
                          fundamental_stage_timeout_seconds=3, fundamental_fetch_timeout_seconds=2,
                          fundamental_retry_max=1)
    # An empty list makes the manager build the default fetchers (real Tushare etc.); one inert fetcher instead.
    manager = DataFetcherManager(fetchers=[SimpleNamespace(name="InertFetcher", priority=99)])
    with patch("src.config.get_config", return_value=cfg), patch(
            "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_capital_flow",
            side_effect=ConnectionError("东财冷却中")):
        ctx = manager.get_capital_flow_context("600089", budget_seconds=2)
    assert calls == ["600089"]
    assert ctx["data"]["stock_flow"]["main_net_inflow"] == 1.0e8
    assert "sina:moneyflow:20261106" in [item["provider"] for item in ctx["source_chain"]]

    calls.clear()
    with patch("src.config.get_config", return_value=cfg), patch(
            "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_capital_flow",
            return_value={"status": "ok", "stock_flow": {"main_net_inflow": 5.0}, "sector_rankings": {},
                          "source_chain": ["akshare"], "errors": []}):
        manager.get_capital_flow_context("600089", budget_seconds=2)
        manager.get_capital_flow_context("920002", budget_seconds=2)
    assert calls == []  # primary source had data; 北交所 is not covered


# ----------------------------------------------------------------------
# 两融 / 北向 / overview
# ----------------------------------------------------------------------
def test_margin_summary_from_real_row_shape():
    real = _json("tushare/margin_detail_600089_20261106.json")[0]
    rows = [dict(real, trade_date=d, rzye=real["rzye"] - i * 1e8) for i, d in
            enumerate(["20261106", "20261105", "20261104", "20261103", "20261102", "20261030"])]
    result = margin_from(rows, days=5)
    summary = result["summary"]
    assert result["status"] == "ok" and len(result["rows"]) == 5 and summary["date"] == "2026-11-06"
    assert summary["financing_balance_yi"] == pytest.approx(45.46)
    assert summary["financing_balance_change_pct"] == pytest.approx((4546051907 / (4546051907 - 4e8) - 1) * 100, abs=0.01)
    assert result["rows"][0]["financing_net_buy"] == pytest.approx(163594064.0 - 189295890.0)
    assert margin_from([], days=5)["status"] == "empty"


def test_capital_overview_sections_fail_independently(db, monkeypatch):
    fake = FakeTushare({
        "margin_detail": _json("tushare/margin_detail_600089_20261106.json"),
        "moneyflow_hsgt": _json("tushare/moneyflow_hsgt_20261106.json"),
        "top_list": TOP_LIST, "top_inst": TOP_INST,
    })
    events = StockEventService(StockEventRepository(db), fake, today=lambda: TODAY)
    events.repo.upsert(parse_dragon_tiger(TOP_LIST, TOP_INST))
    monkeypatch.setattr(sina_fund_flow, "fetch", lambda *a, **k: (_ for _ in ()).throw(SourceError("新浪改版")))
    service = CapitalService(fake, events, today=lambda: TODAY)

    result = service.overview("002086", days=20)
    assert result["margin"]["status"] == "ok" and result["margin"]["rows"][0]["date"] == "2026-11-06"
    assert result["fund_flow"] == {"status": "failed", "error": "新浪改版"}
    assert len(result["dragon_tiger"]) == 1
    north = result["northbound"]
    assert north["rows"][0]["north_money"] == pytest.approx(262593.3) and "成交额" in north["note"]

    service.overview("600089", days=20)
    assert sum(1 for api, _ in fake.calls if api == "moneyflow_hsgt") == 1  # market data cached
    assert not any(api in ("top_list", "top_inst") for api, _ in fake.calls)  # 龙虎榜 read from storage only


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


def _primary():
    """The acting user the open stock routes receive for the primary user (CLI / local mode)."""
    from api.deps import CurrentUser
    from src.config import get_primary_user_id

    uid = get_primary_user_id()
    return CurrentUser(user_id=uid, handle=uid, display_name=uid, role="admin", is_admin=True)

def test_research_and_capital_endpoints(db, monkeypatch):
    from fastapi import HTTPException

    from api.v1.endpoints import stocks
    from src.services import stock_event_service

    fake = FakeTushare({"margin_detail": [], "moneyflow_hsgt": []})
    repo = StockEventRepository(db)
    repo.upsert(parse_research(REPORT_RC))
    repo.upsert(parse_dragon_tiger(TOP_LIST, TOP_INST))
    real_init = StockEventService.__init__
    monkeypatch.setattr(stock_event_service.StockEventService, "__init__",
                        lambda self, repo_=None, fetcher=None, **k: real_init(self, repo, fake, today=lambda: TODAY))
    monkeypatch.setattr(sina_fund_flow, "fetch", lambda *a, **k: [])

    research = stocks.get_stock_research("600089", days=90, user=_primary())
    assert research.summary["institutions"] == 2 and len(research.reports) == 2
    assert research.reports[0].kind_label == "券商研报" and research.reports[0].payload["forecasts"]

    capital = stocks.get_stock_capital("002086", days=20, user=_primary())
    assert capital.margin["status"] == "empty" and capital.fund_flow["status"] == "empty"
    assert capital.dragon_tiger[0].kind == "dragon_tiger" and capital.dragon_tiger[0].kind_label == "龙虎榜"

    events = stocks.get_stock_events("002086", kinds="dragon_tiger", days=30, user=_primary())
    assert [e.kind for e in events.events] == ["dragon_tiger"]
    for call in (lambda: stocks.get_stock_research("sh000001", days=90, user=_primary()),
                 lambda: stocks.get_stock_capital("sh000001", days=20, user=_primary())):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 400


def test_missing_tushare_fields_become_none_not_nan_text():
    """pandas 3 keeps NaN on float / string columns through ``where(..., None)``; rows must carry None."""
    import numpy as np

    from src.services.stock_event_service import _DAY_KINDS, parse_holder_trades, query_rows

    frame_rows = [dict(REPORT_RC[0], rating=np.nan, max_price=np.nan),
                  {"ts_code": "600089.SH", "ann_date": "20261105", "holder_name": np.nan, "holder_type": np.nan,
                   "in_de": "DE", "change_vol": 1.5e7, "change_ratio": np.nan, "avg_price": np.nan}]
    fake = FakeTushare({"report_rc": [frame_rows[0]], "stk_holdertrade": [frame_rows[1]]})
    research_rows = query_rows(fake, _DAY_KINDS["research"], report_date="20261030")
    assert research_rows[0]["rating"] is None and research_rows[0]["max_price"] is None
    event = parse_research(research_rows)[0]
    assert "nan" not in event["title"] and event["payload"].get("rating") is None
    assert research_summary([event], TODAY)["ratings"] == {}
    holder = parse_holder_trades(query_rows(fake, _DAY_KINDS["holder_trade"], ann_date="20261105"))[0]
    assert "nan" not in holder["title"] and holder["title"].startswith("股东减持")
