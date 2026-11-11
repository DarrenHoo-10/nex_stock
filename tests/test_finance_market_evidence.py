import json
import pytest

from src.services.finance_market_evidence import evidence_for


def test_yesterday_daily_and_missing_time_snapshot_are_not_minute_evidence():
    daily = evidence_for("/api/v1/stocks/000001/history", {
        "stock_code": "000001", "source": "sample", "period": "daily",
        "data": [{"date": "2026-11-09", "close": 100}]})
    assert daily["granularity"] == "daily_bar"
    assert daily["tradingDay"] == "2026-11-09"
    assert daily["marketTimestamp"] is None and daily["minuteEvidence"] is False
    quote = evidence_for("/api/v1/stocks/000001/quote", {
        "stock_code": "000001", "current_price": 100, "update_time": "2026-11-10T14:30:00"})
    assert quote["marketTimestamp"] is None and quote["coverage"] == "unknown"
    assert quote["minuteEvidence"] is False


def test_matching_intraday_bars_have_citable_rows_and_stale_does_not_claim_current():
    payload = {"stock_code": "000001", "source": "fixture", "interval": "5min",
               "trade_date": "2026-11-10", "as_of": "2026-11-10 14:30:00", "is_stale": False,
               "data": [{"time": "2026-11-10 14:30:00", "close": 100}]}
    result = evidence_for("/api/v1/stocks/000001/intraday", payload)
    assert result["minuteEvidence"] is True
    assert result["marketTimezone"] == "Asia/Shanghai"
    assert result["units"]["volume"] == "shares"
    assert result["rows"][0]["observedAt"] == "2026-11-10T14:30:00+08:00"
    payload["is_stale"] = True
    assert evidence_for("/api/v1/stocks/000001/intraday", payload)["minuteEvidence"] is False


def test_intraday_rejects_wrong_day_non_bar_minute_and_synthetic_as_of():
    payload = {"stock_code": "000001", "source": "fixture", "interval": "5min", "trade_date": "2026-11-10",
               "as_of": "2026-11-10 14:30:00", "data": [{"time": "2026-11-09 14:30:00", "close": 1}]}
    result = evidence_for("/api/v1/stocks/000001/intraday", payload)
    assert result["minuteEvidence"] is False and result["rows"][0]["observedAt"] is None
    payload["data"][0]["time"] = "2026-11-10 14:31:00"
    assert evidence_for("/api/v1/stocks/000001/intraday", payload)["minuteEvidence"] is False
    payload["data"][0]["time"] = "2026-11-10 14:25:00"
    assert evidence_for("/api/v1/stocks/000001/intraday", payload)["marketTimestamp"] is None


def test_us_hk_evidence_uses_own_market_timezone_and_currency():
    for code, zone, currency in (("AAPL", "America/New_York", "USD"), ("HK00700", "Asia/Hong_Kong", "HKD")):
        result = evidence_for(f"/api/v1/stocks/{code}/history", {"stock_code": code, "source": "fixture", "data": [{"date": "2026-11-09"}]})
        assert result["marketTimezone"] == zone
        assert result["units"]["price"] == currency


def test_cross_stock_strategy_inputs_have_no_fixed_count_and_use_real_quantitative_analysis(monkeypatch):
    from src.services.finance_strategy_inputs import strategy_inputs
    codes = [f"{i:06}" for i in range(12)]
    calls = []
    def daily(method, path, *, user_id, params):
        assert user_id == "strategy-owner"
        calls.append(path)
        rows = [{"date": f"2026-10-{i+1:02}", "open": 10 + i, "close": 10.5 + i,
                 "high": 11 + i, "low": 9 + i, "volume": 1000 + i, "change_percent": 1} for i in range(30)]
        return 200, "application/json", json.dumps({"stock_code": path.split("/")[-2], "source": "fixture", "data": rows})
    monkeypatch.setattr("api.internal_client.call", daily)
    result = strategy_inputs({"stock_codes": codes, "days": 30}, user_id="strategy-owner")
    assert len(calls) == len(result["items"]) == 12
    assert all(item["technical"]["code"] == code for item, code in zip(result["items"], codes))
    assert result["modelUsage"] == "none"


def test_batch_strategy_security_path_cannot_traverse_into_personal_history(monkeypatch):
    from fastapi import HTTPException
    from src.services.finance_strategy_inputs import strategy_inputs
    monkeypatch.setattr("api.internal_client.call", lambda *args, **kwargs: pytest.fail("traversal reached a private API"))
    for code in ("..", ".", "%2e%2e", "../history", "TEST\x00", "\t..\t"):
        with pytest.raises(HTTPException) as exc:
            strategy_inputs({"stock_codes": [code]}, user_id="strategy-owner")
        assert exc.value.status_code == 422


def test_aggregate_profile_with_portfolio_never_enters_public_route():
    from api.v1.endpoints.finance import public_route
    assert not public_route("GET", "/api/v1/stocks/000001/profile")


def test_real_stock_http_quote_keeps_provenance_and_distinguishes_index_same_bare_code(monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints import stocks
    from data_provider.base import DataFetcherManager
    from src.services.stock_list_parser import parse_analysis_target
    monkeypatch.setattr(DataFetcherManager, "__init__", lambda self: None)
    monkeypatch.setattr("src.services.stock_service.StockRepository", lambda: None)
    observed = []
    def quote(self, stock_code):
        identity = parse_analysis_target(stock_code)
        observed.append(identity)
        return SimpleNamespace(code="000001", name="上证指数" if identity.asset_type == "index" else "平安银行",
                               price=3400 if identity.asset_type == "index" else 10,
                               source="synthetic-provider", market="cn", currency="CNY", is_stale=False,
                               provider_timestamp="2026-11-10T14:30:00+08:00", fetched_at="2026-11-10T14:30:02+08:00")
    monkeypatch.setattr(DataFetcherManager, "get_realtime_quote", quote)
    app = FastAPI()
    app.include_router(stocks.router, prefix="/api/v1/stocks")
    client = TestClient(app)
    index = client.get("/api/v1/stocks/000001.SH/quote")
    equity = client.get("/api/v1/stocks/000001/quote")
    assert index.status_code == equity.status_code == 200
    for response in (index, equity):
        assert response.json()["source"] == "synthetic-provider"
        assert response.json()["provider_timestamp"] == "2026-11-10T14:30:00+08:00"
        assert response.json()["fetched_at"] == "2026-11-10T14:30:02+08:00"
        assert response.json()["market"] == "cn" and response.json()["currency"] == "CNY"
        assert response.json()["is_stale"] is False
    assert index.json()["asset_type"] == "index" and index.json()["exchange"] == "SH"
    assert equity.json()["asset_type"] == "stock" and equity.json()["exchange"] == "SZ"
    assert index.json()["canonical_id"] != equity.json()["canonical_id"]
    assert evidence_for("/api/v1/stocks/000001.SH/quote", index.json())["units"]["price"] == "index_points"
    assert evidence_for("/api/v1/stocks/000001/quote", equity.json())["units"]["price"] == "CNY"


def test_real_intraday_endpoint_never_reuses_equity_cache_for_index(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.v1.endpoints import stocks
    from src.services.intraday_snapshot_service import IntradaySnapshotService
    service = IntradaySnapshotService(ttl_seconds=60, now_fn=lambda: datetime(2026, 11, 10, 14, 30, tzinfo=timezone(timedelta(hours=8))))
    calls = []
    def equity_source(stock_code, code, day, is_today):
        calls.append(stock_code)
        return [{"time": "2026-11-10T14:25:00+08:00", "close": 10}], "synthetic-equity"
    monkeypatch.setattr(service, "_load_session_bars", equity_source)
    monkeypatch.setattr(service, "_market_open", lambda: False)
    monkeypatch.setattr(stocks, "get_intraday_snapshot_service", lambda: service)
    app = FastAPI()
    app.include_router(stocks.router, prefix="/api/v1/stocks")
    client = TestClient(app)
    equity = client.get("/api/v1/stocks/000001/intraday?date=2026-11-10")
    index = client.get("/api/v1/stocks/000001.SH/intraday?date=2026-11-10")
    assert equity.status_code == 200, equity.text
    assert index.status_code == 422, index.text
    assert index.json()["detail"]["error"] == "intraday_identity_unsupported"
    assert calls == ["000001"]
