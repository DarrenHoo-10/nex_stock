"""Offline classification contracts; unknown views never acquire public provenance."""
import pytest
from src.services.finance_data_visibility import api_tool_call_is_private, classify_api_data


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/v1/stocks/AAPL/quote", "public"),
    ("get", "stocks/600519/history?limit=5", "public"),
    ("GET", "/api/v1/stocks/hk00700/intraday", "public"),
    ("GET", "/api/v1/screening/strategies", "public"),
    ("GET", "/api/v1/stocks/AAPL/profile", "private"),
    ("GET", "/api/v1/stocks/AAPL/research", "public"),
    ("GET", "/api/v1/data/overview", "public"),
    ("GET", "/api/v1/data/indices/000300/constituents", "public"),
    ("GET", "/api/v1/boards/anomalies", "public"),
    ("POST", "/api/v1/stocks/AAPL/quote", "private"),
    ("GET", "/api/v1/portfolio/accounts", "private"),
    ("GET", "/api/v1/stocks/watchlist", "private"),
    ("GET", "/api/v1/history/1", "private"),
    ("GET", "/api/v1/decision-signals", "private"),
    ("GET", "/api/v1/screening/results", "private"),
    ("GET", "/api/v1/unclassified", "private"),
    ("GET", "https://another.invalid/api/v1/stocks/AAPL/quote", "private"),
    ("GET", "http://[malformed", "private"),
    ("GET", "/api/v1/stocks/AAPL/%252f../quote", "private"),
])
def test_method_and_return_view_classification(method,path,expected):
    assert classify_api_data(method,path)["audience"] == expected


def test_view_provenance_cannot_promote_a_private_or_unknown_route():
    assert classify_api_data("GET","/api/v1/history/1",response={"data_visibility":{"audience":"public"}})["audience"] == "private"
    assert classify_api_data("GET","/api/v1/stocks/AAPL/quote",response={"is_private":True})["audience"] == "private"
    assert classify_api_data("GET","/api/v1/stocks/AAPL/quote",response={"data_visibility":{"audience":"private"}})["audience"] == "private"


def test_runner_and_codex_log_arguments_both_classify_private_data():
    for key in ("arguments","arguments_summary","args"):
        assert api_tool_call_is_private({key:{"method":"GET","path":"/portfolio/accounts"}})
        assert not api_tool_call_is_private({key:{"method":"GET","path":"/stocks/AAPL/quote"}})
    assert api_tool_call_is_private({"arguments":'{"method":"GET","path":"/portfolio/accounts"}'})
    assert api_tool_call_is_private({"arguments":"truncated diagnostic"})
