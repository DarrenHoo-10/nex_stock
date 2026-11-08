# -*- coding: utf-8 -*-
"""Screening Tushare snapshot and daily fetchers use DSA's Tushare HTTP client.

Regression for the old tushare SDK URL override. tushare>=1.4 posts to
``{http_url}/{api_name}`` and returns an empty frame on HTTP errors, so
setting the SDK URL to ``http://api.waditu.com`` emptied every call and
screening fell back to Sina without an error. All Tushare traffic below is
mocked at ``requests.post``; no real request is made.
"""

import json as _json
import sys

import pandas as pd
import pytest
import requests

from src.services.screening import daily as screening_daily
from src.services.screening import snapshot as screening_snapshot

_OFFICIAL_URL = "http://api.tushare.pro"
_GATEWAY_URL = "https://gw.example.com/tushare"
_POST_TARGET = "data_provider.tushare_fetcher.requests.post"

_SNAPSHOT_TABLES = {
    "trade_cal": (["cal_date", "is_open"], [["20261106", 1], ["20261105", 1]]),
    "daily": (
        ["ts_code", "trade_date", "close", "pct_chg", "amount"],
        [
            ["600519.SH", "20261106", 1500.0, 1.2, 2500000.0],
            ["000001.SZ", "20261106", 11.5, -0.5, 900000.0],
        ],
    ),
    "daily_basic": (
        ["ts_code", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv"],
        [
            ["600519.SH", 0.3, 1.1, 22.0, 8.0, 188430000.0, 188430000.0],
            ["000001.SZ", 0.6, 0.9, 5.0, 0.5, 22300000.0, 22300000.0],
        ],
    ),
    "stock_basic": (
        ["ts_code", "symbol", "name", "industry"],
        [
            ["600519.SH", "600519", "贵州茅台", "白酒"],
            ["000001.SZ", "000001", "平安银行", "银行"],
        ],
    ),
}

_DAILY_TABLES = {
    # Tushare returns the newest bar and adjustment factor first.
    "daily": (
        ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"],
        [
            ["600519.SH", "20261106", 10.0, 11.0, 9.0, 10.5, 1000.0, 1050.0],
            ["600519.SH", "20261105", 9.0, 10.0, 8.0, 9.5, 900.0, 855.0],
            ["600519.SH", "20261104", 8.0, 9.0, 7.0, 8.5, 800.0, 680.0],
        ],
    ),
    "adj_factor": (
        ["trade_date", "adj_factor"],
        [["20261106", 2.0], ["20261105", 2.0], ["20261104", 1.0]],
    ),
}


def _response(status_code: int, body: str) -> requests.Response:
    # A real Response keeps ``bool(response) == response.ok``, which the
    # tushare SDK relies on to return an empty frame for HTTP errors.
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    return response


class _FakeTushareEndpoint:
    """Classic Tushare endpoint: POSTs to its root URL, ``api_name`` in the body.

    Any other URL (such as the SDK-style ``{base}/{api_name}``) gets a 404,
    like the real server.
    """

    def __init__(self, tables, *, url=_OFFICIAL_URL, status_code=200, error=None, empty=()):
        self.tables = tables
        self.url = url
        self.status_code = status_code
        self.error = error
        self.empty = set(empty)
        self.calls = []

    def __call__(self, url, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "timeout": timeout, **json})
        if url != self.url:
            return _response(404, "<html>404 Not Found</html>")
        if self.status_code != 200:
            return _response(self.status_code, f"<html>HTTP {self.status_code}</html>")
        if self.error is not None:
            code, msg = self.error
            return _response(200, _json.dumps({"code": code, "msg": msg, "data": None}, ensure_ascii=False))
        fields, items = self.tables[json["api_name"]]
        if json["api_name"] in self.empty:
            items = []
        payload = {"code": 0, "msg": "", "data": {"fields": fields, "items": items}}
        return _response(200, _json.dumps(payload, ensure_ascii=False))

    @property
    def api_names(self):
        return [call["api_name"] for call in self.calls]

    @property
    def urls(self):
        return {call["url"] for call in self.calls}


@pytest.fixture(autouse=True)
def _isolated_tushare_env(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "demo-token")
    for name in (
        "TUSHARE_API_TOKEN",
        "TUSHARE_HTTP_URL",
        "TUSHARE_API_URL",
        "TUSHARE_TRADE_DATE",
        "TUSHARE_DAILY_ADJ",
        "SCREENING_SOURCE_CALL_TIMEOUT_SEC",
        "SCREENING_SNAPSHOT_CALL_TIMEOUT_SEC",
        "SCREENING_DAILY_CALL_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(name, raising=False)
    # The screening path must not need the tushare SDK at all.
    monkeypatch.setitem(sys.modules, "tushare", None)
    monkeypatch.setattr(screening_snapshot, "_SOURCE_HEALTH", {})
    monkeypatch.setattr(screening_daily, "_SOURCE_HEALTH", {})


def _install_endpoint(monkeypatch, endpoint: _FakeTushareEndpoint) -> _FakeTushareEndpoint:
    monkeypatch.setattr(_POST_TARGET, endpoint)
    return endpoint


@pytest.mark.parametrize(
    ("env", "expected_url"),
    [
        ({}, _OFFICIAL_URL),
        # The undocumented SDK-era override no longer redirects screening.
        ({"TUSHARE_API_URL": "http://api.waditu.com"}, _OFFICIAL_URL),
        ({"TUSHARE_HTTP_URL": f"  {_GATEWAY_URL}  "}, _GATEWAY_URL),
    ],
)
def test_tushare_snapshot_posts_classic_endpoint_and_normalizes(monkeypatch, env, expected_url) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    endpoint = _install_endpoint(monkeypatch, _FakeTushareEndpoint(_SNAPSHOT_TABLES, url=expected_url))

    result = screening_snapshot.fetch_cn_snapshot("tushare")

    assert endpoint.api_names == ["trade_cal", "daily", "daily_basic", "stock_basic"]
    # One POST URL for every interface: no SDK-style ``/{api_name}`` suffix.
    assert endpoint.urls == {expected_url}
    assert {call["token"] for call in endpoint.calls} == {"demo-token"}
    assert endpoint.calls[0]["params"]["is_open"] == "1"
    assert endpoint.calls[1]["params"] == {"trade_date": "20261106"}
    assert endpoint.calls[2]["params"] == {"trade_date": "20261106"}

    assert result.attrs["snapshot_source"] == "tushare"
    moutai = result.set_index("code").loc["600519"]
    assert moutai["name"] == "贵州茅台"
    assert moutai["industry"] == "白酒"
    assert moutai["price"] == 1500.0
    assert moutai["amount"] == 2_500_000_000.0  # thousand yuan -> yuan
    assert moutai["total_mv"] == 1_884_300_000_000.0  # ten-thousand yuan -> yuan


@pytest.mark.parametrize(
    ("endpoint_kwargs", "expected_error"),
    [
        ({"status_code": 404}, "Tushare API HTTP 404"),
        ({"error": (40203, "抱歉，您没有访问该接口的权限")}, "抱歉，您没有访问该接口的权限"),
    ],
)
def test_tushare_snapshot_errors_surface_and_fall_back_to_next_source(
    monkeypatch, endpoint_kwargs, expected_error,
) -> None:
    endpoint = _install_endpoint(monkeypatch, _FakeTushareEndpoint(_SNAPSHOT_TABLES, **endpoint_kwargs))
    sina_frame = screening_snapshot._normalize(
        pd.DataFrame([{"code": "000001", "name": "平安银行", "trade": 11.5}]),
        source="sina",
    )
    monkeypatch.setattr(screening_snapshot, "_fetch_sina", lambda: sina_frame.copy())

    result = screening_snapshot.fetch_snapshot_with_fallback(["tushare", "sina"])

    assert endpoint.api_names == ["trade_cal"]
    assert result.attrs["snapshot_source"] == "sina"
    assert result.attrs["source_errors"] == [f"tushare: {expected_error}"]
    health = screening_snapshot.snapshot_source_health_snapshot(["tushare"])["tushare"]
    assert health["failures"] == 1
    assert expected_error in health["last_error"]


@pytest.mark.parametrize(
    ("empty_api", "expected_error", "expected_calls"),
    [
        ("trade_cal", "tushare trade_cal returned no open trading days", ["trade_cal"]),
        ("daily", "tushare daily returned empty data for 20261106", ["trade_cal", "daily"]),
        (
            "daily_basic",
            "tushare daily_basic returned empty data for 20261106",
            ["trade_cal", "daily", "daily_basic"],
        ),
        (
            "stock_basic",
            "tushare stock_basic returned empty data",
            ["trade_cal", "daily", "daily_basic", "stock_basic"],
        ),
    ],
)
def test_tushare_snapshot_empty_table_is_an_error(monkeypatch, empty_api, expected_error, expected_calls) -> None:
    endpoint = _install_endpoint(monkeypatch, _FakeTushareEndpoint(_SNAPSHOT_TABLES, empty=[empty_api]))

    with pytest.raises(RuntimeError, match=expected_error):
        screening_snapshot.fetch_cn_snapshot("tushare")

    # The fetch stops at the first empty table instead of issuing the rest.
    assert endpoint.api_names == expected_calls


def test_tushare_snapshot_rejects_malformed_http_url_without_requests(monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_HTTP_URL", "gw.example.com")
    endpoint = _install_endpoint(monkeypatch, _FakeTushareEndpoint(_SNAPSHOT_TABLES))

    with pytest.raises(ValueError, match="TUSHARE_HTTP_URL"):
        screening_snapshot.fetch_cn_snapshot("tushare")

    assert endpoint.calls == []


@pytest.mark.parametrize(
    ("env", "expected_url"),
    [
        ({}, _OFFICIAL_URL),
        ({"TUSHARE_API_URL": "http://api.waditu.com"}, _OFFICIAL_URL),
        ({"TUSHARE_HTTP_URL": _GATEWAY_URL}, _GATEWAY_URL),
    ],
)
def test_tushare_daily_history_uses_classic_endpoint_and_qfq(monkeypatch, env, expected_url) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    endpoint = _install_endpoint(monkeypatch, _FakeTushareEndpoint(_DAILY_TABLES, url=expected_url))

    result = screening_daily._fetch_daily_tushare("600519", lookback_days=30)

    assert endpoint.api_names == ["daily", "adj_factor"]
    assert endpoint.urls == {expected_url}
    assert endpoint.calls[0]["params"]["ts_code"] == "600519.SH"
    assert endpoint.calls[1]["fields"] == "trade_date,adj_factor"
    assert list(result["date"]) == ["20261104", "20261105", "20261106"]
    # qfq: price * factor / latest factor (latest factor is 2.0).
    assert list(result["close"]) == [4.25, 9.5, 10.5]
    assert list(result["volume"]) == [800.0, 900.0, 1000.0]


def test_tushare_daily_http_error_surfaces_in_source_errors(monkeypatch) -> None:
    endpoint = _install_endpoint(monkeypatch, _FakeTushareEndpoint(_DAILY_TABLES, status_code=404))

    with pytest.raises(RuntimeError, match="tushare after 1 attempts: Tushare API HTTP 404") as exc_info:
        screening_daily.fetch_daily_history("600519", source="tushare", retries=0)

    assert endpoint.api_names == ["daily"]
    health = exc_info.value.daily_metadata["daily_source_health"]["tushare"]
    assert health["failures"] == 1
    assert "Tushare API HTTP 404" in health["last_error"]


def test_tushare_daily_empty_history_is_an_error(monkeypatch) -> None:
    endpoint = _install_endpoint(monkeypatch, _FakeTushareEndpoint(_DAILY_TABLES, empty=["daily"]))

    with pytest.raises(RuntimeError, match="tushare daily history empty for 600519"):
        screening_daily._fetch_daily_tushare("600519", lookback_days=30)

    assert endpoint.api_names == ["daily"]
