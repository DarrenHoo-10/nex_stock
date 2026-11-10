# -*- coding: utf-8 -*-
"""Watchlist per user (multi-user P2a): primary -> .env STOCK_LIST, others -> user_watchlist table.

Offline: the real app, routes and SQLite run against a temp directory; nothing touches the network.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from tests.multi_user_helpers import (  # noqa: E402
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    teardown_multi_user_env,
)
from api.deps import get_current_user  # noqa: E402
from src.services.watchlist_service import WatchlistService  # noqa: E402
from src.storage import UserWatchlist  # noqa: E402


@pytest.fixture
def mu(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch, stock_list="600519,AAPL")
    yield app
    teardown_multi_user_env()


def _codes(client) -> list:
    response = client.get("/api/v1/stocks/watchlist")
    assert response.status_code == 200, response.text
    return response.json()["stock_codes"]


def test_primary_watchlist_is_env_stock_list(mu):
    client = mu.as_user(PRIMARY_ID)
    assert _codes(client) == ["600519", "AAPL"]

    added = client.post("/api/v1/stocks/watchlist/add", json={"stock_code": "000001"})
    assert added.status_code == 200
    assert added.json()["stock_codes"] == ["600519", "AAPL", "000001"]
    env_text = open(mu.env_file, encoding="utf-8").read()
    assert "STOCK_LIST=600519,AAPL,000001" in env_text
    with mu.db.get_session() as session:
        assert session.query(UserWatchlist).count() == 0


def test_other_users_watchlists_are_private_and_start_empty(mu):
    assert _codes(mu.as_user(USER_A)) == []

    mu.as_user(USER_A).post("/api/v1/stocks/watchlist/add", json={"stock_code": "300750"})
    mu.as_user(USER_A).post("/api/v1/stocks/watchlist/add", json={"stock_code": "AAPL"})
    mu.as_user(USER_B).post("/api/v1/stocks/watchlist/add", json={"stock_code": "AAPL"})

    assert _codes(mu.as_user(USER_A)) == ["300750", "AAPL"]
    assert _codes(mu.as_user(USER_B)) == ["AAPL"]
    # The primary user's .env list is untouched by other users' writes.
    assert _codes(mu.as_user(PRIMARY_ID)) == ["600519", "AAPL"]
    assert "300750" not in open(mu.env_file, encoding="utf-8").read()


def test_remove_only_affects_own_watchlist(mu):
    mu.as_user(USER_A).post("/api/v1/stocks/watchlist/add", json={"stock_code": "AAPL"})
    mu.as_user(USER_B).post("/api/v1/stocks/watchlist/add", json={"stock_code": "AAPL"})

    removed = mu.as_user(USER_A).post("/api/v1/stocks/watchlist/remove", json={"stock_code": "AAPL"})
    assert removed.status_code == 200
    assert removed.json()["stock_codes"] == []
    assert _codes(mu.as_user(USER_B)) == ["AAPL"]
    # Removing a code the caller does not hold never reaches another user's or the primary list.
    mu.as_user(USER_A).post("/api/v1/stocks/watchlist/remove", json={"stock_code": "600519"})
    assert _codes(mu.as_user(PRIMARY_ID)) == ["600519", "AAPL"]


def test_equivalent_code_variants_deduplicate_per_user(mu):
    client = mu.as_user(USER_A)
    client.post("/api/v1/stocks/watchlist/add", json={"stock_code": "00700"})
    again = client.post("/api/v1/stocks/watchlist/add", json={"stock_code": "HK00700"})
    assert again.json()["stock_codes"] == ["00700"]
    gone = client.post("/api/v1/stocks/watchlist/remove", json={"stock_code": "hk00700"})
    assert gone.json()["stock_codes"] == []


def test_service_defaults_and_ownership(mu):
    service = WatchlistService(db_manager=mu.db)
    service.add(USER_A, "600036")
    assert service.list_codes(USER_A) == ["600036"]
    assert service.list_codes(USER_B) == []
    assert service.contains(USER_A, "600036") and not service.contains(USER_B, "600036")
    # primary resolves through .env, never through the table
    assert service.list_codes(PRIMARY_ID) == ["600519", "AAPL"]


def test_watchlist_routes_depend_on_current_user_and_take_no_user_id():
    from api.app import create_app

    app = create_app()
    checked = 0
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/stocks/watchlist"):
            checked += 1
            dependencies = {dep.call for dep in route.dependant.dependencies}
            assert get_current_user in dependencies, route.path
            assert "user_id" not in {param.name for param in route.dependant.query_params}
    assert checked == 3


def test_scheduled_jobs_read_the_primary_users_watchlist(mu, monkeypatch):
    """preopen push and the news-pool daily fetch are primary-user system jobs in P2."""
    from src.services import preopen_push
    from src.services.news_pool_service import NewsPoolService

    mu.as_user(USER_A).post("/api/v1/stocks/watchlist/add", json={"stock_code": "300750"})

    config = SimpleNamespace(stock_list=["600519", "AAPL"], refresh_stock_list=lambda: None)
    assert preopen_push._load_watchlist(config) == ["600519", "AAPL"]

    service = NewsPoolService.__new__(NewsPoolService)
    service.config = config
    service._holdings_provider = lambda: []
    assert service.wanted_codes() == ["600519"]
