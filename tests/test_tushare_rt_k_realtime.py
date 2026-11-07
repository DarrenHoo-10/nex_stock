# -*- coding: utf-8 -*-
"""Tushare single-stock realtime quote via rt_k (mocked at the pro_api object)."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote, format_cn_provider_timestamp
from data_provider.tushare_fetcher import (
    RealtimePermissionError,
    TushareFetcher,
    _is_permission_error,
)

RT_K_COLUMNS = [
    "ts_code", "name", "pre_close", "high", "open", "low", "close", "vol", "amount", "num",
    "ask_price1", "ask_volume1", "bid_price1", "bid_volume1", "trade_time",
]


def _rt_k_frame(**overrides):
    row = {
        "ts_code": "600519.SH", "name": "贵州茅台", "pre_close": 1500.0, "high": 1530.0,
        "open": 1505.0, "low": 1498.0, "close": 1515.0, "vol": 2345600, "amount": 3.5e9,
        "num": 1, "ask_price1": 1515.5, "ask_volume1": 100, "bid_price1": 1515.0,
        "bid_volume1": 200, "trade_time": "2026-11-06 10:32:05",
    }
    row.update(overrides)
    return pd.DataFrame([row], columns=RT_K_COLUMNS)


def _fetcher() -> TushareFetcher:
    with patch.object(TushareFetcher, "_init_api", return_value=None):
        fetcher = TushareFetcher()
    fetcher._api = MagicMock()
    fetcher.priority = 2
    return fetcher


class TestRtKQuote(unittest.TestCase):
    def test_rt_k_row_is_parsed_with_share_units_and_provider_time(self):
        fetcher = _fetcher()
        fetcher._api.rt_k.return_value = _rt_k_frame()
        with patch.object(fetcher, "_check_rate_limit"):
            quote = fetcher.get_realtime_quote("600519")

        fetcher._api.rt_k.assert_called_once_with(ts_code="600519.SH")
        fetcher._api.quotation.assert_not_called()
        self.assertEqual(quote.source, RealtimeSource.TUSHARE)
        self.assertEqual(quote.code, "600519")
        self.assertEqual(quote.name, "贵州茅台")
        self.assertAlmostEqual(quote.price, 1515.0)
        self.assertAlmostEqual(quote.change_amount, 15.0)
        self.assertAlmostEqual(quote.change_pct, 1.0)
        self.assertEqual(quote.volume, 2345600)  # rt_k vol is already shares
        self.assertAlmostEqual(quote.amount, 3.5e9)
        self.assertAlmostEqual(quote.amplitude, (1530.0 - 1498.0) / 1500.0 * 100, places=3)
        self.assertEqual(quote.provider_timestamp, "2026-11-06T10:32:05+08:00")

    def test_provider_timestamp_feeds_manager_staleness(self):
        fetcher = _fetcher()
        fetcher._api.rt_k.return_value = _rt_k_frame(trade_time="2026-11-06 10:32:05")
        manager = DataFetcherManager(fetchers=[fetcher])
        config = SimpleNamespace(
            enable_realtime_quote=True, realtime_source_priority="tushare", realtime_cache_ttl=600,
        )
        with patch("src.config.get_config", return_value=config), patch.object(fetcher, "_check_rate_limit"):
            quote = manager.get_realtime_quote("600519")
        self.assertIsNotNone(quote.stale_seconds)
        self.assertTrue(quote.is_stale)  # long past 10 minutes
        self.assertTrue(quote.provider_timestamp.endswith("+00:00"))  # normalised to UTC, offset kept correct

    def test_bare_time_uses_given_date(self):
        today = pd.Timestamp("2026-11-06 00:00:00", tz="Asia/Shanghai").to_pydatetime()
        self.assertEqual(
            format_cn_provider_timestamp("09:31:00", today=today), "2026-11-06T09:31:00+08:00"
        )
        self.assertIsNone(format_cn_provider_timestamp("not a time"))

    def test_permission_error_is_clean_miss_and_cooldown_skips_network(self):
        fetcher = _fetcher()
        fetcher._api.rt_k.side_effect = Exception("抱歉，您没有接口访问权限，权限的具体详情访问：https://tushare.pro")
        fake_tushare = MagicMock()
        with patch.dict(sys.modules, {"tushare": fake_tushare}), patch.object(fetcher, "_check_rate_limit"):
            self.assertIsNone(fetcher.get_realtime_quote("600519"))
            self.assertIsNone(fetcher.get_realtime_quote("000001"))
        # legacy sina-scraping fallback must not run, and the second call must not hit rt_k
        fake_tushare.get_realtime_quotes.assert_not_called()
        self.assertEqual(fetcher._api.rt_k.call_count, 1)

    def test_permission_error_falls_through_to_next_source_in_manager(self):
        fetcher = _fetcher()
        fetcher._api.rt_k.side_effect = Exception("抱歉，您没有接口访问权限")

        class _Next:
            name = "BiyingFetcher"
            priority = 3

            def get_realtime_quote(self, code):
                return UnifiedRealtimeQuote(code="600519", price=9.0, source=RealtimeSource.BIYING)

        manager = DataFetcherManager(fetchers=[fetcher, _Next()])
        config = SimpleNamespace(
            enable_realtime_quote=True, realtime_source_priority="tushare,biying", realtime_cache_ttl=600,
        )
        with patch("src.config.get_config", return_value=config), patch.object(fetcher, "_check_rate_limit"):
            quote = manager.get_realtime_quote("600519")
        self.assertEqual(quote.source, RealtimeSource.BIYING)
        self.assertEqual(quote.fallback_from, "tushare")

    def test_rate_limit_message_is_not_a_permission_error(self):
        self.assertTrue(_is_permission_error(Exception("抱歉，您没有接口访问权限")))
        self.assertFalse(_is_permission_error(Exception("抱歉，您每分钟最多访问该接口2次，权限的具体详情访问：x")))
        self.assertFalse(_is_permission_error(Exception("connection reset")))

    def test_non_permission_failure_keeps_legacy_fallback(self):
        fetcher = _fetcher()
        fetcher._api.rt_k.side_effect = Exception("timeout")
        fake_tushare = MagicMock()
        fake_tushare.get_realtime_quotes.return_value = pd.DataFrame(
            [{"name": "X", "price": "10", "pre_close": "9", "volume": "1000", "amount": "1",
              "high": "10", "low": "9", "open": "9"}]
        )
        with patch.dict(sys.modules, {"tushare": fake_tushare}), patch.object(fetcher, "_check_rate_limit"):
            quote = fetcher.get_realtime_quote("600519")
        self.assertIsNotNone(quote)
        fake_tushare.get_realtime_quotes.assert_called_once()

    def test_zero_close_is_a_miss(self):
        fetcher = _fetcher()
        fetcher._api.rt_k.return_value = _rt_k_frame(close=0.0)
        fake_tushare = MagicMock()
        fake_tushare.get_realtime_quotes.return_value = pd.DataFrame()
        with patch.dict(sys.modules, {"tushare": fake_tushare}), patch.object(fetcher, "_check_rate_limit"):
            self.assertIsNone(fetcher.get_realtime_quote("600519"))

    def test_call_rt_api_wraps_permission_errors(self):
        fetcher = _fetcher()
        fetcher._api.rt_min_daily.side_effect = Exception("没有接口访问权限")
        with patch.object(fetcher, "_check_rate_limit"):
            with self.assertRaises(RealtimePermissionError):
                fetcher.fetch_rt_min_daily("600519.SH")


if __name__ == "__main__":
    unittest.main()
