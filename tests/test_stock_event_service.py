# -*- coding: utf-8 -*-
"""Company events: parsing, daily whole-market ingest, per-stock fetch, risks, API, screening, plan (no network).

Row shapes follow the Tushare samples taken on 2026-11-09 (stk_holdertrade / repurchase / stk_surv for
600089 / 002773) and the documented default fields for the others.
"""

from datetime import date, timedelta
from typing import Any, Dict, List

import pandas as pd
import pytest

from src.repositories.stock_event_repo import StockEventRepository
from src.services import stock_event_service as events_module
from src.services.stock_event_service import StockEventService, risk_notes_from
from src.storage import DatabaseManager

TODAY = date(2026, 11, 9)
_REAL_NEEDS_CODE_FETCH = StockEventService._needs_code_fetch  # conftest disables it for every test

HOLDER_ROW = {"ts_code": "600089.SH", "ann_date": "20261105", "holder_name": "国泰海通证券股份有限公司总部",
              "holder_type": "C", "in_de": "DE", "change_vol": 15000000.0, "change_ratio": 0.6, "avg_price": 21.83}
REPURCHASE_ROW = {"ts_code": "600089.SH", "ann_date": "20261012", "end_date": "20261012", "proc": "完成",
                  "amount": None, "high_limit": 24.44}
SURVEY_ROWS = [
    {"ts_code": "002773.SZ", "name": "康弘药业", "surv_date": "20261105", "rece_org": "华夏基金", "rece_mode": "电话会议"},
    {"ts_code": "002773.SZ", "name": "康弘药业", "surv_date": "20261105", "rece_org": "易方达基金", "rece_mode": "电话会议"},
]
FORECAST_ROW = {"ts_code": "000001.SZ", "ann_date": "20261105", "end_date": "20261231", "type": "预减",
                "p_change_min": -50.0, "p_change_max": -30.0}
EXPRESS_ROW = {"ts_code": "300750.SZ", "ann_date": "20261105", "end_date": "20260930", "revenue": 2.5e11,
               "n_income": 4.1e10, "yoy_net_profit": 22.5}
FLOAT_ROWS = [
    {"ts_code": "600089.SH", "ann_date": "20261101", "float_date": "20261120", "float_share": 3.0e7, "float_ratio": 0.8,
     "holder_name": "A", "share_type": "定增"},
    {"ts_code": "600089.SH", "ann_date": "20261101", "float_date": "20261120", "float_share": 1.5e7, "float_ratio": 0.4,
     "holder_name": "B", "share_type": "定增"},
]
PLEDGE_ROWS = [
    {"ts_code": "600089.SH", "end_date": "20261106", "pledge_count": 12, "pledge_ratio": 55.2},
    {"ts_code": "000001.SZ", "end_date": "20261106", "pledge_count": 1, "pledge_ratio": 2.0},  # below storage floor
]


class FakeTushare:
    """Stands in for TushareFetcher._market_query_paged."""

    def __init__(self, by_api: Dict[str, Any]):
        self.by_api = by_api
        self.calls: List[tuple] = []

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
    manager = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'events.db').as_posix()}")
    yield manager
    DatabaseManager.reset_instance()


def make_service(db, by_api, today=TODAY):
    fake = FakeTushare(by_api)
    return StockEventService(StockEventRepository(db), fake, today=lambda: today), fake


def _on_day(rows, field="ann_date"):
    return lambda params: [r for r in rows if str(r.get(field)) == params.get(field, params.get("trade_date"))]


def _primary():
    """The acting user the open stock routes receive for the primary user (CLI / local mode)."""
    from api.deps import CurrentUser
    from src.config import get_primary_user_id

    uid = get_primary_user_id()
    return CurrentUser(user_id=uid, handle=uid, display_name=uid, role="admin", is_admin=True)



# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
def test_titles_are_short_chinese_summaries():
    assert events_module.parse_holder_trades([HOLDER_ROW])[0]["title"] == (
        "公司股东国泰海通证券股份有限公司总部减持 1500 万股（占总股本 0.6%），均价 21.83 元")
    assert events_module.parse_forecast([FORECAST_ROW])[0]["title"] == "业绩预告（2026 年年报）：预减，净利润同比 -50%~-30%"
    assert events_module.parse_express([EXPRESS_ROW])[0]["title"] == (
        "业绩快报（2026 年三季报）：营收 2500 亿元，净利润 410 亿元，同比 22.5%")
    assert events_module.parse_repurchases([REPURCHASE_ROW])[0]["title"] == "回购（完成），价格上限 24.44 元"
    survey = events_module.parse_surveys(SURVEY_ROWS)
    assert len(survey) == 1 and survey[0]["title"] == "机构调研：2 家机构（电话会议）"
    unlock = events_module.parse_share_floats(FLOAT_ROWS)
    assert len(unlock) == 1 and unlock[0]["payload"]["float_ratio"] == pytest.approx(1.2)
    assert unlock[0]["title"].startswith("2026-11-20 限售解禁 4500 万股，占流通股 1.2%")
    assert [e["code"] for e in events_module.parse_pledges(PLEDGE_ROWS)] == ["600089"]


def test_risk_rules():
    events = (events_module.parse_share_floats(FLOAT_ROWS) + events_module.parse_holder_trades([HOLDER_ROW])
              + events_module.parse_pledges(PLEDGE_ROWS) + events_module.parse_forecast([FORECAST_ROW]))
    events = [dict(e, event_date=e["event_date"].isoformat()) for e in events]
    notes = risk_notes_from(events, TODAY)
    assert "2026-11-20 将解禁占流通股 1.2% 的股份，存在抛压" in notes
    assert any("减持占总股本 0.6%" in n for n in notes)
    assert any("股权质押比例 55.2%" in n for n in notes)
    assert "2026-11-05 业绩预告预减" in notes
    # Outside the windows / below the thresholds: nothing.
    assert risk_notes_from(events, TODAY + timedelta(days=60)) == ["股权质押比例 55.2%（截至 2026-11-06），平仓风险高"]
    small = dict(events[1], payload=dict(events[1]["payload"], change_ratio=0.1))
    assert risk_notes_from([small], TODAY) == []


# ----------------------------------------------------------------------
# daily whole-market ingest
# ----------------------------------------------------------------------
def test_daily_run_backfills_then_only_reads_today_again(db):
    service, fake = make_service(db, {
        "stk_holdertrade": _on_day([HOLDER_ROW]),
        "forecast": _on_day([FORECAST_ROW]),
        "express": _on_day([EXPRESS_ROW]),
        "repurchase": _on_day([REPURCHASE_ROW]),
        "stk_surv": _on_day(SURVEY_ROWS, "surv_date"),
        "share_float": FLOAT_ROWS,
        "pledge_stat": lambda p: PLEDGE_ROWS if p.get("end_date") == "20261106" else [],
    })
    first = service.run_daily(backfill_days=7)
    assert first["status"] == "ok"
    assert first["kinds"]["holder_trade"] == {"days": 8, "fetched": 1, "inserted": 1, "errors": []}
    assert first["kinds"]["pledge"]["end_date"] == "2026-11-06" and first["kinds"]["pledge"]["inserted"] == 1
    calls_first = len(fake.calls)

    fake.calls.clear()
    second = service.run_daily(backfill_days=7)
    day_calls = [c for c in fake.calls if c[0] == "stk_holdertrade"]
    assert day_calls == [("stk_holdertrade", {"ann_date": "20261109"})]  # only today is read again
    assert second["kinds"]["pledge"].get("cached") is True and second["kinds"]["holder_trade"]["inserted"] == 0
    assert len(fake.calls) < calls_first

    stored = service.events_for("600089", fetch=False)
    # 回购 (2026-10-12) is outside the 7-day backfill window
    assert {e["kind"] for e in stored["events"]} == {"holder_trade", "share_float", "pledge"}


def test_failed_kind_is_retried_next_run_and_others_still_land(db):
    state = {"broken": True}

    def holders(params):
        if state["broken"]:
            raise RuntimeError("timeout")
        return [HOLDER_ROW] if params.get("ann_date") == "20261105" else []

    service, _fake = make_service(db, {"stk_holdertrade": holders, "forecast": _on_day([FORECAST_ROW])})
    first = service.run_daily(backfill_days=5)
    assert first["status"] == "partial" and first["kinds"]["holder_trade"]["errors"]
    assert first["kinds"]["forecast"]["inserted"] == 1
    state["broken"] = False
    second = service.run_daily(backfill_days=5)
    assert second["kinds"]["holder_trade"]["inserted"] == 1 and second["kinds"]["holder_trade"]["days"] == 6


def test_permission_error_stops_that_kind_after_one_call(db):
    service, fake = make_service(db, {"stk_surv": RuntimeError("Tushare stk_surv 无权限或积分不足")})
    result = service.run_daily(backfill_days=10)
    assert sum(1 for c in fake.calls if c[0] == "stk_surv") == 1
    assert result["kinds"]["survey"]["errors"]


# ----------------------------------------------------------------------
# one stock on demand
# ----------------------------------------------------------------------
def test_events_for_fetches_the_stock_when_the_daily_run_did_not_cover_it(db, monkeypatch):
    monkeypatch.setattr(StockEventService, "_needs_code_fetch", _REAL_NEEDS_CODE_FETCH)
    service, fake = make_service(db, {
        "stk_holdertrade": [HOLDER_ROW], "share_float": FLOAT_ROWS,
        "pledge_stat": [dict(PLEDGE_ROWS[0], end_date="20261030", pledge_ratio=40.0), PLEDGE_ROWS[0]],
    })
    result = service.events_for("600089")
    assert ("stk_holdertrade", {"ts_code": "600089.SH", "start_date": "20261010", "end_date": "20261109"}) in fake.calls
    assert result["fetch"]["holder_trade"]["inserted"] == 1
    pledges = [e for e in result["events"] if e["kind"] == "pledge"]
    assert [p["event_date"] for p in pledges] == ["2026-11-06"]  # latest snapshot only
    assert any("将解禁" in r for r in result["risks"]) and any("减持" in r for r in result["risks"])

    fake.calls.clear()
    again = service.events_for("600089")  # fetched within 24 h: no second fetch
    assert fake.calls == [] and again["fetch"] == {}


def test_events_for_survives_a_missing_tushare(db, monkeypatch):
    monkeypatch.setattr(StockEventService, "_needs_code_fetch", _REAL_NEEDS_CODE_FETCH)

    class NoToken:
        def _market_query_paged(self, *a, **k):
            raise RuntimeError("Tushare 未配置（缺少 TUSHARE_TOKEN）")

    service = StockEventService(StockEventRepository(db), NoToken(), today=lambda: TODAY)
    result = service.events_for("600089")
    assert result["events"] == [] and result["fetch"]["holder_trade"]["status"] == "failed"


# ----------------------------------------------------------------------
# consumers
# ----------------------------------------------------------------------
def _seed(db):
    service, _ = make_service(db, {"stk_holdertrade": _on_day([HOLDER_ROW]), "share_float": FLOAT_ROWS,
                                   "pledge_stat": lambda p: PLEDGE_ROWS if p.get("end_date") == "20261106" else []})
    service.run_daily(backfill_days=7)


def test_api_returns_events_risks_and_rejects_index_codes(db, monkeypatch):
    from fastapi import HTTPException

    from api.v1.endpoints.stocks import get_stock_events

    _seed(db)
    monkeypatch.setattr(events_module.StockEventService, "__init__",
                        lambda self, repo=None, fetcher=None, today=None: setattr(self, "_repo", StockEventRepository(db))
                        or setattr(self, "_fetcher", None) or setattr(self, "_today", lambda: TODAY))
    response = get_stock_events("600089", kinds=None, days=30, user=_primary())
    assert response.stock_code == "600089" and {e.kind for e in response.events} == {"holder_trade", "share_float",
                                                                                      "pledge"}
    assert {e.kind_label for e in response.events} >= {"股东增减持", "限售解禁"}
    assert len(response.risks) == 3
    only = get_stock_events("600089", kinds="share_float", days=30, user=_primary())
    assert [e.kind for e in only.events] == ["share_float"]
    for bad, error in (("sh000001", "invalid_stock_code"), ("600089", "invalid_kind")):
        with pytest.raises(HTTPException) as caught:
            get_stock_events(bad, kinds="nope" if error == "invalid_kind" else None, days=30, user=_primary())
        assert caught.value.status_code == 400 and caught.value.detail["error"] == error


def test_screening_puts_structured_events_first_and_flags_risks(db, monkeypatch):
    from src.services import screening_service

    _seed(db)
    original_init = events_module.StockEventService.__init__

    def init(self, repo=None, fetcher=None, today=None):
        original_init(self, StockEventRepository(db), fetcher, today=lambda: TODAY)

    monkeypatch.setattr(events_module.StockEventService, "__init__", init)
    monkeypatch.setattr(screening_service, "_get_dsa_search_service", lambda: type("S", (), {"is_available": False})())

    events = screening_service.search_dsa_stock_events("600089", "特变电工", max_results=3)
    assert events["provider"].startswith("stock_event") and events["success"] is True
    assert events["results"][0]["title"].startswith("限售解禁：")
    assert events["results"][0]["source"] == "Tushare 公司事件"

    candidates = screening_service._attach_event_risks([{"code": "600089", "risk_flags": ["high_turnover"]},
                                                         {"code": "300750"}])
    assert candidates[0]["risk_flags"][0] == "high_turnover" and len(candidates[0]["event_risks"]) == 3
    assert "event_risks" not in candidates[1]


def test_plan_lists_event_risks_and_tells_the_model():
    from src.services.daily_plan_engine import AccountCtx, Bar, PlanInput, build_plan
    from src.services.daily_plan_sizing import PositionRules

    bars = [Bar(date=date(2026, 10, 1) + timedelta(days=i), close=10.0 + i * 0.1, amount=1e8) for i in range(30)]
    inp = PlanInput(code="600089", name="特变电工", board_market="main", is_st=False, suspended=False, bars=bars,
                    rules=PositionRules(), account=AccountCtx(), trade_date=bars[-1].date,
                    next_trade_date=bars[-1].date + timedelta(days=1),
                    event_risks=["2026-11-20 将解禁占流通股 1.2% 的股份，存在抛压"])
    prompts = []
    payload = build_plan(inp, lambda prompt, system: prompts.append(prompt) or "{}").payload
    assert "2026-11-20 将解禁占流通股 1.2% 的股份，存在抛压" in payload["risks"]
    assert "## 公司事件" in prompts[0] and "将解禁占流通股 1.2%" in prompts[0]


def test_news_daily_job_runs_events_only_with_tushare(db):
    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_pool_service import NewsPoolService
    from tests.news_fixtures import make_config

    calls = []
    skipped = NewsPoolService(IntelligenceRepository(db), make_config(stock_list=[]), is_trading_day=lambda d: True)
    assert skipped._fetch_events() == {"status": "skipped", "reason": "tushare_not_configured"}
    ran = NewsPoolService(IntelligenceRepository(db), make_config(stock_list=[]), is_trading_day=lambda d: True,
                          events_runner=lambda: calls.append(1) or {"status": "ok"})
    assert ran._fetch_events() == {"status": "ok"} and calls == [1]
    broken = NewsPoolService(IntelligenceRepository(db), make_config(stock_list=[]), is_trading_day=lambda d: True,
                             events_runner=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert broken._fetch_events()["status"] == "failed"
