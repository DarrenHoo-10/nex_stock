# -*- coding: utf-8 -*-
"""Multi-user P2b-2: a user-triggered analysis fetches with that user's own data-source credentials (offline).

Credentials only: priorities, adjust mode, timeouts and every other data-semantic setting stay the system's, the
fetched data lands in the shared cache as before, nothing is written to ``os.environ`` or to a global singleton,
and system jobs keep using the system's tokens. The HTTP boundary of the Tushare client is mocked; nothing here
reaches a real data source.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from data_provider.base import DataFetcherManager
from src.config import get_config
from src.core.pipeline import StockAnalysisPipeline
from src.services.user_config_service import DATA_SOURCE_CREDENTIAL_KEYS, get_user_config
from src.services.user_settings_service import UserSettingsService
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    stub_dns,
    teardown_multi_user_env,
)

SYSTEM_TOKENS = {
    "TUSHARE_TOKEN": "sys-tushare-token",
    "FINNHUB_API_KEY": "sys-finnhub-key",
    "ALPHAVANTAGE_API_KEY": "sys-alphavantage-key",
}
USER_TOKENS = {
    "TUSHARE_TOKEN": "user-tushare-token",
    "FINNHUB_API_KEY": "user-finnhub-key",
    "ALPHAVANTAGE_API_KEY": "user-alphavantage-key",
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    stub_dns(monkeypatch)
    for name, value in SYSTEM_TOKENS.items():
        monkeypatch.setenv(name, value)
    from src.config import Config

    Config.reset_instance()
    give_user_model(USER_B)
    give_user_model(USER_A)
    yield app
    teardown_multi_user_env()


def _tushare_token(manager) -> str:
    fetcher = manager.get_fetcher("TushareFetcher")
    assert fetcher is not None
    return fetcher._api._token


def _tushare_priority(manager) -> int:
    return manager.get_fetcher("TushareFetcher").priority


def test_the_system_manager_uses_the_system_tokens(world):
    manager = DataFetcherManager()

    assert _tushare_token(manager) == "sys-tushare-token" and _tushare_priority(manager) == -1
    assert manager.get_fetcher("FinnhubFetcher")._api_key == "sys-finnhub-key"
    assert manager.get_fetcher("AlphaVantageFetcher")._api_key == "sys-alphavantage-key"


def test_a_manager_over_a_user_view_uses_that_users_tokens_only(world):
    UserSettingsService().set_settings(USER_B, USER_TOKENS)
    before = dict(os.environ)

    user_manager = DataFetcherManager(config=get_user_config(USER_B))
    system_manager = DataFetcherManager()

    assert _tushare_token(user_manager) == "user-tushare-token"
    assert user_manager.get_fetcher("FinnhubFetcher")._api_key == "user-finnhub-key"
    assert user_manager.get_fetcher("AlphaVantageFetcher")._api_key == "user-alphavantage-key"
    # the user's credentials leaked neither into the environment nor into a manager built for the system
    assert dict(os.environ) == before
    assert _tushare_token(system_manager) == "sys-tushare-token"
    assert system_manager.get_fetcher("FinnhubFetcher")._api_key == "sys-finnhub-key"
    assert get_config().tushare_token == "sys-tushare-token"


def test_the_users_token_is_what_reaches_the_tushare_http_boundary(world):
    UserSettingsService().set_settings(USER_B, USER_TOKENS)
    posted = []

    def fake_post(url, json=None, timeout=None, **kwargs):  # the HTTP boundary
        posted.append(json)
        return SimpleNamespace(
            status_code=200,
            text='{"code": 0, "data": {"fields": ["ts_code"], "items": [["600519.SH"]]}}',
        )

    with patch("data_provider.tushare_fetcher.requests.post", fake_post):
        _tushare_client(DataFetcherManager(config=get_user_config(USER_B))).daily(ts_code="600519.SH")
        _tushare_client(DataFetcherManager()).daily(ts_code="600519.SH")  # a system job

    assert [call["token"] for call in posted] == ["user-tushare-token", "sys-tushare-token"]
    assert all(call["api_name"] == "daily" for call in posted)


def _tushare_client(manager):
    return manager.get_fetcher("TushareFetcher")._api


def test_a_user_without_own_tokens_falls_back_to_the_systems(world):
    manager = DataFetcherManager(config=get_user_config(USER_B))

    assert _tushare_token(manager) == "sys-tushare-token" and _tushare_priority(manager) == -1
    assert manager.get_fetcher("FinnhubFetcher")._api_key == "sys-finnhub-key"


def test_the_users_token_never_changes_the_source_priority_order(world, monkeypatch):
    """Priorities are the system's: the user's own Tushare token sits where the system's would, never ahead."""
    monkeypatch.delenv("TUSHARE_TOKEN")
    from src.config import Config

    Config.reset_instance()
    UserSettingsService().set_settings(USER_B, {"TUSHARE_TOKEN": "user-tushare-token"})

    system = DataFetcherManager()
    personal = DataFetcherManager(config=get_user_config(USER_B))

    assert system.get_fetcher("TushareFetcher") is None  # the system has no token: not in its chain at all
    assert _tushare_token(personal) == "user-tushare-token"
    assert _tushare_priority(personal) == 2  # default position, not the -1 boost a system token earns
    names = [f.name for f in sorted(personal._get_fetchers_snapshot(), key=lambda f: f.priority)]
    assert names.index("EfinanceFetcher") < names.index("TushareFetcher")


def test_only_credentials_come_from_the_user_view(world):
    UserSettingsService().set_settings(
        USER_B, {"TICKFLOW_API_KEY": "user-tickflow-key", "BIYING_LICENCE": "user-licence", "MX_APIKEY": "user-mx"},
    )
    built = {}

    def factory(name):
        def make(**kwargs):
            built[name] = kwargs
            return SimpleNamespace(name=name, priority=kwargs.get("priority", 5), is_available=lambda: True)

        return make

    with patch("data_provider.tickflow_fetcher.TickFlowFetcher", factory("TickFlowFetcher")), patch(
        "data_provider.biying_fetcher.BiyingFetcher", factory("BiyingFetcher")
    ), patch("data_provider.miaoxiang_fetcher.MiaoxiangFetcher", factory("MiaoxiangFetcher")):
        DataFetcherManager(config=get_user_config(USER_B))

    system = get_config()
    assert built["TickFlowFetcher"]["api_key"] == "user-tickflow-key"
    assert built["BiyingFetcher"]["licence"] == "user-licence"
    assert built["MiaoxiangFetcher"]["api_key"] == "user-mx"
    # data semantics are read from the system config: the same values the system's own manager would use
    assert built["TickFlowFetcher"]["kline_adjust"] == system.tickflow_kline_adjust
    assert built["TickFlowFetcher"]["batch_size"] == system.tickflow_batch_size
    assert built["TickFlowFetcher"]["priority"] == system.tickflow_priority
    assert built["BiyingFetcher"]["timeout"] == system.biying_timeout_seconds


def test_longbridge_stays_on_the_system_config_because_its_sdk_binds_credentials_through_the_environment(world):
    UserSettingsService().set_settings(USER_B, {"LONGBRIDGE_APP_KEY": "user-lb-key", "LONGBRIDGE_APP_SECRET": "s",
                                                 "LONGBRIDGE_ACCESS_TOKEN": "t"})
    seen = []

    def has_credentials(config=None):
        seen.append(config)
        return False

    with patch("data_provider.longbridge_fetcher.LongbridgeFetcher.has_configured_credentials", staticmethod(has_credentials)):
        DataFetcherManager(config=get_user_config(USER_B))

    assert seen and all(config is get_config() for config in seen)


def test_pipeline_gives_a_personal_account_its_own_credentials_and_the_primary_user_the_systems(world):
    UserSettingsService().set_settings(USER_B, USER_TOKENS)

    personal = StockAnalysisPipeline(config=get_user_config(USER_B), user_id=USER_B)
    primary = StockAnalysisPipeline()

    assert _tushare_token(personal.fetcher_manager) == "user-tushare-token"
    assert _tushare_token(primary.fetcher_manager) == "sys-tushare-token"
    assert primary.user_id == PRIMARY_ID
    assert getattr(primary.fetcher_manager, "_credentials_config", None) is None  # unchanged construction


def test_two_users_do_not_share_fetchers_or_tokens(world):
    UserSettingsService().set_settings(USER_A, {"TUSHARE_TOKEN": "token-of-a"})
    UserSettingsService().set_settings(USER_B, {"TUSHARE_TOKEN": "token-of-b"})

    manager_a = DataFetcherManager(config=get_user_config(USER_A))
    manager_b = DataFetcherManager(config=get_user_config(USER_B))

    assert _tushare_token(manager_a) == "token-of-a" and _tushare_token(manager_b) == "token-of-b"
    assert manager_a.get_fetcher("TushareFetcher") is not manager_b.get_fetcher("TushareFetcher")


def test_the_lazily_created_tickflow_fetcher_also_uses_the_users_key(world):
    UserSettingsService().set_settings(USER_B, {"TICKFLOW_API_KEY": "user-tickflow-key"})
    manager = DataFetcherManager(fetchers=[SimpleNamespace(name="X", priority=1)], config=get_user_config(USER_B))
    created = {}

    def make(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(name="TickFlowFetcher", priority=2)

    with patch("data_provider.tickflow_fetcher.TickFlowFetcher", make):
        fetcher = manager._get_tickflow_fetcher()

    assert fetcher is not None and created["api_key"] == "user-tickflow-key"


def test_credential_keys_that_are_really_wired_are_documented_user_keys():
    wired = {"TUSHARE_TOKEN", "BIYING_LICENCE", "TICKFLOW_API_KEY", "MX_APIKEY", "FINNHUB_API_KEY", "ALPHAVANTAGE_API_KEY"}
    assert wired <= DATA_SOURCE_CREDENTIAL_KEYS
    # SOCIAL_SENTIMENT_API_KEY / FXMACRODATA_API_KEY stay system keys (documented decision), never per-user
    assert not ({"SOCIAL_SENTIMENT_API_KEY", "FXMACRODATA_API_KEY"} & DATA_SOURCE_CREDENTIAL_KEYS)
    from src.core.config_registry import SCOPE_SYSTEM, get_config_scope

    assert get_config_scope("SOCIAL_SENTIMENT_API_KEY") == SCOPE_SYSTEM
    assert get_config_scope("FXMACRODATA_API_KEY") == SCOPE_SYSTEM
    assert json.dumps(sorted(wired))  # keep the set printable in failures
