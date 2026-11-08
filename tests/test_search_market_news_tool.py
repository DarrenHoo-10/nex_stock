# -*- coding: utf-8 -*-
"""search_market_news: theme news from the stored news pool, web topic search as fallback (no network)."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.agent.tools import search_tools
from src.config import Config
from src.storage import DatabaseManager, IntelligenceItem

NOW = datetime(2026, 11, 8, 10, 0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    DatabaseManager.reset_instance()
    Config.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'news.db'}")
    from src.services import news_pool_service

    monkeypatch.setattr(news_pool_service, "china_now", lambda: NOW)
    yield manager
    DatabaseManager.reset_instance()
    Config.reset_instance()


def _add(db, title, *, hours_ago, summary="", boards=None, codes=None):
    with db.get_session() as session:
        session.add(IntelligenceItem(
            source_name="cls", source_type="akshare", title=title, summary=summary,
            url=f"https://example.invalid/{abs(hash(title))}", published_at=NOW - timedelta(hours=hours_ago),
            item_kind="flash", importance=3, board_names=json.dumps(boards or [], ensure_ascii=False),
            stock_codes=json.dumps(codes or []),
        ))
        session.commit()


class _Search:
    def __init__(self, available=True, results=()):
        self.is_available = available
        self.calls = []
        self._results = list(results)

    def search_topic_news(self, topic, max_results=5, focus_keywords=None):
        self.calls.append((topic, max_results, focus_keywords))
        return SimpleNamespace(success=True, error_message=None, results=self._results)


def test_pool_hits_by_title_summary_or_board_and_window(db, monkeypatch):
    search = _Search()
    monkeypatch.setattr(search_tools, "_get_search_service", lambda: search)
    _add(db, "中央气象台发布寒潮预警", hours_ago=2)
    _add(db, "天然气价格上调", hours_ago=5, summary="受寒潮影响，北方供暖需求上升", codes=["600803"])
    _add(db, "燃气公司公告", hours_ago=6, boards=["寒潮概念"])
    _add(db, "寒潮旧闻", hours_ago=24 * 5)  # outside the default 3-day window
    _add(db, "芯片新品发布", hours_ago=1)

    result = search_tools._handle_search_market_news("寒潮")

    titles = [item["title"] for item in result["pool_results"]]
    assert titles == ["中央气象台发布寒潮预警", "天然气价格上调", "燃气公司公告"]
    assert result["pool_results"][1]["stock_codes"] == ["600803"]
    assert result["pool_results"][0]["published_at"] == "2026-11-08 08:00"
    assert search.calls == [] and "web_results" not in result  # enough stored hits, no web search
    assert search_tools._handle_search_market_news("寒潮", days=7)["pool_results"][-1]["title"] == "寒潮旧闻"


def test_few_pool_hits_fall_back_to_web_topic_search(db, monkeypatch):
    web = [SimpleNamespace(title="寒潮来袭 燃气股走强", snippet="...", url="https://example.invalid/w",
                           source="web", published_date="2026-11-07")]
    search = _Search(results=web)
    monkeypatch.setattr(search_tools, "_get_search_service", lambda: search)
    _add(db, "寒潮预警", hours_ago=1)

    result = search_tools._handle_search_market_news("寒潮 供暖", days=30)

    assert result["days"] == 7
    assert [item["title"] for item in result["pool_results"]] == ["寒潮预警"]
    assert result["web_results"][0]["title"] == "寒潮来袭 燃气股走强"
    assert search.calls == [("寒潮 供暖", 8, ["寒潮", "供暖", "最新消息"])]


def test_no_search_engine_and_empty_keyword_are_reported(db, monkeypatch):
    monkeypatch.setattr(search_tools, "_get_search_service", lambda: _Search(available=False))
    result = search_tools._handle_search_market_news("寒潮")
    assert result["pool_results"] == [] and result["web_error"] == "未配置搜索引擎"
    assert "没有找到" in result["note"]
    assert "error" in search_tools._handle_search_market_news("  ")


def test_tool_is_registered_without_stock_scope():
    from src.agent.factory import get_tool_registry
    from src.agent.stock_scope import StockScope
    from src.agent.tool_surface import ToolSurface
    from src.agent.tools.execution import ToolAccessContext

    registry = get_tool_registry()
    tool = registry.resolve("search_market_news")
    assert tool is not None and "stock" not in tool.policy.scope_dimensions
    # A closed (empty) strict scope does not block it: it is not a per-stock tool.
    calls = []
    original = tool.handler
    try:
        object.__setattr__(tool, "handler", lambda keyword, days=3: calls.append((keyword, days)) or {"ok": 1})
        result = ToolSurface(registry).execute_tool(
            "search_market_news", {"keyword": "寒潮"}, ToolAccessContext(stock_scope=StockScope(strict=True)))
    finally:
        object.__setattr__(tool, "handler", original)
    assert result["ok"] and calls == [("寒潮", 3)]
