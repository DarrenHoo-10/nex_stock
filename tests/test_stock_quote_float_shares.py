# -*- coding: utf-8 -*-
"""Quote float-shares field and the longer history window used by the K-line / chip distribution view."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi import HTTPException

from api.v1.endpoints.stocks import get_stock_history, get_stock_quote
from src.services.stock_service import resolve_float_shares


def test_float_shares_prefers_circulating_market_value() -> None:
    quote = SimpleNamespace(circ_mv=1_000_000_000.0, price=10.0, volume=5.0, turnover_rate=50.0)
    assert resolve_float_shares(quote) == pytest.approx(100_000_000.0)


def test_float_shares_falls_back_to_volume_over_turnover() -> None:
    quote = SimpleNamespace(circ_mv=None, price=10.0, volume=2_000_000, turnover_rate=2.0)
    assert resolve_float_shares(quote) == pytest.approx(100_000_000.0)


@pytest.mark.parametrize(
    "quote",
    [
        None,
        SimpleNamespace(circ_mv=0, price=None, volume=None, turnover_rate=None),
        SimpleNamespace(circ_mv="n/a", price=10.0, volume=100, turnover_rate=True),
    ],
)
def test_float_shares_missing(quote) -> None:
    assert resolve_float_shares(quote) is None


def _manager() -> MagicMock:
    manager = MagicMock()
    manager.get_realtime_quote.return_value = SimpleNamespace(
        code="510300",
        name="沪深300ETF华泰柏瑞",
        price=4.0,
        change_amount=-0.07,
        change_pct=-1.69,
        open_price=4.1,
        high=4.17,
        low=4.09,
        pre_close=4.19,
        volume=1_263_000_000,
        amount=5.2e9,
        turnover_rate=2.72,
        circ_mv=None,
    )
    manager.get_daily_data.return_value = (
        pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-11-05", "2026-11-06"]),
                "open": [4.2, 4.1],
                "high": [4.25, 4.17],
                "low": [4.15, 4.09],
                "close": [4.19, 4.12],
                "volume": [9.0e8, 1.263e9],
                "amount": [3.8e9, 5.2e9],
                "pct_chg": [0.5, -1.69],
            }
        ),
        "MockFetcher",
    )
    manager.get_stock_name.return_value = "华泰柏瑞沪深300ETF"
    return manager


def test_quote_endpoint_exposes_turnover_and_float_shares() -> None:
    manager = _manager()
    with patch("data_provider.base.DataFetcherManager", return_value=manager):
        quote = get_stock_quote("510300")

    assert quote.turnover_rate == pytest.approx(2.72)
    assert quote.float_shares == pytest.approx(1_263_000_000 / 0.0272)
    assert quote.current_price == pytest.approx(4.0)


def test_quote_endpoint_without_float_fields_keeps_contract() -> None:
    manager = _manager()
    manager.get_realtime_quote.return_value.turnover_rate = None
    with patch("data_provider.base.DataFetcherManager", return_value=manager):
        quote = get_stock_quote("510300")

    assert quote.turnover_rate is None
    assert quote.float_shares is None


def test_quote_endpoint_not_found() -> None:
    manager = _manager()
    manager.get_realtime_quote.return_value = None
    with patch("data_provider.base.DataFetcherManager", return_value=manager):
        with pytest.raises(HTTPException) as exc:
            get_stock_quote("510300")
    assert exc.value.status_code == 404


def test_history_endpoint_accepts_long_window() -> None:
    manager = _manager()
    with patch("data_provider.base.DataFetcherManager", return_value=manager):
        response = get_stock_history("510300.SH", period="daily", days=750)

    manager.get_daily_data.assert_called_once_with("510300.SH", days=750)
    manager.get_realtime_quote.assert_not_called()
    assert [bar.close for bar in response.data] == [4.19, 4.12]
    assert response.data[1].volume == pytest.approx(1.263e9)


def test_history_days_upper_bound_is_1000() -> None:
    from fastapi.testclient import TestClient
    from fastapi import FastAPI

    from api.v1.endpoints import stocks

    app = FastAPI()
    app.include_router(stocks.router, prefix="/api/v1/stocks")
    client = TestClient(app)
    with patch("data_provider.base.DataFetcherManager", return_value=_manager()):
        assert client.get("/api/v1/stocks/510300/history", params={"days": 1000}).status_code == 200
    assert client.get("/api/v1/stocks/510300/history", params={"days": 1001}).status_code == 422
