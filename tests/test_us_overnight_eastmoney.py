# -*- coding: utf-8 -*-
"""US overnight service, Eastmoney (AKShare) source: parsing, session date, source order, overwrite priority, timeout.

Offline: AKShare is replaced by a fake module at the call boundary (``stock_us_hist`` /
``index_global_spot_em``), shaped like the columns verified live with akshare 1.19.3; Tushare and
yfinance are the fakes from ``tests/test_us_overnight_service.py``. No network.
"""

from __future__ import annotations

import threading
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from data_provider.base import DataFetchError
from data_provider.tushare_market_data import (
    US_DAILY_PAGE_CAP,
    IncompleteMarketDataError,
    TushareMarketDataMixin,
    TushareRateLimitedError,
)

from src.services import news_akshare_source as ak_source
from src.services.us_leaders import UsLeader, UsMoveThresholds, load_us_leaders
from src.services.us_overnight_service import (
    SOURCE_EASTMONEY_INDEX,
    SOURCE_EASTMONEY_STOCK,
    SOURCE_TUSHARE_INDEX,
    SOURCE_TUSHARE_STOCK,
    SOURCE_YFINANCE,
    SOURCES_ALL,
    SOURCES_PREOPEN,
    UsOvernightService,
    _frame_from_eastmoney_hist,
    _frame_from_eastmoney_index_hist,
    _frame_from_eastmoney_spot,
    source_rank,
)
from src.storage import DatabaseManager
from tests.test_us_overnight_service import (
    CN,
    FRIDAY,
    FRIDAY_BOARD_RUN,
    MONDAY_PREOPEN,
    FakeTushare,
    FakeYfinance,
    closes_for,
    sessions,
    stored,
    tushare_frame,
    yfinance_frame,
)

NVDA = UsLeader("NVDA", "英伟达", "stock", ("算力",), em="105.NVDA")
TSM = UsLeader("TSM", "台积电", "stock", ("半导体",), em="106.TSM")
SOXX = UsLeader("SOXX", "费城半导体ETF", "etf", ("半导体",), em="105.SOXX")
SPX = UsLeader("SPX", "标普500指数", "index", em="SPX")
NDX = UsLeader("NDX", "纳斯达克100指数", "index", em="NDX")
NVDA_PLAIN = UsLeader("NVDA", "英伟达", "stock", ("算力",))  # user config without ``em``

HIST_COLUMNS = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]
SPOT_COLUMNS = ["序号", "代码", "名称", "最新价", "涨跌额", "涨跌幅", "开盘价", "最高价", "最低价", "昨收价", "振幅", "最新行情时间"]


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _clean_inflight():
    ak_source.reset_inflight_for_tests()
    yield
    ak_source.reset_inflight_for_tests()


def hist_frame(days: List[date], closes: List[float], *, pct_last: Optional[float] = None, as_text: bool = True) -> pd.DataFrame:
    """``stock_us_hist`` shaped rows: date strings, 涨跌额 = close - previous close, 涨跌幅 in percent."""
    rows = []
    for index, (day, close) in enumerate(zip(days, closes)):
        prev = closes[index - 1] if index else close - 1.0
        last = index == len(days) - 1
        rows.append({
            "日期": day.isoformat() if as_text else day,
            "开盘": close - 0.5, "收盘": close, "最高": close + 1, "最低": close - 1,
            "成交量": 1000 + index, "成交额": 1000.0 * close, "振幅": 1.0,
            "涨跌幅": pct_last if (last and pct_last is not None) else round((close / prev - 1) * 100, 2),
            "涨跌额": round(close - prev, 2), "换手率": 0.1,
        })
    return pd.DataFrame(rows, columns=HIST_COLUMNS)


def spot_frame(*rows: Dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=SPOT_COLUMNS)


def spot_row(code: str, name: str, last: float, pct: float, prev: float, stamp: str, *, open_: Optional[float] = None) -> Dict[str, Any]:
    return {
        "序号": 1, "代码": code, "名称": name, "最新价": last, "涨跌额": round(last - prev, 2), "涨跌幅": pct,
        "开盘价": open_ if open_ is not None else prev, "最高价": max(last, prev) + 1, "最低价": min(last, prev) - 1,
        "昨收价": prev, "振幅": 1.0, "最新行情时间": stamp,
    }


# Friday 2026-11-06 closed at 16:00 EST = Saturday 2026-11-07 05:00 Beijing time
SPOT_AFTER_FRIDAY = spot_frame(
    spot_row("SPX", "标普500", 5050.0, 1.0, 5000.0, "2026-11-07 05:00:00", open_=5010.0),
    spot_row("NDX", "纳斯达克", 18200.0, -1.8, 18533.0, "2026-11-07 05:00:00"),
    spot_row("DJIA", "道琼斯", 40000.0, 0.2, 39920.0, "2026-11-07 05:00:00"),
)


class FakeAkshare:
    """The two AKShare interfaces the Eastmoney source calls, recording every call."""

    def __init__(self, hist: Optional[Dict[str, Any]] = None, spot: Any = None, index_hist: Optional[Dict[str, Any]] = None):
        self.hist = hist or {}
        self.spot = spot
        self.index_hist = index_hist or {}
        self.hist_calls: List[Dict[str, Any]] = []
        self.index_hist_calls: List[str] = []
        self.spot_calls = 0

    def stock_us_hist(self, symbol, period="daily", start_date="", end_date="", adjust=""):
        self.hist_calls.append({"symbol": symbol, "period": period, "start_date": start_date, "end_date": end_date, "adjust": adjust})
        value = self.hist.get(symbol, pd.DataFrame())
        if isinstance(value, Exception):
            raise value
        return value

    def index_global_hist_em(self, symbol="美元指数"):
        self.index_hist_calls.append(symbol)
        value = self.index_hist.get(symbol, pd.DataFrame())
        if isinstance(value, Exception):
            raise value
        return value

    def index_global_spot_em(self):
        self.spot_calls += 1
        if isinstance(self.spot, Exception):
            raise self.spot
        return self.spot if self.spot is not None else pd.DataFrame()


def make_service(db, leaders, *, ak=None, tushare=None, yfinance=None, timeout: float = 5.0) -> UsOvernightService:
    return UsOvernightService(
        SimpleNamespace(), leaders=leaders, thresholds=UsMoveThresholds(4.0, 1.5), tushare_fetcher=tushare,
        yfinance_fetcher=yfinance, akshare_module=ak, em_timeout_sec=timeout, db=db,
    )


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
def test_hist_frame_parses_the_verified_stock_columns():
    days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
    raw = hist_frame(days, [180.0, 181.5, 181.62, 186.64], pct_last=2.78)
    frame = _frame_from_eastmoney_hist(raw)
    assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg", "pre_close"]
    last = frame.iloc[-1]
    assert last["date"] == date(2026, 11, 6) and last["close"] == 186.64 and last["pct_chg"] == 2.78
    assert last["pre_close"] == pytest.approx(181.62)  # close - 涨跌额
    assert frame.iloc[0]["pre_close"] == pytest.approx(179.0)  # the first row has a previous close too (no bar is dropped)
    assert len(frame) == 4
    # dates may arrive as date objects, numbers as strings
    raw2 = hist_frame(days, [10.0, 11.0, 12.0, 13.0], as_text=False)
    raw2["收盘"] = raw2["收盘"].astype(str)
    assert list(_frame_from_eastmoney_hist(raw2)["close"]) == [10.0, 11.0, 12.0, 13.0]


def test_hist_frame_falls_back_to_the_previous_bar_and_computes_a_missing_pct():
    raw = hist_frame([date(2026, 11, 5), date(2026, 11, 6)], [100.0, 104.0])
    raw["涨跌额"] = float("nan")
    raw["涨跌幅"] = float("nan")
    frame = _frame_from_eastmoney_hist(raw)
    assert pd.isna(frame.iloc[0]["pre_close"]) and pd.isna(frame.iloc[0]["pct_chg"])
    assert frame.iloc[1]["pre_close"] == 100.0 and frame.iloc[1]["pct_chg"] == pytest.approx(4.0)
    assert _frame_from_eastmoney_hist(pd.DataFrame()).empty
    assert _frame_from_eastmoney_hist(pd.DataFrame({"foo": [1]})).empty
    assert _frame_from_eastmoney_hist(None).empty


def test_spot_frame_builds_one_bar_with_the_new_york_session_date():
    frame = _frame_from_eastmoney_spot(SPOT_AFTER_FRIDAY, "SPX")
    assert len(frame) == 1
    bar = frame.iloc[0]
    assert bar["date"] == FRIDAY  # 2026-11-07 05:00 Beijing = 2026-11-06 16:00 New York
    assert (bar["open"], bar["close"], bar["pre_close"], bar["pct_chg"]) == (5010.0, 5050.0, 5000.0, 1.0)
    assert pd.isna(bar["volume"]) and pd.isna(bar["amount"])
    assert _frame_from_eastmoney_spot(SPOT_AFTER_FRIDAY, "ndx").iloc[0]["pct_chg"] == -1.8  # code match is case-insensitive
    assert _frame_from_eastmoney_spot(SPOT_AFTER_FRIDAY, "SOX").empty  # not in the snapshot
    assert _frame_from_eastmoney_spot(pd.DataFrame(), "SPX").empty


def test_session_date_follows_new_york_dst_and_the_quote_time():
    # summer time: the 16:00 EDT close is 04:00 Beijing time of the next day
    summer = spot_frame(spot_row("SPX", "标普500", 5050.0, 1.0, 5000.0, "2026-07-11 04:00:00"))
    assert _frame_from_eastmoney_spot(summer, "SPX").iloc[0]["date"] == date(2026, 7, 10)
    # an early-close day (13:00 New York = 02:00 Beijing in winter) is still a closing quote
    early = spot_frame(spot_row("SPX", "标普500", 5050.0, 1.0, 5000.0, "2026-11-28 02:00:00"))
    assert _frame_from_eastmoney_spot(early, "SPX").iloc[0]["date"] == date(2026, 11, 27)
    # a snapshot taken during the session (23:30 Beijing = 10:30 New York) is intraday: no bar
    live = spot_frame(spot_row("SPX", "标普500", 5050.0, 0.4, 5030.0, "2026-11-06 23:30:00"))
    assert _frame_from_eastmoney_spot(live, "SPX").empty
    # an unparsable timestamp or price yields no bar instead of an exception
    broken = spot_frame(spot_row("SPX", "标普500", 5050.0, 1.0, 5000.0, "not a time"))
    assert _frame_from_eastmoney_spot(broken, "SPX").empty
    no_price = spot_frame(spot_row("SPX", "标普500", float("nan"), 1.0, 5000.0, "2026-11-07 05:00:00"))
    assert _frame_from_eastmoney_spot(no_price, "SPX").empty


# ----------------------------------------------------------------------
# refresh through the AKShare boundary
# ----------------------------------------------------------------------
def test_preopen_run_stores_eastmoney_stocks_etfs_and_indices(db):
    days = sessions(FRIDAY)
    ak = FakeAkshare(
        hist={
            "105.NVDA": hist_frame(days, closes_for(days, 186.64), pct_last=2.78),
            "106.TSM": hist_frame(days, closes_for(days, 300.0, step=2.0)),
            "105.SOXX": hist_frame(days, closes_for(days, 250.0, step=3.0)),
        },
        spot=SPOT_AFTER_FRIDAY,
    )
    yf = FakeYfinance({})
    result = make_service(db, [NVDA, TSM, SOXX, SPX, NDX], ak=ak, yfinance=yf).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert result.session == FRIDAY and result.complete and result.failures == {}
    assert result.sources == {
        "NVDA": SOURCE_EASTMONEY_STOCK, "TSM": SOURCE_EASTMONEY_STOCK, "SOXX": SOURCE_EASTMONEY_STOCK,
        "SPX": SOURCE_EASTMONEY_INDEX, "NDX": SOURCE_EASTMONEY_INDEX,
    }
    assert yf.calls == []  # yfinance is only the fallback
    # one stock_us_hist per stock / ETF with the unadjusted daily request, ONE index snapshot for all indices
    assert [c["symbol"] for c in ak.hist_calls] == ["105.NVDA", "106.TSM", "105.SOXX"]
    assert {(c["period"], c["adjust"]) for c in ak.hist_calls} == {("daily", "")}
    assert ak.hist_calls[0]["end_date"] == "20261108"  # New York date of as_of (Sunday evening), YYYYMMDD
    assert ak.hist_calls[0]["start_date"] == "20261019"  # WINDOW_DAYS earlier
    assert ak.spot_calls == 1
    by_symbol = {m.symbol: m for m in result.moves}
    assert by_symbol["NVDA"].pct_chg == pytest.approx(2.78) and by_symbol["NVDA"].close == 186.64
    assert by_symbol["SPX"].pct_chg == pytest.approx(1.0) and by_symbol["SPX"].crossed is False
    assert by_symbol["NDX"].pct_chg == pytest.approx(-1.8) and by_symbol["NDX"].crossed and by_symbol["NDX"].direction == "down"
    assert by_symbol["SOXX"].threshold == 1.5 and by_symbol["SOXX"].kind == "etf"
    rows = stored(db, "NDX")
    assert set(rows) == {FRIDAY} and rows[FRIDAY].data_source == SOURCE_EASTMONEY_INDEX
    assert rows[FRIDAY].close == 18200.0 and rows[FRIDAY].ma5 is None  # a single snapshot bar has no history for MAs
    assert stored(db, "NVDA")[FRIDAY].data_source == SOURCE_EASTMONEY_STOCK and stored(db, "NVDA")[FRIDAY].ma5 is not None


def test_intraday_index_snapshot_is_not_stored_as_a_session(db):
    live = spot_frame(spot_row("SPX", "标普500", 5050.0, 0.4, 5030.0, "2026-11-06 23:30:00"))
    now = datetime(2026, 11, 6, 23, 31, tzinfo=CN)  # 10:31 in New York, the session is open
    result = make_service(db, [SPX], ak=FakeAkshare(spot=live), yfinance=FakeYfinance({})).refresh(now, sources=SOURCES_PREOPEN)
    assert not result.ok and "SPX" in result.failures and stored(db, "SPX") == {}


def test_leader_without_em_skips_eastmoney_and_uses_yfinance(db):
    days = sessions(FRIDAY)
    ak = FakeAkshare(hist={"105.NVDA": hist_frame(days, closes_for(days, 111.0))})
    yf = FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 110.0))})
    result = make_service(db, [NVDA_PLAIN], ak=ak, yfinance=yf).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert ak.hist_calls == [] and ak.spot_calls == 0
    assert result.sources == {"NVDA": SOURCE_YFINANCE}


def test_user_config_without_em_keeps_the_previous_tushare_yfinance_behaviour(db):
    leaders = load_us_leaders('[{"symbol":"NVDA","name":"英伟达","kind":"stock","boards":["算力"]}]').leaders
    days = sessions(FRIDAY)
    ak = FakeAkshare()
    tushare = FakeTushare({"NVDA": tushare_frame(days, closes_for(days, 110.0))})
    result = make_service(db, leaders, ak=ak, tushare=tushare, yfinance=FakeYfinance({})).refresh(FRIDAY_BOARD_RUN, sources=SOURCES_ALL)
    assert result.sources == {"NVDA": SOURCE_TUSHARE_STOCK}
    assert ak.hist_calls == [] and ak.spot_calls == 0


# ----------------------------------------------------------------------
# source order
# ----------------------------------------------------------------------
def test_source_constants_encode_the_documented_order():
    assert SOURCES_PREOPEN == ("eastmoney", "yfinance")
    assert SOURCES_ALL == ("eastmoney", "tushare", "yfinance")  # Tushare us_daily (1 call / minute) is a fallback only
    assert source_rank(SOURCE_TUSHARE_STOCK) < source_rank(SOURCE_EASTMONEY_STOCK) < source_rank(SOURCE_YFINANCE) < source_rank("AkshareFetcher")


def test_eastmoney_failure_falls_back_to_yfinance(db):
    days = sessions(FRIDAY)
    ak = FakeAkshare(hist={"105.NVDA": RuntimeError("boom")}, spot=RuntimeError("down"))
    yf = FakeYfinance({
        "NVDA": yfinance_frame(days, closes_for(days, 110.0)),
        "^GSPC": yfinance_frame(days, closes_for(days, 5000.0, step=10)),
    })
    result = make_service(db, [NVDA, SPX], ak=ak, yfinance=yf).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert result.sources == {"NVDA": SOURCE_YFINANCE, "SPX": SOURCE_YFINANCE}
    assert result.complete
    assert yf.calls == ["NVDA", "^GSPC"]


def test_stale_or_empty_eastmoney_data_is_topped_up_by_yfinance(db):
    days = sessions(FRIDAY)
    older = days[:-1]  # Eastmoney's history stops one session early
    ak = FakeAkshare(hist={"105.NVDA": hist_frame(older, closes_for(older, 109.0))})
    yf = FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 110.0))})
    result = make_service(db, [NVDA, TSM], ak=ak, yfinance=yf).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert result.sources["NVDA"] == SOURCE_YFINANCE and stored(db, "NVDA")[FRIDAY].data_source == SOURCE_YFINANCE
    assert "TSM" in result.failures and "东方财富无数据" in result.failures["TSM"] and "yfinance 无数据" in result.failures["TSM"]


def test_board_run_order_is_eastmoney_then_tushare_then_yfinance(db):
    days = sessions(FRIDAY)
    ak = FakeAkshare(
        hist={"105.NVDA": hist_frame(days, closes_for(days, 111.0)), "106.TSM": RuntimeError("blocked")},
        spot=SPOT_AFTER_FRIDAY,
    )
    tushare = FakeTushare({"TSM": tushare_frame(days, closes_for(days, 300.0))})
    yf = FakeYfinance({})
    result = make_service(db, [NVDA, TSM, SPX, NDX], ak=ak, tushare=tushare, yfinance=yf).refresh(FRIDAY_BOARD_RUN, sources=SOURCES_ALL)
    assert result.sources == {
        "NVDA": SOURCE_EASTMONEY_STOCK,   # Eastmoney first: Tushare / yfinance never asked for it
        "TSM": SOURCE_TUSHARE_STOCK,      # Eastmoney failed -> Tushare fallback
        "SPX": SOURCE_EASTMONEY_INDEX,
        "NDX": SOURCE_EASTMONEY_INDEX,    # NDX is not on Tushare index_global
    }
    assert [c["symbol"] for c in ak.hist_calls] == ["105.NVDA", "106.TSM"]
    assert tushare.calls == [("us_daily", FRIDAY)]  # ONE call by trade_date, only for the leader still missing
    assert yf.calls == []


def test_tushare_fallback_is_a_single_trade_date_call_for_all_missing_leaders(db):
    days = sessions(FRIDAY)
    amd = UsLeader("AMD", "超威", "stock", em="105.AMD")
    ak = FakeAkshare(
        hist={"105.NVDA": hist_frame(days, closes_for(days, 111.0))},  # TSM / SOXX / AMD: Eastmoney has nothing
        spot=SPOT_AFTER_FRIDAY,
    )
    tushare = FakeTushare({
        "TSM": tushare_frame(days, closes_for(days, 300.0)),
        "SOXX": tushare_frame(days, closes_for(days, 250.0)),
        "SPX": tushare_frame(days[:-1], closes_for(days[:-1], 5000.0)),  # unused: Eastmoney delivers SPX
    })
    yf = FakeYfinance({"AMD": yfinance_frame(days, closes_for(days, 150.0))})  # Tushare lacks AMD
    result = make_service(db, [NVDA, TSM, SOXX, amd, SPX], ak=ak, tushare=tushare, yfinance=yf).refresh(
        FRIDAY_BOARD_RUN, sources=SOURCES_ALL
    )
    assert tushare.calls == [("us_daily", FRIDAY)]
    assert result.sources == {
        "NVDA": SOURCE_EASTMONEY_STOCK, "TSM": SOURCE_TUSHARE_STOCK, "SOXX": SOURCE_TUSHARE_STOCK,
        "AMD": SOURCE_YFINANCE, "SPX": SOURCE_EASTMONEY_INDEX,
    }
    assert yf.calls == ["AMD"]


def test_tushare_index_global_is_asked_per_index_after_eastmoney_fails(db):
    days = sessions(FRIDAY)
    ak = FakeAkshare(spot=RuntimeError("down"))
    tushare = FakeTushare({"SPX": tushare_frame(days, closes_for(days, 5050.0, step=5))})
    result = make_service(db, [SPX, NDX], ak=ak, tushare=tushare, yfinance=FakeYfinance({})).refresh(
        FRIDAY_BOARD_RUN, sources=SOURCES_ALL
    )
    assert result.sources == {"SPX": SOURCE_TUSHARE_INDEX}
    assert tushare.calls == [("index_global", "SPX")]  # NDX has no Tushare code; no us_daily call without stocks
    assert "NDX" in result.failures


def test_nothing_is_fetched_from_unlisted_sources(db):
    days = sessions(FRIDAY)
    ak = FakeAkshare(hist={"105.NVDA": hist_frame(days, closes_for(days, 111.0))})
    yf = FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 110.0))})
    result = make_service(db, [NVDA], ak=ak, yfinance=yf).refresh(MONDAY_PREOPEN, sources=("yfinance",))
    assert ak.hist_calls == [] and result.sources == {"NVDA": SOURCE_YFINANCE}


# ----------------------------------------------------------------------
# overwrite priority: Tushare > Eastmoney > yfinance
# ----------------------------------------------------------------------
def test_eastmoney_overwrites_yfinance_but_yfinance_never_overwrites_eastmoney(db):
    days = sessions(FRIDAY)
    make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 100.0))})).refresh(
        MONDAY_PREOPEN, sources=("yfinance",)
    )
    assert stored(db, "NVDA")[FRIDAY].data_source == SOURCE_YFINANCE and stored(db, "NVDA")[FRIDAY].close == 100.0

    ak = FakeAkshare(hist={"105.NVDA": hist_frame(days, closes_for(days, 110.0))})
    make_service(db, [NVDA], ak=ak).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    row = stored(db, "NVDA")[FRIDAY]
    assert row.data_source == SOURCE_EASTMONEY_STOCK and row.close == 110.0  # yfinance row replaced

    rerun = make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 99.0))}))
    result = rerun.refresh(MONDAY_PREOPEN, sources=("yfinance",))
    row = stored(db, "NVDA")[FRIDAY]
    assert row.data_source == SOURCE_EASTMONEY_STOCK and row.close == 110.0 and result.stored_rows == 0


def test_tushare_overwrites_eastmoney_and_eastmoney_never_overwrites_tushare(db):
    days = sessions(FRIDAY)
    older = days[:-1]
    ak = FakeAkshare(hist={"105.NVDA": hist_frame(days, closes_for(days, 110.0))})
    make_service(db, [NVDA], ak=ak).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert stored(db, "NVDA")[FRIDAY].data_source == SOURCE_EASTMONEY_STOCK

    # a later board run whose Eastmoney answer is one session behind falls back to Tushare, which replaces that row
    ak_late = FakeAkshare(hist={"105.NVDA": hist_frame(older, closes_for(older, 109.0))})
    official = tushare_frame(days, closes_for(days, 110.5), pct_last=0.91)
    make_service(db, [NVDA], ak=ak_late, tushare=FakeTushare({"NVDA": official})).refresh(FRIDAY_BOARD_RUN, sources=SOURCES_ALL)
    row = stored(db, "NVDA")[FRIDAY]
    assert row.data_source == SOURCE_TUSHARE_STOCK and row.close == 110.5

    # a later pre-open style run (Eastmoney with different numbers) leaves the Tushare row alone
    ak2 = FakeAkshare(hist={"105.NVDA": hist_frame(days, closes_for(days, 99.0))})
    result = make_service(db, [NVDA], ak=ak2).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    row = stored(db, "NVDA")[FRIDAY]
    assert row.data_source == SOURCE_TUSHARE_STOCK and row.close == 110.5
    assert result.moves[0].source == SOURCE_TUSHARE_STOCK


def test_index_snapshot_row_is_overwritten_by_tushare_later(db):
    ak = FakeAkshare(spot=SPOT_AFTER_FRIDAY)
    make_service(db, [SPX], ak=ak).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert stored(db, "SPX")[FRIDAY].data_source == SOURCE_EASTMONEY_INDEX
    days = sessions(FRIDAY)
    make_service(db, [SPX], tushare=FakeTushare({"SPX": tushare_frame(days, closes_for(days, 5051.0, step=5))})).refresh(
        FRIDAY_BOARD_RUN, sources=("tushare",)
    )
    assert stored(db, "SPX")[FRIDAY].data_source != SOURCE_EASTMONEY_INDEX and stored(db, "SPX")[FRIDAY].close == 5051.0


# ----------------------------------------------------------------------
# timeout / failure handling
# ----------------------------------------------------------------------
def test_a_hanging_akshare_call_times_out_and_falls_back(db):
    days = sessions(FRIDAY)
    release = threading.Event()

    class HangingAkshare(FakeAkshare):
        def stock_us_hist(self, symbol, **kwargs):
            release.wait(10)
            return pd.DataFrame()

    yf = FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 110.0))})
    try:
        result = make_service(db, [NVDA], ak=HangingAkshare(), yfinance=yf, timeout=0.2).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    finally:
        release.set()
    assert result.sources == {"NVDA": SOURCE_YFINANCE}  # fell back; the refresh did not block for the full 10s


def test_timeout_is_reported_when_no_fallback_exists(db):
    release = threading.Event()

    class HangingAkshare(FakeAkshare):
        def stock_us_hist(self, symbol, **kwargs):
            release.wait(10)
            return pd.DataFrame()

    try:
        result = make_service(db, [NVDA], ak=HangingAkshare(), timeout=0.2).refresh(MONDAY_PREOPEN, sources=("eastmoney",))
    finally:
        release.set()
    assert not result.ok and "东方财富" in result.failures["NVDA"] and "no response" in result.failures["NVDA"]


def test_index_snapshot_failure_is_fetched_once_for_all_indices(db):
    ak = FakeAkshare(spot=RuntimeError("scraper changed"))
    result = make_service(db, [SPX, NDX], ak=ak, yfinance=FakeYfinance({})).refresh(MONDAY_PREOPEN, sources=("eastmoney",))
    assert ak.spot_calls == 1
    assert set(result.failures) == {"SPX", "NDX"} and "scraper changed" in result.failures["SPX"]


def test_repeated_eastmoney_failures_switch_it_off_for_the_rest_of_the_run(db):
    days = sessions(FRIDAY)
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    leaders = [UsLeader(s, s, "stock", em=f"105.{s}") for s in symbols]
    ak = FakeAkshare(hist={f"105.{s}": RuntimeError("blocked") for s in symbols})
    yf = FakeYfinance({s: yfinance_frame(days, closes_for(days, 10.0)) for s in symbols})
    result = make_service(db, leaders, ak=ak, yfinance=yf).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert len(ak.hist_calls) == 3  # EASTMONEY_MAX_CONSECUTIVE_FAILURES, then no more requests
    assert result.complete and set(result.sources.values()) == {SOURCE_YFINANCE}
    assert any("东方财富连续" in note for note in result.notes)


def test_an_empty_answer_does_not_count_as_a_failure_streak(db):
    days = sessions(FRIDAY)
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    leaders = [UsLeader(s, s, "stock", em=f"105.{s}") for s in symbols]
    ak = FakeAkshare(hist={"105.DDD": hist_frame(days, closes_for(days, 10.0))})  # the first three are simply unknown
    yf = FakeYfinance({})
    result = make_service(db, leaders, ak=ak, yfinance=yf).refresh(MONDAY_PREOPEN, sources=SOURCES_PREOPEN)
    assert len(ak.hist_calls) == 4 and result.sources == {"DDD": SOURCE_EASTMONEY_STOCK}
    assert not any("东方财富连续" in note for note in result.notes)


# ----------------------------------------------------------------------
# NDX: live 17:00 board run replayed for 2026-11-06 (as_of Friday 17:00 Beijing = 04:00 New York) while the
# Eastmoney snapshot already carries Friday's close. Rows are the ones sampled live on Sunday 2026-11-08.
# ----------------------------------------------------------------------
LIVE_SPOT = spot_frame(
    spot_row("DJIA", "道琼斯", 40000.0, 0.2, 39920.0, "2026-11-07 04:59:46"),
    spot_row("SPX", "标普500", 5050.0, 1.0, 5000.0, "2026-11-07 04:59:46"),
    {  # exactly as sampled
        "序号": 3, "代码": "NDX", "名称": "纳斯达克", "最新价": 24579.42, "涨跌额": 264.42, "涨跌幅": 1.09,
        "开盘价": 24602.48, "最高价": 24825.55, "最低价": 24507.63, "昨收价": 24315.00, "振幅": 1.31,
        "最新行情时间": "2026-11-07 05:00:00",
    },
)
# tail of ``index_global_hist_em("纳斯达克")`` (live sample, one call): 日期 代码 名称 今开 最新价 最高 最低 振幅
NDX_HIST = pd.DataFrame(
    [
        (date(2026, 11, 2), "NDX", "纳斯达克", 25600.17, 25164.36, 25799.46, 25145.95, 2.57),
        (date(2026, 11, 3), "NDX", "纳斯达克", 25246.64, 25372.01, 25474.46, 25246.64, 0.91),
        (date(2026, 11, 4), "NDX", "纳斯达克", 25652.78, 25736.51, 26048.83, 25602.14, 1.76),
        (date(2026, 11, 5), "NDX", "纳斯达克", 25558.11, 24315.00, 25559.03, 24262.14, 5.04),
        (date(2026, 11, 6), "NDX", "纳斯达克", 24602.48, 24579.42, 24825.55, 24507.63, 1.31),
    ],
    columns=["日期", "代码", "名称", "今开", "最新价", "最高", "最低", "振幅"],
)
FRIDAY_17_CN = datetime(2026, 11, 6, 17, 0, tzinfo=CN)  # the board run of 2026-11-06, replayed after the fact


def test_ndx_sample_rows_parse_to_the_friday_close_bar():
    frame = _frame_from_eastmoney_spot(LIVE_SPOT, "NDX")
    assert len(frame) == 1
    bar = frame.iloc[0]
    assert bar["date"] == FRIDAY  # 05:00 Beijing = 16:00 New York (EST)
    assert (bar["close"], bar["pct_chg"], bar["pre_close"]) == (24579.42, 1.09, 24315.0)
    # SPX / DJIA stamped 04:59:46 are 15:59:46 New York: still a closing quote on the same session date
    assert _frame_from_eastmoney_spot(LIVE_SPOT, "SPX").iloc[0]["date"] == FRIDAY
    assert _frame_from_eastmoney_spot(LIVE_SPOT, "DJIA").iloc[0]["date"] == FRIDAY


def test_index_history_frame_derives_previous_close_and_pct_inside_the_window():
    frame = _frame_from_eastmoney_index_hist(NDX_HIST, date(2026, 11, 3), date(2026, 11, 6))
    assert list(frame["date"]) == [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
    nov5 = frame[frame["date"] == date(2026, 11, 5)].iloc[0]
    assert (nov5["open"], nov5["close"], nov5["pre_close"]) == (25558.11, 24315.0, 25736.51)
    assert nov5["pct_chg"] == pytest.approx((24315.0 / 25736.51 - 1) * 100)
    assert pd.isna(nov5["volume"]) and pd.isna(nov5["amount"])
    # the first history row has no previous close and is dropped; junk input yields no bars
    assert _frame_from_eastmoney_index_hist(NDX_HIST, date(2026, 10, 1), date(2026, 11, 6)).iloc[0]["date"] == date(2026, 11, 3)
    assert _frame_from_eastmoney_index_hist(pd.DataFrame(), date(2026, 10, 1), date(2026, 11, 6)).empty
    assert _frame_from_eastmoney_index_hist(pd.DataFrame({"foo": [1]}), date(2026, 10, 1), date(2026, 11, 6)).empty


def test_ndx_replay_of_a_past_board_day_uses_the_index_history_not_a_newer_snapshot(db):
    ak = FakeAkshare(spot=LIVE_SPOT, index_hist={"纳斯达克": NDX_HIST})
    yf = FakeYfinance({})  # ^NDX would fail ("未查询到") - it must not even be needed
    result = make_service(db, [NDX], ak=ak, tushare=FakeTushare({}), yfinance=yf).refresh(FRIDAY_17_CN, sources=SOURCES_ALL)
    # Friday's close (the snapshot) is after the run time: the last completed session is Thursday 2026-11-05
    assert result.complete and result.session == date(2026, 11, 5) and result.failures == {}
    assert result.sources == {"NDX": SOURCE_EASTMONEY_INDEX}
    assert ak.spot_calls == 1 and ak.index_hist_calls == ["纳斯达克"] and yf.calls == []
    move = result.moves[0]
    assert move.close == 24315.0 and move.pre_close == 25736.51
    assert move.pct_chg == pytest.approx((24315.0 / 25736.51 - 1) * 100) and move.crossed and move.direction == "down"
    assert set(stored(db, "NDX")) == {date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)}  # Friday is not stored yet


def test_ndx_live_board_run_after_the_close_uses_the_snapshot_without_a_history_call(db):
    ak = FakeAkshare(spot=LIVE_SPOT, index_hist={"纳斯达克": NDX_HIST})
    result = make_service(db, [NDX], ak=ak).refresh(FRIDAY_BOARD_RUN, sources=SOURCES_ALL)  # Saturday 17:00 Beijing
    assert result.session == FRIDAY and result.sources == {"NDX": SOURCE_EASTMONEY_INDEX}
    assert ak.index_hist_calls == []
    move = result.moves[0]
    assert (move.close, move.pct_chg) == (24579.42, 1.09)  # pct comes from the snapshot's own 涨跌幅


def test_intraday_snapshot_falls_back_to_the_completed_history(db):
    live = spot_frame(spot_row("NDX", "纳斯达克", 24700.0, 0.5, 24579.42, "2026-11-09 23:30:00"))  # Monday 10:30 New York
    hist = pd.concat([NDX_HIST, pd.DataFrame(
        [(date(2026, 11, 9), "NDX", "纳斯达克", 24580.0, 24700.0, 24710.0, 24500.0, 0.9)], columns=NDX_HIST.columns
    )], ignore_index=True)
    now = datetime(2026, 11, 9, 23, 31, tzinfo=CN)
    result = make_service(db, [NDX], ak=FakeAkshare(spot=live, index_hist={"纳斯达克": hist})).refresh(now, sources=SOURCES_PREOPEN)
    assert result.session == FRIDAY  # Monday's bar is still in progress and is not stored
    assert set(stored(db, "NDX")) <= {date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), FRIDAY}


def test_index_history_failure_is_reported_and_falls_back_to_yfinance(db):
    days = sessions(FRIDAY)
    ak = FakeAkshare(spot=LIVE_SPOT, index_hist={"纳斯达克": RuntimeError("push2his blocked")})
    yf = FakeYfinance({"^NDX": yfinance_frame(days[:-1], closes_for(days[:-1], 25000.0, step=10))})
    result = make_service(db, [NDX], ak=ak, yfinance=yf).refresh(FRIDAY_17_CN, sources=SOURCES_ALL)
    assert result.sources == {"NDX": SOURCE_YFINANCE} and yf.calls == ["^NDX"]


# ----------------------------------------------------------------------
# Tushare us_daily frequency limit (1 call / minute): no 30s sleeps, Tushare is skipped for the run
# ----------------------------------------------------------------------
class _RateLimitedSdk(TushareMarketDataMixin):
    """Real retry classification of the mixin; the SDK boundary answers with the live frequency-limit error."""

    def __init__(self):
        self.api_calls: List[str] = []

    def _call_api_with_rate_limit(self, api_name, **params):
        self.api_calls.append(api_name)
        raise Exception(f"抱歉，您访问接口({api_name})频率超限(1次/分钟)，详情见接口文档")


def test_rate_limited_us_daily_skips_tushare_for_the_run_without_sleeping(db, monkeypatch):
    def no_sleep(seconds):
        raise AssertionError(f"slept {seconds}s on a frequency limit")

    monkeypatch.setattr("data_provider.tushare_market_data.time.sleep", no_sleep)
    days = sessions(FRIDAY)
    sdk = _RateLimitedSdk()
    leaders = [NVDA_PLAIN, UsLeader("AMD", "超威", "stock"), UsLeader("SPX", "标普500指数", "index")]  # no ``em``
    yf = FakeYfinance({
        "NVDA": yfinance_frame(days, closes_for(days, 110.0)),
        "AMD": yfinance_frame(days, closes_for(days, 150.0)),
        "^GSPC": yfinance_frame(days, closes_for(days, 5000.0, step=10)),
    })
    result = make_service(db, leaders, tushare=sdk, yfinance=yf).refresh(FRIDAY_BOARD_RUN, sources=SOURCES_ALL)
    assert sdk.api_calls == ["us_daily"]  # one attempt; no retry, no per-ticker calls, index_global not asked either
    assert set(result.sources.values()) == {SOURCE_YFINANCE} and result.complete
    assert any("频率超限" in note for note in result.notes)


def test_mixin_does_not_retry_rate_limited_us_daily_but_still_retries_other_apis(monkeypatch):
    sleeps: List[float] = []
    monkeypatch.setattr("data_provider.tushare_market_data.time.sleep", sleeps.append)
    sdk = _RateLimitedSdk()
    with pytest.raises(TushareRateLimitedError):
        sdk.fetch_us_daily_market(FRIDAY)
    with pytest.raises(TushareRateLimitedError):
        sdk.fetch_index_global("SPX", FRIDAY, FRIDAY)
    assert sdk.api_calls == ["us_daily", "index_global"] and sleeps == []
    # other interfaces keep the 30s rate-limit backoff
    with pytest.raises(DataFetchError):
        sdk.fetch_market_daily(FRIDAY)
    assert sdk.api_calls.count("daily") == 3 and sleeps == [30.0, 30.0]


def _us_daily_page(count: int, nvda_first: bool = False) -> pd.DataFrame:
    codes = [f"T{i:04d}" for i in range(count)]
    if nvda_first:
        codes[0] = "NVDA"
    return pd.DataFrame({
        "ts_code": codes, "trade_date": ["20261106"] * count, "close": [10.0] * count, "open": [9.0] * count,
        "high": [11.0] * count, "low": [8.0] * count, "pre_close": [9.5] * count, "pct_change": [5.0] * count,
        "vol": [1.0] * count, "amount": [10.0] * count,
    })


class _PagedSdk(_RateLimitedSdk):
    """Page one is a full page; page two hits the frequency limit."""

    def __init__(self, first: pd.DataFrame):
        super().__init__()
        self.first = first
        self.params: List[Dict[str, Any]] = []

    def _call_api_with_rate_limit(self, api_name, **params):
        self.api_calls.append(api_name)
        self.params.append(params)
        if len(self.api_calls) == 1:
            return self.first
        raise Exception("抱歉，您访问接口(us_daily)频率超限(1次/分钟)")


def test_us_daily_page_one_is_kept_when_page_two_is_rate_limited(monkeypatch):
    sleeps: List[float] = []
    monkeypatch.setattr("data_provider.tushare_market_data.time.sleep", sleeps.append)
    sdk = _PagedSdk(_us_daily_page(US_DAILY_PAGE_CAP))
    with pytest.raises(IncompleteMarketDataError) as caught:
        sdk.fetch_us_daily_market(FRIDAY)
    assert len(caught.value.partial) == US_DAILY_PAGE_CAP and "ts_code" in caught.value.partial.columns
    assert sdk.params[0] == {"trade_date": "20261106", "fields": sdk._US_DAILY_FIELDS}  # no ts_code: all tickers at once
    assert sdk.params[1]["offset"] == US_DAILY_PAGE_CAP and sleeps == []


def test_service_keeps_the_leaders_found_on_page_one_when_page_two_is_rate_limited(db, monkeypatch):
    monkeypatch.setattr("data_provider.tushare_market_data.time.sleep", lambda seconds: None)
    days = sessions(FRIDAY)
    sdk = _PagedSdk(_us_daily_page(US_DAILY_PAGE_CAP, nvda_first=True))
    amd = UsLeader("AMD", "超威", "stock")
    yf = FakeYfinance({"AMD": yfinance_frame(days, closes_for(days, 150.0))})
    result = make_service(db, [NVDA_PLAIN, amd], tushare=sdk, yfinance=yf).refresh(FRIDAY_BOARD_RUN, sources=SOURCES_ALL)
    assert result.sources == {"NVDA": SOURCE_TUSHARE_STOCK, "AMD": SOURCE_YFINANCE}  # AMD was not on page one
    assert yf.calls == ["AMD"] and len(sdk.api_calls) == 2
