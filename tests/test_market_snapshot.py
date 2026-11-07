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


def test_range_refresh_resumes_per_board_and_never_repeats_finished_boards(service, db, api):
    """``refresh_all_membership`` (cold start / manual repair) still works board by board."""
    _members(api, "BK1184.DC", {"20261102": ["600519.SH", "300750.SZ"]})
    _members(api, "BK0477.DC", {"20261102": ["600519.SH"]})
    service.ingest_trade_date(D1)
    first = service.refresh_all_membership(D1)
    assert first["refreshed"] == 2 and not first["failed"]
    assert service.repo.get_members_on(_board_id(service, "BK1184.DC"), D1) == {"600519", "300750"}
    assert service.repo.get_members_on(_board_id(service, "BK0477.DC"), D1) == {"600519"}

    # the next day 300750 leaves; the refresh covers every date since the last refresh
    _members(api, "BK1184.DC", {"20261103": ["600519.SH"], "20261104": ["600519.SH", "000001.SZ"]})
    _members(api, "BK0477.DC", {"20261103": ["600519.SH"], "20261104": ["600519.SH"]})
    api.calls.clear()
    summary = service.refresh_all_membership(D3)
    assert summary["refreshed"] == 2 and not summary["failed"]
    ranges = {kw["ts_code"]: (kw["start_date"], kw["end_date"]) for kw in api.calls_of("dc_member")}
    assert ranges["BK1184.DC"] == ("20261103", "20261104")  # resumes after the last checked date
    assert ranges["BK0477.DC"] == ("20261103", "20261104")
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


# ----------------------------------------------------------------------
# 17:00 flow: derived fields, provisional dates, full-day membership
# ----------------------------------------------------------------------
# D1 is fully published; on D2 only ``daily`` / ``stk_limit`` / ``suspend_d`` exist (the 17:00 state).
_EVENING_DAILY_COLS = ["ts_code", "trade_date", "open", "high", "low", "pre_close", "close", "pct_chg", "vol", "amount"]
_EVENING_NAMES = {
    "600519.SH": ("贵州茅台", "主板"), "300750.SZ": ("宁德时代", "创业板"), "000001.SZ": ("平安银行", "主板"),
    "600001.SH": ("*ST某某", "主板"), "688001.SH": ("某科创", "科创板"), "000333.SZ": ("美的集团", "主板"),
    "002001.SZ": ("新和成", "主板"), "002002.SZ": ("鸿达兴业", "主板"), "002003.SZ": ("伟星股份", "主板"),
    "000002.SZ": ("万科A", "主板"),
}
# code -> (open, high, low, pre_close, close, vol, up_limit, down_limit)
_EVENING_D1 = {
    "600519.SH": (100, 101, 99, 99, 100, 10, 108.9, 89.1),
    "300750.SZ": (110, 120, 110, 100, 120, 50, 120, 80),  # closed at +20%
    "000001.SZ": (10, 10.3, 10, 10, 10.2, 30, 11, 9),
    "600001.SH": (10, 10.5, 10, 10, 10.5, 20, 10.5, 9.5),  # ST at +5%
    "688001.SH": (50, 50, 48, 50, 49, 40, 60, 40),
    "000333.SZ": (10, 10.2, 9.9, 10, 10, 70, 11, 9),
    "002001.SZ": (20, 20, 19.5, 20, 19.8, 25, 22, 18),
    "002002.SZ": (10, 10.1, 9.9, 10, 10, 15, 11, 9),
    "002003.SZ": (10, 10.1, 9.9, 10, 10, 15, 11, 9),
}
_EVENING_D2 = {
    "600519.SH": (100, 101.5, 100, 100, 101, 20, 110, 90),
    "300750.SZ": (130, 144, 130, 120, 144, 60, 144, 96),  # second limit-up in a row
    "000001.SZ": (10.3, 11.22, 10.3, 10.2, 10.8, 40, 11.22, 9.18),  # touched, did not hold: broken
    "600001.SH": (10.6, 11.03, 10.6, 10.5, 11.03, 25, 11.03, 9.98),  # ST closed at +5%
    "688001.SH": (45, 45, 39.2, 49, 39.2, 90, 58.8, 39.2),  # down limit, not one-word
    "000333.SZ": (11, 11, 11, 10, 11, 5, 11, 9),  # one-word up
    "002001.SZ": (19, 19, 18, 19.8, 18, 30, 21.78, 17.82),  # one tick above the down limit: not a limit
    "002002.SZ": (10.5, 11, 10.5, 10, 10.9999999, 22, 11, 9),  # float noise below the limit: still limit-up
    "002003.SZ": (10.5, 10.99, 10.5, 10, 10.99, 22, 11, 9),  # one tick below the limit: not a limit
}


def _evening_daily(day: str, data) -> List[tuple]:
    rows = []
    for code, (o, h, low, pre, c, vol, _up, _down) in data.items():
        rows.append((code, day, o, h, low, pre, c, round((c / pre - 1) * 100, 4), vol, 1000.0))
    return rows


def build_evening_api() -> FakeTushareApi:
    api = FakeTushareApi()
    api.tables["daily"] = _table(
        _EVENING_DAILY_COLS,
        _evening_daily("20261102", _EVENING_D1) + _evening_daily("20261103", _EVENING_D2),
    )
    api.tables["stk_limit"] = _table(
        ["ts_code", "trade_date", "up_limit", "down_limit"],
        [(code, day, v[6], v[7]) for day, data in (("20261102", _EVENING_D1), ("20261103", _EVENING_D2))
         for code, v in data.items()] + [("510300.SH", "20261103", 5.0, 4.0)],  # a fund: ignored
    )
    # D1 daily_basic: circ_mv in 万元 (600519: 1000 -> 1e7 yuan, close 100 -> 1e5 shares)
    api.tables["daily_basic"] = _table(
        ["ts_code", "trade_date", "turnover_rate", "circ_mv", "limit_status"],
        [
            ("600519.SH", "20261102", 1.0, 1000.0, 1),
            ("300750.SZ", "20261102", 5.0, 500.0, 2),
            ("000001.SZ", "20261102", 1.0, 400.0, 1),
            ("600001.SH", "20261102", 2.0, 100.0, 2),
            ("688001.SH", "20261102", 3.0, 300.0, 4),
            ("000333.SZ", "20261102", 1.5, 800.0, 0),
            ("002001.SZ", "20261102", 1.0, 200.0, 4),
            ("002002.SZ", "20261102", 1.0, 200.0, 0),
            ("002003.SZ", "20261102", 1.0, 200.0, 0),
        ],
    )
    api.tables["limit_list_d"] = _table(
        ["trade_date", "ts_code", "name", "open_times", "limit_times", "limit"],
        [("20261102", "300750.SZ", "宁德时代", 0, 2, "U")],
    )
    api.tables["suspend_d"] = _table(
        ["ts_code", "trade_date", "suspend_type", "suspend_timing"],
        [("000002.SZ", "20261102", "S", None), ("000002.SZ", "20261103", "S", None)],
    )
    api.tables["stock_basic"] = _table(
        ["ts_code", "symbol", "name", "market", "list_date", "list_status"],
        [(code, code.split(".")[0], name, market, "20100101", "L") for code, (name, market) in _EVENING_NAMES.items()],
    )
    api.tables["index_daily"] = _table(
        ["ts_code", "trade_date", "close", "pct_chg"],
        [("000985.CSI", "20261102", 5000.0, 0.5), ("000985.CSI", "20261103", 5010.0, 0.2)],
    )
    api.tables["dc_index"] = _table(
        ["ts_code", "trade_date", "name", "idx_type", "level", "total_mv"],
        [("BK0477.DC", "20261102", "酿酒行业", "行业板块", "1", 100.0),
         ("BK1184.DC", "20261102", "人形机器人", "概念板块", None, 200.0)],
    )
    api.tables["dc_daily"] = _table(
        ["ts_code", "trade_date", "pct_change", "amount"],
        [("BK0477.DC", "20261102", 1.0, 1e10), ("BK1184.DC", "20261102", 2.0, 2e10)],
    )
    api.tables["dc_member"] = _table(["trade_date", "ts_code", "con_code"], [])
    return api


def _concat(api: FakeTushareApi, name: str, columns: List[str], rows: List[tuple]) -> None:
    api.tables[name] = pd.concat([api.tables[name], _table(columns, rows)], ignore_index=True)


@pytest.fixture()
def evening(db, fetcher, config):
    """A service wired to the evening fake API (calendar: D1, D2, D3 are consecutive sessions)."""
    fetcher._api = build_evening_api()
    svc = MarketSnapshotService(
        fetcher=fetcher, repo=MarketSnapshotRepository(db), config=config,
        calendar_fn=lambda start, end: [d for d in (D1, D2, D3) if start <= d <= end],
    )
    svc.api = fetcher._api
    return svc


def test_derived_limit_flags_use_stk_limit_with_tick_tolerance(evening, db):
    evening.ingest_trade_date(D1)
    report = evening.ingest_trade_date(D2, allow_derived=True, boards=False)
    assert report.derived == ["daily_basic", "limit_list_d"]
    assert report.missing == []
    rows = stock_rows(db, D2)

    up = rows["300750"]  # closed at the limit
    assert up.limit_state == "up" and up.touched_limit_up is True
    assert up.open_times is None  # needs limit_list_d
    broken = rows["000001"]  # touched, closed below: 炸板
    assert broken.touched_limit_up is True and broken.limit_state == "none"
    assert rows["600519"].touched_limit_up is False and rows["600519"].limit_state == "none"

    assert rows["000333"].limit_state == "one_word_up" and rows["000333"].touched_limit_up is True
    down = rows["688001"]  # limit-down is a close state; it never counts as touching the up limit
    assert down.limit_state == "down" and down.touched_limit_up is False
    assert rows["002001"].limit_state == "none"  # one tick above the down limit is not a limit

    assert rows["002002"].limit_state == "up" and rows["002002"].touched_limit_up is True  # float noise
    assert rows["002003"].limit_state == "none" and rows["002003"].touched_limit_up is False  # 10.99 < 11.00

    st = rows["600001"]  # ST: state known from stk_limit, but limit_list_d facts stay empty (as with the real list)
    assert st.is_st is True and st.limit_state == "up"
    assert st.touched_limit_up is None and st.open_times is None and st.consecutive_up is None
    assert rows["000002"].is_suspended is True and rows["000002"].limit_state is None


def test_derived_turnover_and_float_cap_come_from_the_previous_daily_basic(evening, db):
    evening.ingest_trade_date(D1)
    evening.ingest_trade_date(D2, allow_derived=True, boards=False)
    rows = stock_rows(db, D2)
    moutai = rows["600519"]  # D1: float cap 1e7 yuan at close 100 -> 1e5 shares; D2: +1%, volume 20 lots
    assert moutai.float_mv == pytest.approx(1.0e7 * 1.01)
    assert moutai.turnover_rate == pytest.approx(20 * 100 / 1.0e5 * 100)  # 2.0 %
    catl = rows["300750"]  # D1: 5e6 yuan at close 120; D2: +20%, 60 lots
    assert catl.float_mv == pytest.approx(5.0e6 * 1.2)
    assert catl.turnover_rate == pytest.approx(60 * 100 / (5.0e6 / 120) * 100)
    assert rows["000002"].float_mv is None and rows["000002"].turnover_rate is None  # suspended, no daily row

    q = quality(db, D2)
    assert q["status"] == "provisional" and q["derived"] == ["daily_basic", "limit_list_d"]
    assert q["basic_source"] == "derived" and q["limit_source"] == "derived"
    assert q["derived_from"] == "2026-11-02" and q["boards"] == "pending" and q["missing"] == []
    assert quality(db, D1)["basic_source"] == "daily_basic" and quality(db, D1)["limit_source"] == "limit_list_d"
    assert quality(db, D1)["status"] == "complete" and quality(db, D1)["boards"] == "done"


def test_derived_streak_continues_only_from_a_clean_previous_row(evening, db):
    api = evening.api
    evening.ingest_trade_date(D1)
    evening.ingest_trade_date(D2, allow_derived=True, boards=False)
    rows = stock_rows(db, D2)
    assert rows["300750"].consecutive_up == 3  # D1 closed up with a real streak of 2
    assert rows["000333"].consecutive_up == 1  # D1 was a normal day: a new streak
    assert rows["000001"].consecutive_up is None  # touched but closed below the limit

    # D3 is derived while D2 (a session in between) is missing locally: the previous row is D1, which is
    # not the previous session, so the streak is unknown (not reset to 1) and the cap still comes from D1
    _concat(api, "daily", _EVENING_DAILY_COLS, _evening_daily("20261104", _EVENING_D2))
    _concat(api, "stk_limit", ["ts_code", "trade_date", "up_limit", "down_limit"],
            [(c, "20261104", v[6], v[7]) for c, v in _EVENING_D2.items()])
    with db.session_scope() as session:
        session.query(MarketStockDaily).filter(MarketStockDaily.trade_date == D2).delete()
    evening.ingest_trade_date(D3, allow_derived=True, boards=False)
    d3 = stock_rows(db, D3)
    assert d3["300750"].limit_state == "up" and d3["300750"].consecutive_up is None
    assert d3["600519"].float_mv == pytest.approx(1.0e7 * 1.01)
    assert quality(db, D3)["derived_from"] == "2026-11-02"


def test_real_interfaces_published_early_replace_derivation(evening, db):
    api = evening.api
    _concat(api, "daily_basic", ["ts_code", "trade_date", "turnover_rate", "circ_mv", "limit_status"],
            [(code, "20261103", 9.9, 1234.0, 0) for code in ("600519.SH", "300750.SZ")])
    _concat(api, "limit_list_d", ["trade_date", "ts_code", "name", "open_times", "limit_times", "limit"],
            [("20261103", "300750.SZ", "宁德时代", 2, 3, "U")])
    evening.ingest_trade_date(D1)
    api.calls.clear()
    report = evening.ingest_trade_date(D2, allow_derived=True, boards=False)
    assert report.derived == []
    rows = stock_rows(db, D2)
    assert rows["600519"].turnover_rate == pytest.approx(9.9) and rows["600519"].float_mv == pytest.approx(1234.0e4)
    assert rows["300750"].open_times == 2 and rows["300750"].consecutive_up == 3
    q = quality(db, D2)
    assert q["basic_source"] == "daily_basic" and q["limit_source"] == "limit_list_d" and q["derived"] == []
    assert api.calls_of("stk_limit") == []  # nothing to derive: the price-limit table is not even requested
    assert q["status"] == "provisional"  # only because dc_index / dc_daily of D2 are still pending


def test_derivation_is_opt_in_and_needs_the_previous_day(evening, db):
    # default (backfill) behaviour: a missing daily_basic stays a quality flag
    evening.ingest_trade_date(D1)
    plain = evening.ingest_trade_date(D2)
    assert plain.status == "partial" and "daily_basic" in plain.missing and plain.derived == []
    assert stock_rows(db, D2)["600519"].float_mv is None
    assert evening.api.calls_of("stk_limit") == []

    # cold start: nothing to derive the float cap from; the limit facts can still be derived
    with db.session_scope() as session:
        session.query(MarketStockDaily).filter(MarketStockDaily.trade_date == D1).delete()
    cold = evening.ingest_trade_date(D2, allow_derived=True, boards=False, force=True)
    assert cold.derived == ["limit_list_d"] and "daily_basic" in cold.missing and cold.status == "partial"
    assert stock_rows(db, D2)["600519"].float_mv is None
    assert stock_rows(db, D2)["300750"].touched_limit_up is True


def test_failed_stk_limit_keeps_the_day_partial_and_derives_no_limits(evening, db):
    evening.ingest_trade_date(D1)
    evening.api.errors["stk_limit"] = Exception("boom")
    report = evening.ingest_trade_date(D2, allow_derived=True, boards=False)
    assert "stk_limit" in report.missing and report.status == "partial"
    assert report.derived == ["daily_basic"]  # the float cap needs only the previous day
    rows = stock_rows(db, D2)
    assert rows["600519"].float_mv == pytest.approx(1.0e7 * 1.01)
    assert rows["300750"].limit_state is None and rows["300750"].touched_limit_up is None  # unknown, never 'none'


def test_provisional_day_is_replaced_with_real_data_on_the_next_ingest(evening, db):
    api = evening.api
    evening.ingest_trade_date(D1)
    first = evening.ingest_trade_date(D2, allow_derived=True, boards=False)
    assert first.status == "provisional"
    assert evening.ingest_trade_date(D2, allow_derived=True, boards=False).status == "provisional"  # not skipped
    assert all(kw["trade_date"] == "20261102" for kw in api.calls_of("dc_index"))  # D2's boards not requested
    before = stock_rows(db, D2)["300750"]
    assert before.open_times is None and before.turnover_rate != pytest.approx(7.7)

    # the evening publications arrive
    _concat(api, "daily_basic", ["ts_code", "trade_date", "turnover_rate", "circ_mv", "limit_status"],
            [("300750.SZ", "20261103", 7.7, 600.0, 2), ("600519.SH", "20261103", 0.4, 1010.0, 1)])
    _concat(api, "limit_list_d", ["trade_date", "ts_code", "name", "open_times", "limit_times", "limit"],
            [("20261103", "300750.SZ", "宁德时代", 1, 3, "U")])
    _concat(api, "dc_index", ["ts_code", "trade_date", "name", "idx_type", "level", "total_mv"],
            [("BK0477.DC", "20261103", "酿酒行业", "行业板块", "1", 100.0),
             ("BK1184.DC", "20261103", "人形机器人", "概念板块", None, 200.0),
             ("BK3000.DC", "20261103", "新概念", "概念板块", None, 50.0)])  # a concept that appears on D2
    _concat(api, "dc_daily", ["ts_code", "trade_date", "pct_change", "amount"],
            [("BK0477.DC", "20261103", 1.5, 1e10), ("BK3000.DC", "20261103", 4.5, 3e9)])

    done = evening.ingest_trade_date(D2, allow_derived=True, boards=True)
    assert done.status == "complete" and done.derived == []
    after = stock_rows(db, D2)["300750"]
    assert after.turnover_rate == pytest.approx(7.7) and after.float_mv == pytest.approx(600.0e4)
    assert after.open_times == 1 and after.consecutive_up == 3
    q = quality(db, D2)
    assert q["basic_source"] == "daily_basic" and q["limit_source"] == "limit_list_d"
    assert q["boards"] == "done" and q["derived"] == [] and q["derived_from"] is None
    # the board catalog / vendor columns of D2 arrived with the completion: new boards appear one day late
    boards = {b["code"]: b for b in evening.repo.list_boards(status=None)}
    assert boards["BK3000.DC"]["first_seen_date"] == D2
    with db.get_session() as session:
        vendor = {r.board_id: r.vendor_ret for r in session.execute(
            select(BoardDaily).where(BoardDaily.trade_date == D2)).scalars()}
    assert vendor[boards["BK3000.DC"]["id"]] == pytest.approx(4.5)


def test_completed_day_is_final_and_skipped_afterwards(evening, db):
    api = evening.api
    evening.ingest_trade_date(D1)
    evening.ingest_trade_date(D2, allow_derived=True, boards=False)
    _concat(api, "daily_basic", ["ts_code", "trade_date", "turnover_rate", "circ_mv", "limit_status"],
            [(code, "20261103", 1.0, 100.0, 1) for code in _EVENING_D2])
    _concat(api, "limit_list_d", ["trade_date", "ts_code", "name", "open_times", "limit_times", "limit"],
            [("20261103", "300750.SZ", "宁德时代", 0, 3, "U")])
    _concat(api, "dc_index", ["ts_code", "trade_date", "name", "idx_type", "level", "total_mv"],
            [("BK0477.DC", "20261103", "酿酒行业", "行业板块", "1", 100.0),
             ("BK1184.DC", "20261103", "人形机器人", "概念板块", None, 200.0)])
    _concat(api, "dc_daily", ["ts_code", "trade_date", "pct_change", "amount"], [("BK0477.DC", "20261103", 1.5, 1e10)])
    assert evening.ingest_trade_date(D2, allow_derived=True, boards=True).status == "complete"
    api.calls.clear()
    assert evening.ingest_trade_date(D2, allow_derived=True, boards=True).status == "skipped"
    assert api.calls == []  # zero requests for a final day


def _board_members(api, board: str, day: str, codes: List[str]) -> None:
    _concat(api, "dc_member", ["trade_date", "ts_code", "con_code"], [(day, board, c) for c in codes])


def test_full_day_membership_is_paged_and_folded_for_all_boards(evening, db, monkeypatch):
    api = evening.api
    evening.ingest_trade_date(D1)
    _board_members(api, "BK1184.DC", "20261102", ["600519.SH", "300750.SZ", "000001.SZ"])
    _board_members(api, "BK0477.DC", "20261102", ["600519.SH", "000333.SZ"])
    _board_members(api, "BK9999.DC", "20261102", ["600519.SH"])  # a region board: not in the catalog
    monkeypatch.setattr(market_data_module, "DC_MEMBER_PAGE_CAP", 2)
    api.server_cap["dc_member"] = 2

    result = evening.refresh_membership_for_day(D1)
    assert result["status"] == "ok" and result["boards"] == 2 and result["rows"] == 5
    assert result["unknown_rows"] == 1 and result["opened"] == 5
    calls = api.calls_of("dc_member")
    assert len(calls) == 4  # 6 rows, 2 per page, plus the empty page that proves the end
    assert all("ts_code" not in kw and kw["trade_date"] == "20261102" for kw in calls)  # all boards at once
    bk, liquor = _board_id(evening, "BK1184.DC"), _board_id(evening, "BK0477.DC")
    assert evening.repo.get_members_on(bk, D1) == {"600519", "300750", "000001"}
    assert evening.repo.get_members_on(liquor, D1) == {"600519", "000333"}
    assert quality(db, D1)["membership"]["status"] == "ok"
    assert quality(db, D1)["status"] == "complete"  # the marker lives next to the stock-fact quality

    api.calls.clear()  # idempotent: a folded day makes no request
    assert evening.refresh_membership_for_day(D1)["status"] == "skipped" and not api.calls_of("dc_member")
    # a re-ingest of the day's stock facts keeps the membership marker
    evening.ingest_trade_date(D1, force=True)
    assert quality(db, D1)["membership"]["status"] == "ok"


def test_full_day_membership_closes_and_opens_rows_against_yesterday(evening, db):
    api = evening.api
    evening.ingest_trade_date(D1)
    _board_members(api, "BK1184.DC", "20261102", ["600519.SH", "300750.SZ", "000001.SZ"])
    _board_members(api, "BK0477.DC", "20261102", ["600519.SH", "000333.SZ"])
    assert evening.refresh_membership_for_day(D1)["status"] == "ok"
    evening.ingest_trade_date(D2, allow_derived=True)
    _board_members(api, "BK1184.DC", "20261103", ["600519.SH", "000001.SZ", "688001.SH"])  # 300750 out, 688001 in
    _board_members(api, "BK0477.DC", "20261103", ["600519.SH", "000333.SZ"])
    result = evening.refresh_membership_for_day(D2)
    assert result["status"] == "ok" and result["opened"] == 1 and result["closed"] == 1
    bk = _board_id(evening, "BK1184.DC")
    assert evening.repo.get_members_on(bk, D1) == {"600519", "300750", "000001"}
    assert evening.repo.get_members_on(bk, D2) == {"600519", "000001", "688001"}


def test_implausibly_small_full_day_membership_keeps_yesterdays_rows(evening, db):
    api = evening.api
    evening.ingest_trade_date(D1)
    _board_members(api, "BK1184.DC", "20261102", ["600519.SH", "300750.SZ", "000001.SZ", "688001.SH"])
    _board_members(api, "BK0477.DC", "20261102", ["600519.SH", "000333.SZ", "002001.SZ", "002002.SZ"])
    assert evening.refresh_membership_for_day(D1)["status"] == "ok"  # 8 pairs
    evening.ingest_trade_date(D2, allow_derived=True)

    # D2 answers with 3 pairs (< 50% of 8): paging "succeeded" but the day looks truncated
    _board_members(api, "BK1184.DC", "20261103", ["600519.SH", "300750.SZ"])
    _board_members(api, "BK0477.DC", "20261103", ["600519.SH"])
    result = evening.refresh_membership_for_day(D2)
    assert result["status"] == "incomplete" and result["rows"] == 3 and result["baseline"] == 8
    with db.get_session() as session:
        rows = session.execute(select(BoardMembership)).scalars().all()
        assert all(r.valid_to is None for r in rows)  # nothing closed
        assert len(rows) == 8
        assert all(b.membership_checked_date == D1 for b in session.execute(select(BoardCatalog)).scalars())
    assert quality(db, D2)["membership"]["status"] == "incomplete"

    # the vendor completes the day later: not skipped, folded now
    _board_members(api, "BK1184.DC", "20261103", ["000001.SZ", "688001.SH"])
    _board_members(api, "BK0477.DC", "20261103", ["000333.SZ", "002001.SZ", "002002.SZ"])
    again = evening.refresh_membership_for_day(D2)
    assert again["status"] == "ok" and again["rows"] == 8 and again["closed"] == 0


def test_unconfirmed_paging_changes_no_membership(evening, db, monkeypatch):
    api = evening.api
    evening.ingest_trade_date(D1)
    _board_members(api, "BK1184.DC", "20261102", ["600519.SH", "300750.SZ", "000001.SZ"])
    _board_members(api, "BK0477.DC", "20261102", ["600519.SH", "000333.SZ"])
    monkeypatch.setattr(market_data_module, "DC_MEMBER_PAGE_CAP", 2)
    api.server_cap["dc_member"] = 2
    api.ignore_offset = True  # the interface ignores offset: the total cannot be confirmed
    result = evening.refresh_membership_for_day(D1)
    assert result["status"] == "incomplete" and "offset" in result["reason"]
    with db.get_session() as session:
        assert session.execute(select(BoardMembership)).scalars().all() == []


def test_membership_failure_and_empty_response_are_reported_not_raised(evening, db):
    api = evening.api
    assert evening.refresh_membership_for_day(D1)["status"] == "no_boards"  # empty catalog
    evening.ingest_trade_date(D1)
    assert evening.refresh_membership_for_day(D1)["status"] == "empty"
    api.errors["dc_member"] = Exception("boom")
    failed = evening.refresh_membership_for_day(D1)
    assert failed["status"] == "error" and "boom" in failed["error"]
    assert evening.refresh_membership_for_day(date(2024, 12, 1))["status"] == "skipped"  # before dc_member history
