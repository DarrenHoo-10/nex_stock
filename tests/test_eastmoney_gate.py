# -*- coding: utf-8 -*-
"""East Money gate, stock-code guards and frozen-quote flags (no network)."""

from types import SimpleNamespace

import pytest
import requests

from data_provider import eastmoney_gate
from data_provider.eastmoney_gate import EastmoneyCoolingDown, EastmoneyGate


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


def _gate(clock):
    return EastmoneyGate(clock=clock, wall_clock=lambda: 1_800_000_000.0, sleep=clock.sleep, jitter=lambda: 0.2)


def test_host_of_only_matches_east_money():
    assert eastmoney_gate.host_of("https://push2.eastmoney.com/api/qt/clist/get?x=1") == "push2.eastmoney.com"
    assert eastmoney_gate.host_of("https://datacenter-web.eastmoney.com/api") == "datacenter-web.eastmoney.com"
    assert eastmoney_gate.host_of("https://www.cninfo.com.cn/new") is None
    assert eastmoney_gate.host_of("https://eastmoney.com.evil.example/") is None


def test_same_host_is_paced_other_hosts_are_independent(monkeypatch):
    monkeypatch.setenv("EASTMONEY_MIN_INTERVAL_SEC", "1.0")
    clock = _Clock()
    gate = _gate(clock)
    sent = []

    def send(method, url, **kwargs):
        sent.append((url, clock.now))
        return SimpleNamespace(status_code=200)

    gate.request(send, "GET", "https://push2.eastmoney.com/a")
    gate.request(send, "GET", "https://push2.eastmoney.com/b")
    gate.request(send, "GET", "https://push2his.eastmoney.com/c")
    assert clock.sleeps == [1.2]  # second push2 call waited interval + jitter; push2his did not wait
    assert [round(t - sent[0][1], 1) for _u, t in sent] == [0.0, 1.2, 1.2]
    assert gate.request(send, "GET", "https://www.example.com/") .status_code == 200  # untouched


def test_403_cools_the_host_down_without_touching_others(monkeypatch):
    monkeypatch.setenv("EASTMONEY_MIN_INTERVAL_SEC", "0")
    monkeypatch.setenv("EASTMONEY_BAN_COOLDOWN_MINUTES", "30")
    clock = _Clock()
    gate = _gate(clock)
    calls = []

    def send(method, url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=403 if "push2." in url else 200)

    assert gate.request(send, "GET", "https://push2.eastmoney.com/a").status_code == 403
    with pytest.raises(EastmoneyCoolingDown) as caught:
        gate.request(send, "GET", "https://push2.eastmoney.com/b")
    assert isinstance(caught.value, requests.exceptions.ConnectionError)  # callers' fallbacks catch it
    assert gate.request(send, "GET", "https://datacenter-web.eastmoney.com/x").status_code == 200
    assert calls == ["https://push2.eastmoney.com/a", "https://datacenter-web.eastmoney.com/x"]

    status = {row["host"]: row for row in gate.status()}
    assert status["push2.eastmoney.com"]["cooling_down"] and status["push2.eastmoney.com"]["last_error"] == "HTTP 403"
    assert not status["datacenter-web.eastmoney.com"]["cooling_down"]

    clock.now += 30 * 60 + 1
    assert gate.request(send, "GET", "https://push2.eastmoney.com/c").status_code == 403  # allowed again


def test_hook_routes_library_calls_and_composes_with_the_old_patch(monkeypatch):
    seen = []

    def fake_request(self, method, url, **kwargs):
        seen.append(url)
        return SimpleNamespace(status_code=403 if "push2" in url else 200)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_gate, "_underlying_request", None)
    eastmoney_gate.install()
    assert eastmoney_gate.is_installed()
    eastmoney_gate.install()  # idempotent: no double wrapping

    session = requests.Session()
    assert session.request("GET", "https://push2.eastmoney.com/api").status_code == 403
    with pytest.raises(EastmoneyCoolingDown):
        session.request("GET", "https://push2.eastmoney.com/api")
    assert session.request("GET", "https://www.sina.com.cn/").status_code == 200

    # The opt-in fingerprint patch must wrap the gate, not the import-time original.
    from src.patches import eastmoney_patch as patch_module

    monkeypatch.setattr(patch_module, "_get_nid", lambda ua: None)
    monkeypatch.setattr(patch_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(patch_module._patch_sign, "patched", False)
    patch_module.eastmoney_patch()
    with pytest.raises(EastmoneyCoolingDown):
        session.request("GET", "https://push2.eastmoney.com/again")
    assert seen == ["https://push2.eastmoney.com/api", "https://www.sina.com.cn/"]


def test_get_helper_uses_caller_interval_without_double_gating(monkeypatch):
    calls = []

    def fake_request(self, method, url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(eastmoney_gate, "_underlying_request", None)
    eastmoney_gate.install()
    response = eastmoney_gate.get("https://data.eastmoney.com/dataapi/xuangu/list", interval=0)
    assert response.status_code == 200 and calls == ["https://data.eastmoney.com/dataapi/xuangu/list"]


def test_child_process_akshare_calls_hold_their_host():
    from data_provider.akshare_fetcher import _eastmoney_host_for

    def named(name):
        return SimpleNamespace(__name__=name)

    assert _eastmoney_host_for(named("stock_zh_a_hist")) == "push2his.eastmoney.com"
    assert _eastmoney_host_for(named("stock_zh_index_daily_em")) == "push2his.eastmoney.com"
    assert _eastmoney_host_for(named("stock_zh_a_spot_em")) == "push2.eastmoney.com"
    assert _eastmoney_host_for(named("stock_zh_a_hist_tx")) is None
    assert _eastmoney_host_for(named("stock_zh_a_spot")) is None


def test_data_status_shows_the_gate_and_its_config_keys(monkeypatch):
    from src.services.data_capability_service import DataCapabilityService

    service = DataCapabilityService(config=SimpleNamespace(), fetcher_manager=SimpleNamespace(_fetchers=[]))
    row = next(p for p in service.get_overview()["providers"] if p["name"] == "eastmoney")
    assert row["status"] == "ok" and row["cooldown"] is False
    assert row["config_keys"] == ["EASTMONEY_MIN_INTERVAL_SEC", "EASTMONEY_BAN_COOLDOWN_MINUTES"]

    eastmoney_gate.GATE.mark_banned("push2.eastmoney.com", "HTTP 403")
    row = next(p for p in service.get_overview()["providers"] if p["name"] == "eastmoney")
    assert row["status"] == "partial" and row["cooldown"] is True
    assert any(w.startswith("cooldown:push2.eastmoney.com") for w in row["warnings"])


def test_config_keys_are_registered():
    from src.core.config_registry import get_field_definition

    for key in ("EASTMONEY_MIN_INTERVAL_SEC", "EASTMONEY_BAN_COOLDOWN_MINUTES"):
        field = get_field_definition(key)
        assert field and field["category"] == "data_source" and field["data_type"] == "number"


# ---------------------------------------------------------------------------
# stock-code guards
# ---------------------------------------------------------------------------

@pytest.fixture
def orgids(monkeypatch):
    from src.services import news_cninfo_source

    mapping = {"920002": "gfbj0873843", "920982": "gfbj0832982", "600089": "gssh0600089", "300750": "GD165627"}
    monkeypatch.setattr(news_cninfo_source, "orgid_map", lambda **kwargs: mapping)
    return mapping


def test_old_bse_codes_map_to_new_codes_and_unknown_ones_are_rejected(orgids):
    from src.services.stock_code_guard import LegacyBseCodeError, is_legacy_bse_code, resolve_bse_code

    assert is_legacy_bse_code("832982") and not is_legacy_bse_code("920982") and not is_legacy_bse_code("600089")
    assert resolve_bse_code("873843") == "920002"
    assert resolve_bse_code("832982") == "920982"
    assert resolve_bse_code("600089") == "600089"
    with pytest.raises(LegacyBseCodeError):
        resolve_bse_code("430999")


def test_require_stock_code_rejects_index_codes(orgids):
    from src.services.stock_code_guard import require_stock_code

    assert require_stock_code("600089") == "600089"
    assert require_stock_code("600089.SH") == "600089"
    assert require_stock_code("000001") == "000001"  # bare 000001 is 平安银行
    assert require_stock_code("832982") == "920982"
    for bad in ("sh000001", "000300.SH", "AAPL", ""):
        with pytest.raises(ValueError):
            require_stock_code(bad)


def test_watchlist_add_stores_the_new_bse_code(orgids):
    from api.v1.endpoints.stocks import add_to_watchlist
    from api.v1.schemas.history import WatchlistRequest
    from fastapi import HTTPException
    from tests.test_stock_watchlist_api import FakeSystemConfigService

    service = FakeSystemConfigService("600089")
    response = add_to_watchlist(WatchlistRequest(stock_code="832982"), service=service)
    assert response.stock_codes == ["600089", "920982"] and "旧代码" in response.message
    with pytest.raises(HTTPException) as caught:
        add_to_watchlist(WatchlistRequest(stock_code="430999"), service=service)
    assert caught.value.status_code == 400 and caught.value.detail["error"] == "legacy_bse_code"


# ---------------------------------------------------------------------------
# frozen quotes
# ---------------------------------------------------------------------------

def test_frozen_quotes_are_labelled():
    from data_provider.base import DataFetcherManager
    from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote

    legacy = UnifiedRealtimeQuote(code="832982", source=RealtimeSource.TENCENT, price=112.6, pre_close=112.6, amount=0.0)
    DataFetcherManager._mark_frozen_quote(legacy)
    assert legacy.stale_reason == "legacy_bse_code" and legacy.is_stale is True

    quiet = UnifiedRealtimeQuote(code="600089", source=RealtimeSource.TENCENT, price=18.5, pre_close=18.5, amount=0.0)
    DataFetcherManager._mark_frozen_quote(quiet)
    assert quiet.stale_reason == "no_trade_today" and quiet.is_stale is None
    assert quiet.to_dict()["stale_reason"] == "no_trade_today"

    traded = UnifiedRealtimeQuote(code="600089", source=RealtimeSource.TENCENT, price=18.5, pre_close=18.5, amount=1e6)
    DataFetcherManager._mark_frozen_quote(traded)
    assert traded.stale_reason is None and "stale_reason" not in traded.to_dict()
