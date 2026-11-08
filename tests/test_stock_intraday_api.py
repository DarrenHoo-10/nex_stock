# -*- coding: utf-8 -*-
"""GET /api/v1/stocks/{code}/intraday: payload shape and error mapping (service mocked at the provider edge)."""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.v1.endpoints import stocks as stocks_endpoint
from src.services.intraday_snapshot_service import (
    BarsResult,
    IntradayDataUnavailable,
    IntradaySnapshotService,
)


def _service_with(result=None, error=None):
    service = MagicMock(spec=IntradaySnapshotService)
    if error is not None:
        service.get_session_bars.side_effect = error
    else:
        service.get_session_bars.return_value = result
    return service


def _patch_service(monkeypatch, service):
    monkeypatch.setattr(stocks_endpoint, "get_intraday_snapshot_service", lambda: service)


def test_returns_bars_with_source_and_freshness(monkeypatch) -> None:
    result = BarsResult(
        code="002029",
        bars=[
            {"time": "2026-11-06T09:35:00+08:00", "open": 8.51, "high": 8.64, "low": 8.35, "close": 8.51,
             "volume": 12307400, "amount": 104632755.19},
            {"time": "2026-11-06T15:00:00+08:00", "open": 8.91, "high": 8.99, "low": 8.9, "close": 8.9,
             "volume": 3433300, "amount": 30629167.12},
        ],
        source="biying",
        as_of="2026-11-06T15:00:00+08:00",
        fetched_at="2026-11-08T20:00:00+08:00",
        trade_date="2026-11-06",
    )
    service = _service_with(result)
    _patch_service(monkeypatch, service)

    response = stocks_endpoint.get_stock_intraday("002029", date="2026-11-06")

    service.get_session_bars.assert_called_once_with("002029", "2026-11-06")
    assert response.stock_code == "002029"
    assert response.trade_date == "2026-11-06"
    assert response.interval == "5min"
    assert response.source == "biying"
    assert response.is_stale is False
    assert [bar.close for bar in response.data] == [8.51, 8.9]
    assert response.data[0].volume == 12307400


def test_invalid_or_future_date_is_422(monkeypatch) -> None:
    _patch_service(monkeypatch, _service_with(error=ValueError("date 2099-01-01 is in the future")))
    with pytest.raises(HTTPException) as caught:
        stocks_endpoint.get_stock_intraday("002029", date="2099-01-01")
    assert caught.value.status_code == 422
    assert caught.value.detail["error"] == "invalid_date"


def test_no_source_is_503_without_leaking_provider_detail(monkeypatch) -> None:
    _patch_service(
        monkeypatch,
        _service_with(error=IntradayDataUnavailable("biying: Biying error: bad licence SECRET-123")),
    )
    with pytest.raises(HTTPException) as caught:
        stocks_endpoint.get_stock_intraday("002029", date="2026-11-05")
    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == "intraday_unavailable"
    assert "SECRET-123" not in str(caught.value.detail)


def test_unexpected_error_is_500(monkeypatch) -> None:
    _patch_service(monkeypatch, _service_with(error=RuntimeError("boom")))
    with pytest.raises(HTTPException) as caught:
        stocks_endpoint.get_stock_intraday("002029", date="2026-11-05")
    assert caught.value.status_code == 500
