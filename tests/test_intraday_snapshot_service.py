# -*- coding: utf-8 -*-
"""IntradaySnapshotService: rt_k batching, fallback, TTL cache, single-flight, aggregation."""

import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetchError, DataFetcherManager
from data_provider.biying_fetcher import BiyingFetcher
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from data_provider.tushare_fetcher import RealtimePermissionError, TushareFetcher
from src.services.intraday_snapshot_service import (
    RT_K_BATCH_PATTERNS,
    IntradayDataUnavailable,
    IntradaySnapshotService,
    aggregate_snapshot,
)

CN = timezone(timedelta(hours=8))
COLUMNS = ["ts_code", "name", "pre_close", "high", "open", "low", "close", "vol", "amount",
           "num", "ask_price1", "ask_volume1", "bid_price1", "bid_volume1", "trade_time"]


def _rows(codes, close=11.0, pre_close=10.0, trade_time="2026-11-06 10:32:00"):
    return pd.DataFrame(
        [[c, f"N{c[:6]}", pre_close, close + 0.1, pre_close, close - 0.1, close, 1000, 1e6, 1,
          close, 1, close, 1, trade_time] for c in codes],
        columns=COLUMNS,
    )


class _Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = datetime(2026, 11, 6, 10, 32, 30, tzinfo=CN)  # a Friday, intraday

    def monotonic(self):
        return self.mono

    def now(self):
        return self.wall + timedelta(seconds=self.mono - 1000.0)

    def advance(self, seconds):
        self.mono += seconds


def _tushare(rt_k):
    with patch.object(TushareFetcher, "_init_api", return_value=None):
        fetcher = TushareFetcher()
    fetcher._api = MagicMock()
    fetcher._api.rt_k.side_effect = rt_k
    fetcher._get_trade_dates = lambda end_date=None: ["20261106", "20261105"]
    fetcher._api.daily_basic.return_value = pd.DataFrame(
        {"ts_code": ["600000.SH", "000001.SZ", "300001.SZ", "920001.BJ"], "circ_mv": [100.0, 300.0, 50.0, 10.0]}
    )
    fetcher._check_rate_limit = lambda: None
    return fetcher


def _by_pattern(frames):
    def rt_k(ts_code):
        return frames[ts_code]
    return rt_k


GOOD_FRAMES = {
    "6*.SH": _rows(["600000.SH"], close=11.0),
    "0*.SZ": _rows(["000001.SZ"], close=9.0),
    "3*.SZ": _rows(["300001.SZ"], close=10.0),
    "9*.BJ": _rows(["920001.BJ"], close=10.5),
}


def _service(tushare=None, biying=None, clock=None, ttl=60):
    clock = clock or _Clock()
    manager = MagicMock()
    fetchers = {"TushareFetcher": tushare, "BiyingFetcher": biying}
    manager.get_fetcher.side_effect = lambda name, capability="": fetchers.get(name)
    service = IntradaySnapshotService(manager, ttl_seconds=ttl, now_fn=clock.now, monotonic=clock.monotonic)
    return service, manager, clock


class TestMarketSnapshot(unittest.TestCase):
    def test_rt_k_is_batched_by_board_and_weighted_by_prev_circ_mv(self):
        tushare = _tushare(_by_pattern(GOOD_FRAMES))
        service, _, _ = _service(tushare=tushare)
        snapshot = service.get_market_snapshot()

        called = [c.kwargs["ts_code"] for c in tushare._api.rt_k.call_args_list]
        self.assertEqual(called, list(RT_K_BATCH_PATTERNS))
        self.assertEqual(set(snapshot.quotes), {"600000", "000001", "300001", "920001"})
        self.assertEqual(snapshot.source, "tushare")
        self.assertEqual(snapshot.as_of, "2026-11-06T10:32:00+08:00")
        self.assertFalse(snapshot.is_stale)
        self.assertEqual(snapshot.quotes["000001"].circ_mv, 300.0 * 10000)  # 万元 -> 元
        self.assertTrue(snapshot.quotes["600000"].fetched_at)
        tushare._api.daily_basic.assert_called_once()

    def test_incomplete_tushare_batch_falls_back_to_biying(self):
        frames = dict(GOOD_FRAMES, **{"9*.BJ": _rows([])})
        tushare = _tushare(_by_pattern(frames))
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_all_realtime_quotes.return_value = {
            "600000": UnifiedRealtimeQuote(code="600000", price=11.0, change_pct=10.0, circ_mv=1e9,
                                           provider_timestamp="2026-11-06T10:32:01+08:00", source=RealtimeSource.BIYING),
        }
        service, _, _ = _service(tushare=tushare, biying=biying)
        snapshot = service.get_market_snapshot()
        self.assertEqual(snapshot.source, "biying")
        self.assertEqual(set(snapshot.quotes), {"600000"})

    def test_permission_error_falls_back_to_biying_and_both_failing_raises(self):
        def denied(ts_code):
            raise Exception("抱歉，您没有接口访问权限")

        tushare = _tushare(denied)
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_all_realtime_quotes.side_effect = DataFetchError("Biying HTTP 403")
        service, _, _ = _service(tushare=tushare, biying=biying)
        with self.assertRaises(IntradayDataUnavailable) as caught:
            service.get_market_snapshot()
        self.assertIn("tushare", str(caught.exception))
        self.assertIn("biying", str(caught.exception))
        # permission denial is remembered: the second attempt does not call rt_k again
        calls_after_first = tushare._api.rt_k.call_count
        with self.assertRaises(IntradayDataUnavailable):
            service.get_market_snapshot()
        self.assertEqual(tushare._api.rt_k.call_count, calls_after_first)
        self.assertEqual(calls_after_first, 1)

    def test_ttl_cache_reuses_within_ttl_and_refreshes_after(self):
        tushare = _tushare(_by_pattern(GOOD_FRAMES))
        service, _, clock = _service(tushare=tushare, ttl=60)
        service.get_market_snapshot()
        clock.advance(30)
        service.get_market_snapshot()
        self.assertEqual(tushare._api.rt_k.call_count, 4)  # one full fetch only
        clock.advance(31)
        service.get_market_snapshot()
        self.assertEqual(tushare._api.rt_k.call_count, 8)

    def test_concurrent_callers_share_one_request(self):
        gate = threading.Event()
        started = []

        def slow_rt_k(ts_code):
            started.append(ts_code)
            gate.wait(5)
            return GOOD_FRAMES[ts_code]

        tushare = _tushare(slow_rt_k)
        service, _, _ = _service(tushare=tushare)
        results = []
        threads = [threading.Thread(target=lambda: results.append(service.get_market_snapshot())) for _ in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 6)
        self.assertEqual(sorted(started), sorted(RT_K_BATCH_PATTERNS))  # each batch requested exactly once
        self.assertTrue(all(r.source == "tushare" for r in results))

    def test_failed_refresh_serves_previous_snapshot_marked_stale(self):
        state = {"fail": False}

        def rt_k(ts_code):
            if state["fail"]:
                raise Exception("network down")
            return GOOD_FRAMES[ts_code]

        tushare = _tushare(rt_k)
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_all_realtime_quotes.side_effect = DataFetchError("down")
        service, _, clock = _service(tushare=tushare, biying=biying)
        service.get_market_snapshot()
        state["fail"] = True
        clock.advance(90)
        snapshot = service.get_market_snapshot()
        self.assertTrue(snapshot.is_stale)
        self.assertEqual(snapshot.stale_reason, "refresh_failed")
        clock.advance(600)  # too old to serve at all
        with self.assertRaises(IntradayDataUnavailable):
            service.get_market_snapshot()

    def test_provider_delay_while_market_open_is_stale(self):
        frames = {k: _rows([v.iloc[0]["ts_code"]], trade_time="2026-11-06 10:20:00") for k, v in GOOD_FRAMES.items()}
        service, _, _ = _service(tushare=_tushare(_by_pattern(frames)))
        snapshot = service.get_market_snapshot()
        self.assertTrue(snapshot.is_stale)
        self.assertEqual(snapshot.stale_reason, "provider_delay")

    def test_service_has_no_daily_table_writes(self):
        with patch("src.storage.DatabaseManager.save_daily_data") as save:
            service, _, _ = _service(tushare=_tushare(_by_pattern(GOOD_FRAMES)))
            service.get_market_snapshot()
        save.assert_not_called()
        source = open(sys.modules[IntradaySnapshotService.__module__].__file__, encoding="utf-8").read()
        self.assertNotIn("save_daily_data", source)
        self.assertNotIn("stock_daily", source.replace("never written to ``stock_daily``", ""))


class TestAggregate(unittest.TestCase):
    def test_cap_weighted_and_equal_weight_over_code_list(self):
        service, _, _ = _service(tushare=_tushare(_by_pattern(GOOD_FRAMES)))
        snapshot = service.get_market_snapshot()
        # changes: 600000 +10%, 000001 -10%, 300001 0%, 920001 +5%; circ_mv 100/300/50/10 (万元)
        everything = aggregate_snapshot(snapshot)
        self.assertEqual(everything.matched, 4)
        self.assertEqual((everything.up_count, everything.down_count, everything.flat_count), (2, 1, 1))
        self.assertAlmostEqual(everything.equal_weight_change_pct, 1.25)
        self.assertAlmostEqual(everything.cap_weighted_change_pct, (100 * 10 + 300 * -10 + 50 * 0 + 10 * 5) / 460)
        self.assertEqual(everything.weight_coverage, 1.0)
        self.assertEqual(everything.source, "tushare")

        subset = aggregate_snapshot(snapshot, ["600000.SH", "sz000001", "999999"])
        self.assertEqual((subset.requested, subset.matched), (3, 2))
        self.assertAlmostEqual(subset.cap_weighted_change_pct, (100 * 10 + 300 * -10) / 400)

    def test_external_weights_override_and_missing_weights_reduce_coverage(self):
        service, _, _ = _service(tushare=_tushare(_by_pattern(GOOD_FRAMES)))
        snapshot = service.get_market_snapshot()
        stats = aggregate_snapshot(snapshot, ["600000", "000001"], weights={"600000": 1.0})
        self.assertAlmostEqual(stats.cap_weighted_change_pct, 10.0)
        self.assertAlmostEqual(stats.weight_coverage, 0.5)
        none = aggregate_snapshot(snapshot, ["600000"], weights={})
        self.assertIsNone(none.cap_weighted_change_pct)


class TestQuoteAndBars(unittest.TestCase):
    def test_quote_uses_fresh_market_snapshot_without_new_request(self):
        tushare = _tushare(_by_pattern(GOOD_FRAMES))
        service, manager, _ = _service(tushare=tushare)
        service.get_market_snapshot()
        result = service.get_quote("600000")
        self.assertEqual(result.source, "tushare")
        self.assertEqual(result.as_of, "2026-11-06T10:32:00+08:00")
        manager.get_realtime_quote.assert_not_called()

    def test_quote_without_snapshot_goes_through_manager_chain_and_is_cached(self):
        quote = UnifiedRealtimeQuote(code="600519", price=1500.0, source=RealtimeSource.BIYING,
                                     provider_timestamp="2026-11-06T10:32:10+08:00")
        service, manager, clock = _service()
        manager.get_realtime_quote.return_value = quote
        first = service.get_quote("600519")
        second = service.get_quote("SH600519")
        self.assertEqual(manager.get_realtime_quote.call_count, 1)
        self.assertEqual(first.source, "biying")
        self.assertEqual(first.as_of, "2026-11-06T10:32:10+08:00")
        clock.advance(61)
        service.get_quote("600519")
        self.assertEqual(manager.get_realtime_quote.call_count, 2)
        self.assertIs(second.quote, quote)

    def test_quote_unavailable_raises(self):
        service, manager, _ = _service()
        manager.get_realtime_quote.return_value = None
        with self.assertRaises(IntradayDataUnavailable):
            service.get_quote("600519")

    def test_bars_from_tushare_rt_min_daily_then_biying_fallback(self):
        tushare = _tushare(_by_pattern(GOOD_FRAMES))
        tushare._api.rt_min_daily.return_value = pd.DataFrame({
            "ts_code": ["600000.SH"] * 2, "time": ["2026-11-06 09:40:00", "2026-11-06 09:35:00"],
            "open": [10.0, 9.9], "close": [10.1, 10.0], "high": [10.2, 10.0], "low": [9.9, 9.8],
            "vol": [2000, 1000], "amount": [2e4, 1e4],
        })
        service, _, _ = _service(tushare=tushare)
        result = service.get_intraday_bars("600000")
        tushare._api.rt_min_daily.assert_called_once_with(ts_code="600000.SH", freq="5MIN")
        self.assertEqual(result.source, "tushare")
        self.assertEqual([b["time"][11:16] for b in result.bars], ["09:35", "09:40"])
        self.assertEqual(result.bars[0]["volume"], 1000)
        self.assertEqual(result.as_of, "2026-11-06T09:40:00+08:00")

        # permission missing -> Biying 5-minute bars (volume already normalised to shares)
        tushare2 = _tushare(_by_pattern(GOOD_FRAMES))
        tushare2._api.rt_min_daily.side_effect = Exception("没有接口访问权限")
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_intraday_bars.return_value = [
            {"time": "2026-11-05T15:00:00+08:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "amount": 1},
            {"time": "2026-11-06T09:35:00+08:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 100, "amount": 1},
        ]
        service2, _, _ = _service(tushare=tushare2, biying=biying)
        result2 = service2.get_intraday_bars("600000")
        self.assertEqual(result2.source, "biying")
        self.assertEqual(len(result2.bars), 1)  # only the latest session is returned


def _bar(time_text, close=10.0):
    return {"time": time_text, "open": close, "high": close, "low": close, "close": close,
            "volume": 100, "amount": 1000.0}


class TestSessionBars(unittest.TestCase):
    """Bars of one given trading day (double-click on a daily candle)."""

    def test_past_date_uses_biying_history_and_is_cached(self):
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_history_bars.return_value = [
            _bar("2026-11-05T09:35:00+08:00"), _bar("2026-11-05T15:00:00+08:00", 10.5),
        ]
        service, _, clock = _service(biying=biying)

        first = service.get_session_bars("600000", "2026-11-05")
        clock.advance(3600)  # far beyond the 60s live TTL: a finished session stays cached
        second = service.get_session_bars("SH600000", "2026-11-05")

        biying.get_history_bars.assert_called_once_with("600000", "2026-11-05")
        self.assertEqual(first.trade_date, "2026-11-05")
        self.assertEqual(first.source, "biying")
        self.assertEqual(first.as_of, "2026-11-05T15:00:00+08:00")
        self.assertFalse(first.is_stale)
        self.assertEqual(len(second.bars), 2)

    def test_past_date_never_touches_tushare_live_chain(self):
        tushare = _tushare(_by_pattern(GOOD_FRAMES))
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_history_bars.return_value = [_bar("2026-11-05T09:35:00+08:00")]
        service, _, _ = _service(tushare=tushare, biying=biying)
        service.get_session_bars("600000", "2026-11-05")
        tushare._api.rt_min_daily.assert_not_called()

    def test_today_uses_live_chain_and_keeps_only_that_day(self):
        tushare = _tushare(_by_pattern(GOOD_FRAMES))
        tushare._api.rt_min_daily.return_value = pd.DataFrame({
            "ts_code": ["600000.SH"], "time": ["2026-11-06 09:35:00"],
            "open": [10.0], "close": [10.1], "high": [10.2], "low": [9.9], "vol": [1000], "amount": [1e4],
        })
        biying = MagicMock(spec=BiyingFetcher)
        service, _, _ = _service(tushare=tushare, biying=biying)
        result = service.get_session_bars("600000", "2026-11-06")
        self.assertEqual(result.source, "tushare")
        self.assertEqual(result.trade_date, "2026-11-06")
        biying.get_history_bars.assert_not_called()

    def test_today_falls_back_to_history_when_live_chain_has_no_bars_for_the_day(self):
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_intraday_bars.return_value = [_bar("2026-11-05T15:00:00+08:00")]  # latest session is yesterday
        biying.get_history_bars.return_value = [_bar("2026-11-06T09:35:00+08:00")]
        service, _, _ = _service(biying=biying)
        result = service.get_session_bars("600000", "2026-11-06")
        self.assertEqual(result.source, "biying")
        self.assertEqual(result.bars[0]["time"], "2026-11-06T09:35:00+08:00")
        biying.get_history_bars.assert_called_once_with("600000", "2026-11-06")

    def test_unavailable_when_history_is_empty_or_failing(self):
        biying = MagicMock(spec=BiyingFetcher)
        biying.get_history_bars.return_value = []
        service, _, _ = _service(biying=biying)
        with self.assertRaises(IntradayDataUnavailable):
            service.get_session_bars("600000", "2026-11-05")

        failing = MagicMock(spec=BiyingFetcher)
        failing.get_history_bars.side_effect = DataFetchError("Biying HTTP 500")
        service2, _, _ = _service(biying=failing)
        with self.assertRaises(IntradayDataUnavailable) as caught:
            service2.get_session_bars("600000", "2026-11-05")
        self.assertIn("Biying HTTP 500", str(caught.exception))

    def test_missing_biying_is_unavailable_not_a_crash(self):
        service, _, _ = _service()
        with self.assertRaises(IntradayDataUnavailable):
            service.get_session_bars("600000", "2026-11-05")

    def test_malformed_and_future_dates_are_rejected_before_any_fetch(self):
        biying = MagicMock(spec=BiyingFetcher)
        service, _, _ = _service(biying=biying)
        for bad in ("2026/11/05", "yesterday", "2026-13-40", "2026-11-07"):
            with self.subTest(date=bad), self.assertRaises(ValueError):
                service.get_session_bars("600000", bad)
        biying.get_history_bars.assert_not_called()


class TestPermissionError(unittest.TestCase):
    def test_realtime_permission_error_is_a_fetch_error(self):
        self.assertTrue(issubclass(RealtimePermissionError, DataFetchError))


if __name__ == "__main__":
    unittest.main()
