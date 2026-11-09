# -*- coding: utf-8 -*-
"""Macro / index data (Tushare) and the Tencent K-line host rotation (no network).

Fixtures are single samples taken on 2026-11-09: Tushare cn_pmi / cn_m / sf_month / shibor_lpr /
index_dailybasic / index_weight (tests/fixtures/tushare/macro_samples.json) and one Tencent K-line
answer per host (tests/fixtures/tencent).
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.repositories.macro_repo import MacroRepository
from src.services.macro_service import MacroService, pmi_rows, valuation_rows, weight_rows
from src.storage import DatabaseManager

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = json.loads((FIXTURES / "tushare/macro_samples.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 9, 8, 5)


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
    manager = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'macro.db').as_posix()}")
    yield manager
    DatabaseManager.reset_instance()


def _fake():
    return FakeTushare({
        "cn_pmi": SAMPLES["cn_pmi"], "cn_m": SAMPLES["cn_m"], "sf_month": SAMPLES["sf_month"],
        "shibor_lpr": SAMPLES["shibor_lpr"],
        "index_dailybasic": lambda p: SAMPLES["index_dailybasic"] if p["trade_date"] == "20261106" else [],
        "index_weight": lambda p: SAMPLES["index_weight_000016"] if p["index_code"] == "000016.SH" else [],
    })


def test_parsers_on_real_samples():
    pmi = pmi_rows(SAMPLES["cn_pmi"])
    assert pmi[0] == {"period": "202610", "value": {"manufacturing": 49.3, "non_manufacturing": 49.7,
                                                    "composite": 49.7}}
    valuation = valuation_rows(SAMPLES["index_dailybasic"])
    assert len(valuation) == 1 and valuation[0]["period"] == "20261106"
    hs300 = valuation[0]["value"]["indices"]["000300.SH"]
    assert hs300["name"] == "沪深300" and hs300["pe_ttm"] == pytest.approx(13.5314) and hs300["pb"] == pytest.approx(1.3331)
    weights = weight_rows(SAMPLES["index_weight_000016"])
    assert weights[0]["series"] == "index_weight:000016.SH" and weights[0]["period"] == "20261030"
    assert weights[0]["value"]["constituents"][0] == ["600519", 9.819]


def test_refresh_stores_series_and_skips_recent_ones(db):
    fake = _fake()
    service = MacroService(MacroRepository(db), fake, now=lambda: NOW)
    summary = service.refresh()
    assert summary["status"] == "ok"
    assert summary["pmi"] == {"status": "ok", "rows": 4, "stored": 4}
    assert summary["index_weight"]["rows"] == 1  # only 上证50 has rows in the fake
    weight_calls = [p["index_code"] for api, p in fake.calls if api == "index_weight"]
    assert len(weight_calls) == 6 and "000852.SH" in weight_calls
    assert not any(p["trade_date"] in ("20261107", "20261108") for api, p in fake.calls if api == "index_dailybasic")

    fake.calls.clear()
    again = service.refresh()
    assert fake.calls == [] and {v["status"] for k, v in again.items() if k != "status"} == {"cached"}

    overview = service.overview()
    assert overview["pmi"][0]["period"] == "202610" and overview["money"][0]["m2_yoy"] == 7.2
    assert overview["lpr"][0] == {"period": "20260820", "lpr_1y": 3.0, "lpr_5y": 3.5}
    assert overview["index_valuation"]["trade_date"] == "20261106"
    assert service.constituents("000016.SH")["constituents"][0] == ["600519", 9.819]
    assert service.constituents("000905.SH") is None


def test_one_failing_series_keeps_the_others(db):
    fake = _fake()
    fake.by_api["cn_m"] = RuntimeError("接口超时")
    summary = MacroService(MacroRepository(db), fake, now=lambda: NOW).refresh()
    assert summary["status"] == "partial" and summary["money"]["status"] == "failed"
    assert summary["pmi"]["status"] == "ok"


def test_macro_endpoints(db, monkeypatch):
    from fastapi import HTTPException

    from api.v1.endpoints import data as data_endpoints
    from src.services import macro_service

    MacroService(MacroRepository(db), _fake(), now=lambda: NOW).refresh()
    monkeypatch.setattr(macro_service.MacroService, "__init__",
                        lambda self, repo=None, fetcher=None, **k: (setattr(self, "_repo", MacroRepository(db)),
                                                                    setattr(self, "_fetcher", None),
                                                                    setattr(self, "_now", lambda: NOW), None)[-1])
    overview = data_endpoints.get_macro_overview(months=6)
    assert overview["pmi"][0]["manufacturing"] == 49.3 and "pmi" in overview["units"]
    weights = data_endpoints.get_index_constituents("000016.SH")
    assert weights["name"] == "上证50" and weights["count"] == 12
    with pytest.raises(HTTPException) as exc:
        data_endpoints.get_index_constituents("000905.SH")
    assert exc.value.status_code == 404


def test_daily_job_refreshes_macro_with_a_token(monkeypatch):
    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_pool_service import NewsPoolService
    from tests.news_fixtures import NOW as NEWS_NOW, install_stub, make_config

    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    install_stub(monkeypatch)
    calls = []
    service = NewsPoolService(IntelligenceRepository(db), make_config(stock_list=[]), is_trading_day=lambda d: True,
                              holdings_provider=lambda: [], sleep_fn=lambda s: None,
                              risk_lists_runner=lambda: {"status": "ok"}, articles_query=lambda s, e: [],
                              macro_runner=lambda: calls.append(1) or {"status": "ok"})
    result = service.run_daily_fetch(day=NEWS_NOW.date(), now=NEWS_NOW)
    assert result["macro"] == {"status": "ok"} and calls == [1]
    DatabaseManager.reset_instance()


# ----------------------------------------------------------------------
# Tencent K-line host rotation
# ----------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_tencent_rotates_to_the_next_host_on_empty_answers(monkeypatch):
    from src.services.screening import daily

    daily.reset_tencent_hosts_for_tests()
    sample = json.loads((FIXTURES / "tencent/fqkline_sh600089.json").read_text(encoding="utf-8"))
    asked = []

    def fake_get(url, params=None, **kwargs):
        asked.append(url.split("/")[2])
        return _Resp({} if "web.ifzq" in url else sample)

    monkeypatch.setattr(daily.requests, "get", fake_get)
    clock = [1000.0]
    monkeypatch.setattr(daily.time, "monotonic", lambda: clock[0])
    df = daily._fetch_daily_tencent("600089", lookback_days=30)
    assert asked == ["web.ifzq.gtimg.cn", "proxy.finance.qq.com"] and len(df) == len(sample["data"]["sh600089"]["qfqday"])
    assert df.iloc[-1]["close"] == pytest.approx(float(sample["data"]["sh600089"]["qfqday"][-1][2]))

    asked.clear()
    daily._fetch_daily_tencent("600089", lookback_days=30)
    assert asked == ["proxy.finance.qq.com"]  # the empty host is cooling down

    clock[0] += daily.TENCENT_HOST_COOLDOWN_SECONDS + 1
    asked.clear()
    daily._fetch_daily_tencent("600089", lookback_days=30)
    assert asked[0] == "web.ifzq.gtimg.cn"
    daily.reset_tencent_hosts_for_tests()


def test_tencent_all_hosts_empty_raises(monkeypatch):
    from src.services.screening import daily

    daily.reset_tencent_hosts_for_tests()
    monkeypatch.setattr(daily.requests, "get", lambda url, **k: _Resp({"code": 0, "data": {}}))
    with pytest.raises(RuntimeError, match="missing payload"):
        daily._fetch_daily_tencent("600089", lookback_days=30)
    assert len(daily._tencent_urls_in_order(daily.time.monotonic())) == 1  # all cooling: the first to recover
    daily.reset_tencent_hosts_for_tests()
