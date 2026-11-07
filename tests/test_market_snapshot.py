# -*- coding: utf-8 -*-
"""Offline tests for board / whole-market snapshot ingestion.

The Tushare client is replaced by an in-memory fake at the ``pro_api()`` boundary
(``TushareFetcher._api``), so unit conversion, paging, retry, merging and persistence
logic all really run. No external request is ever made.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import data_provider.tushare_fetcher as tushare_fetcher_module
import data_provider.tushare_market_data as market_data_module
from data_provider.base import DataFetchError
from data_provider.tushare_fetcher import TushareFetcher
from src.repositories.market_snapshot_repo import MarketSnapshotRepository
from src.services.market_snapshot_service import MarketSnapshotService
from src.storage import (
    BoardCatalog,
    BoardDaily,
    BoardMembership,
    BoardSignal,
    DailyRun,
    DatabaseManager,
    MarketDaily,
    MarketStockDaily,
)

D1 = date(2026, 11, 2)
D2 = date(2026, 11, 3)
D3 = date(2026, 11, 4)


# ----------------------------------------------------------------------
# Fake Tushare API
# ----------------------------------------------------------------------
class FakeTushareApi:
    """Mimics ``pro_api()``: raw string dates, Tushare units, row caps, offset/limit."""

    def __init__(self) -> None:
        self.tables: Dict[str, pd.DataFrame] = {}
        self.calls: List[tuple] = []
        self.errors: Dict[str, Any] = {}  # api_name -> Exception or callable(n_calls) -> Exception|None
        self.server_cap: Dict[str, int] = {}
        self.ignore_offset = False

    def __getattr__(self, api_name: str):
        if api_name.startswith("_"):
            raise AttributeError(api_name)

        def caller(**kwargs):
            return self._query(api_name, **kwargs)

        return caller

    def calls_of(self, api_name: str) -> List[Dict[str, Any]]:
        return [kw for name, kw in self.calls if name == api_name]

    def _query(self, api_name: str, **kwargs) -> pd.DataFrame:
        self.calls.append((api_name, dict(kwargs)))
        err = self.errors.get(api_name)
        if callable(err):
            err = err(len(self.calls_of(api_name)))
        if err is not None:
            raise err
        df = self.tables.get(api_name)
        if df is None:
            return pd.DataFrame()
        df = df.copy()
        for key in ("trade_date", "ts_code", "list_status", "idx_type", "suspend_type"):
            if key in kwargs and kwargs[key] not in (None, "") and key in df.columns:
                df = df[df[key] == kwargs[key]]
        if "start_date" in kwargs or "end_date" in kwargs:
            col = "cal_date" if "cal_date" in df.columns else "trade_date"
            if "start_date" in kwargs:
                df = df[df[col] >= kwargs["start_date"]]
            if "end_date" in kwargs:
                df = df[df[col] <= kwargs["end_date"]]
        df = df.reset_index(drop=True)
        offset = 0 if self.ignore_offset else int(kwargs.get("offset", 0))
        limit = int(kwargs.get("limit", 10 ** 9))
        cap = self.server_cap.get(api_name, 10 ** 9)
        df = df.iloc[offset: offset + min(limit, cap)]
        fields = kwargs.get("fields")
        if fields:
            wanted = [f for f in fields.split(",") if f in df.columns]
            df = df[wanted]
        return df.reset_index(drop=True)


def _table(columns: List[str], rows: List[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def build_api(days=(D1, D2)) -> FakeTushareApi:
    api = FakeTushareApi()
    ymd = [d.strftime("%Y%m%d") for d in days]

    daily_cols = ["ts_code", "trade_date", "pre_close", "close", "pct_chg", "amount"]
    basic_cols = ["ts_code", "trade_date", "turnover_rate", "circ_mv", "limit_status"]
    limit_cols = ["trade_date", "ts_code", "name", "open_times", "limit_times", "limit"]
    susp_cols = ["ts_code", "trade_date", "suspend_type", "suspend_timing"]
    daily, basic, limits, susp = [], [], [], []
    for d in ymd:
        daily += [
            ("600519.SH", d, 1500.0, 1515.0, 1.0, 1000000.5),  # amount 千元
            ("300750.SZ", d, 200.0, 240.0, 20.0, 500000.0),
            ("000001.SZ", d, 11.0, 11.5, 4.55, 300000.0),
            ("600001.SH", d, 3.0, 3.15, 5.0, 1000.0),  # ST
            ("688001.SH", d, 50.0, 49.0, -2.0, 2000.0),
            ("900901.SH", d, 0.5, 0.5, 0.0, 1.0),  # B share
        ]
        basic += [
            ("600519.SH", d, 0.3, 200.0, 1),  # circ_mv 万元
            ("300750.SZ", d, 5.0, 100.0, 2),
            ("000001.SZ", d, 1.0, 50.0, 1),
            ("600001.SH", d, 2.0, 10.0, 2),
            ("688001.SH", d, 3.0, 20.0, 5),
        ]
        limits += [
            (d, "300750.SZ", "宁德时代", 0, 2, "U"),
            (d, "000001.SZ", "平安银行", 3, 1, "Z"),
            (d, "688001.SH", "某科创", 1, 1, "D"),
        ]
        susp.append(("000002.SZ", d, "S", None))
    api.tables["daily"] = _table(daily_cols, daily)
    api.tables["daily_basic"] = _table(basic_cols, basic)
    api.tables["limit_list_d"] = _table(limit_cols, limits)
    api.tables["suspend_d"] = _table(susp_cols, susp)
    api.tables["stock_basic"] = _table(
        ["ts_code", "symbol", "name", "market", "list_date", "list_status"],
        [
            ("600519.SH", "600519", "贵州茅台", "主板", "20010827", "L"),
            ("300750.SZ", "300750", "宁德时代", "创业板", "20180611", "L"),
            ("000001.SZ", "000001", "平安银行", "主板", "19910403", "L"),
            ("600001.SH", "600001", "*ST某某", "主板", "19980122", "L"),
            ("688001.SH", "688001", "某科创", "科创板", "20190722", "L"),
            ("000002.SZ", "000002", "万科A", "主板", "19910129", "L"),
        ],
    )
    api.tables["index_daily"] = _table(
        ["ts_code", "trade_date", "close", "pct_chg"],
        [("000985.CSI", d, 5000.0, 0.8) for d in ymd],
    )
    idx_cols = ["ts_code", "trade_date", "name", "idx_type", "level", "total_mv"]
    idx = []
    for d in ymd:
        idx += [
            ("BK0477.DC", d, "酿酒行业", "行业板块", "1", 100.0),  # total_mv 万元
            ("BK1184.DC", d, "人形机器人", "概念板块", None, 200.0),
            ("BK9999.DC", d, "某地域", "地域板块", None, 1.0),
        ]
    api.tables["dc_index"] = _table(idx_cols, idx)
    dc_daily = []
    for d in ymd:
        dc_daily += [
            ("BK0477.DC", d, 1.23, 1.0e10),
            ("BK1184.DC", d, 3.45, 2.0e10),
            ("BK9999.DC", d, 0.1, 1.0e8),
        ]
    api.tables["dc_daily"] = _table(["ts_code", "trade_date", "pct_change", "amount"], dc_daily)
    api.tables["dc_member"] = _table(["trade_date", "ts_code", "con_code"], [])
    api.tables["trade_cal"] = _table(
        ["exchange", "cal_date", "is_open"],
        [("SSE", d, 1) for d in ymd],
    )
    return api


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def config():
    return SimpleNamespace(
        market_snapshot_retention_days=250,
        market_snapshot_backfill_days=60,
        market_benchmark_code="000985.CSI",
        market_snapshot_requests_per_minute=10000,
    )


@pytest.fixture()
def api():
    return build_api()


@pytest.fixture()
def fetcher(monkeypatch, api):
    monkeypatch.setattr(
        tushare_fetcher_module, "get_config", lambda: SimpleNamespace(tushare_token=None)
    )
    monkeypatch.setattr(TushareFetcher, "_market_retry_base_delay", 0.0)
    monkeypatch.setattr(TushareFetcher, "_market_rate_limit_wait", 0.0)
    instance = TushareFetcher(rate_limit_per_minute=100000)
    instance._api = api
    return instance


@pytest.fixture()
def service(db, fetcher, config):
    return MarketSnapshotService(fetcher=fetcher, repo=MarketSnapshotRepository(db), config=config)


def stock_rows(db, day) -> Dict[str, MarketStockDaily]:
    with db.get_session() as session:
        rows = session.execute(select(MarketStockDaily).where(MarketStockDaily.trade_date == day)).scalars().all()
        for r in rows:
            session.expunge(r)
    return {r.code: r for r in rows}


def quality(db, day) -> Dict[str, Any]:
    with db.get_session() as session:
        raw = session.execute(select(MarketDaily.quality).where(MarketDaily.trade_date == day)).scalar_one()
    return json.loads(raw)["ingestion"]


# ----------------------------------------------------------------------
# Schema
# ----------------------------------------------------------------------
def test_tables_created_with_constraints(db):
    with db.session_scope() as session:
        run = DailyRun(trade_date=D1, status="done")
        board = BoardCatalog(
            source="dc", board_type="concept", code="BK1.DC", name="x",
            first_seen_date=D1, last_seen_date=D1, status="active",
        )
        session.add_all([run, board])
        session.flush()
        session.add(BoardSignal(run_id=run.id, trade_date=D1, board_id=board.id, signal_type="first_launch"))
        session.flush()
        run_id, board_id = run.id, board.id
    with pytest.raises(IntegrityError):
        with db.session_scope() as session:
            session.add(BoardSignal(run_id=run_id, trade_date=D1, board_id=board_id, signal_type="ebb"))
    with pytest.raises(IntegrityError):
        with db.session_scope() as session:
            session.add(BoardCatalog(
                source="dc", board_type="concept", code="BK1.DC", name="dup",
                first_seen_date=D1, last_seen_date=D1, status="active",
            ))
    # board_daily can hold a vendor-only row (all calculated columns nullable)
    with db.session_scope() as session:
        session.add(BoardDaily(trade_date=D1, board_id=board_id, vendor_ret=1.0))


# ----------------------------------------------------------------------
# Fetcher: units, paging, retry
# ----------------------------------------------------------------------
def test_fetcher_unit_conversions(fetcher):
    daily = fetcher.fetch_market_daily(D1).set_index("code")
    assert daily.loc["600519", "amount"] == pytest.approx(1000000.5 * 1000)
    basic = fetcher.fetch_market_daily_basic(D1).set_index("code")
    assert basic.loc["600519", "float_mv"] == pytest.approx(200.0 * 10000)
    dc = fetcher.fetch_dc_index(D1, "行业板块").set_index("ts_code")
    assert dc.loc["BK0477.DC", "total_mv"] == pytest.approx(100.0 * 10000)
    # dc_daily amount is documented in yuan: unchanged
    vendor = fetcher.fetch_dc_daily(D1).set_index("ts_code")
    assert vendor.loc["BK0477.DC", "amount"] == pytest.approx(1.0e10)


def test_fetcher_pages_when_response_hits_row_cap(fetcher, api, monkeypatch):
    monkeypatch.setattr(market_data_module, "DAILY_PAGE_CAP", 4)
    api.server_cap["daily"] = 4  # 6 rows per date, server returns at most 4 per call
    df = fetcher.fetch_market_daily(D1)
    assert sorted(df["code"]) == ["000001", "300750", "600001", "600519", "688001", "900901"]
    calls = api.calls_of("daily")
    assert len(calls) == 2
    assert "offset" not in calls[0]
    assert calls[1]["offset"] == 4 and calls[1]["limit"] == 4


def test_fetcher_does_not_page_below_cap(fetcher, api):
    fetcher.fetch_market_daily(D1)
    assert len(api.calls_of("daily")) == 1


def test_fetcher_flags_incomplete_when_offset_ignored(fetcher, api, monkeypatch):
    monkeypatch.setattr(market_data_module, "DAILY_PAGE_CAP", 4)
    api.server_cap["daily"] = 4
    api.ignore_offset = True
    with pytest.raises(market_data_module.IncompleteMarketDataError) as exc_info:
        fetcher.fetch_market_daily(D1)
    assert len(exc_info.value.partial) == 4


def test_fetcher_retries_transient_errors_but_not_permission_errors(fetcher, api):
    api.errors["daily"] = lambda n: ConnectionError("timeout") if n < 3 else None
    assert len(fetcher.fetch_market_daily(D1)) == 6
    assert len(api.calls_of("daily")) == 3

    api.calls.clear()
    api.errors["daily_basic"] = Exception("抱歉，您没有接口访问权限")
    with pytest.raises(DataFetchError):
        fetcher.fetch_market_daily_basic(D1)
    assert len(api.calls_of("daily_basic")) == 1


def test_fetcher_trade_cal_and_stock_basic(fetcher):
    assert fetcher.fetch_trading_dates(D1, D2) == [D1, D2]
    basic = fetcher.fetch_stock_basic_snapshot().set_index("code")
    assert basic.loc["300750", "exchange_board"] == "gem"
    assert basic.loc["688001", "exchange_board"] == "star"
    assert basic.loc["600519", "list_date"] == date(2001, 8, 27)


def test_fetcher_dc_member_pages_by_date_range(fetcher, api, monkeypatch):
    monkeypatch.setattr(market_data_module, "DC_MEMBER_PAGE_CAP", 3)
    api.server_cap["dc_member"] = 3
    api.tables["dc_member"] = _table(
        ["trade_date", "ts_code", "con_code"],
        [(d, "BK1184.DC", c) for d in ("20261102", "20261103") for c in ("600519.SH", "300750.SZ", "000001.SZ")],
    )
    df = fetcher.fetch_dc_member("BK1184.DC", start_date=D1, end_date=D2)
    assert len(df) == 6
    assert set(df["trade_date"]) == {D1, D2}


# ----------------------------------------------------------------------
# Ingestion service
# ----------------------------------------------------------------------
def test_ingest_merges_sources_and_converts_units(service, db):
    report = service.ingest_trade_date(D1)
    assert report.status == "complete", report.to_dict()
    rows = stock_rows(db, D1)
    assert set(rows) == {"600519", "300750", "000001", "600001", "688001", "000002"}  # B share excluded
    moutai = rows["600519"]
    assert moutai.name == "贵州茅台"
    assert moutai.exchange_board == "main"
    assert moutai.amount == pytest.approx(1000000.5 * 1000)
    assert moutai.float_mv == pytest.approx(2_000_000.0)
    assert moutai.turnover_rate == pytest.approx(0.3)
    assert moutai.prev_close == 1500.0 and moutai.close == 1515.0
    assert moutai.list_date == date(2001, 8, 27)
    assert moutai.limit_state == "none"
    assert moutai.data_source == "tushare"
    assert rows["300750"].exchange_board == "gem"
    assert rows["688001"].exchange_board == "star"


def test_limit_fields_and_st_and_suspended(service, db):
    service.ingest_trade_date(D1)
    rows = stock_rows(db, D1)

    up = rows["300750"]
    assert up.limit_state == "up" and up.touched_limit_up is True
    assert up.open_times == 0 and up.consecutive_up == 2

    broke = rows["000001"]  # 炸板
    assert broke.touched_limit_up is True and broke.open_times == 3
    assert broke.limit_state == "none"

    assert rows["600519"].touched_limit_up is False  # not on the board, but the list was complete
    assert rows["600519"].open_times is None

    assert rows["688001"].limit_state == "down"
    assert rows["688001"].touched_limit_up is False  # 跌停榜不是触及涨停
    assert rows["688001"].open_times is None

    st = rows["600001"]
    assert st.is_st is True
    assert st.touched_limit_up is None and st.open_times is None and st.consecutive_up is None
    assert st.limit_state == "up"  # daily_basic still reports its close state

    susp = rows["000002"]
    assert susp.is_suspended is True
    assert susp.close is None and susp.prev_close is None and susp.pct_chg is None and susp.amount is None
    assert susp.name == "万科A" and susp.is_st is False
    assert rows["600519"].is_suspended is False


def test_missing_daily_basic_is_quality_flag_not_zero(service, db, api):
    api.errors["daily_basic"] = Exception("service unavailable")
    report = service.ingest_trade_date(D1)
    assert report.status == "partial"
    assert "daily_basic" in report.missing
    rows = stock_rows(db, D1)
    assert rows["600519"].float_mv is None and rows["600519"].turnover_rate is None
    assert rows["600519"].limit_state is None  # unknown, not 'none'
    assert rows["300750"].limit_state == "up"  # recovered from limit_list_d
    q = quality(db, D1)
    assert q["status"] == "partial" and q["missing"] == ["daily_basic"]
    assert "service unavailable" in q["errors"]["daily_basic"]


def test_missing_limit_list_leaves_touched_null(service, db, api):
    api.tables["limit_list_d"] = api.tables["limit_list_d"].iloc[0:0]  # late data: empty
    report = service.ingest_trade_date(D1)
    assert report.status == "partial" and "limit_list_d" in report.missing
    rows = stock_rows(db, D1)
    assert all(r.touched_limit_up is None for r in rows.values())
    assert rows["300750"].limit_state == "up"  # from daily_basic
    assert quality(db, D1)["missing"] == ["limit_list_d"]


def test_incomplete_limit_list_only_marks_listed_stocks(service, db, api, monkeypatch):
    monkeypatch.setattr(market_data_module, "LIMIT_LIST_PAGE_CAP", 2)
    api.server_cap["limit_list_d"] = 2
    api.ignore_offset = True  # paging cannot make progress
    report = service.ingest_trade_date(D1)
    assert "limit_list_d" in report.incomplete and report.status == "partial"
    rows = stock_rows(db, D1)
    assert rows["300750"].touched_limit_up is True
    assert rows["600519"].touched_limit_up is None  # absence is not proof when the list is truncated


def test_no_daily_means_no_data_and_existing_rows_are_kept(service, db, api):
    service.ingest_trade_date(D1)
    before = stock_rows(db, D1)
    api.errors["daily"] = Exception("not ready")
    report = service.ingest_trade_date(D1, force=True)
    assert report.status == "no_data"
    assert stock_rows(db, D1).keys() == before.keys()
    assert quality(db, D1)["status"] == "complete"


def test_benchmark_and_missing_benchmark(service, db, api):
    service.ingest_trade_date(D1)
    with db.get_session() as session:
        row = session.get(MarketDaily, D1)
        assert row.benchmark_code == "000985.CSI" and row.benchmark_ret == pytest.approx(0.8)

    api.tables["index_daily"] = api.tables["index_daily"].iloc[0:0]
    report = service.ingest_trade_date(D2)
    assert "index_daily" in report.missing
    with db.get_session() as session:
        row = session.get(MarketDaily, D2)
        assert row.benchmark_ret is None
        # ingestion does not fabricate market width numbers
        assert row.total_amount is None and row.up_count is None


def test_rerun_is_idempotent_and_skips_complete_dates(service, db, api):
    first = service.ingest_trade_date(D1)
    calls_after_first = len(api.calls)
    again = service.ingest_trade_date(D1)
    assert again.status == "skipped" and again.stock_rows == first.stock_rows
    assert len(api.calls) == calls_after_first  # zero requests

    forced = service.ingest_trade_date(D1, force=True)
    assert forced.status == "complete"
    assert len(stock_rows(db, D1)) == first.stock_rows
    with db.get_session() as session:
        assert len(session.execute(select(BoardCatalog)).scalars().all()) == 2  # no duplicate boards
        assert len(session.execute(select(BoardDaily).where(BoardDaily.trade_date == D1)).scalars().all()) == 2
        assert len(session.execute(select(MarketDaily)).scalars().all()) == 1


def test_late_data_is_filled_by_rerun(service, db, api):
    api.errors["daily_basic"] = Exception("not updated yet")
    assert service.ingest_trade_date(D1).status == "partial"
    api.errors.clear()
    report = service.ingest_trade_date(D1)  # partial dates are not skipped
    assert report.status == "complete"
    assert stock_rows(db, D1)["600519"].float_mv == pytest.approx(2_000_000.0)
    assert quality(db, D1)["missing"] == []


def test_catalog_and_vendor_reference_columns(service, db):
    service.ingest_trade_date(D1)
    boards = {b["code"]: b for b in service.repo.list_boards(status=None)}
    assert set(boards) == {"BK0477.DC", "BK1184.DC"}  # region boards are not ingested in v1
    assert boards["BK0477.DC"]["board_type"] == "industry"
    assert boards["BK1184.DC"]["board_type"] == "concept"
    assert boards["BK1184.DC"]["first_seen_date"] == D1

    with db.get_session() as session:
        rows = {r.board_id: r for r in session.execute(select(BoardDaily)).scalars()}
    bk = rows[boards["BK1184.DC"]["id"]]
    assert bk.vendor_ret == pytest.approx(3.45) and bk.vendor_amount == pytest.approx(2.0e10)
    assert bk.ret_cap is None and bk.rank_pct is None  # calculated columns untouched


def test_catalog_first_last_seen_and_retirement(service, db, api):
    service.ingest_trade_date(D2)
    service.ingest_trade_date(D1)  # older date back-filled afterwards
    boards = {b["code"]: b for b in service.repo.list_boards(status=None)}
    bk = boards["BK1184.DC"]
    assert bk["first_seen_date"] == D1 and bk["last_seen_date"] == D2 and bk["status"] == "active"

    # BK1184 disappears from the list for more than 10 days
    late = date(2026, 11, 20)
    idx = api.tables["dc_index"]
    gone = idx[(idx["ts_code"] != "BK1184.DC") & (idx["trade_date"] == "20261103")].copy()
    gone["trade_date"] = "20261120"
    # another concept board is still listed, so the concept list itself is not a vendor hole
    gone.loc[len(gone)] = ("BK2000.DC", "20261120", "另一个概念", "概念板块", None, 1.0)
    api.tables["dc_index"] = pd.concat([idx, gone], ignore_index=True)
    for name in ("daily", "daily_basic", "limit_list_d", "suspend_d", "index_daily", "dc_daily"):
        table = api.tables[name]
        copy = table[table["trade_date"] == "20261103"].copy()
        copy["trade_date"] = "20261120"
        api.tables[name] = pd.concat([table, copy], ignore_index=True)
    service.ingest_trade_date(late)
    boards = {b["code"]: b for b in service.repo.list_boards(status=None)}
    assert boards["BK1184.DC"]["status"] == "retired"
    assert boards["BK0477.DC"]["status"] == "active"


def test_stock_basic_fetched_once_across_dates(service, api):
    service.ingest_trade_date(D1)
    service.ingest_trade_date(D2)
    assert len(api.calls_of("stock_basic")) == 3  # L / D / P once; second date reuses the cache


def test_retention_prunes_oldest_trading_days(db, fetcher, config):
    config.market_snapshot_retention_days = 2
    service = MarketSnapshotService(fetcher=fetcher, repo=MarketSnapshotRepository(db), config=config)
    fetcher._api.tables = build_api(days=(D1, D2, D3)).tables
    for day in (D1, D2, D3):
        service.ingest_trade_date(day)
    assert stock_rows(db, D1) == {}
    assert stock_rows(db, D2) and stock_rows(db, D3)


# ----------------------------------------------------------------------
# Membership zipper table
# ----------------------------------------------------------------------
def _members(api, board, per_day: Dict[str, List[str]]):
    rows = [(d, board, c) for d, codes in per_day.items() for c in codes]
    api.tables["dc_member"] = pd.concat(
        [api.tables["dc_member"], _table(["trade_date", "ts_code", "con_code"], rows)], ignore_index=True
    )


def _board_id(service, code):
    return service.repo.get_board_code_map()[code]


def test_zipper_enter_leave_reenter(service, db, api):
    service.ingest_trade_date(D1)
    board_id = _board_id(service, "BK1184.DC")
    repo = service.repo
    days = {d: date(2026, 11, d) for d in (2, 3, 4, 5)}

    repo.apply_membership_snapshots(board_id, {days[2]: {"600519", "300750"}})
    repo.apply_membership_snapshots(board_id, {days[3]: {"600519", "300750", "000001"}})  # 000001 enters
    repo.apply_membership_snapshots(board_id, {days[4]: {"600519", "000001"}})  # 300750 leaves
    repo.apply_membership_snapshots(board_id, {days[5]: {"600519", "000001", "300750"}})  # 300750 re-enters

    with db.get_session() as session:
        rows = session.execute(
            select(BoardMembership).where(BoardMembership.board_id == board_id).order_by(
                BoardMembership.stock_code, BoardMembership.valid_from
            )
        ).scalars().all()
        table = [(r.stock_code, r.valid_from, r.valid_to, r.origin) for r in rows]
    assert table == [
        ("000001", days[3], None, "observed"),
        ("300750", days[2], days[3], "observed"),  # last seen on the 3rd
        ("300750", days[5], None, "observed"),  # new row for the re-entry
        ("600519", days[2], None, "observed"),  # unchanged members never get a new row
    ]
    assert repo.get_members_on(board_id, days[2]) == {"600519", "300750"}
    assert repo.get_members_on(board_id, days[3]) == {"600519", "300750", "000001"}
    assert repo.get_members_on(board_id, days[4]) == {"600519", "000001"}
    assert repo.get_members_on(board_id, days[5]) == {"600519", "000001", "300750"}


def test_membership_reapply_is_idempotent_and_empty_snapshot_is_ignored(service, db):
    service.ingest_trade_date(D1)
    board_id = _board_id(service, "BK1184.DC")
    repo = service.repo
    first = repo.apply_membership_snapshots(
        board_id, {D1: {"600519", "300750"}, D2: {"600519"}}
    )
    assert first["opened"] == 2 and first["closed"] == 1
    again = repo.apply_membership_snapshots(board_id, {D1: {"600519", "300750"}, D2: {"600519"}})
    assert again["opened"] == 0 and again["closed"] == 0 and len(again["skipped_dates"]) == 2

    empty = repo.apply_membership_snapshots(board_id, {D3: set()})  # vendor hole, not "everyone left"
    assert empty["empty_dates"] == [D3] and empty["closed"] == 0
    assert repo.get_members_on(board_id, D3) == {"600519"}
    with db.get_session() as session:
        assert session.get(BoardCatalog, board_id).membership_checked_date == D2


def test_daily_candidate_refresh_and_weekly_full_refresh(service, db, api):
    _members(api, "BK1184.DC", {"20261102": ["600519.SH", "300750.SZ"]})
    _members(api, "BK0477.DC", {"20261102": ["600519.SH"]})
    service.ingest_trade_date(D1)
    # candidate refresh still runs when the date itself is already complete (skipped)
    report = service.ingest_trade_date(D1, candidate_board_ids=[_board_id(service, "BK1184.DC")])
    assert report.status == "skipped"
    assert report.membership["BK1184.DC"]["status"] == "ok"
    assert len(api.calls_of("dc_member")) == 1  # candidates only
    assert api.calls_of("dc_member")[0]["trade_date"] == "20261102"
    assert service.repo.get_members_on(_board_id(service, "BK1184.DC"), D1) == {"600519", "300750"}
    assert service.repo.get_members_on(_board_id(service, "BK0477.DC"), D1) == set()

    # the next day 300750 leaves; the weekly refresh covers every date since the last refresh
    _members(api, "BK1184.DC", {"20261103": ["600519.SH"], "20261104": ["600519.SH", "000001.SZ"]})
    _members(api, "BK0477.DC", {"20261103": ["600519.SH"], "20261104": ["600519.SH"]})
    api.calls.clear()
    summary = service.refresh_all_membership(D3)
    assert summary["refreshed"] == 2 and not summary["failed"]
    ranges = {kw["ts_code"]: (kw["start_date"], kw["end_date"]) for kw in api.calls_of("dc_member")}
    assert ranges["BK1184.DC"] == ("20261103", "20261104")  # resumes after the last checked date
    assert ranges["BK0477.DC"] == ("20241220", "20261104")  # never refreshed: from the first dc_member date
    bk = _board_id(service, "BK1184.DC")
    assert service.repo.get_members_on(bk, D1) == {"600519", "300750"}
    assert service.repo.get_members_on(bk, D2) == {"600519"}
    assert service.repo.get_members_on(bk, D3) == {"600519", "000001"}

    api.calls.clear()  # already up to date: nothing requested
    again = service.refresh_all_membership(D3)
    assert again["up_to_date"] == 2 and not api.calls_of("dc_member")


def test_membership_failure_of_one_board_does_not_stop_the_batch(service, api):
    _members(api, "BK0477.DC", {"20261102": ["600519.SH"]})
    service.ingest_trade_date(D1)
    api.errors["dc_member"] = lambda n: Exception("boom") if n <= 3 else None  # BK1184 exhausts its retries
    summary = service.refresh_all_membership(D1)
    assert "BK1184.DC" in summary["failed"] or "BK0477.DC" in summary["failed"]
    assert summary["refreshed"] + len(summary["failed"]) == 2


# ----------------------------------------------------------------------
# Backfill
# ----------------------------------------------------------------------
def test_backfill_resumes_without_repeating_finished_dates(service, db, api):
    api.tables = build_api(days=(D1, D2, D3)).tables
    api.errors["daily"] = lambda n: Exception("not ready") if any(
        kw.get("trade_date") == "20261103" for kw in api.calls_of("daily")[-1:]
    ) else None

    first = service.backfill(end_date=D3, days=3, include_members=False)
    assert first.ingested == ["2026-11-02", "2026-11-04"]
    assert first.no_data == ["2026-11-03"]
    assert not first.aborted

    api.errors.clear()
    api.calls.clear()
    second = service.backfill(end_date=D3, days=3, include_members=False)
    assert second.skipped == ["2026-11-02", "2026-11-04"]
    assert second.ingested == ["2026-11-03"]
    assert {kw["trade_date"] for kw in api.calls_of("daily")} == {"20261103"}  # only the missing date
    assert len(stock_rows(db, D2)) == 6


def test_backfill_aborts_after_consecutive_failures(service, monkeypatch):
    attempts = []

    def boom(day, **kwargs):
        attempts.append(day)
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "ingest_trade_date", boom)
    report = service.backfill(end_date=D2, days=2, include_members=False, max_consecutive_failures=2)
    assert report.aborted and len(report.failed) == 2 and len(attempts) == 2


def test_backfill_includes_members_resumable(service, api):
    _members(api, "BK1184.DC", {"20261102": ["600519.SH"], "20261103": ["600519.SH", "300750.SZ"]})
    _members(api, "BK0477.DC", {"20261102": ["600519.SH"], "20261103": ["600519.SH"]})
    first = service.backfill(end_date=D2, days=2)
    assert first.membership["refreshed"] == 2
    api.calls.clear()
    second = service.backfill(end_date=D2, days=2)
    assert second.skipped == ["2026-11-02", "2026-11-03"]
    assert second.membership["up_to_date"] == 2
    assert {name for name, _ in api.calls} == {"trade_cal"}  # a finished backfill only re-reads the calendar
