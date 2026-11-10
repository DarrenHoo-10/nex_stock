# -*- coding: utf-8 -*-
"""Merge audit (docs/design/multi-user.md section 23): isolation gaps in code that arrived from main.

* 2. open stock-detail routes bound what they spend: "fetched, nothing found" is remembered, capital sections are cached,
     one fetcher / limiter serves every request, a non-primary user is rate limited and pays with their own token;
* 3. ``ASHARE_API_KEY`` is a system paid key: blank in user views, the ashareapi branch is the primary user's only;
* 4. a user's webhook cannot trip the shared East Money gate (bypass inside the user scope, host refused as a target);
* 5. per-code pool rows (announcements and investor Q&A) are not shown to users who do not follow the code;
* 6. the compression trigger ratio scopes like its siblings, a failed cninfo orgId load backs off.

Everything external is replaced at the boundary (Tushare query layer, HTTP adapter, source modules); no network.
"""

from __future__ import annotations

import datetime as dt
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from fastapi import HTTPException

from api.deps import CurrentUser
from src.config import Config, get_config
from src.repositories.intelligence_repo import IntelligenceRepository
from src.repositories.stock_event_repo import StockEventRepository
from src.services import capital_service, news_pool_service, on_demand_fetch
from src.services.capital_service import CapitalService
from src.services.news_akshare_source import KIND_ANNOUNCEMENT, KIND_FLASH, KIND_QA, NewsRecord
from src.services.sources import sina_fund_flow
from src.services.stock_event_service import StockEventService
from src.services.user_config_service import get_user_config, invalidate_user_config
from src.services.watchlist_service import WatchlistService
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    stub_dns,
    teardown_multi_user_env,
)
from tests.test_stock_event_service import HOLDER_ROW, FakeTushare

TODAY = date(2026, 11, 9)
_REAL_NEEDS_CODE_FETCH = StockEventService._needs_code_fetch  # conftest disables it for every test


class Available(FakeTushare):
    """``FakeTushare`` as the object ``tushare_fetcher_for`` hands out."""

    def is_available(self):
        return True


def acting(user_id: str) -> CurrentUser:
    return CurrentUser(user_id=user_id, handle=user_id, display_name=user_id, role="user", is_admin=user_id == PRIMARY_ID)


@pytest.fixture
def mu(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    stub_dns(monkeypatch)
    on_demand_fetch.reset_for_tests()
    capital_service.reset_cache_for_tests()
    yield app
    on_demand_fetch.reset_for_tests()
    teardown_multi_user_env()


def make_events(mu, fake, *, user_id=None):
    return StockEventService(StockEventRepository(mu.db), fake, today=lambda: TODAY, user_id=user_id)


# ====================================================================================== 2. on-demand external fetches


def test_a_stock_without_events_is_not_refetched_on_every_request(mu, monkeypatch):
    monkeypatch.setattr(StockEventService, "_needs_code_fetch", _REAL_NEEDS_CODE_FETCH)
    fake = Available({})  # every Tushare interface answers with no rows
    first = make_events(mu, fake).events_for("600089")
    assert first["events"] == [] and first["fetch"]["holder_trade"]["status"] == "ok"
    made = len(fake.calls)
    assert made >= 8  # one call per event kind: this is what must not repeat

    again = make_events(mu, fake).events_for("600089")  # a new request = a new service instance
    assert again["fetch"] == {} and len(fake.calls) == made
    make_events(mu, fake).events_for("600089", kinds=("research",))
    assert len(fake.calls) == made
    make_events(mu, fake).events_for("600090")  # a different stock is fetched once
    assert len(fake.calls) == 2 * made
    # the marker is bookkeeping, not an ingest kind of the data-center diagnostics
    assert all(not row["kind"].startswith("cf:") for row in StockEventRepository(mu.db).ingest_status())


def test_a_failed_per_stock_fetch_is_retried_after_a_short_pause_not_per_request(mu, monkeypatch):
    import src.storage as storage

    monkeypatch.setattr(StockEventService, "_needs_code_fetch", _REAL_NEEDS_CODE_FETCH)
    fake = Available({name: RuntimeError("Tushare 无权限或积分不足")
                      for name in ("forecast", "express", "stk_holdertrade", "repurchase", "stk_surv", "report_rc",
                                   "share_float", "pledge_stat")})
    first = make_events(mu, fake).events_for("600089")
    assert first["fetch"]["holder_trade"]["status"] == "failed"
    made = len(fake.calls)
    make_events(mu, fake).events_for("600089")
    assert len(fake.calls) == made  # not asked again at once
    real_now = storage.utc_naive_now
    monkeypatch.setattr(storage, "utc_naive_now", lambda: real_now() + dt.timedelta(minutes=11))
    make_events(mu, fake).events_for("600089")
    assert len(fake.calls) == 2 * made  # ... but not given up on either


def test_a_stock_with_stored_events_keeps_the_24_hour_rule(mu, monkeypatch):
    monkeypatch.setattr(StockEventService, "_needs_code_fetch", _REAL_NEEDS_CODE_FETCH)
    fake = Available({"stk_holdertrade": [HOLDER_ROW]})
    first = make_events(mu, fake).events_for("600089")
    assert first["fetch"]["holder_trade"]["inserted"] == 1
    made = len(fake.calls)
    again = make_events(mu, fake).events_for("600089")
    assert again["fetch"] == {} and len(again["events"]) == 1 and len(fake.calls) == made


def test_capital_sections_are_cached_and_failures_remembered_briefly(mu, monkeypatch):
    fake = Available({"margin_detail": [], "moneyflow_hsgt": []})
    sina = []
    monkeypatch.setattr(sina_fund_flow, "fetch", lambda *a, **k: sina.append(a) or [])
    clock = [1000.0]
    events = make_events(mu, fake)
    service = CapitalService(fake, events, today=lambda: TODAY, clock=lambda: clock[0])

    first = service.overview("002086")
    second = CapitalService(fake, events, today=lambda: TODAY, clock=lambda: clock[0]).overview("002086")
    assert first["margin"]["status"] == "empty" and second["margin"]["status"] == "empty"  # empty answers are cached too
    assert sum(1 for api, _ in fake.calls if api == "margin_detail") == 1 and len(sina) == 1
    service.overview("002086", days=30)  # another window is another question
    assert sum(1 for api, _ in fake.calls if api == "margin_detail") == 2
    clock[0] += capital_service.SECTION_CACHE_SECONDS + 1
    service.overview("002086")
    assert sum(1 for api, _ in fake.calls if api == "margin_detail") == 3 and len(sina) == 2

    broken = Available({"margin_detail": RuntimeError("quota")})
    sick = CapitalService(broken, make_events(mu, broken), today=lambda: TODAY, clock=lambda: clock[0])
    assert sick.overview("600089")["margin"]["status"] == "failed"
    assert sick.overview("600089")["margin"]["status"] == "failed"
    assert sum(1 for api, _ in broken.calls if api == "margin_detail") == 1
    clock[0] += capital_service.SECTION_FAILURE_SECONDS + 1
    sick.overview("600089")
    assert sum(1 for api, _ in broken.calls if api == "margin_detail") == 2


def test_the_per_user_limit_is_per_user_and_does_not_apply_to_the_primary_user(mu, monkeypatch):
    monkeypatch.setattr(get_config(), "user_ondemand_fetch_max_per_hour", 2)
    assert [on_demand_fetch.allow_on_demand_fetch(USER_A) for _ in range(3)] == [True, True, False]
    assert on_demand_fetch.allow_on_demand_fetch(USER_B) is True  # A's use does not count against B
    assert all(on_demand_fetch.allow_on_demand_fetch(PRIMARY_ID) for _ in range(20))
    assert all(on_demand_fetch.allow_on_demand_fetch(None) for _ in range(20))  # CLI / system jobs


def test_events_beyond_the_user_limit_answer_from_storage_without_touching_the_source(mu, monkeypatch):
    monkeypatch.setattr(StockEventService, "_needs_code_fetch", _REAL_NEEDS_CODE_FETCH)
    monkeypatch.setattr(get_config(), "user_ondemand_fetch_max_per_hour", 2)
    fake = Available({"stk_holdertrade": [HOLDER_ROW]})
    make_events(mu, fake).events_for("600089")  # a system fetch stores the stock's events
    fake.calls.clear()

    for code in ("600001", "600002"):
        make_events(mu, fake, user_id=USER_B).events_for(code)
    assert fake.calls
    fake.calls.clear()
    limited = make_events(mu, fake, user_id=USER_B).events_for("600003")
    assert limited["fetch"]["rate_limited"]["status"] == "rate_limited" and fake.calls == []
    stored = make_events(mu, fake, user_id=USER_B).events_for("600089", fetch=False)
    assert [e["kind"] for e in stored["events"]] == sorted(["holder_trade"])  # stored data is still served
    # the limit does not leak to others: the primary user and another user still fetch
    make_events(mu, fake, user_id=PRIMARY_ID).events_for("600004")
    make_events(mu, fake, user_id=USER_A).events_for("600005")
    assert fake.calls
    # a rate-limited request is not remembered as "fetched": it fetches once the window allows it
    on_demand_fetch.reset_for_tests()
    fake.calls.clear()
    make_events(mu, fake, user_id=USER_B).events_for("600003")
    assert fake.calls


def test_capital_sections_report_rate_limited_and_do_not_cache_that(mu, monkeypatch):
    monkeypatch.setattr(get_config(), "user_ondemand_fetch_max_per_hour", 1)
    monkeypatch.setattr(sina_fund_flow, "fetch", lambda *a, **k: [])
    fake = Available({"margin_detail": [], "moneyflow_hsgt": []})
    service = CapitalService(fake, make_events(mu, fake, user_id=USER_B), today=lambda: TODAY, user_id=USER_B)
    result = service.overview("002086")
    statuses = {result["margin"]["status"], result["fund_flow"]["status"], result["northbound"]["status"]}
    assert "rate_limited" in statuses and fake.calls == [("margin_detail", fake.calls[0][1])]  # exactly one permit's worth
    on_demand_fetch.reset_for_tests()
    again = CapitalService(fake, make_events(mu, fake, user_id=USER_B), today=lambda: TODAY, user_id=USER_B).overview("002086")
    assert again["margin"]["status"] == "empty"  # the rate-limited answer was not cached as the answer


def test_one_fetcher_serves_every_request_and_a_users_own_token_pays_for_their_fetches(mu, monkeypatch):
    from src.services.user_settings_service import UserSettingsService

    built = []

    class CountingFetcher:
        def __init__(self, *args, token=None, boost_priority=None, **kwargs):
            built.append((token, boost_priority))
            self.token = token

        def is_available(self):
            return True

    monkeypatch.setattr("data_provider.tushare_fetcher.TushareFetcher", CountingFetcher)
    monkeypatch.setenv("TUSHARE_TOKEN", "sys-token")
    Config.reset_instance()
    invalidate_user_config()
    on_demand_fetch.reset_for_tests()

    shared = on_demand_fetch.shared_tushare_fetcher()
    assert on_demand_fetch.shared_tushare_fetcher() is shared and built == [(None, None)]  # one limiter, built once
    for user in (None, PRIMARY_ID, USER_A):
        assert on_demand_fetch.tushare_fetcher_for(user) is shared  # no own token: the system token, same limiter
    for user in (None, PRIMARY_ID, USER_A):
        assert StockEventService(user_id=user).fetcher is shared  # the services use it, not a new fetcher per request
    assert CapitalService(user_id=USER_A).fetcher is shared
    assert built == [(None, None)]

    UserSettingsService().set_settings(USER_B, {"TUSHARE_TOKEN": "tok-b"})
    invalidate_user_config(USER_B)
    own = on_demand_fetch.tushare_fetcher_for(USER_B)
    assert own is not shared and own.token == "tok-b" and built[-1] == ("tok-b", True)
    assert on_demand_fetch.tushare_fetcher_for(USER_B) is own and StockEventService(user_id=USER_B).fetcher is own
    assert on_demand_fetch.tushare_fetcher_for(USER_A) is shared  # B's token is B's only

    UserSettingsService().set_settings(USER_B, {"TUSHARE_TOKEN": "sys-token"})  # same as the system token: shared
    invalidate_user_config(USER_B)
    assert on_demand_fetch.tushare_fetcher_for(USER_B) is shared

    monkeypatch.setenv("TUSHARE_TOKEN", "sys-token-2")  # a reconfigured system token rebuilds the shared fetcher
    Config.reset_instance()
    assert on_demand_fetch.shared_tushare_fetcher() is not shared


def _seed_qa(code: str, *, followed_by=()):
    now = dt.datetime.now().replace(microsecond=0)
    IntelligenceRepository().upsert_news_records([NewsRecord(
        title=f"{code}：问", summary="问：x\n答：y", url=f"no-url:intel:irm:{code}", published_at=now - dt.timedelta(hours=1),
        source_key="irm", kind=KIND_QA, scope_type="symbol", scope_value=code, stock_codes=[code],
        raw_payload={"question": "q?", "answer": "a!", "platform": "irm", "asked_at": None},
    )], fetched_at=now, dedup_window_minutes=0)
    for user in followed_by:
        WatchlistService().add(user, code)


class _PoolSpy:
    """Stands in for ``NewsPoolService``: records Q&A refreshes, stores nothing."""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    def fetch_qa_for_codes(self, codes, *, now, since):
        type(self).calls.append(list(codes))
        return {"status": "ok", "fetched": 0, "inserted": 0}


def test_qa_route_remembers_an_empty_answer_and_rate_limits_other_users(mu, monkeypatch):
    from api.v1.endpoints.stocks import get_stock_qa

    _PoolSpy.calls = []
    monkeypatch.setattr(news_pool_service, "NewsPoolService", _PoolSpy)
    monkeypatch.setattr(get_config(), "user_ondemand_fetch_max_per_hour", 1)

    for _ in range(3):  # the primary user: a stock without Q&A is asked about once, not on every request
        assert get_stock_qa("002773", days=30, user=acting(PRIMARY_ID)).items == []
    assert _PoolSpy.calls == [["002773"]]

    assert get_stock_qa("002773", days=30, user=acting(USER_B)).fetch["status"] == "ok"  # B's one permit
    assert get_stock_qa("002773", days=30, user=acting(USER_B)).fetch == {}  # remembered for B as well
    limited = get_stock_qa("600519", days=30, user=acting(USER_B))
    assert limited.fetch["status"] == "rate_limited" and _PoolSpy.calls == [["002773"], ["002773"]]
    assert get_stock_qa("600519", days=30, user=acting(PRIMARY_ID)).fetch["status"] == "ok"  # the primary user is not limited


def test_qa_route_does_not_show_stored_answers_that_exist_only_because_another_user_follows_the_code(mu, monkeypatch):
    from api.v1.endpoints.stocks import get_stock_qa

    _PoolSpy.calls = []
    monkeypatch.setattr(news_pool_service, "NewsPoolService", _PoolSpy)
    _seed_qa("002773", followed_by=[USER_A])

    assert len(get_stock_qa("002773", days=30, user=acting(USER_A)).items) == 1  # the follower: served, no fetch
    assert get_stock_qa("002773", days=30, user=acting(PRIMARY_ID)).items
    assert _PoolSpy.calls == []
    monkeypatch.setattr(get_config(), "user_ondemand_fetch_max_per_hour", 1)
    on_demand_fetch.allow_on_demand_fetch(USER_B)  # B has no permit left
    blind = get_stock_qa("002773", days=30, user=acting(USER_B))
    assert blind.items == [] and blind.fetch["status"] == "rate_limited"  # stored rows would tell B that A follows it
    on_demand_fetch.reset_for_tests()
    own = get_stock_qa("002773", days=30, user=acting(USER_B))  # after B's own refresh B sees what a fetch returns
    assert own.fetch["status"] == "ok" and len(own.items) == 1 and _PoolSpy.calls == [["002773"]]


def test_the_stock_routes_take_the_acting_user_and_name_only_non_primary_users(mu, monkeypatch):
    from api.v1.endpoints import stocks

    seen = {}

    class SpyEvents:
        def __init__(self, **kwargs):
            seen["events"] = kwargs

        def events_for(self, *args, **kwargs):
            return {"as_of": "2026-11-09", "events": [], "risks": [], "fetch": {}}

    class SpyCapital:
        def __init__(self, **kwargs):
            seen["capital"] = kwargs

        def overview(self, code, days=20):
            return {"stock_code": code, "as_of": "2026-11-09", "margin": {}, "fund_flow": {}, "dragon_tiger": [],
                    "dragon_tiger_status": {}, "northbound": {}}

    monkeypatch.setattr("src.services.stock_event_service.StockEventService", SpyEvents)
    monkeypatch.setattr("src.services.capital_service.CapitalService", SpyCapital)
    stocks.get_stock_events("600089", kinds=None, days=30, user=acting(USER_B))
    stocks.get_stock_capital("600089", days=20, user=acting(USER_B))
    assert seen == {"events": {"user_id": USER_B}, "capital": {"user_id": USER_B}}
    seen.clear()
    stocks.get_stock_events("600089", kinds=None, days=30, user=acting(PRIMARY_ID))
    stocks.get_stock_capital("600089", days=20, user=acting(PRIMARY_ID))
    assert seen == {"events": {}, "capital": {}}  # the primary user's calls are exactly what they were


# ====================================================================================== 3. ASHARE_API_KEY


def _auction_service(tushare):
    from tests.test_intraday_snapshot_service import _service

    return _service(tushare=tushare)


def _tushare_with_auction():
    from tests.test_intraday_snapshot_service import _tushare

    fetcher = _tushare(None)
    fetcher._api.stk_auction.return_value = pd.DataFrame([
        {"ts_code": "600000.SH", "trade_date": "20261105", "price": 10, "vol": 300},
    ])
    return fetcher


def test_ashare_api_key_is_a_system_paid_key_blank_in_every_non_primary_view(mu, monkeypatch):
    from src.core.config_registry import SCOPE_SYSTEM, get_config_scope
    from src.services.user_config_service import _BLANKED_SYSTEM_KEYS

    monkeypatch.setenv("ASHARE_API_KEY", "ashare-SYSTEM")
    Config.reset_instance()
    invalidate_user_config()
    assert get_config().ashare_api_key == "ashare-SYSTEM"
    assert get_user_config(PRIMARY_ID).ashare_api_key == "ashare-SYSTEM"
    assert not get_user_config(USER_B).ashare_api_key
    assert "ASHARE_API_KEY" in _BLANKED_SYSTEM_KEYS and get_config_scope("ASHARE_API_KEY") == SCOPE_SYSTEM


def test_the_ashareapi_branch_serves_the_primary_user_only_and_the_cache_does_not_mix_the_two(mu, monkeypatch):
    monkeypatch.setenv("ASHARE_API_KEY", "ashare-SYSTEM")
    Config.reset_instance()
    service, _manager, _clock = _auction_service(_tushare_with_auction())
    with patch("data_provider.auction_quotes.requests.get") as http:
        http.return_value.json.return_value = {"ok": True, "data": [{"time": "09:25:00", "price": 10}]}
        free = service.get_opening_auction("600000", "2026-11-05", user_id=USER_B)
        assert free["source"] == "tushare" and http.call_count == 0  # B: free sources, the system key is never sent
        paid = service.get_opening_auction("600000", "2026-11-05")  # primary: the richer source, not B's cached answer
        assert paid["source"] == "ashareapi" and http.call_count == 1
        assert http.call_args.kwargs["params"]["code"] == "600000"
        assert service.get_opening_auction("600000", "2026-11-05", user_id=USER_B)["source"] == "tushare"
        assert service.get_opening_auction("600000", "2026-11-05", user_id=PRIMARY_ID)["source"] == "ashareapi"
        assert http.call_count == 1  # both answers cached apart


def test_opening_auction_and_intraday_cost_a_permit_only_when_they_reach_a_source(mu, monkeypatch):
    from api.v1.endpoints import stocks
    from src.services.on_demand_fetch import OnDemandFetchLimited

    monkeypatch.setattr(get_config(), "user_ondemand_fetch_max_per_hour", 1)
    service, _manager, _clock = _auction_service(_tushare_with_auction())
    assert service.get_opening_auction("600000", "2026-11-05", user_id=USER_B)["points"]
    assert service.get_opening_auction("600000", "2026-11-05", user_id=USER_B)["points"]  # cached: no permit
    with pytest.raises(OnDemandFetchLimited):
        service.get_opening_auction("600000", "2026-11-04", user_id=USER_B)
    assert service.get_opening_auction("600000", "2026-11-04")  # the primary user is not limited

    monkeypatch.setattr(stocks, "get_intraday_snapshot_service", lambda: service)
    with pytest.raises(HTTPException) as caught:
        stocks.get_opening_auction("600000", "2026-11-03", user=acting(USER_B))
    assert caught.value.status_code == 429 and caught.value.detail["error"] == "rate_limited"
    with pytest.raises(HTTPException) as caught:
        stocks.get_stock_intraday("600000", "2026-11-03", user=acting(USER_B))
    assert caught.value.status_code == 429

    spy = MagicMock()
    spy.get_opening_auction.return_value = {"points": [], "source": None}
    monkeypatch.setattr(stocks, "get_intraday_snapshot_service", lambda: spy)
    stocks.get_opening_auction("600000", "2026-11-05", user=acting(PRIMARY_ID))
    assert spy.get_opening_auction.call_args.kwargs == {}
    stocks.get_opening_auction("600000", "2026-11-05", user=acting(USER_A))
    assert spy.get_opening_auction.call_args.kwargs == {"user_id": USER_A}


# ====================================================================================== 4. East Money gate


def _install_gate(monkeypatch, answers):
    from data_provider import eastmoney_gate

    seen = []

    def fake_request(self, method, url, **kwargs):
        seen.append(url)
        return SimpleNamespace(status_code=answers.get(url.split("/")[2], 200))

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_gate, "_underlying_request", None)
    eastmoney_gate.install()
    return eastmoney_gate, seen


def test_a_user_scope_request_to_an_east_money_host_cannot_ban_it_for_everyone(monkeypatch):
    from src.utils.safe_requests import user_outbound_requests

    gate, seen = _install_gate(monkeypatch, {"push2.eastmoney.com": 403})
    session = requests.Session()
    with user_outbound_requests():
        assert session.request("GET", "https://push2.eastmoney.com/user-webhook").status_code == 403
        assert session.request("GET", "https://push2.eastmoney.com/user-webhook").status_code == 403  # no cooldown
    assert gate.GATE.cooling_down("push2.eastmoney.com") == 0
    assert all(row["requests"] == 0 and not row["cooling_down"] for row in gate.GATE.status())  # took no turn either
    # outside a user scope the gate is exactly as before: one 403 cools the host down for the process
    assert session.request("GET", "https://push2.eastmoney.com/system").status_code == 403
    with pytest.raises(gate.EastmoneyCoolingDown):
        session.request("GET", "https://push2.eastmoney.com/system")
    assert seen == sorted(["https://push2.eastmoney.com/user-webhook"]) * 2 + ["https://push2.eastmoney.com/system"]


@pytest.mark.parametrize("url", [
    "https://push2.eastmoney.com/hook", "https://eastmoney.com/hook", "https://PUSH2.EastMoney.com./hook",
    "https://datacenter-web.eastmoney.com:443/a?b=c",
])
def test_east_money_hosts_are_not_acceptable_user_targets(url):
    from src.utils import url_safety

    with pytest.raises(url_safety.UnsafeUrlError) as caught:
        url_safety.check_public_https_url(url, resolver=lambda host, port: ["93.184.216.34"])
    assert caught.value.reason == "shared_data_source_host"
    with pytest.raises(url_safety.UnsafeUrlError):
        url_safety.check_public_https_url(url, resolve=False)
    with pytest.raises(url_safety.UnsafeUrlError):
        url_safety.resolve_public_https_url(url, resolver=lambda host, port: ["93.184.216.34"])


@pytest.mark.parametrize("url", [
    "https://noteastmoney.com/hook", "https://eastmoney.com.example.org/hook", "https://example.org/eastmoney.com",
])
def test_lookalike_hosts_are_still_acceptable(url):
    from src.utils import url_safety

    assert url_safety.check_public_https_url(url, resolver=lambda host, port: ["93.184.216.34"]) == url


def test_personal_channels_cannot_target_east_money_on_save_or_update_or_send(mu, monkeypatch):
    from tests.p3a_helpers import CUSTOM, FakeHTTP, create_channel

    http = FakeHTTP().install(monkeypatch)
    client = mu.as_user(USER_A)
    for channel_type in ("custom", "feishu", "dingtalk", "wecom"):
        refused = create_channel(client, channel_type, {"webhook_url": "https://push2.eastmoney.com/robot/send?token=x"})
        assert refused.status_code == 400 and refused.json()["error"] == "unsafe_target", channel_type
        assert "eastmoney" not in refused.text  # the rejected value is not echoed

    created = create_channel(client, "custom", dict(CUSTOM))
    assert created.status_code in (200, 201), created.text
    channel_id = created.json()["id"]
    moved = client.put(f"/api/v1/me/notification-channels/{channel_id}",
                       json={"config": {"webhook_url": "https://datacenter-web.eastmoney.com/hook"}})
    assert moved.status_code == 400 and moved.json()["error"] == "unsafe_target"
    assert http.calls_to("push2.eastmoney.com") == [] and http.calls_to("datacenter-web.eastmoney.com") == []

    # a target that was valid when saved cannot be turned into an East Money request later either (call-time re-check)
    from src.utils import safe_requests

    with safe_requests.user_outbound_requests():
        with pytest.raises(safe_requests.BlockedRequestError):
            requests.Session().get("https://push2.eastmoney.com/x")
    assert http.calls_to("push2.eastmoney.com") == []


# ====================================================================================== 5. per-code pool rows


def _seed_pool():
    published = dt.datetime.now().replace(microsecond=0) - dt.timedelta(hours=1)

    def ann(code, name):
        return NewsRecord(title=f"{name}公告 年报", summary="", url=f"no-url:ann:{code}", published_at=published,
                          source_key="cninfo", kind=KIND_ANNOUNCEMENT, scope_type="symbol", scope_value=code,
                          stock_codes=[code])

    def qa(code, name):
        return NewsRecord(title=f"{name}：投资者问答 年报", summary="问：x\n答：y", url=f"no-url:intel:irm:{code}",
                          published_at=published, source_key="irm", kind=KIND_QA, scope_type="symbol", scope_value=code,
                          stock_codes=[code], raw_payload={"question": "q", "answer": "a", "platform": "irm"})

    flash = NewsRecord(title="年报季快讯", summary="", url="https://news.example.com/f", published_at=published,
                       source_key="cls", kind=KIND_FLASH, stock_codes=["600519"])
    IntelligenceRepository().upsert_news_records(
        [ann("600519", "贵州茅台"), qa("600519", "贵州茅台"), ann("000001", "平安银行"), qa("000001", "平安银行"), flash],
        fetched_at=published, dedup_window_minutes=0,
    )
    WatchlistService().add(USER_A, "600519")
    WatchlistService().add(USER_B, "000001")
    return published


def _titles(result):
    return sorted(item["title"] for item in result["items"])


def test_per_code_kinds_are_exactly_the_kinds_the_pool_fetches_per_wanted_code():
    from src.services.news_akshare_source import KIND_ARTICLE, KIND_MORNING_BRIEF, PER_CODE_KINDS

    assert PER_CODE_KINDS == {KIND_ANNOUNCEMENT, KIND_QA}
    assert not PER_CODE_KINDS & {KIND_FLASH, KIND_MORNING_BRIEF, KIND_ARTICLE}  # whole-market feeds


def test_intelligence_items_hide_other_users_per_code_rows_and_keep_totals_consistent(mu):
    from src.services.intelligence_service import IntelligenceService

    _seed_pool()
    service = IntelligenceService()
    everything = service.list_items(scope_type="symbol")
    assert everything["total"] == 4  # primary / no viewer: the whole pool, unchanged
    assert len(service.list_items(scope_type="symbol", viewer_user_id=PRIMARY_ID)["items"]) == 4

    a = service.list_items(scope_type="symbol", viewer_user_id=USER_A)
    assert _titles(a) == sorted(["贵州茅台：投资者问答 年报", "贵州茅台公告 年报"]) and a["total"] == 2
    b = service.list_items(scope_type="symbol", viewer_user_id=USER_B)
    assert _titles(b) == sorted(["平安银行：投资者问答 年报", "平安银行公告 年报"]) and b["total"] == 2
    page = service.list_items(scope_type="symbol", viewer_user_id=USER_B, page=1, page_size=1)
    assert len(page["items"]) == 1 and page["total"] == 2  # filtered in SQL: pagination stays correct
    assert service.list_items(scope_type="symbol", scope_value="600519", viewer_user_id=USER_B)["total"] == 0
    nobody = service.list_items(viewer_user_id="uid-follows-nothing")  # no scope filter: the flash is general news
    assert _titles(nobody) == sorted(["年报季快讯"])


def test_intelligence_items_route_and_stock_profile_apply_the_filter_to_the_acting_user(mu):
    from src.services.stock_profile_service import StockProfileService

    _seed_pool()
    route = "/api/v1/intelligence/items?scope_type=symbol&scope_value=600519"
    assert mu.as_user(USER_B).get(route).json()["total"] == 0
    assert mu.as_user(USER_A).get(route).json()["total"] == 2
    assert mu.as_user(PRIMARY_ID).get(route).json()["total"] == 2

    def profile_titles(user):
        block = StockProfileService(user_id=user)._intelligence_block("600519", market="cn")
        return sorted(item["title"] for item in block.get("items") or [])

    assert profile_titles(USER_B) == []
    assert profile_titles(USER_A) == sorted(["贵州茅台：投资者问答 年报", "贵州茅台公告 年报"])
    assert profile_titles(PRIMARY_ID) == sorted(["贵州茅台：投资者问答 年报", "贵州茅台公告 年报"])


def test_the_digest_and_news_search_hide_other_users_qa_rows_as_well(mu):
    from src.agent.tools.search_tools import _pool_news_hits
    from src.services.news_digest_service import build_news_digest

    published = _seed_pool()
    since, until = published - dt.timedelta(hours=1), published + dt.timedelta(hours=3)
    digest = lambda viewer: sorted(i["title"] for i in build_news_digest(since, until, viewer_user_id=viewer))  # noqa: E731
    assert digest(USER_A) == sorted(["年报季快讯", "贵州茅台：投资者问答 年报", "贵州茅台公告 年报"])
    assert digest(USER_B) == sorted(["年报季快讯", "平安银行：投资者问答 年报", "平安银行公告 年报"])
    assert len(digest(None)) == 5 and len(digest(PRIMARY_ID)) == 5
    hits = lambda user: sorted(h["title"] for h in _pool_news_hits(["年报"], 3, user))  # noqa: E731
    assert hits(USER_B) == sorted(["年报季快讯", "平安银行：投资者问答 年报", "平安银行公告 年报"]) and len(hits(None)) == 5


# ====================================================================================== 6. minor


def test_the_compression_trigger_ratio_scopes_like_its_siblings():
    from src.core.config_registry import SCOPE_USER, get_config_scope

    for sibling in ("AGENT_CONTEXT_COMPRESSION_ENABLED", "AGENT_CONTEXT_COMPRESSION_PROFILE",
                    "AGENT_CONTEXT_COMPRESSION_TRIGGER_TOKENS", "AGENT_CONTEXT_COMPRESSION_TRIGGER_RATIO"):
        assert get_config_scope(sibling) == SCOPE_USER, sibling


def test_a_failed_cninfo_orgid_load_is_not_retried_for_every_code():
    from src.services import news_cninfo_source as cninfo

    cninfo.reset_cache_for_tests()
    clock = [100.0]
    gets = []

    class Down:
        @staticmethod
        def get(url, **kwargs):
            gets.append(url)
            raise ConnectionError("cninfo unreachable")

    with patch.object(cninfo, "_session", lambda: Down):
        for code in ("600519", "000001", "300750"):
            assert cninfo.org_id(code, timeout_sec=1, now=lambda: clock[0]) == cninfo._pattern_org_id(code)
        assert len(gets) == 1  # one attempt, then the code pattern without another request
        clock[0] += cninfo.ORGID_FAILURE_RETRY_SECONDS + 1
        cninfo.org_id("600519", timeout_sec=1, now=lambda: clock[0])
        assert len(gets) == 2  # retried once the pause is over

        class Up:
            @staticmethod
            def get(url, **kwargs):
                gets.append(url)
                return SimpleNamespace(raise_for_status=lambda: None,
                                       json=lambda: {"stockList": [{"code": "300750", "orgId": "GD165627"}]})

        clock[0] += cninfo.ORGID_FAILURE_RETRY_SECONDS + 1
        with patch.object(cninfo, "_session", lambda: Up):
            assert cninfo.org_id("300750", timeout_sec=1, now=lambda: clock[0]) == "GD165627"
        assert cninfo.org_id("300750", timeout_sec=1, now=lambda: clock[0]) == "GD165627"
        assert len(gets) == 3  # loaded once, cached for the day
    cninfo.reset_cache_for_tests()
