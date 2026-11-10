# -*- coding: utf-8 -*-
"""Multi-user P2a: agent tools act for the session owner, never for the primary user by accident.

Offline: the LLM adapter is a scripted fake, no market data or model is contacted.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agent.agents.base_agent import BaseAgent
from src.agent.llm_adapter import LLMResponse, ToolCall
from src.agent.orchestrator import AgentOrchestrator
from src.agent.protocols import AgentContext
from src.agent.runner import run_agent_loop
from src.agent.tool_surface import ToolSurface
from src.agent.tools.execution import ToolAccessContext, execute_runner_tool_call
from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolRegistry, ToolUserContextRequired
from src.services.portfolio_service import PortfolioService
from tests.multi_user_helpers import PRIMARY_ID, USER_A, USER_B, prepare_multi_user_env, teardown_multi_user_env


@pytest.fixture
def env(tmp_path, monkeypatch):
    prepare_multi_user_env(tmp_path, monkeypatch)
    from src.storage import DatabaseManager

    yield DatabaseManager.get_instance()
    teardown_multi_user_env()


def _spy_registry(seen: list) -> ToolRegistry:
    def personal(user_id: str, label: str = "") -> dict:
        seen.append(user_id)
        return {"user": user_id, "label": label}

    def shared(label: str = "") -> dict:
        seen.append("shared")
        return {"label": label}

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="personal", description="personal", handler=personal, user_scoped=True,
        parameters=[ToolParameter(name="label", type="string", description="x", required=False)],
    ))
    registry.register(ToolDefinition(
        name="shared", description="shared", handler=shared,
        parameters=[ToolParameter(name="label", type="string", description="x", required=False)],
    ))
    return registry


# ------------------------------------------------------------------ registry / runner contract


def test_user_scoped_tool_fails_closed_without_a_user():
    seen: list = []
    registry = _spy_registry(seen)

    with pytest.raises(ToolUserContextRequired):
        registry.execute("personal", label="x")
    assert seen == []
    assert registry.execute("personal", user_id=USER_A, label="x") == {"user": USER_A, "label": "x"}
    assert registry.execute("shared", label="y") == {"label": "y"}


def test_model_supplied_user_id_is_discarded():
    seen: list = []
    registry = _spy_registry(seen)

    registry.execute("personal", user_id=USER_A, **{"label": "x"})
    result = execute_runner_tool_call(
        tool_call=SimpleNamespace(name="personal", arguments={"label": "x", "user_id": PRIMARY_ID}),
        tool_registry=registry, user_id=USER_A,
    )

    assert seen == [USER_A, USER_A]
    assert '"user": "uid-user-a"' in result[1]
    # a non-scoped tool never receives a user id either
    execute_runner_tool_call(
        tool_call=SimpleNamespace(name="shared", arguments={"label": "x", "user_id": PRIMARY_ID}),
        tool_registry=registry, user_id=USER_A,
    )
    assert seen[-1] == "shared"


def test_runner_without_user_id_refuses_personal_tools():
    seen: list = []
    _, result_text, ok, *_ = execute_runner_tool_call(
        tool_call=SimpleNamespace(name="personal", arguments={}), tool_registry=_spy_registry(seen),
    )
    assert ok is False and seen == []
    assert "needs the acting user" in result_text


class _ScriptedAdapter:
    """Asks for one personal tool call, then answers."""

    def __init__(self):
        self.calls = 0

    def call_with_tools(self, messages, tools, timeout=None):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                tool_calls=[ToolCall(id="c1", name="personal", arguments={"label": "go"})], provider="fake", model="fake",
            )
        return LLMResponse(content="done", provider="fake", model="fake")


@pytest.mark.parametrize("acting_user", [USER_A, USER_B, PRIMARY_ID])
def test_run_agent_loop_hands_the_acting_user_to_tools(acting_user):
    seen: list = []
    result = run_agent_loop(
        messages=[{"role": "user", "content": "hi"}], tool_registry=_spy_registry(seen),
        llm_adapter=_ScriptedAdapter(), max_steps=3, user_id=acting_user,
    )
    assert result.success and seen == [acting_user]


def test_tool_surface_and_codex_context_carry_the_user():
    seen: list = []
    surface = ToolSurface(_spy_registry(seen))

    ok = surface.execute_tool("personal", {"label": "x"}, ToolAccessContext(user_id=USER_A))
    missing = surface.execute_tool("personal", {"label": "x"}, ToolAccessContext())

    assert ok["ok"] is True and seen == [USER_A]
    assert missing["ok"] is False and missing["error"]["code"] == "user_context_required"

    from src.agent.codex_tool_process import _context_from_payload, _context_payload

    assert _context_from_payload(_context_payload(ToolAccessContext(user_id=USER_A))).user_id == USER_A


# ------------------------------------------------------------------ get_portfolio_snapshot (real DB)


def _snapshot_accounts(registry_user: str):
    from src.agent.tools.data_tools import get_portfolio_snapshot_tool

    registry = ToolRegistry()
    registry.register(get_portfolio_snapshot_tool)
    result = registry.execute("get_portfolio_snapshot", user_id=registry_user, include_risk=False)
    assert result["status"] == "ok", result
    return {account["account_name"] for account in result["snapshot"]["accounts"]}


def test_portfolio_snapshot_tool_reads_the_session_owners_accounts(env):
    PortfolioService(user_id=PRIMARY_ID).create_account(name="primary-acct", broker=None, market="cn", base_currency="CNY")
    PortfolioService(user_id=USER_A).create_account(name="a-acct", broker=None, market="cn", base_currency="CNY")
    PortfolioService(user_id=USER_B).create_account(name="b-acct", broker=None, market="cn", base_currency="CNY")

    assert _snapshot_accounts(USER_A) == {"a-acct"}
    assert _snapshot_accounts(USER_B) == {"b-acct"}
    assert _snapshot_accounts(PRIMARY_ID) == {"primary-acct"}


def test_portfolio_snapshot_tool_cannot_be_pointed_at_a_foreign_account(env):
    foreign = PortfolioService(user_id=USER_B).create_account(
        name="b-acct", broker=None, market="cn", base_currency="CNY",
    )
    from src.agent.tools.data_tools import get_portfolio_snapshot_tool

    registry = ToolRegistry()
    registry.register(get_portfolio_snapshot_tool)
    result = registry.execute("get_portfolio_snapshot", user_id=USER_A, account_id=foreign["id"], include_risk=False)

    assert result.get("status") != "ok" or result["snapshot"]["accounts"] == []
    assert "b-acct" not in str(result)


def test_every_registered_tool_is_classified():
    """Adding a tool that reads personal data must mark it ``user_scoped`` (and extend this list)."""
    from src.agent.factory import get_tool_registry

    registry = get_tool_registry()
    scoped = {tool.name for tool in registry.list_tools() if tool.user_scoped}
    # personal data (portfolio), tools that search with the acting user's own search keys, and the FXMacroData
    # tools (which authorization they use - the administrator's key or the public baseline - depends on the user)
    fxmacro = {name for name in scoped if name.startswith("fxmacrodata_")}
    assert fxmacro, "FXMacroData tools must be user_scoped"
    assert scoped - fxmacro == {
        "get_portfolio_snapshot", "search_stock_news", "search_comprehensive_intel", "search_market_news",
    }


# ------------------------------------------------------------------ execution context threading


def test_orchestrator_context_and_stage_agents_carry_the_user():
    registry = ToolRegistry()
    orchestrator = AgentOrchestrator(tool_registry=registry, llm_adapter=SimpleNamespace(), user_id=USER_A)
    ctx = orchestrator._build_context("analyse 600519", {"stock_code": "600519"})
    assert ctx.user_id == USER_A
    from src.config import get_primary_user_id

    assert AgentOrchestrator(tool_registry=registry, llm_adapter=SimpleNamespace()).user_id == get_primary_user_id()

    captured = {}

    def fake_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            success=False, error="stop", total_tokens=0, total_steps=0, content="", tool_calls_log=[],
            models_used=[], failure_reason=None, messages=[],
        )

    class _Agent(BaseAgent):
        agent_name = "probe"

        def system_prompt(self, ctx):
            return "sys"

        def build_user_message(self, ctx):
            return "user"

        def post_process(self, ctx, raw_text):
            return None

    agent = _Agent(tool_registry=registry, llm_adapter=SimpleNamespace())
    with patch("src.agent.agents.base_agent.run_agent_loop", fake_loop):
        agent.run(ctx)
    assert captured["user_id"] == USER_A
    assert AgentContext().user_id is None


def test_single_agent_executor_hands_its_user_to_the_loop():
    from src.agent.executor import AgentExecutor

    captured = {}

    def fake_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            success=True, content="ok", tool_calls_log=[], total_steps=1, total_tokens=0, provider="p",
            model="m", error=None, messages=[],
        )

    from src.config import get_primary_user_id

    for acting, expected in ((USER_A, USER_A), (None, get_primary_user_id())):
        executor = AgentExecutor(tool_registry=ToolRegistry(), llm_adapter=SimpleNamespace(), user_id=acting)
        with patch("src.agent.executor.run_agent_loop", fake_loop):
            executor._run_loop([{"role": "user", "content": "x"}], [], parse_dashboard=False)
        assert captured["user_id"] == expected


# ------------------------------------------------------------------ audit guard


_PERSONAL_DATA_MARKERS = (
    "WatchlistService", "UserWatchlist", "STOCK_LIST", r"\.stock_list\b", "refresh_stock_list", "DailyPlan", "daily_plan_repo", "plan_holding",
    "BoardPreferenceRepository", "board_follow", "AlertRepository", "AlertService", "alert_rules",
    "PortfolioService", "PortfolioRepository", "PortfolioRiskService", "AssistantNote", "assistant_note",
)
# file -> markers it is allowed to contain, and why
_AUDITED_ALLOWANCES = {
    "src/agent/tools/data_tools.py": {"PortfolioService", "PortfolioRiskService"},  # get_portfolio_snapshot: user_scoped
    "src/agent/assistant_memory.py": {"AssistantNote", "assistant_note"},  # takes the session owner's user_id
    "src/agent/events.py": {"alert_rules"},  # in-memory EventMonitor rules, not the per-user alert_rules table
}


def test_agent_package_reads_personal_data_only_where_audited():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / "src" / "agent").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        allowed = _AUDITED_ALLOWANCES.get(rel, set())
        for marker in _PERSONAL_DATA_MARKERS:
            if marker in allowed:
                continue
            if re.search(marker if marker.startswith("\\") else rf"(?<![A-Za-z_]){re.escape(marker)}", text):
                offenders.append(f"{rel}: {marker}")
    assert offenders == []
