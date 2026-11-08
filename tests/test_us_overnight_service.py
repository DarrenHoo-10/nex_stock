# -*- coding: utf-8 -*-
"""US overnight service: latest-session detection, Tushare -> yfinance fallback, stock_daily upsert, moves.

Everything runs against fakes of the Tushare fetcher / yfinance fetcher and an in-memory database;
no network. The Tushare mixin is exercised at the SDK boundary (``_call_api_with_rate_limit``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data_provider.base import DataFetchError
from data_provider.tushare_market_data import TushareMarketDataMixin
from src.services.us_leaders import UsLeader, UsMoveThresholds
from src.services.us_overnight_service import (
    SOURCE_TUSHARE_INDEX,
    SOURCE_TUSHARE_STOCK,
    SOURCE_YFINANCE,
    UsOvernightService,
    expected_weekday_session,
    session_closed,
)
from src.storage import DatabaseManager

CN = ZoneInfo("Asia/Shanghai")
FRIDAY = date(2026, 11, 6)
MONDAY_PREOPEN = datetime(2026, 11, 9, 8, 15, tzinfo=CN)   # Sunday evening in New York: last session = Friday
FRIDAY_BOARD_RUN = datetime(2026, 11, 7, 17, 0, tzinfo=CN)  # Saturday 17:00: last session = Friday
TUESDAY_BOARD_RUN = datetime(2026, 11, 10, 17, 0, tzinfo=CN)  # last session = Monday


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def sessions(last: date, count: int = 6) -> List[date]:
    days: List[date] = []
    cursor = last
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def closes_for(days: List[date], last_close: float, step: float = 1.0) -> List[float]:
    """Closes rising by ``step`` per session, ending at ``last_close``."""
    return [round(last_close - step * (len(days) - 1 - i), 4) for i in range(len(days))]


def tushare_frame(days: List[date], closes: List[float], *, pct_last: Optional[float] = None, open_last: Optional[float] = None) -> pd.DataFrame:
    rows = []
    for index, (day, close) in enumerate(zip(days, closes)):
        pre_close = closes[index - 1] if index else close - 1.0
        last = index == len(days) - 1
        rows.append({
            "trade_date": day, "open": open_last if (last and open_last is not None) else close,
            "high": close, "low": close, "close": close, "pre_close": pre_close,
            "pct_chg": pct_last if (last and pct_last is not None) else (close / pre_close - 1) * 100,
            "vol": 1000.0, "amount": 1000.0 * close,
        })
    return pd.DataFrame(rows)


def yfinance_frame(days: List[date], closes: List[float], *, open_last: Optional[float] = None) -> pd.DataFrame:
    rows = []
    for index, (day, close) in enumerate(zip(days, closes)):
        last = index == len(days) - 1
        rows.append({
            "date": pd.Timestamp(day), "open": open_last if (last and open_last is not None) else close,
            "high": close, "low": close, "close": close, "volume": 500.0, "amount": 500.0 * close, "pct_chg": 0.0,
        })
    return pd.DataFrame(rows)


class FakeTushare:
    """``us_daily`` is served by trade_date (one call, all tickers), ``index_global`` per code - like the real helpers.

    ``errors`` are per index code; ``market_error`` is raised by the (single) ``us_daily`` call.
    """

    INDEX_CODES = {"SPX", "IXIC", "DJI"}

    def __init__(self, frames: Dict[str, pd.DataFrame], errors: Optional[Dict[str, Exception]] = None,
                 market_error: Optional[Exception] = None):
        self.frames = frames
        self.errors = errors or {}
        self.market_error = market_error
        self.calls: List[tuple] = []

    def fetch_us_daily_market(self, trade_date):
        self.calls.append(("us_daily", trade_date))
        if self.market_error is not None:
            raise self.market_error
        parts = []
        for code, frame in self.frames.items():
            if code in self.INDEX_CODES or frame.empty:
                continue
            part = frame[frame["trade_date"] == trade_date].copy()
            part.insert(0, "ts_code", code)
            parts.append(part)
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    def fetch_index_global(self, ts_code, start, end):
        self.calls.append(("index_global", ts_code))
        if ts_code in self.errors:
            raise self.errors[ts_code]
        return self.frames.get(ts_code, pd.DataFrame())


class FakeYfinance:
    def __init__(self, frames: Dict[str, pd.DataFrame], errors: Optional[Dict[str, Exception]] = None):
        self.frames = frames
        self.errors = errors or {}
        self.calls: List[str] = []

    def get_daily_data(self, symbol, start_date=None, end_date=None, days=30):
        self.calls.append(symbol)
        if symbol in self.errors:
            raise self.errors[symbol]
        return self.frames.get(symbol, pd.DataFrame())


NVDA = UsLeader("NVDA", "英伟达", "stock", ("算力",))
SPX = UsLeader("SPX", "标普500指数", "index")
SOX = UsLeader("SOX", "费城半导体指数", "index", ("半导体",))


def make_service(db, leaders, tushare=None, yfinance=None, thresholds=UsMoveThresholds(4.0, 1.5)) -> UsOvernightService:
    return UsOvernightService(
        SimpleNamespace(), leaders=leaders, thresholds=thresholds, tushare_fetcher=tushare, yfinance_fetcher=yfinance, db=db,
    )


def stored(db, code: str) -> Dict[date, Any]:
    return {bar.date: bar for bar in db.get_data_range(code, date(2026, 10, 1), date(2026, 11, 30))}


# ----------------------------------------------------------------------
# latest completed session comes from the data and the New York clock
# ----------------------------------------------------------------------
def test_session_close_and_expected_session_helpers():
    assert session_closed(FRIDAY, MONDAY_PREOPEN)
    assert not session_closed(date(2026, 11, 9), MONDAY_PREOPEN)  # Monday's session has not even opened
    # 16:00 EST on Monday 2026-11-09 is Tuesday 05:00 Beijing time
    assert not session_closed(date(2026, 11, 9), datetime(2026, 11, 10, 4, 59, tzinfo=CN))
    assert session_closed(date(2026, 11, 9), datetime(2026, 11, 10, 5, 0, tzinfo=CN))
    assert expected_weekday_session(MONDAY_PREOPEN) == FRIDAY
    assert expected_weekday_session(FRIDAY_BOARD_RUN) == FRIDAY
    assert expected_weekday_session(TUESDAY_BOARD_RUN) == date(2026, 11, 9)
    assert expected_weekday_session(datetime(2026, 11, 9, 20, 0, tzinfo=CN)) == FRIDAY  # Monday, US still before the close


def test_weekend_gap_takes_the_last_bar_and_ignores_an_unfinished_session(db):
    days = sessions(FRIDAY) + [date(2026, 11, 9)]  # Monday's bar exists in the feed but is not closed yet
    closes = closes_for(days, 110.0)
    service = make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": yfinance_frame(days, closes)}))
    result = service.refresh(MONDAY_PREOPEN, sources=("yfinance",))
    assert result.session == FRIDAY
    assert date(2026, 11, 9) not in stored(db, "NVDA")
    assert result.stale == [] and result.failures == {}


def test_holiday_session_is_detected_from_the_data_without_a_calendar(db):
    # Wednesday 2026-11-11 run; the feed's newest bar is Monday (no Tuesday bar: pretend a US holiday)
    days = [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 9)]
    service = make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 100.0))}))
    result = service.refresh(datetime(2026, 11, 11, 8, 15, tzinfo=CN), sources=("yfinance",))
    assert result.session == date(2026, 11, 9)


def test_leader_with_an_older_last_bar_is_stale_and_has_no_move(db):
    days = sessions(FRIDAY)
    older = days[:-1]
    yf = FakeYfinance({
        "NVDA": yfinance_frame(days, closes_for(days, 110.0)),
        "^GSPC": yfinance_frame(older, closes_for(older, 5000.0)),
    })
    result = make_service(db, [NVDA, SPX], yfinance=yf).refresh(MONDAY_PREOPEN, sources=("yfinance",))
    assert result.session == FRIDAY
    assert result.stale == ["SPX"]
    assert [m.symbol for m in result.moves] == ["NVDA"]
    assert not result.complete and result.ok


def test_no_data_anywhere_is_reported_not_raised(db):
    yf = FakeYfinance({}, errors={"NVDA": RuntimeError("offline")})
    result = make_service(db, [NVDA, SPX], yfinance=yf).refresh(MONDAY_PREOPEN, sources=("yfinance",))
    assert not result.ok and result.session is None
    assert set(result.failures) == {"NVDA", "SPX"}
    assert "offline" in result.failures["NVDA"] and "无数据" in result.failures["SPX"]


def test_empty_leader_list_is_a_noop(db):
    result = make_service(db, []).refresh(MONDAY_PREOPEN)
    assert not result.ok and result.notes


# ----------------------------------------------------------------------
# Tushare first, yfinance fallback
# ----------------------------------------------------------------------
def test_tushare_is_used_when_fresh_and_yfinance_is_not_called(db):
    days = sessions(FRIDAY)
    tushare = FakeTushare({
        "NVDA": tushare_frame(days, closes_for(days, 110.0)),
        "SPX": tushare_frame(days, closes_for(days, 5000.0, step=10)),
    })
    yfinance = FakeYfinance({})
    result = make_service(db, [NVDA, SPX], tushare, yfinance).refresh(FRIDAY_BOARD_RUN)
    assert result.session == FRIDAY and result.complete
    assert result.sources == {"NVDA": SOURCE_TUSHARE_STOCK, "SPX": SOURCE_TUSHARE_INDEX}
    assert tushare.calls == [("us_daily", FRIDAY), ("index_global", "SPX")]  # ONE us_daily call (by trade_date)
    assert yfinance.calls == []
    assert {bar.data_source for bar in stored(db, "NVDA").values()} == {SOURCE_TUSHARE_STOCK}


def test_index_not_on_tushare_goes_to_yfinance_and_errors_fall_back(db):
    days = sessions(FRIDAY)
    tushare = FakeTushare(
        {"NVDA": tushare_frame(days, closes_for(days, 110.0))},
        errors={"SPX": DataFetchError("Tushare index_global 请求失败")},
    )
    yf = FakeYfinance({
        "^SOX": yfinance_frame(days, closes_for(days, 5000.0, step=20)),
        "^GSPC": yfinance_frame(days, closes_for(days, 6000.0, step=10)),
    })
    result = make_service(db, [NVDA, SPX, SOX], tushare, yf).refresh(FRIDAY_BOARD_RUN)
    assert result.sources == {"NVDA": SOURCE_TUSHARE_STOCK, "SPX": SOURCE_YFINANCE, "SOX": SOURCE_YFINANCE}
    assert tushare.calls == [("us_daily", FRIDAY), ("index_global", "SPX")]  # SOX has no Tushare api: not asked
    assert yf.calls == ["^GSPC", "^SOX"]
    assert result.complete


def test_permission_error_stops_asking_tushare_for_the_remaining_leaders(db):
    days = sessions(FRIDAY)
    tushare = FakeTushare({}, market_error=DataFetchError("Tushare us_daily 无权限或积分不足: x"))
    yf = FakeYfinance({
        "NVDA": yfinance_frame(days, closes_for(days, 110.0)),
        "^GSPC": yfinance_frame(days, closes_for(days, 6000.0, step=10)),
    })
    result = make_service(db, [NVDA, SPX], tushare, yf).refresh(FRIDAY_BOARD_RUN)
    assert tushare.calls == [("us_daily", FRIDAY)]  # the index is not asked either once Tushare is unusable
    assert set(result.sources.values()) == {SOURCE_YFINANCE}
    assert any("无权限" in note for note in result.notes)


def test_stale_tushare_falls_back_to_yfinance_when_it_has_the_newer_session(db):
    days = sessions(FRIDAY)
    stale = days[:-1]  # Tushare has not published Friday yet
    tushare = FakeTushare({"NVDA": tushare_frame(stale, closes_for(stale, 109.0))})
    yf = FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 110.0))})
    result = make_service(db, [NVDA], tushare, yf).refresh(FRIDAY_BOARD_RUN)
    assert result.session == FRIDAY and result.sources["NVDA"] == SOURCE_YFINANCE
    assert stored(db, "NVDA")[FRIDAY].data_source == SOURCE_YFINANCE


def test_holiday_run_asks_tushare_for_the_expected_session_once_and_falls_back_to_yfinance(db):
    # the expected weekday session (Monday 2026-11-09) is a US holiday: Tushare has no rows for that date, and it is
    # asked for that date only once (no hunting through earlier dates with the 1-call-per-minute us_daily budget)
    days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
    tushare = FakeTushare({"NVDA": tushare_frame(days, closes_for(days, 110.0))})
    yf = FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 110.0))})
    result = make_service(db, [NVDA], tushare, yf).refresh(datetime(2026, 11, 10, 17, 0, tzinfo=CN))
    assert tushare.calls == [("us_daily", date(2026, 11, 9))]
    assert yf.calls == ["NVDA"]
    assert result.session == FRIDAY and result.sources["NVDA"] == SOURCE_YFINANCE


# ----------------------------------------------------------------------
# stock_daily: upsert, later Tushare overwrite, yfinance never overwrites Tushare
# ----------------------------------------------------------------------
def test_yfinance_rows_are_overwritten_by_the_later_tushare_run(db):
    days = sessions(FRIDAY)
    yf_closes = closes_for(days, 110.0)
    morning = make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": yfinance_frame(days, yf_closes)}))
    first = morning.refresh(MONDAY_PREOPEN, sources=("yfinance",))
    assert first.stored_rows == len(days) - 1  # the window's first bar has no previous close and is dropped
    rows = stored(db, "NVDA")
    assert rows[FRIDAY].data_source == SOURCE_YFINANCE and rows[FRIDAY].close == 110.0
    assert rows[FRIDAY].ma5 is not None  # indicators are kept so the regular analysis can use the rows

    official = tushare_frame(days, closes_for(days, 110.5), pct_last=0.91)
    afternoon = make_service(db, [NVDA], tushare=FakeTushare({"NVDA": official}), yfinance=FakeYfinance({}))
    afternoon.refresh(MONDAY_PREOPEN)
    rows = stored(db, "NVDA")
    assert rows[FRIDAY].data_source == SOURCE_TUSHARE_STOCK
    assert rows[FRIDAY].close == 110.5 and rows[FRIDAY].pct_chg == pytest.approx(0.91)
    assert len([r for r in rows.values() if r.date == FRIDAY]) == 1  # upsert, not a second row


def test_yfinance_never_overwrites_a_tushare_row(db):
    days = sessions(FRIDAY)
    make_service(db, [NVDA], tushare=FakeTushare({"NVDA": tushare_frame(days, closes_for(days, 110.5))})).refresh(MONDAY_PREOPEN)
    rerun = make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 99.0))}))
    result = rerun.refresh(MONDAY_PREOPEN, sources=("yfinance",))
    rows = stored(db, "NVDA")
    assert rows[FRIDAY].close == 110.5 and rows[FRIDAY].data_source == SOURCE_TUSHARE_STOCK
    assert rows[date(2026, 11, 5)].data_source == SOURCE_YFINANCE  # the single-session Tushare fetch left earlier days empty
    assert result.stored_rows == len(days) - 2  # older days only (Friday is protected, the first window bar has no previous close)
    assert result.moves[0].source == SOURCE_TUSHARE_STOCK  # moves are read from storage


# ----------------------------------------------------------------------
# moves: pct change, gap, threshold crossing
# ----------------------------------------------------------------------
def test_threshold_crossing_uses_separate_stock_and_index_thresholds(db):
    days = sessions(FRIDAY, 4)
    # NVDA +4.4% (>= 4.0), AMD-like +3.9% stays below; SPX +1.6% crosses the index threshold, SOX -1.4% does not
    def pair(prev, last):
        return [prev * 0.97, prev * 0.98, prev, last]

    amd = UsLeader("AMD", "超威", "stock")
    frames = {
        "NVDA": yfinance_frame(days, pair(100.0, 104.4)),
        "AMD": yfinance_frame(days, pair(100.0, 103.9)),
        "^GSPC": yfinance_frame(days, pair(5000.0, 5080.0)),
        "^SOX": yfinance_frame(days, pair(5000.0, 4930.0)),
    }
    service = make_service(db, [NVDA, amd, SPX, SOX], yfinance=FakeYfinance(frames))
    result = service.refresh(MONDAY_PREOPEN, sources=("yfinance",))
    by_symbol = {m.symbol: m for m in result.moves}
    assert by_symbol["NVDA"].crossed and by_symbol["NVDA"].direction == "up"
    assert not by_symbol["AMD"].crossed and by_symbol["AMD"].direction is None
    assert by_symbol["SPX"].crossed and by_symbol["SPX"].threshold == 1.5
    assert not by_symbol["SOX"].crossed
    assert by_symbol["SOX"].pct_chg == pytest.approx(-1.4)
    assert [m.symbol for m in result.crossed] == ["NVDA", "SPX"]


def test_down_move_and_gap_are_computed_from_stored_bars(db):
    days = sessions(FRIDAY, 3)
    closes = [100.0, 100.0, 94.0]
    frame = yfinance_frame(days, closes, open_last=97.0)  # opened 3% below the previous close, closed -6%
    result = make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": frame})).refresh(MONDAY_PREOPEN, sources=("yfinance",))
    move = result.moves[0]
    assert move.pct_chg == pytest.approx(-6.0) and move.gap_pct == pytest.approx(-3.0)
    assert move.crossed and move.direction == "down" and move.pre_close == 100.0
    assert move.to_dict()["direction"] == "down"


def test_load_moves_reads_the_newest_stored_session_without_fetching(db):
    days = sessions(FRIDAY)
    make_service(db, [NVDA], yfinance=FakeYfinance({"NVDA": yfinance_frame(days, closes_for(days, 110.0))})).refresh(
        MONDAY_PREOPEN, sources=("yfinance",)
    )
    offline = make_service(db, [NVDA])  # no fetchers at all: pure storage read
    moves, stale = offline.load_moves(as_of=MONDAY_PREOPEN)
    assert [m.trade_date for m in moves] == [FRIDAY] and stale == []
    assert offline.load_moves(as_of=datetime(2026, 11, 3, 8, 0, tzinfo=CN))[0][0].trade_date == date(2026, 11, 2)  # time-travel: older run
    empty = make_service(db, [UsLeader("AMD", "超威", "stock")])
    assert empty.load_moves(as_of=MONDAY_PREOPEN) == ([], [])


# ----------------------------------------------------------------------
# Tushare mixin at the SDK boundary
# ----------------------------------------------------------------------
class _SdkFetcher(TushareMarketDataMixin):
    def __init__(self, frames: Dict[str, pd.DataFrame]):
        self.frames = frames
        self.calls: List[tuple] = []

    def _call_api_with_rate_limit(self, api_name, **params):
        self.calls.append((api_name, params))
        return self.frames[api_name]


def test_mixin_normalises_us_daily_and_index_global(db):
    us_daily = pd.DataFrame({
        "ts_code": ["NVDA", "NVDA"], "trade_date": ["20261106", "20261105"], "close": [110.0, 108.0], "open": [109.0, 107.0],
        "high": [111.0, 109.0], "low": [108.0, 106.0], "pre_close": [108.0, 106.0], "pct_change": [1.85, 1.89],
        "vol": [1000.0, 900.0], "amount": [1.1e5, 9.7e4],
    })
    index_global = pd.DataFrame({
        "ts_code": ["SPX"], "trade_date": ["20261106"], "open": [5000.0], "close": [5050.0], "high": [5060.0],
        "low": [4990.0], "pre_close": [5000.0], "pct_chg": [1.0], "vol": [None],
    })
    fetcher = _SdkFetcher({"us_daily": us_daily, "index_global": index_global})
    frame = fetcher.fetch_us_daily("NVDA", date(2026, 10, 20), date(2026, 11, 7))
    assert list(frame["trade_date"]) == [date(2026, 11, 5), date(2026, 11, 6)]  # ascending dates
    assert frame["pct_chg"].tolist() == [1.89, 1.85]
    name, params = fetcher.calls[0]
    assert name == "us_daily" and params["ts_code"] == "NVDA" and params["start_date"] == "20261020"
    assert "pct_change" in params["fields"] and "vwap" not in params["fields"]
    index = fetcher.fetch_index_global("SPX", date(2026, 10, 20), date(2026, 11, 7))
    assert index.iloc[0]["close"] == 5050.0 and "amount" in index.columns

    adapter = SimpleNamespace(fetch_us_daily_market=fetcher.fetch_us_daily_market, fetch_index_global=fetcher.fetch_index_global)
    result = make_service(db, [NVDA, SPX], adapter).refresh(FRIDAY_BOARD_RUN)
    assert result.session == FRIDAY
    assert {m.symbol for m in result.moves} == {"NVDA", "SPX"}
    assert {m.symbol: m.pct_chg for m in result.moves} == {"NVDA": pytest.approx(1.85), "SPX": pytest.approx(1.0)}


def test_mixin_returns_empty_frames_for_empty_responses():
    fetcher = _SdkFetcher({"us_daily": pd.DataFrame(), "index_global": pd.DataFrame()})
    assert fetcher.fetch_us_daily("NVDA", FRIDAY, FRIDAY).empty
    assert fetcher.fetch_index_global("SPX", FRIDAY, FRIDAY).empty
