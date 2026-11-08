# -*- coding: utf-8 -*-
"""Next-day plan service: account, plans (model + rule fallback), switch, runs, push, schedule, endpoints.

In-memory SQLite, no network: the model call, the Feishu sender and the board anomaly service are stubbed at their
boundaries; everything between (local data reads, levels, sizing, storage, run orchestration) is real. The
in-memory database is per thread, so the tests run plans with one worker and spawn runs inline.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from api.v1.endpoints import daily_plan as plan_api
from api.v1.schemas.daily_plan import (
    AccountResponse,
    HoldingsSnapshotRequest,
    LatestRunResponse,
    PlanRun,
    Sizing,
    SizingRequest,
    StockPlan,
    StockPlanListResponse,
)
from src.services import daily_plan_service as service_module
from src.services.daily_plan_engine import (
    AccountCtx,
    Bar,
    Candidate,
    HoldingInfo,
    PlanInput,
    build_plan,
    build_switch,
    parse_reply,
)
from src.services.daily_plan_schedule import (
    build_daily_plan_jobs,
    configured_times,
    run_scheduled_daily_plan,
)
from src.services.daily_plan_service import (
    DailyPlanRunConflictError,
    DailyPlanService,
    DailyPlanValidationError,
)
from src.services.daily_plan_sizing import PositionRules
from src.storage import BoardCatalog, BoardMembership, DatabaseManager, MarketStockDaily, StockDaily
from tests.board_fixtures import END_DATE, weekdays

CN = ZoneInfo("Asia/Shanghai")
NEXT_DATE = date(2026, 11, 9)  # END_DATE is a Friday


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    service_module._ACTIVE["run_id"] = None
    yield manager
    service_module._ACTIVE["run_id"] = None
    DatabaseManager.reset_instance()


def make_config(stock_list=(), **overrides):
    values = dict(
        litellm_model="test/plan-model", max_workers=1, stock_list=list(stock_list), daily_plan_enabled=True,
        position_stop_loss_pct=10.0, position_max_total_pct=100.0, position_max_holdings=1,
        position_max_single_pct=None, position_stop_mode="fixed",
        schedule_time="16:00", schedule_times=["16:00"],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class NoBoards:
    """Board anomaly stand-in without any completed board run."""

    repo = SimpleNamespace(latest_completed_run=lambda trade_date=None: None)

    def list_anomalies(self, *args, **kwargs):  # pragma: no cover - never reached without a run
        raise AssertionError("no board run")


def make_service(db, config, llm=None, *, pusher=None, configured=True, anomaly=None, spawn=None,
                 model=True, workers=1) -> DailyPlanService:
    return DailyPlanService(
        db_manager=db, config=config if model else SimpleNamespace(**{**vars(config), "litellm_model": ""}),
        llm_call=llm if model else None, spawn=spawn or (lambda fn: fn()), pusher=pusher or (lambda text: None),
        push_configured=lambda: configured, anomaly_service=anomaly or NoBoards(), calendar_fn=None,
        now_fn=lambda: datetime(2026, 11, 6, 16, 0, tzinfo=CN), workers=workers,
    )


def seed_stock(db, code, closes, *, name="测试股份", is_st=False, suspended=False, exchange_board="main",
               limit_state="none", end=END_DATE, with_ohlc=False, drop_last=False):
    days = weekdays(end, len(closes))
    with db.session_scope() as session:
        for index, (day, close) in enumerate(zip(days, closes)):
            if drop_last and index == len(closes) - 1:
                continue
            prev = closes[index - 1] if index else close
            last = index == len(closes) - 1
            session.add(MarketStockDaily(
                trade_date=day, code=code, name=name, exchange_board=exchange_board, is_st=is_st,
                is_suspended=bool(suspended and last), prev_close=prev, close=close,
                pct_chg=round((close / prev - 1) * 100, 2), amount=5e8, turnover_rate=2.0,
                limit_state=limit_state if last else "none",
            ))
            if with_ohlc:
                session.add(StockDaily(code=code, date=day, open=close, high=close * 1.01, low=close * 0.99,
                                       close=close, pct_chg=0.0, amount=5e8))


def uptrend(end_close=48.35, days=30, start=38.0):
    step = (end_close - start) / (days - 1)
    return [round(start + step * i, 2) for i in range(days)]


def downtrend(days=30, start=60.0, end_close=44.0):
    step = (end_close - start) / (days - 1)
    return [round(start + step * i, 2) for i in range(days)]


def model_reply(verdict="buy", entry=47.0, zone=(46.8, 47.2), invalid=45.9, breakout=49.6, profit=52.0,
                situation="放量回踩平台，量能温和。", held=False):
    horizon = {"verdict": verdict, "reason": "回踩平台支撑", "entry_price": entry, "entry_zone": list(zone),
               "breakout_price": breakout, "invalid_price": invalid, "take_profit_price": profit}
    return json.dumps({"situation": situation, "risks": ["板块情绪降温"], "short": horizon, "swing": horizon},
                      ensure_ascii=False)


class FakeLlm:
    def __init__(self, reply: Any = None, error: Optional[Exception] = None):
        self.reply, self.error, self.calls = reply, error, []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        return self.reply(prompt) if callable(self.reply) else (self.reply or "")


def save_account(service, cash=100_000.0, holdings=()):
    return service.set_holdings_snapshot(cash, list(holdings))


# ---------------------------------------------------------------------------- account
def test_empty_account_has_rules_and_no_holdings(db):
    account = AccountResponse.model_validate(make_service(db, make_config()).get_account())
    assert account.cash == 0 and account.total_capital == 0 and account.holdings == [] and account.updated_at is None
    assert account.position_pct == 0
    assert account.rules.max_single_pct == 100 and account.rules.max_single_pct_derived is True
    assert account.rules.stop_loss_pct == 10 and account.rules.stop_mode == "fixed"


def test_snapshot_replaces_everything_and_enriches_holdings(db):
    seed_stock(db, "600519", [48.0, 49.0, 50.0], name="贵州茅台")
    seed_stock(db, "000988", [10.0, 10.5, 10.4], name="华工科技")
    service = make_service(db, make_config())
    save_account(service, 20_000.0, [
        {"code": "600519", "shares": 300, "cost": 45.0, "buy_date": "2026-11-05", "horizon": "swing"},
        {"code": "000988", "shares": 1000, "cost": 11.0, "buy_date": None, "horizon": None},
    ])
    account = AccountResponse.model_validate(service.get_account())
    assert [h.code for h in account.holdings] == ["600519", "000988"]
    first, second = account.holdings
    assert first.name == "贵州茅台" and first.close == 50.0 and first.market_value == 15_000
    assert first.pnl == 1500 and first.pnl_pct == round((50 / 45 - 1) * 100, 2)
    assert first.horizon == "swing" and first.sellable is True and first.sellable_note is None
    assert second.sellable is True and "没有填买入日期" in second.sellable_note
    assert second.pnl == -600
    assert account.market_value == 15_000 + 10_400
    assert account.total_capital == 20_000 + 25_400
    assert account.position_pct == round(25_400 / 45_400 * 100, 2)
    assert account.updated_at.endswith("Z")

    save_account(service, 5_000.0, [])  # a second PUT replaces the first
    again = service.get_account()
    assert again["holdings"] == [] and again["cash"] == 5_000


def test_t_plus_one_blocks_a_holding_bought_on_the_latest_trade_date(db):
    seed_stock(db, "600519", [48.0, 49.0, 50.0])
    service = make_service(db, make_config())
    save_account(service, 0.0, [{"code": "600519", "shares": 100, "cost": 49.5, "buy_date": "2026-11-06"}])
    holding = service.get_account()["holdings"][0]
    assert holding["sellable"] is False and "T+1" in holding["sellable_note"]
    save_account(service, 0.0, [{"code": "600519", "shares": 100, "cost": 49.5, "buy_date": "2026-11-05"}])
    assert service.get_account()["holdings"][0]["sellable"] is True


def test_holding_without_market_data_is_valued_at_cost(db):
    service = make_service(db, make_config())
    save_account(service, 0.0, [{"code": "603999", "shares": 200, "cost": 10.0}])
    holding = service.get_account()["holdings"][0]
    assert holding["close"] is None and holding["market_value"] == 2000 and holding["pnl"] == 0


@pytest.mark.parametrize(
    "cash,holdings,message",
    [(-1, [], "现金不能为负"),
     (0, [{"code": "600519", "shares": 1, "cost": 1}] * 2, "重复"),
     (0, [{"code": "60051", "shares": 1, "cost": 1}], "6 位"),
     (0, [{"code": "600519", "shares": 0, "cost": 1}], "大于 0"),
     (0, [{"code": "600519", "shares": 1, "cost": 1, "buy_date": "yesterday"}], "YYYY-MM-DD"),
     (0, [{"code": "600519", "shares": 1, "cost": 1, "horizon": "long"}], "short 或 swing"),
     (0, [{"code": f"{600000 + i}", "shares": 1, "cost": 1} for i in range(51)], "最多录入 50")],
)
def test_snapshot_validation(db, cash, holdings, message):
    with pytest.raises(DailyPlanValidationError, match=message):
        make_service(db, make_config()).set_holdings_snapshot(cash, holdings)


# ---------------------------------------------------------------------------- plans
def test_model_plan_gives_the_spec_example_numbers(db):
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    llm = FakeLlm(model_reply())
    service = make_service(db, make_config(["000988"]), llm)
    save_account(service, 100_000.0)
    plan = StockPlan.model_validate(service.get_stock_plan("000988"))

    assert plan.status == "ready" and plan.verdict == "buy" and plan.held is False
    assert plan.trade_date == "2026-11-06" and plan.next_trade_date == "2026-11-09"
    assert plan.limit_pct == 10 and (plan.next_limit_up, plan.next_limit_down) == (53.19, 43.52)
    assert plan.default_horizon == "short" and plan.model.model == "test/plan-model"
    short = plan.horizons.short
    assert short.entry_price == 47.0 and short.entry_zone == (46.8, 47.2) and short.invalid_price == 45.9
    assert short.breakout_price == 49.6 and short.take_profit_price == 52.0
    sizing = short.sizing
    assert sizing.status == "ok" and sizing.shares == 2100 and sizing.amount == 98_700
    assert sizing.capital_pct == 98.7 and sizing.max_loss == 9_870 and sizing.stop.price == 42.3
    assert short.stop.price == 42.3
    buy = short.conditions[0]
    assert buy.action == "buy" and buy.shares == 2100 and buy.price == 47.0 and "回踩 46.80–47.20" in buy.if_text
    assert not buy.outside_limit
    assert [c.action for c in short.conditions][-1] == "skip"  # fall below the invalid price -> give up
    kinds = {level.kind for level in plan.levels}
    assert {"close", "entry", "support", "pressure", "stop", "take_profit", "limit_up", "limit_down"} <= kinds
    assert plan.situation == "放量回踩平台，量能温和。" and "板块情绪降温" in plan.risks
    assert any("跌停" in risk for risk in plan.risks)
    assert plan.horizons.swing.sizing.shares == 2100
    assert len(llm.calls) == 1 and "000988" in llm.calls[0] and "不要输出股数" in llm.calls[0]


def test_the_model_never_decides_shares(db):
    seed_stock(db, "000988", uptrend(48.35))
    reply = json.loads(model_reply())
    reply["short"]["shares"] = 99_999
    reply["short"]["headline"] = "买 99999 股"
    service = make_service(db, make_config(["000988"]), FakeLlm(json.dumps(reply)))
    save_account(service, 100_000.0)
    short = service.get_stock_plan("000988")["horizons"]["short"]
    assert short["sizing"]["shares"] == 2100 and "99999" not in short["headline"]


def test_model_prices_outside_the_limit_range_are_dropped(db):
    seed_stock(db, "000988", uptrend(48.35))
    reply = model_reply(entry=60.0, zone=(59.0, 61.0), invalid=20.0, breakout=80.0, profit=90.0)
    service = make_service(db, make_config(["000988"]), FakeLlm(reply))
    save_account(service, 100_000.0)
    short = service.get_stock_plan("000988")["horizons"]["short"]
    low, high = 43.52, 53.19
    for key in ("entry_price", "breakout_price", "invalid_price", "take_profit_price"):
        assert short[key] is None or low <= short[key] <= high, key
    assert short["entry_price"] != 60.0


def test_model_failure_falls_back_to_a_rule_plan(db):
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    service = make_service(db, make_config(["000988"]), FakeLlm(error=RuntimeError("quota exceeded")))
    save_account(service, 100_000.0)
    plan = StockPlan.model_validate(service.get_stock_plan("000988"))
    assert plan.status == "ready" and plan.model is None
    assert any("按规则生成" in gap and "quota exceeded" in gap for gap in plan.data_gaps)
    assert plan.horizons.short and plan.horizons.swing and plan.levels
    assert plan.verdict in ("buy", "wait", "skip")


def test_unusable_model_reply_also_falls_back(db):
    seed_stock(db, "000988", uptrend(48.35))
    service = make_service(db, make_config(["000988"]), FakeLlm("抱歉，我无法给出 JSON"))
    plan = service.get_stock_plan("000988")
    assert plan["model"] is None and any("按规则生成" in gap for gap in plan["data_gaps"])


def test_no_model_configured_gives_a_rule_plan_without_calling_anything(db):
    seed_stock(db, "000988", uptrend(48.35))
    service = make_service(db, make_config(["000988"]), model=False)
    plan = service.get_stock_plan("000988")
    assert plan["model"] is None and "没有配置分析模型，这份计划按规则生成" in plan["data_gaps"]


def test_parse_reply_tolerates_fences_and_noise():
    assert parse_reply('```json\n{"short": {"verdict": "buy"}}\n```')["short"]["verdict"] == "buy"
    assert parse_reply("没有 JSON") is None and parse_reply("") is None and parse_reply("{bad json}") is None


def test_rule_verdicts_follow_trend_and_distance_to_support(db):
    seed_stock(db, "000001", downtrend(), name="下跌股")
    seed_stock(db, "000002", uptrend(48.35), name="上涨股")
    service = make_service(db, make_config(["000001", "000002"]), model=False)
    save_account(service, 100_000.0)
    down = service.get_stock_plan("000001")
    assert down["verdict"] == "skip" and down["horizons"]["short"]["sizing"] is None
    assert "低于" in down["horizons"]["short"]["reason"] and down["horizons"]["short"]["conditions"][0]["action"] == "wait"
    up = service.get_stock_plan("000002")
    assert up["verdict"] in ("buy", "wait")
    short = up["horizons"]["short"]
    zone = short["entry_zone"]
    assert short["entry_price"] <= zone[1] < up["close"]  # a pullback price, below today's close
    assert short["invalid_price"] < zone[0]
    assert short["sizing"]["shares"] % 100 == 0 and short["sizing"]["shares"] > 0
    for model in (up, down):
        StockPlan.model_validate(model)


def test_suspended_stock_has_no_trade_plan(db):
    seed_stock(db, "600001", uptrend(20.0, start=18.0), name="停牌股", suspended=True)
    service = make_service(db, make_config(["600001"]), FakeLlm(model_reply()))
    plan = StockPlan.model_validate(service.get_stock_plan("600001"))
    assert plan.verdict == "suspended" and plan.status == "ready"
    for horizon in (plan.horizons.short, plan.horizons.swing):
        assert horizon.verdict == "suspended" and horizon.conditions == [] and horizon.sizing is None
    assert any("复牌" in risk for risk in plan.risks)
    assert service._llm_call.calls == []  # no model call for a suspended stock


def test_stock_without_local_data_is_a_failed_plan_not_an_exception(db):
    service = make_service(db, make_config(["603999"]), FakeLlm(model_reply()))
    plan = StockPlan.model_validate(service.get_stock_plan("603999"))
    assert plan.status == "failed" and plan.verdict is None
    assert "没有 2026-11-06 的本地行情" in plan.failure_reason
    assert plan.horizons.short is None and plan.horizons.swing is None


def test_held_stock_closing_below_the_stop_is_cleared(db):
    seed_stock(db, "600519", downtrend(end_close=44.0), name="贵州茅台")
    reply = model_reply(verdict="hold", entry=44.5, zone=(44.2, 44.8), invalid=43.8, breakout=46.0, profit=48.0)
    service = make_service(db, make_config(), FakeLlm(reply))
    save_account(service, 0.0, [{"code": "600519", "shares": 1000, "cost": 50.0, "buy_date": "2026-10-20"}])
    plan = StockPlan.model_validate(service.get_stock_plan("600519"))
    assert plan.held is True and plan.verdict == "clear"  # the model said hold; the rule wins
    short = plan.horizons.short
    assert short.stop.price == 45.0 and short.stop.mode == "fixed"
    row = short.conditions[0]
    assert row.action == "clear" and row.shares == 1000 and "跌破止损价 45.00" in row.if_text
    assert "全部卖出 1,000 股" in row.then_text
    assert any(level.kind == "cost" and level.price == 50.0 for level in plan.levels)
    assert plan.default_horizon == "short"


def test_held_plan_uses_the_holding_horizon_and_sizes_sells_from_shares(db):
    seed_stock(db, "600519", uptrend(48.35), name="贵州茅台")
    reply = model_reply(verdict="hold", entry=47.0, zone=(46.8, 47.2), invalid=45.9, breakout=49.6, profit=52.0)
    service = make_service(db, make_config(), FakeLlm(reply))
    save_account(service, 0.0, [{"code": "600519", "shares": 1001, "cost": 40.0, "buy_date": "2026-10-20",
                                 "horizon": "swing"}])
    plan = StockPlan.model_validate(service.get_stock_plan("600519"))
    assert plan.default_horizon == "swing" and plan.verdict == "hold"
    actions = {c.action: c for c in plan.horizons.swing.conditions}
    assert actions["clear"].shares == 1001
    assert actions["reduce"].shares == 500  # half, odd lots are fine when selling
    assert actions["hold"].price == 49.6
    assert plan.horizons.swing.stop.price == 36.0


def test_t_plus_one_next_day_blocks_sell_conditions(db):
    seed_stock(db, "600519", downtrend(end_close=44.0))
    service = make_service(db, make_config(), model=False)
    save_account(service, 0.0, [{"code": "600519", "shares": 1000, "cost": 50.0, "buy_date": "2026-11-09"}])
    plan = service.get_stock_plan("600519")
    assert plan["verdict"] == "hold" and any("T+1" in risk and "明天还不能卖出" in risk for risk in plan["risks"])


def test_limit_down_stock_flags_prices_outside_the_next_day_range(db):
    seed_stock(db, "300750", uptrend(48.35), name="创业板股", exchange_board="gem")
    reply = model_reply(entry=47.0, zone=(46.8, 47.2))
    service = make_service(db, make_config(["300750"]), FakeLlm(reply))
    save_account(service, 100_000.0)
    plan = service.get_stock_plan("300750")
    assert plan["board_market"] == "chinext" and plan["limit_pct"] == 20
    assert (plan["next_limit_up"], plan["next_limit_down"]) == (58.02, 38.68)
    risks = "\n".join(plan["risks"])
    assert "20%" in risks and "跳空" in risks  # spec 5.5: gaps over the stop on the 20% boards
    from src.services.daily_plan_engine import _outside
    assert _outside(60.0, {"up": 58.02, "down": 38.68}) and not _outside(50.0, {"up": 58.02, "down": 38.68})


def test_max_holdings_turns_a_buy_into_wait_and_adds_a_switch_decision(db):
    seed_stock(db, "600519", uptrend(48.35, start=44.0), name="贵州茅台")
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    llm = FakeLlm(lambda prompt: model_reply())
    service = make_service(db, make_config(["000988"]), llm)
    save_account(service, 0.0, [{"code": "600519", "shares": 2000, "cost": 47.0, "buy_date": "2026-10-20"}])
    run = service.trigger_run()
    assert run["status"] in ("succeeded", "partial")

    flat = StockPlan.model_validate(service.get_stock_plan("000988"))
    assert flat.horizons.short.sizing.status == "max_holdings" and flat.horizons.short.sizing.shares == 0
    assert flat.verdict == "wait" and "最多持股数" in "".join(c.then_text for c in flat.horizons.short.conditions)

    held = StockPlan.model_validate(service.get_stock_plan("600519"))
    assert held.switch is not None and held.switch.decision == "keep"  # default: do not switch
    assert held.switch.candidate.code == "000988" and held.switch.candidate.verdict == "buy"
    assert len(held.switch.conditions) == 2 and held.switch.conditions[0].met is False
    assert any("T+1" in cost for cost in held.switch.costs)
    assert any("止损价" in cost for cost in held.switch.costs)
    assert "不换" in held.switch.summary
    assert held.switch.costs  # T+1, stops, max loss after switching


def test_no_switch_while_holdings_are_not_full(db):
    seed_stock(db, "600519", uptrend(48.35, start=44.0))
    cfg = make_config(position_max_holdings=2)  # one holding of up to two: not full
    service = make_service(db, cfg, FakeLlm(lambda prompt: model_reply(verdict="hold")))
    save_account(service, 50_000.0, [{"code": "600519", "shares": 500, "cost": 47.0, "buy_date": "2026-10-20"}])
    run = service.trigger_run()
    assert service.get_stock_plan("600519")["switch"] is None
    assert {s["key"]: s for s in run["steps"]}["switch"]["status"] == "skipped"
    assert "未达到最多持股数" in {s["key"]: s for s in run["steps"]}["switch"]["detail"]


def _held_payload(verdict: str, close: float = 44.0) -> Dict[str, Any]:
    plan = {"verdict": verdict, "headline": "", "stop": {"price": 45.0, "tech_support": 46.0}}
    return {"code": "600519", "name": "贵州茅台", "default_horizon": "short", "horizons": {"short": plan, "swing": plan},
            "close": close}


def _candidate(close: float, zone=(46.8, 47.2), raw="buy") -> Candidate:
    horizon = {"verdict": "wait", "headline": "回踩买入", "entry_price": 47.0, "entry_zone": list(zone),
               "stop": {"price": 42.3, "tech_support": 46.9}}
    payload = {"code": "000988", "name": "华工科技", "status": "ready", "close": close, "board_market": "main",
               "is_st": False, "situation": "放量上涨", "horizons": {"short": horizon, "swing": horizon}}
    return Candidate("000988", "华工科技", payload, {"short": raw, "swing": raw}, "watchlist")


def _switch(held_verdict: str, candidate: Optional[Candidate]) -> Dict[str, Any]:
    holding = HoldingInfo("600519", 1000, 50.0, name="贵州茅台", close=44.0, market_value=44_000.0)
    account = AccountCtx(cash=56_000.0, market_value=44_000.0, held_count=1)
    return build_switch(_held_payload(held_verdict), holding, 44.0, [candidate] if candidate else [], ["600519"],
                        account, PositionRules(), other_market_value=0.0)


def test_switch_needs_both_conditions_and_defaults_to_keep():
    both = _switch("clear", _candidate(close=47.0))  # held broke down, candidate closed inside its buy zone
    assert both["decision"] == "switch" and [c["met"] for c in both["conditions"]] == [True, True]
    assert any("换入后" in cost and "最大亏损" in cost for cost in both["costs"])

    not_pulled_back = _switch("clear", _candidate(close=49.0))
    assert not_pulled_back["decision"] == "keep" and not_pulled_back["conditions"][1]["met"] is None

    held_ok = _switch("hold", _candidate(close=47.0))
    assert held_ok["decision"] == "keep" and held_ok["conditions"][0]["met"] is False

    broke_zone = _switch("clear", _candidate(close=46.0))
    assert broke_zone["decision"] == "keep" and broke_zone["conditions"][1]["met"] is False

    nobody = _switch("clear", None)
    assert nobody["decision"] == "keep" and nobody["candidate"] is None and "没有比当前持仓更好" in nobody["summary"]

    skip_only = _switch("clear", _candidate(close=47.0, raw="skip"))
    assert skip_only["candidate"] is None


def test_switch_candidates_prefer_buy_over_wait_and_skip_held_codes():
    from src.services.daily_plan_engine import pick_candidate

    wait, buy = _candidate(47.0, raw="wait"), _candidate(47.0, raw="buy")
    buy.code = "000989"
    assert pick_candidate([wait, buy], "short", []).code == "000989"
    assert pick_candidate([wait, buy], "short", ["000989", "000988"]) is None


def test_screening_picks_outside_the_watchlist_can_be_switch_candidates(db):
    seed_stock(db, "600519", uptrend(48.35, start=44.0), name="贵州茅台")
    seed_stock(db, "002475", uptrend(30.0, start=24.0), name="立讯精密")
    db.create_screening_task(task_id="t1", strategy="volume_breakout", market="cn", max_results=5, scope={})
    db.save_screening_run({"run_id": "r1", "strategy": "volume_breakout", "market": "cn", "candidate_count": 1,
                           "candidates": [{"code": "002475", "name": "立讯精密", "rank": 1}]})
    db.update_screening_task("t1", status="completed", progress=100, run_id="r1", candidate_count=1)
    service = make_service(db, make_config(), FakeLlm(lambda prompt: model_reply(verdict="hold")))
    save_account(service, 0.0, [{"code": "600519", "shares": 2000, "cost": 47.0, "buy_date": "2026-10-20"}])
    run = service.trigger_run()
    steps = {s["key"]: s for s in run["steps"]}
    assert steps["picks"]["status"] == "succeeded" and "1 只候选" in steps["picks"]["detail"]
    switch = service.get_stock_plan("600519")["switch"]
    assert switch["candidate"]["code"] == "002475" and switch["decision"] == "keep"


# ---------------------------------------------------------------------------- list
def test_list_orders_held_first_then_buy_wait_skip_suspended_then_unplanned(db):
    seed_stock(db, "600519", uptrend(48.35, start=44.0), name="持仓股")
    seed_stock(db, "000001", downtrend(), name="下跌股")
    seed_stock(db, "000002", uptrend(48.35), name="上涨股")
    seed_stock(db, "000003", uptrend(20.0, start=18.0), name="停牌股", suspended=True)
    seed_stock(db, "000004", uptrend(30.0, start=26.0), name="后加入")
    cfg = make_config(["000003", "000001", "000002"])
    skip = model_reply(verdict="skip", entry=None, zone=(None, None), invalid=None, breakout=None, profit=None)
    service = make_service(db, cfg, FakeLlm(lambda prompt: skip if "下跌股" in prompt else model_reply()))
    save_account(service, 0.0, [{"code": "600519", "shares": 100, "cost": 40.0, "buy_date": "2026-10-20"}])
    service.trigger_run()
    cfg.stock_list.append("000004")  # added after the run: no stored plan yet
    cfg.stock_list.append("603999")  # no market data at all: planned during the run? no, added after it too

    listing = StockPlanListResponse.model_validate(service.list_stock_plans())
    assert listing.trade_date == "2026-11-06" and listing.next_trade_date == "2026-11-09"
    codes = [item.code for item in listing.items]
    assert codes[0] == "600519" and listing.items[0].held is True
    flat = listing.items[1:]
    verdict_rank = {"buy": 0, "wait": 1, "skip": 2, "suspended": 3}
    ranks = [verdict_rank[i.verdict] if i.status == "ready" else 4 for i in flat]
    assert ranks == sorted(ranks)
    by_code = {i.code: i for i in listing.items}
    assert by_code["000003"].verdict == "suspended"
    assert by_code["000004"].status == "missing" and by_code["000004"].verdict is None
    assert by_code["000004"].headline
    assert by_code["000001"].verdict == "skip" and by_code["000001"].name == "下跌股"


def test_on_demand_plan_is_stored_and_reused_without_another_model_call(db):
    seed_stock(db, "000988", uptrend(48.35))
    llm = FakeLlm(model_reply())
    service = make_service(db, make_config(["000988"]), llm)
    save_account(service, 100_000.0)
    first = service.get_stock_plan("000988")
    second = service.get_stock_plan("000988")
    assert first == second and len(llm.calls) == 1
    assert service.list_stock_plans()["items"][0]["status"] == "ready"  # now stored


def test_holding_change_makes_the_stored_plan_stale(db):
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    llm = FakeLlm(model_reply())
    service = make_service(db, make_config(["000988"]), llm)
    save_account(service, 100_000.0)
    assert service.get_stock_plan("000988")["held"] is False
    save_account(service, 1_000.0, [{"code": "000988", "shares": 2100, "cost": 47.0, "buy_date": "2026-11-06",
                                     "horizon": "short"}])
    item = service.list_stock_plans()["items"][0]
    assert item["held"] is True and item["status"] == "missing" and "已过期" in item["headline"]
    replanned = service.get_stock_plan("000988")
    assert replanned["held"] is True and replanned["verdict"] in ("hold", "add", "reduce", "clear")
    assert len(llm.calls) == 2


def test_unknown_code_is_none_and_historic_date_is_respected(db):
    seed_stock(db, "000988", uptrend(48.35))
    service = make_service(db, make_config(["000988"]), model=False)
    assert service.get_stock_plan("123456") is None and service.get_stock_plan("abc") is None
    listing = service.list_stock_plans(date(2026, 11, 5))
    assert listing["trade_date"] == "2026-11-05" and listing["next_trade_date"] == "2026-11-06"
    assert listing["items"][0]["status"] == "missing"


def test_non_a_share_watchlist_entries_are_skipped_and_reported(db):
    seed_stock(db, "000988", uptrend(48.35))
    cfg = make_config(["000988", "AAPL", "hk00700", "sh000300"])
    service = make_service(db, cfg, model=False)
    assert [i["code"] for i in service.list_stock_plans()["items"]] == ["000988"]
    run = service.trigger_run()
    assert any("非 A 股" in gap and "AAPL" in gap for gap in run["data_gaps"])


# ---------------------------------------------------------------------------- sizing preview
def test_sizing_preview_uses_account_rules_and_held_shares(db):
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    service = make_service(db, make_config(["000988"]), model=False)
    save_account(service, 100_000.0)
    sizing = Sizing.model_validate(service.preview_sizing("000988", 47.0, "short"))
    assert sizing.status == "ok" and sizing.shares == 2100 and sizing.stop.tech_support is not None
    star = Sizing.model_validate(service.preview_sizing("688981", 50.0, "swing"))
    assert star.lot == 200 and star.shares == 2000
    save_account(service, 0.0, [{"code": "000988", "shares": 2000, "cost": 47.0}])
    blocked = service.preview_sizing("600519", 100.0, "short")
    assert blocked["status"] == "max_holdings"
    with pytest.raises(DailyPlanValidationError):
        service.preview_sizing("000988", 0, "short")
    with pytest.raises(DailyPlanValidationError):
        service.preview_sizing("000988", 10, "monthly")
    with pytest.raises(DailyPlanValidationError):
        service.preview_sizing("abc", 10, "short")


def test_stop_mode_and_loss_pct_come_from_config(db):
    seed_stock(db, "000988", uptrend(48.35))
    cfg = make_config(["000988"], position_stop_loss_pct=5.0, position_stop_mode="nearer")
    service = make_service(db, cfg, model=False)
    save_account(service, 100_000.0)
    sizing = service.preview_sizing("000988", 47.0, "short")
    assert sizing["stop"]["mode"] == "nearer" and sizing["stop"]["price"] >= 44.65
    assert service.get_account()["rules"]["stop_loss_pct"] == 5.0


# ---------------------------------------------------------------------------- runs
def test_run_persists_steps_counts_failures_push_and_model_calls(db):
    seed_stock(db, "600519", uptrend(48.35, start=44.0), name="持仓股")
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    pushed: List[str] = []
    llm = FakeLlm(lambda prompt: model_reply())
    cfg = make_config(["000988", "603999"])  # 603999 has no data: one failing stock does not stop the rest
    service = make_service(db, cfg, llm, pusher=pushed.append)
    save_account(service, 10_000.0, [{"code": "600519", "shares": 100, "cost": 40.0, "buy_date": "2026-10-20"}])

    run = PlanRun.model_validate(service.trigger_run())
    assert run.id.startswith("plan-20261106-") and run.trade_date == "2026-11-06" and run.trigger == "manual"
    assert run.status == "partial"
    assert (run.stock_total, run.stock_succeeded, run.stock_failed) == (3, 2, 1)
    assert [s.key for s in run.steps] == ["boards", "picks", "plans", "switch", "push"]
    steps = {s.key: s for s in run.steps}
    assert steps["boards"].status == "partial" and "没有完成的板块运行" in steps["boards"].detail
    assert steps["picks"].status == "skipped"
    assert steps["plans"].status == "partial" and "失败 1" in steps["plans"].detail
    assert steps["switch"].status == "skipped" or steps["switch"].status == "succeeded"
    assert steps["push"].status == "succeeded"
    assert run.failures[0].code == "603999" and "本地行情" in run.failures[0].reason
    assert run.push.status == "sent" and run.push.configured is True and run.push.sent_at
    assert run.model_calls[0].step == "plans" and run.model_calls[0].calls == 2
    assert run.finished_at and run.duration_sec is not None
    assert any("板块" in gap for gap in run.data_gaps)

    assert len(pushed) == 1 and pushed[0] == run.push_preview_markdown
    text = pushed[0]
    assert text.index("今日结论（持仓）") < text.index("换股判断") < text.index("自选股") < text.index("数据提醒")
    assert "持仓股（600519）" in text and "华工科技（000988）" in text and "603999" in text

    latest = service.get_latest_run()
    assert latest["id"] == run.id
    assert PlanRun.model_validate(service.get_run(run.id)).status == "partial"
    assert service.get_run("nope") is None


def test_push_failure_is_recorded_and_does_not_fail_the_run(db):
    seed_stock(db, "000988", uptrend(48.35))

    def boom(text: str) -> None:
        raise RuntimeError("webhook 500")

    service = make_service(db, make_config(["000988"]), FakeLlm(model_reply()), pusher=boom)
    run = service.trigger_run()
    assert run["push"]["status"] == "failed" and "webhook 500" in run["push"]["error"]
    assert run["status"] in ("succeeded", "partial") and run["stock_succeeded"] == 1
    assert {s["key"]: s["status"] for s in run["steps"]}["push"] == "failed"
    assert run["push_preview_markdown"]


def test_unconfigured_feishu_skips_the_push_but_keeps_the_preview(db):
    seed_stock(db, "000988", uptrend(48.35))
    pushed: List[str] = []
    service = make_service(db, make_config(["000988"]), FakeLlm(model_reply()), pusher=pushed.append, configured=False)
    run = service.trigger_run()
    assert pushed == [] and run["push"]["status"] == "skipped" and run["push"]["configured"] is False
    assert "次日计划 2026-11-09" in run["push_preview_markdown"]
    assert "没有配置飞书" in {s["key"]: s for s in run["steps"]}["push"]["detail"]


def test_run_with_model_failures_still_stores_rule_plans(db):
    seed_stock(db, "000988", uptrend(48.35))
    service = make_service(db, make_config(["000988"]), FakeLlm(error=TimeoutError("slow")))
    run = service.trigger_run()
    assert run["stock_succeeded"] == 1 and run["stock_failed"] == 0
    assert run["model_calls"][0]["calls"] == 1 and "按规则生成" in run["model_calls"][0]["note"]
    assert "模型没有给出结论" in {s["key"]: s for s in run["steps"]}["plans"]["detail"]
    assert service.get_stock_plan("000988")["status"] == "ready"


def test_worker_pool_plans_stocks_in_parallel_and_keeps_the_order(db):
    codes = ["000011", "000012", "000013", "000014"]
    for code in codes:
        seed_stock(db, code, uptrend(48.35), name=f"股{code}")
    llm = FakeLlm(lambda prompt: model_reply())
    service = make_service(db, make_config(codes), llm, workers=3)  # workers never touch the database
    run = service.trigger_run()
    assert run["stock_succeeded"] == 4 and len(llm.calls) == 4
    assert [i["code"] for i in service.list_stock_plans()["items"]] == codes  # same verdict: watchlist order


def test_run_without_stocks_is_skipped(db):
    run = make_service(db, make_config()).trigger_run()
    assert run["status"] == "skipped" and run["stock_total"] == 0
    assert {s["key"]: s["status"] for s in run["steps"]}["plans"] == "skipped"
    assert run["push"]["status"] == "skipped"


def test_stale_market_data_is_reported_as_a_data_gap(db):
    seed_stock(db, "000988", uptrend(48.35), end=date(2026, 11, 5))  # latest ingested day is Thursday
    service = make_service(db, make_config(["000988"]), model=False)
    run = service.trigger_run()
    assert any("早于应有的交易日 2026-11-06" in gap for gap in run["data_gaps"])
    assert run["trade_date"] == "2026-11-05"


def test_board_signals_reach_the_plan_and_the_push(db):
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    with db.session_scope() as session:
        board = BoardCatalog(source="dc", board_type="concept", code="BK1036.DC", name="光模块",
                             first_seen_date=date(2026, 1, 1), last_seen_date=END_DATE, status="active")
        session.add(board)
        session.flush()
        session.add(BoardMembership(board_id=board.id, stock_code="000988", valid_from=date(2026, 1, 1),
                                    valid_to=None, origin="observed"))
    row = {"id": "concept:BK1036", "name": "光模块", "board_type": "concept", "state": "strengthening",
           "tags": [], "change_pct": 2.5, "excess_pct": 2.0, "reason": "量价齐升"}
    anomaly = SimpleNamespace(
        repo=SimpleNamespace(latest_completed_run=lambda trade_date=None: {"id": 1, "trade_date": END_DATE}),
        list_anomalies=lambda *a, **k: {"boards": [row]},
    )
    llm = FakeLlm(lambda prompt: model_reply())
    service = make_service(db, make_config(["000988"]), llm, anomaly=anomaly)
    run = service.trigger_run()
    steps = {s["key"]: s for s in run["steps"]}
    assert steps["boards"]["status"] == "succeeded" and run["status"] == "succeeded"
    assert "光模块" in llm.calls[0] and "走强" in llm.calls[0]
    assert "光模块（走强）：量价齐升" in run["push_preview_markdown"]
    assert "光模块" in llm.calls[0]


def test_run_conflict_while_one_is_in_progress(db):
    seed_stock(db, "000988", uptrend(48.35))
    spawned: List[Callable[[], None]] = []
    service = make_service(db, make_config(["000988"]), model=False, spawn=spawned.append)
    first = service.trigger_run()
    assert first["status"] == "running" and len(spawned) == 1
    with pytest.raises(DailyPlanRunConflictError):
        service.trigger_run()
    with pytest.raises(HTTPException) as conflict:
        plan_api.trigger_run(service=make_service(db, make_config(["000988"]), model=False))
    assert conflict.value.status_code == 409 and conflict.value.detail["error"] == "daily_plan_run_conflict"
    assert service.get_latest_run()["status"] == "running"
    spawned[0]()  # the background work finishes and releases the slot
    assert service.get_run(first["id"])["status"] in ("succeeded", "partial")
    assert service.trigger_run()["id"] != first["id"]


def test_run_left_running_by_a_dead_process_is_reported_failed(db):
    seed_stock(db, "000988", uptrend(48.35))
    service = make_service(db, make_config(["000988"]), model=False, spawn=lambda fn: None)
    first = service.trigger_run()
    service_module._ACTIVE["run_id"] = None  # the process that owned it is gone
    latest = service.get_latest_run()
    assert latest["id"] == first["id"] and latest["status"] == "failed"
    assert service.trigger_run()["id"] != first["id"]


def test_failed_run_is_recorded_when_the_work_blows_up(db):
    service = make_service(db, make_config(["000988"]), model=False)

    def explode(*args, **kwargs):
        raise RuntimeError("db gone")

    service._build_inputs = explode
    seed_stock(db, "000988", uptrend(48.35))
    run = service.trigger_run()
    assert run["status"] == "failed" and run["finished_at"]
    assert service_module._ACTIVE["run_id"] is None


# ---------------------------------------------------------------------------- endpoints
def test_endpoint_payloads_validate_against_the_schemas(db):
    seed_stock(db, "600519", uptrend(48.35, start=44.0), name="贵州茅台")
    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    service = make_service(db, make_config(["000988"]), FakeLlm(lambda prompt: model_reply()))

    assert plan_api.get_account(service=service).cash == 0
    put = plan_api.put_holdings(HoldingsSnapshotRequest.model_validate({
        "cash": 10_000,
        "holdings": [{"code": "600519", "shares": 100, "cost": 40.0, "buy_date": "2026-10-20", "horizon": "short"}],
    }), service=service)
    assert isinstance(put, AccountResponse) and put.holdings[0].code == "600519" and put.total_capital > 10_000

    empty = plan_api.get_latest_run(service=service)
    assert isinstance(empty, LatestRunResponse) and empty.run is None
    started = plan_api.trigger_run(service=service)
    assert isinstance(started, PlanRun) and started.status in ("succeeded", "partial")
    latest = plan_api.get_latest_run(service=service)
    assert latest.run.id == started.id
    assert plan_api.get_run(started.id, service=service).id == started.id
    with pytest.raises(HTTPException) as missing_run:
        plan_api.get_run("nope", service=service)
    assert missing_run.value.status_code == 404

    listing = plan_api.list_stock_plans(trade_date=None, service=service)
    assert isinstance(listing, StockPlanListResponse) and {i.code for i in listing.items} == {"600519", "000988"}
    assert listing.items[0].held is True
    for item in listing.items:
        plan = plan_api.get_stock_plan(code=item.code, trade_date=None, service=service)
        assert isinstance(plan, StockPlan) and plan.code == item.code
        assert plan.horizons.short is not None and plan.horizons.swing is not None
    with pytest.raises(HTTPException) as missing_plan:
        plan_api.get_stock_plan(code="123456", trade_date=None, service=service)
    assert missing_plan.value.status_code == 404

    sizing = plan_api.preview_sizing(SizingRequest(code="000988", price=47.0, horizon="short"), service=service)
    assert isinstance(sizing, Sizing) and sizing.status in ("ok", "max_holdings")

    with pytest.raises(HTTPException) as invalid:
        plan_api.put_holdings(HoldingsSnapshotRequest.model_validate({
            "cash": 1, "holdings": [{"code": "600519", "shares": 1, "cost": 1}, {"code": "600519", "shares": 2, "cost": 1}],
        }), service=service)
    assert invalid.value.status_code == 400 and invalid.value.detail["error"] == "validation_error"


def test_every_stored_plan_payload_validates_for_each_scenario(db):
    scenarios = {
        "000001": dict(closes=downtrend(), kwargs={}),
        "000002": dict(closes=uptrend(48.35), kwargs={}),
        "000003": dict(closes=uptrend(20.0, start=18.0), kwargs=dict(suspended=True)),
        "300750": dict(closes=uptrend(48.35), kwargs=dict(exchange_board="gem")),
        "688981": dict(closes=uptrend(48.35), kwargs=dict(exchange_board="star")),
        "600600": dict(closes=uptrend(10.0, start=8.0), kwargs=dict(is_st=True, limit_state="up")),
        "830799": dict(closes=uptrend(10.0, start=8.0), kwargs=dict(exchange_board="bse")),
        "000005": dict(closes=[10.0, 10.2, 10.1], kwargs={}),  # three sessions only
    }
    for code, spec in scenarios.items():
        seed_stock(db, code, spec["closes"], **spec["kwargs"])
    service = make_service(db, make_config(list(scenarios)), FakeLlm(error=RuntimeError("down")))
    save_account(service, 100_000.0)
    for code in scenarios:
        plan = StockPlan.model_validate(service.get_stock_plan(code))
        assert plan.code == code
    star = service.get_stock_plan("688981")["horizons"]["short"].get("sizing")
    assert star is None or star["lot"] == 200
    assert service.get_stock_plan("000005")["status"] == "failed"
    st = service.get_stock_plan("600600")
    assert st["limit_pct"] == 5 and st["is_st"] is True
    assert any("涨停" in risk for risk in st["risks"])
    bse = service.get_stock_plan("830799")
    assert bse["board_market"] == "bse" and bse["limit_pct"] == 30


def test_ohlc_bars_improve_the_levels_and_remove_the_gap(db):
    seed_stock(db, "000988", uptrend(48.35), with_ohlc=True)
    seed_stock(db, "000989", uptrend(48.35))
    service = make_service(db, make_config(["000988", "000989"]), model=False)
    with_ohlc, without = service.get_stock_plan("000988"), service.get_stock_plan("000989")
    assert not any("缺少日线最高" in gap for gap in with_ohlc["data_gaps"])
    assert any("缺少日线最高" in gap for gap in without["data_gaps"])


# ---------------------------------------------------------------------------- schedule
def test_schedule_jobs_follow_the_enabled_switch_and_the_times():
    cfg = make_config(daily_plan_enabled=False)
    assert build_daily_plan_jobs(cfg, config_provider=lambda: cfg) == []
    on = make_config(schedule_time="16:30", schedule_times=[])
    jobs = build_daily_plan_jobs(on, config_provider=lambda: on)
    assert [j["schedule_time"] for j in jobs] == ["16:30"] and jobs[0]["name"] == "daily_plan_1630"
    assert jobs[0]["schedule_time_provider"]() == "16:30"
    on.schedule_time = "17:10"
    assert jobs[0]["schedule_time_provider"]() == "17:10"
    many = make_config(schedule_times=["09:00", "16:00"])
    assert configured_times(many) == ["09:00", "16:00"]
    jobs = build_daily_plan_jobs(many, config_provider=lambda: many)
    assert [j["schedule_time"] for j in jobs] == ["09:00", "16:00"]
    many.schedule_times = ["16:00"]
    assert jobs[0]["schedule_time_provider"]() == "16:00"  # slots shift with the sorted list; the extra job skips
    assert run_scheduled_daily_plan(lambda: many, 1, today=END_DATE, is_trading_day=lambda d: True,
                                    service_factory=lambda: None)["reason"] == "time_removed"


def test_scheduled_run_skips_off_days_disabled_and_conflicts_and_never_raises():
    cfg = make_config()
    off = run_scheduled_daily_plan(lambda: cfg, today=date(2026, 11, 7), is_trading_day=lambda d: False)
    assert off == {"status": "skipped", "reason": "not_trading_day", "trade_date": "2026-11-07"}
    disabled = make_config(daily_plan_enabled=False)
    assert run_scheduled_daily_plan(lambda: disabled)["reason"] == "disabled"

    class Busy:
        def trigger_run(self, trigger="manual", wait=False):
            raise DailyPlanRunConflictError("busy")

    busy = run_scheduled_daily_plan(lambda: cfg, today=END_DATE, is_trading_day=lambda d: True, service_factory=Busy)
    assert busy["reason"] == "already_running"

    class Broken:
        def trigger_run(self, trigger="manual", wait=False):
            raise RuntimeError("kaboom")

    failed = run_scheduled_daily_plan(lambda: cfg, today=END_DATE, is_trading_day=lambda d: True, service_factory=Broken)
    assert failed["status"] == "failed" and "kaboom" in failed["error"]


def test_scheduled_run_executes_inline_with_the_schedule_trigger(db):
    seed_stock(db, "000988", uptrend(48.35))
    cfg = make_config(["000988"])
    service = make_service(db, cfg, model=False)
    result = run_scheduled_daily_plan(lambda: cfg, today=END_DATE, is_trading_day=lambda d: True,
                                      service_factory=lambda: service)
    assert result["status"] in ("succeeded", "partial") and result["run_id"].startswith("plan-20261106-")
    assert service.get_latest_run()["trigger"] == "schedule"


def test_plan_builder_never_raises_for_odd_inputs():
    bars = [Bar(date=date(2026, 11, 6), close=10.0)]
    inp = PlanInput(code="000001", name="", board_market="main", is_st=False, suspended=False, bars=bars,
                    rules=PositionRules(), account=AccountCtx(), trade_date=END_DATE, next_trade_date=NEXT_DATE)
    assert build_plan(inp).payload["status"] == "failed"
    inp.bars = []
    assert "没有 2026-11-06 的本地行情" in build_plan(inp).payload["failure_reason"]
    flat = [Bar(date=date(2026, 10, 1 + i), close=10.0) for i in range(6)]
    flat[-1] = Bar(date=END_DATE, close=10.0)
    inp.bars = flat
    payload = build_plan(inp).payload  # zero volatility: the average range falls back to a minimum, no crash
    assert payload["status"] == "ready" and payload["horizons"]["short"]["verdict"] in ("skip", "wait", "buy")


def test_on_demand_plan_is_instant_and_the_model_plan_lands_in_the_background(tmp_path):
    """Production default: opening a stock without a stored plan never waits for the model."""
    import threading

    from src.services.daily_plan_service import REFINING_GAP

    # A file database: the background thread opens its own connection (an in-memory one would be empty).
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'plan.db').as_posix()}")

    seed_stock(db, "000988", uptrend(48.35), name="华工科技")
    release = threading.Event()
    done = threading.Event()

    def slow_llm(prompt, system_prompt):
        release.wait(5)
        try:
            return model_reply()
        finally:
            done.set()

    service = DailyPlanService(
        db_manager=db, config=make_config(["000988"]), llm_call=slow_llm, spawn=lambda fn: fn(),
        pusher=lambda text: None, push_configured=lambda: True, anomaly_service=NoBoards(), calendar_fn=None,
        now_fn=lambda: datetime(2026, 11, 6, 16, 0, tzinfo=CN), workers=1, on_demand_sync=False,
    )
    save_account(service, 100_000.0)

    first = StockPlan.model_validate(service.get_stock_plan("000988"))
    assert first.status == "ready" and first.model is None
    assert first.data_gaps[0] == REFINING_GAP and "没有配置分析模型，这份计划按规则生成" not in first.data_gaps

    release.set()
    assert done.wait(5)
    for _ in range(50):  # the background thread stores the model plan right after the call returns
        later = service.get_stock_plan("000988")
        if later.get("model"):
            break
        threading.Event().wait(0.05)
    assert later["model"]["model"] == "test/plan-model" and REFINING_GAP not in later["data_gaps"]
    DatabaseManager.reset_instance()
