# -*- coding: utf-8 -*-
"""Agent tool ``get_board_signal``: closing judgment, and intraday strength with a mocked snapshot."""

from __future__ import annotations

import sys
from datetime import date
from typing import Dict
from unittest.mock import MagicMock

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.agent.tools.board_tools as board_tools
from data_provider.realtime_types import UnifiedRealtimeQuote
from src.agent.tools.registry import ToolRegistry
from src.services import intraday_snapshot_service as intraday
from src.services.board_pipeline import run_board_pipeline
from src.services.intraday_snapshot_service import IntradayDataUnavailable, MarketSnapshot
from src.storage import BoardMembership, DatabaseManager
from tests.board_fixtures import World, thresholds
from tests.test_board_signals import build


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def world(db):
    w = build(db, 11, {10: {"ret": 3.0, "amt": 2.0}})
    th = thresholds()
    run_board_pipeline(w.days[10], repo=w.repo(), skip_ingest=True, metrics_service=w.metrics(th),
                       signal_service=w.signals(th))
    return w


def quote(code: str, change: float, circ_mv: float = 1e10, amount: float = 1e8) -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(
        code=code, price=10.0 * (1 + change / 100), pre_close=10.0, change_pct=change, amount=amount,
        circ_mv=circ_mv, provider_timestamp="2026-11-09T10:32:00+08:00",
    )


def snapshot_for(world: World, board_change: float, market_change: float = 0.5, stale: bool = False,
                 skip: int = 0) -> MarketSnapshot:
    quotes: Dict[str, UnifiedRealtimeQuote] = {}
    for key, codes in world.members.items():
        change = board_change if key == "I0" else market_change
        for code in codes[skip if key == "I0" else 0:]:
            quotes[code] = quote(code, change)
    return MarketSnapshot(quotes=quotes, source="tushare", as_of="2026-11-09T10:32:00+08:00",
                          fetched_at="2026-11-09T10:32:10+08:00", is_stale=stale)


def patch_intraday(monkeypatch, snapshot=None, error=None, open_now=True, today=date(2026, 11, 9)):
    monkeypatch.setattr(board_tools, "_market_is_open", lambda: open_now)
    monkeypatch.setattr(board_tools, "_china_today", lambda: today)

    class FakeService:
        def get_market_snapshot(self):
            if error is not None:
                raise error
            return snapshot

    monkeypatch.setattr(intraday, "get_intraday_snapshot_service", lambda: FakeService())


# ----------------------------------------------------------------------
# registration / policy
# ----------------------------------------------------------------------
def test_tool_is_registered_read_only_and_not_stock_scoped():
    from src.agent.factory import _build_tool_registry

    registry = _build_tool_registry({})
    tool = registry.get("get_board_signal")
    assert tool is not None and tool.category == "market"
    assert tool.policy.policy_status == "declared" and tool.policy.read_only is True
    assert tool.policy.scope_dimensions == [] and "stock_code" not in [p.name for p in tool.parameters]
    schema = tool.to_openai_tool()
    assert schema["function"]["name"] == "get_board_signal"
    assert schema["function"]["parameters"]["required"] == ["board"]
    assert isinstance(registry, ToolRegistry)
    assert [t.name for t in registry.list_tools() if t.name == "get_board_signal"] == ["get_board_signal"]


def test_registry_policy_has_no_issues_for_the_tool():
    from src.agent.factory import _build_tool_registry

    registry = _build_tool_registry({})
    issues = [i for i in registry.validate_tool_policies(strict=True) if i.get("tool") == "get_board_signal"]
    assert issues == []


# ----------------------------------------------------------------------
# closing judgment
# ----------------------------------------------------------------------
def test_closing_judgment_by_id_name_and_code(world, monkeypatch):
    patch_intraday(monkeypatch, open_now=False)
    by_id = board_tools._handle_get_board_signal(world.board_refs["I0"])
    assert by_id["board"] == {"id": world.board_refs["I0"], "name": "indI0", "board_type": "industry"}
    closing = by_id["closing"]
    assert closing["available"] is True and closing["trade_date"] == world.days[10].isoformat()
    assert closing["state"] == "first_launch" and closing["state_label"] == "首次启动"
    assert closing["change_pct"] == pytest.approx(3.0) and closing["amount_ratio"] == pytest.approx(2.0)
    assert closing["reason"].startswith("放量启动") and closing["evidence"] and closing["can_pick"] is True
    assert closing["driver"].startswith("原因未明") and closing["driver_kind"] == "unknown"
    assert "intraday" not in by_id  # market closed: closing only
    assert board_tools._handle_get_board_signal("indI0")["board"]["id"] == world.board_refs["I0"]
    assert board_tools._handle_get_board_signal("BK1000.DC")["board"]["id"] == world.board_refs["I0"]


def test_unknown_ambiguous_and_empty_queries(world, monkeypatch):
    patch_intraday(monkeypatch, open_now=False)
    assert "未找到板块" in board_tools._handle_get_board_signal("不存在")["error"]
    assert "板块" in board_tools._handle_get_board_signal("")["error"]
    ambiguous = board_tools._handle_get_board_signal("ind")  # substring of every industry name
    assert "请指定 id" in ambiguous["error"] and len(ambiguous["candidates"]) == 6
    assert {"id", "name"} == set(ambiguous["candidates"][0])


def test_no_closing_run_is_reported_not_invented(db, monkeypatch):
    world = build(db, 6, {})
    patch_intraday(monkeypatch, open_now=False)
    result = board_tools._handle_get_board_signal(world.board_refs["I0"])
    assert result["closing"]["available"] is False and "暂无板块收盘判定" in result["closing"]["note"]


def test_tool_executes_through_the_registry(world, monkeypatch):
    from src.agent.factory import _build_tool_registry

    patch_intraday(monkeypatch, open_now=False)
    registry = _build_tool_registry({})
    result = registry.execute("get_board_signal", board=world.board_refs["I0"])
    assert result["closing"]["state"] == "first_launch"


# ----------------------------------------------------------------------
# intraday
# ----------------------------------------------------------------------
def test_intraday_aggregate_over_previous_day_members(world, monkeypatch):
    # a stock that joined the board after the closing date must not count (members of the previous trading day)
    newcomer = "999991"
    with world.db.session_scope() as session:
        session.add(BoardMembership(board_id=world.board_ids["I0"], stock_code=newcomer,
                                    valid_from=date(2026, 11, 9), valid_to=None, origin="observed"))
    snap = snapshot_for(world, board_change=2.0, market_change=0.5)
    snap.quotes[newcomer] = quote(newcomer, 9.0)
    patch_intraday(monkeypatch, snap)
    result = board_tools._handle_get_board_signal(world.board_refs["I0"])
    intraday_part = result["intraday"]
    assert intraday_part["available"] is True
    assert intraday_part["as_of"] == "2026-11-09T10:32:00+08:00" and intraday_part["source"] == "tushare"
    assert intraday_part["members_as_of"] == "2026-11-06" and intraday_part["is_stale"] is False
    assert intraday_part["members_requested"] == 5 and intraday_part["members_matched"] == 5
    assert intraday_part["cap_weighted_change_pct"] == pytest.approx(2.0)
    assert intraday_part["equal_weight_change_pct"] == pytest.approx(2.0)
    assert intraday_part["up_count"] == 5 and intraday_part["down_count"] == 0
    assert intraday_part["up_ratio_pct"] == pytest.approx(100.0)
    assert intraday_part["turnover_yi"] == pytest.approx(5 * 1e8 / 1e8)
    assert intraday_part["all_a_cap_weighted_change_pct"] is not None
    assert intraday_part["excess_vs_all_a_pct"] == pytest.approx(
        2.0 - intraday_part["all_a_cap_weighted_change_pct"], abs=0.01)
    assert "收盘判定和次日计划不会因此改变" in intraday_part["note"]
    assert result["closing"]["state"] == "first_launch"  # the closing verdict is untouched


def test_intraday_uses_previous_day_float_cap_weights(world, monkeypatch):
    codes = world.members["I0"]
    with world.db.session_scope() as session:
        from src.storage import MarketStockDaily

        session.query(MarketStockDaily).filter(
            MarketStockDaily.trade_date == world.days[10], MarketStockDaily.code == codes[0]
        ).update({"float_mv": 9e10})  # one heavyweight, previous close cap 9x the others
    snap = snapshot_for(world, board_change=0.0)
    snap.quotes[codes[0]] = quote(codes[0], 5.0)
    for code in codes[1:]:
        snap.quotes[code] = quote(code, 0.0)
    patch_intraday(monkeypatch, snap)
    part = board_tools._handle_get_board_signal(world.board_refs["I0"])["intraday"]
    assert part["cap_weighted_change_pct"] == pytest.approx(5.0 * 9 / 13, abs=0.01)
    assert part["equal_weight_change_pct"] == pytest.approx(1.0)


def test_intraday_flags_low_coverage_and_stale_snapshot(world, monkeypatch):
    patch_intraday(monkeypatch, snapshot_for(world, board_change=1.0, stale=True, skip=3))
    part = board_tools._handle_get_board_signal(world.board_refs["I0"])["intraday"]
    assert part["available"] is True and part["members_matched"] == 2 and part["members_requested"] == 5
    assert part["is_stale"] is True
    assert any("只覆盖 2/5" in gap for gap in part["data_gaps"]) and any("数据延迟" in gap for gap in part["data_gaps"])


def test_intraday_unavailable_still_answers_with_the_closing_judgment(world, monkeypatch):
    patch_intraday(monkeypatch, error=IntradayDataUnavailable("no source"))
    result = board_tools._handle_get_board_signal(world.board_refs["I0"])
    assert result["intraday"]["available"] is False and "盘中板块数据不可用" in result["intraday"]["reason"]
    assert result["closing"]["state"] == "first_launch"
    patch_intraday(monkeypatch, error=RuntimeError("boom"))
    assert board_tools._handle_get_board_signal(world.board_refs["I0"])["intraday"]["available"] is False


def test_intraday_is_skipped_when_requested_off_or_market_closed(world, monkeypatch):
    patch_intraday(monkeypatch, snapshot_for(world, board_change=2.0))
    assert "intraday" not in board_tools._handle_get_board_signal(world.board_refs["I0"], include_intraday=False)
    patch_intraday(monkeypatch, snapshot_for(world, board_change=2.0), open_now=False)
    assert "intraday" not in board_tools._handle_get_board_signal(world.board_refs["I0"])


def test_intraday_needs_members_from_an_earlier_session(world, monkeypatch):
    # closing judgment is dated today (after the close): there is nothing "previous" to take members from
    patch_intraday(monkeypatch, snapshot_for(world, board_change=2.0), today=world.days[10])
    part = board_tools._handle_get_board_signal(world.board_refs["I0"])["intraday"]
    assert part["available"] is False


def test_intraday_board_without_members_or_quotes(world, monkeypatch):
    with world.db.session_scope() as session:
        session.query(BoardMembership).filter(BoardMembership.board_id == world.board_ids["I0"]).delete()
    patch_intraday(monkeypatch, snapshot_for(world, board_change=2.0))
    part = board_tools._handle_get_board_signal(world.board_refs["I0"])["intraday"]
    assert part["available"] is False and "成分数据" in part["reason"]
    patch_intraday(monkeypatch, MarketSnapshot(quotes={}, source="tushare", as_of="x", fetched_at="x"))
    part = board_tools._handle_get_board_signal(world.board_refs["C0"])["intraday"]
    assert part["available"] is False
