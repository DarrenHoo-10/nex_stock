# -*- coding: utf-8 -*-
"""BiyingFetcher: parsing, unit normalisation, fallback order, licence masking (HTTP mocked)."""

import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetchError, DataFetcherManager
from data_provider.biying_fetcher import BiyingFetcher
from data_provider.realtime_types import RealtimeSource
from src.config import Config

LICENCE = "SECRET-LICENCE-1234"

PUBLIC_ROW = {
    "o": 11.69, "fm": 0.17, "h": 11.71, "hs": 0.5, "lb": 0.7, "l": 11.55, "lt": 225881388026.0,
    "pe": 4.26, "pc": -0.17, "p": 11.64, "sz": 225884887825.0, "cje": 1131033823.93, "ud": -0.02,
    "v": 973969, "yc": 11.66, "zf": 1.37, "zs": 0.17, "sjl": 0.54, "zdf60": 0.0, "zdfnc": -0.51,
    "t": "2026-11-06 10:32:05",
}
BROKER_ROW = {
    "p": 11.64, "o": 11.69, "h": 11.71, "l": 11.55, "yc": 11.66, "cje": 1131033823.93,
    "v": 973969, "pv": 97396900, "t": "2026-11-06 10:32:06", "ud": -0.02, "pc": -0.17,
    "zf": 1.37, "pe": 4.26, "tr": 0.5, "pb_ratio": 0.54, "tv": 12,
}


def _response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


class _FakeHttp:
    """Route ``requests.get`` calls by URL substring; records every URL."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        self.urls.append(url)
        self.calls.append((url, params, timeout))
        for needle, result in self.routes.items():
            if needle in url:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"unexpected URL {url}")


def _patch_http(routes):
    fake = _FakeHttp(routes)
    return fake, patch("data_provider.biying_fetcher.requests.get", fake)


class TestBiyingQuote(unittest.TestCase):
    def setUp(self):
        self.fetcher = BiyingFetcher(licence=LICENCE)

    def test_broker_quote_normalises_units_and_fields(self):
        fake, ctx = _patch_http({"hsstock/real/time/": _response(BROKER_ROW)})
        with ctx:
            quote = self.fetcher.get_realtime_quote("600519")
        self.assertEqual(fake.urls, [f"https://api.biyingapi.com/hsstock/real/time/600519.SH/{LICENCE}"])
        self.assertEqual(quote.source, RealtimeSource.BIYING)
        self.assertEqual(quote.code, "600519")
        self.assertEqual(quote.volume, 97396900)  # 手 -> 股 (v * 100); ambiguous pv/tv unused
        self.assertAlmostEqual(quote.amount, 1131033823.93)
        self.assertAlmostEqual(quote.turnover_rate, 0.5)
        self.assertAlmostEqual(quote.pb_ratio, 0.54)
        self.assertAlmostEqual(quote.pre_close, 11.66)
        self.assertEqual(quote.provider_timestamp, "2026-11-06T10:32:06+08:00")

    def test_public_fallback_carries_market_cap_in_yuan(self):
        fake, ctx = _patch_http({
            "hsstock/real/time/": _response({}, status=500),
            "hsrl/ssjy/": _response([PUBLIC_ROW]),
        })
        with ctx:
            quote = self.fetcher.get_realtime_quote("000001")
        self.assertEqual(len(fake.urls), 2)
        self.assertIn("hsstock/real/time/000001.SZ/", fake.urls[0])
        self.assertEqual(fake.urls[1], f"https://api.biyingapi.com/hsrl/ssjy/000001/{LICENCE}")
        self.assertAlmostEqual(quote.circ_mv, 225881388026.0)  # already yuan
        self.assertAlmostEqual(quote.total_mv, 225884887825.0)
        self.assertAlmostEqual(quote.volume_ratio, 0.7)
        self.assertEqual(quote.volume, 97396900)

    def test_bse_and_sh_suffixes(self):
        fake, ctx = _patch_http({"hsstock/real/time/": _response(BROKER_ROW)})
        with ctx:
            self.fetcher.get_realtime_quote("920748")
            self.fetcher.get_realtime_quote("SH510300")
        self.assertIn("920748.BJ", fake.urls[0])
        self.assertIn("510300.SH", fake.urls[1])

    def test_non_a_share_and_missing_licence_are_skipped(self):
        fake, ctx = _patch_http({})
        with ctx:
            self.assertIsNone(self.fetcher.get_realtime_quote("AAPL"))
            self.assertIsNone(self.fetcher.get_realtime_quote("hk00700"))
            self.assertIsNone(BiyingFetcher(licence="").get_realtime_quote("600519"))
        self.assertEqual(fake.urls, [])
        self.assertFalse(BiyingFetcher(licence="").is_available())

    def test_available_only_for_realtime_capability(self):
        self.assertTrue(self.fetcher.is_available_for_request("realtime_quote"))
        self.assertFalse(self.fetcher.is_available_for_request("daily_data"))
        self.assertFalse(self.fetcher.is_available_for_request(""))

    def test_multi_quote_chunks_by_twenty(self):
        rows = [dict(PUBLIC_ROW, dm=f"{600000 + i}") for i in range(20)]
        rows2 = [dict(PUBLIC_ROW, dm=f"{600020 + i}") for i in range(5)]
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params["stock_codes"].split(","))
            return _response(rows if len(calls) == 1 else rows2)

        with patch("data_provider.biying_fetcher.requests.get", fake_get):
            result = self.fetcher.get_realtime_quotes([f"{600000 + i}" for i in range(25)] + ["600000"])
        self.assertEqual([len(c) for c in calls], [20, 5])
        self.assertEqual(len(result), 25)
        self.assertEqual(result["600003"].source, RealtimeSource.BIYING)

    def test_full_market_uses_bulk_host_and_local_rate_limit(self):
        rows = [dict(PUBLIC_ROW, dm="000001"), dict(PUBLIC_ROW, dm="600519")]
        fake, ctx = _patch_http({"hsrl/real/all/": _response(rows)})
        with ctx:
            for _ in range(3):
                result = self.fetcher.get_all_realtime_quotes()
            with self.assertRaises(DataFetchError):
                self.fetcher.get_all_realtime_quotes()
        self.assertEqual(set(result), {"000001", "600519"})
        self.assertEqual(len(fake.urls), 3)  # 4th call blocked locally (bulk limit ~3/min)
        self.assertEqual(fake.urls[0], f"https://vip.biyingapi.com/hsrl/real/all/{LICENCE}")

    def test_five_level_and_bars_and_index(self):
        five = {"ps": [11.65, 11.66], "pb": [11.64, 11.63], "vs": [10, 20], "vb": [30, 40], "t": "2026-11-06 10:32:06"}
        bars = [
            {"t": "2026-11-06 09:35:00", "o": 10.9, "h": 11.0, "l": 10.8, "c": 11.0, "v": 2000, "a": 220000.0},
            {"t": "2026-11-06 09:40:00", "o": 11.0, "h": 11.1, "l": 10.9, "c": 11.05, "v": 1000, "a": 110500.0},
        ]
        fake, ctx = _patch_http({
            "hsstock/real/five/": _response(five),
            "hsstock/latest/": _response(bars),
            "hsindex/real/time/": _response({"p": 3300.5, "pc": 0.3, "v": 5000000, "t": "2026-11-06 10:32:00"}),
        })
        with ctx:
            book = self.fetcher.get_five_level("600519")
            got = self.fetcher.get_intraday_bars("600519", limit=2)
            index = self.fetcher.get_index_quote("000001.SH")
        self.assertEqual(book["asks"][0], {"price": 11.65, "volume": 1000})
        self.assertEqual(book["bids"][1], {"price": 11.63, "volume": 4000})
        self.assertIn(f"hsstock/latest/600519.SH/5/n/{LICENCE}", fake.urls[1])
        self.assertEqual(fake.calls[1][1], {"lt": 2})
        self.assertEqual(got[0]["volume"], 200000)  # 手 -> 股
        self.assertEqual(got[0]["time"], "2026-11-06T09:35:00+08:00")
        self.assertEqual(index.code, "000001.SH")
        self.assertEqual(index.volume, 500000000)


class TestBiyingHistoryBars(unittest.TestCase):
    def setUp(self):
        self.fetcher = BiyingFetcher(licence=LICENCE)

    def test_history_bars_request_one_day_and_normalise_rows(self):
        rows = [
            {"t": "2026-11-05 15:00:00", "o": 9.0, "h": 9.1, "l": 8.9, "c": 9.0, "v": 10, "a": 9000.0, "pc": 9.0},
            {"t": "2026-11-06 09:35:00", "o": 8.51, "h": 8.64, "l": 8.35, "c": 8.51, "v": 123074.0,
             "a": 104632755.19, "pc": 8.86},
            {"t": "2026-11-06 09:40:00", "o": 8.5, "h": 8.6, "l": 8.4, "c": None, "v": 5, "a": 1.0},
        ]
        fake, ctx = _patch_http({"hsstock/history/": _response(rows)})
        with ctx:
            got = self.fetcher.get_history_bars("002029", "2026-11-06")
        self.assertEqual(fake.urls[0], f"https://api.biyingapi.com/hsstock/history/002029.SZ/5/n/{LICENCE}")
        self.assertEqual(fake.calls[0][1], {"st": "20261106", "et": "20261106"})
        # other-day rows and rows without a close are dropped
        self.assertEqual([bar["time"] for bar in got], ["2026-11-06T09:35:00+08:00"])
        self.assertEqual(got[0]["volume"], 12307400)  # 手 -> 股
        self.assertEqual(got[0]["amount"], 104632755.19)

    def test_annual_ticks_are_bucketed_without_inventing_trades(self):
        day = "2026-11-06"
        # Newest first, like the provider. Volume is shares and same-second prints are distinct.
        rows = [{"d": day, "t": stamp, "p": price, "v": volume} for stamp, price, volume in [
            ("15:00:00", 12, 300), ("12:15:00", 100, 1), ("09:32:00", 8, 0),
            ("09:31:00", 11, 500), ("09:31:00", 10, 100), ("09:30:04", 9, 200),
            ("09:25:00", 8, 1000),
        ]]
        rows.append({"d": "2026-11-05", "t": "15:00:00", "p": 99, "v": 100})
        for today, path in [(True, "002029"), (False, "20261106/002029")]:
            fake, ctx = _patch_http({"hsrl/zbjy/": _response(rows)})
            with ctx:
                session = self.fetcher.get_tick_session("002029", day, today)
            self.assertEqual(fake.urls, [f"https://api.biyingapi.com/hsrl/zbjy/{path}/{LICENCE}"])
            self.assertEqual(session.auction, [{"time": "09:25:00", "price": 8, "volume": 1000}])
            self.assertEqual(session.bars, [
                {"time": f"{day}T09:31:00+08:00", "open": 9, "high": 11, "low": 9, "close": 11,
                 "volume": 800, "amount": 8300},
                {"time": f"{day}T15:00:00+08:00", "open": 12, "high": 12, "low": 12, "close": 12,
                 "volume": 300, "amount": 3600},
            ])

    def test_before_tick_archive_has_no_bars_and_needs_no_request(self):
        fake, ctx = _patch_http({})
        with ctx:
            session = self.fetcher.get_tick_session("002029", "2026-08-12", False)
        self.assertEqual(session.bars, [])
        self.assertEqual(fake.urls, [])

    def test_history_bars_error_does_not_leak_licence(self):
        exc = requests.ConnectionError(f"Max retries exceeded with url: /hsstock/history/002029.SZ/5/n/{LICENCE}")
        _, ctx = _patch_http({"hsstock/history/": exc})
        with ctx, self.assertRaises(DataFetchError) as caught:
            self.fetcher.get_history_bars("002029", "2026-11-06")
        self.assertNotIn(LICENCE, str(caught.exception))


class TestBiyingLicenceMasking(unittest.TestCase):
    def setUp(self):
        self.fetcher = BiyingFetcher(licence=LICENCE)

    def test_request_exception_url_is_masked_and_not_chained(self):
        exc = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: /hsrl/ssjy/000001/{LICENCE}")
        _, ctx = _patch_http({"hsstock/real/time/": exc, "hsrl/ssjy/": exc})
        with ctx, self.assertRaises(DataFetchError) as caught:
            self.fetcher.get_realtime_quote("000001")
        self.assertNotIn(LICENCE, str(caught.exception))
        self.assertIn("***", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_error_payload_and_http_status_do_not_leak_licence(self):
        _, ctx = _patch_http({"hsstock/real/time/": _response({"error": f"bad licence {LICENCE}"}),
                              "hsrl/ssjy/": _response({"msg": f"bad {LICENCE}"})})
        with ctx, self.assertRaises(DataFetchError) as caught:
            self.fetcher.get_realtime_quote("000001")
        self.assertNotIn(LICENCE, str(caught.exception))

    def test_urllib3_debug_log_is_masked(self):
        logger = logging.getLogger("urllib3.connectionpool")
        stream = []

        class _Capture(logging.Handler):
            def emit(self, record):
                stream.append(record.getMessage())

        handler = _Capture(level=logging.DEBUG)
        logger.addHandler(handler)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            logger.debug('%s://%s "GET %s HTTP/1.1" %s', "https", "api.biyingapi.com", f"/hsrl/ssjy/000001/{LICENCE}", 200)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertEqual(len(stream), 1)
        self.assertNotIn(LICENCE, stream[0])
        self.assertIn("***", stream[0])


class TestBiyingPriority(unittest.TestCase):
    ENV = {"TUSHARE_TOKEN": "", "BIYING_LICENCE": "", "REALTIME_SOURCE_PRIORITY": ""}

    def _resolve(self, **env):
        merged = dict(self.ENV, **env)
        with patch.dict(os.environ, merged, clear=True):
            return Config._resolve_realtime_source_priority()

    def test_default_order_when_configured(self):
        free = "tencent,akshare_sina,efinance,akshare_em"
        self.assertEqual(self._resolve(), free)
        self.assertEqual(self._resolve(TUSHARE_TOKEN="t"), f"tushare,{free}")
        self.assertEqual(self._resolve(BIYING_LICENCE="l"), f"biying,{free}")
        self.assertEqual(self._resolve(TUSHARE_TOKEN="t", BIYING_LICENCE="l"), f"tushare,biying,{free}")

    def test_explicit_priority_is_respected(self):
        self.assertEqual(
            self._resolve(TUSHARE_TOKEN="t", BIYING_LICENCE="l", REALTIME_SOURCE_PRIORITY="tencent,biying"),
            "tencent,biying",
        )

    def test_config_loads_licence_and_timeout(self):
        with patch.dict(os.environ, {"BIYING_LICENCE": "abc", "BIYING_TIMEOUT_SECONDS": "5"}, clear=True):
            config = Config._load_from_env()
        self.assertEqual(config.biying_licence, "abc")
        self.assertEqual(config.biying_timeout_seconds, 5.0)
        self.assertEqual(config.intraday_snapshot_ttl_seconds, 60)

    def test_manager_walks_tushare_then_biying_then_free(self):
        order = []

        def fetcher(name, result):
            obj = MagicMock()
            obj.name = name
            obj.priority = 1
            del obj.is_available_for_request
            del obj.is_available

            def get(code, **kw):
                order.append(name)
                return result

            obj.get_realtime_quote = get
            return obj

        from data_provider.realtime_types import UnifiedRealtimeQuote

        good = UnifiedRealtimeQuote(code="600519", price=10.0, source=RealtimeSource.TENCENT)
        manager = DataFetcherManager(fetchers=[
            fetcher("TushareFetcher", None),
            fetcher("BiyingFetcher", None),
            fetcher("AkshareFetcher", good),
        ])
        config = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="tushare,biying,tencent,akshare_sina",
            realtime_cache_ttl=600,
        )
        with patch("src.config.get_config", return_value=config):
            quote = manager.get_realtime_quote("600519")
        self.assertEqual(order[:3], ["TushareFetcher", "BiyingFetcher", "AkshareFetcher"])  # later entries are field supplements
        self.assertEqual(quote.source, RealtimeSource.TENCENT)
        self.assertEqual(quote.fallback_from, "tushare")

    def test_daily_routes_never_use_biying(self):
        self.assertEqual(DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT["BiyingFetcher"], set())


if __name__ == "__main__":
    unittest.main()
