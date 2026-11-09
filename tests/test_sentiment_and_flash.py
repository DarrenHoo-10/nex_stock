# -*- coding: utf-8 -*-
"""打板 / 热股榜 / 东方财富风险名单 / 连板梯队 and the backup flash sources (no network).

Fixtures are single samples: Tushare kpl_list / ths_hot / dc_hot / major_news (2026-11-06,
tests/fixtures/tushare), 东方财富 stock_monitor.json and price-anomaly list (tests/fixtures/eastmoney),
财联社 roll list and 华尔街见闻 lives (tests/fixtures/flash), all taken on 2026-11-09.
"""

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from src.repositories.stock_event_repo import StockEventRepository
from src.services import news_akshare_source as ak_source
from src.services.board_metrics_service import limit_ladder
from src.services.sources import SourceError, cls_telegraph, eastmoney_risk, wscn_lives
from src.services.stock_event_service import (
    StockEventService,
    parse_hot_rank,
    parse_limit_board,
    parse_price_anomalies,
    parse_risk_watch,
    risk_notes_from,
)
from src.storage import DatabaseManager

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 9)  # a Monday
TRADE_DAY = date(2026, 11, 6)
_REAL_WATCHED = StockEventService.watched_codes  # conftest stubs it for every test
_REAL_NEEDS_CODE_FETCH = StockEventService._needs_code_fetch


def _json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTushare:
    def __init__(self, by_api):
        self.by_api = by_api
        self.calls = []

    def _market_query_paged(self, api, *, page_cap, key_cols, fields, **params):
        self.calls.append((api, dict(params)))
        outcome = self.by_api.get(api, [])
        if callable(outcome):
            outcome = outcome(params)
        if isinstance(outcome, Exception):
            raise outcome
        return pd.DataFrame(outcome)


@pytest.fixture
def db(tmp_path):
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'p5.db').as_posix()}")
    yield manager
    DatabaseManager.reset_instance()


def make_service(db, by_api=None):
    fake = FakeTushare(by_api or {})
    return StockEventService(StockEventRepository(db), fake, today=lambda: TODAY), fake


# ----------------------------------------------------------------------
# 东方财富风险名单
# ----------------------------------------------------------------------
def test_eastmoney_risk_lists_real_samples():
    watched = eastmoney_risk.parse_monitor(_json("eastmoney/stock_monitor.json"))
    codes = [w.code for w in watched]
    assert "002724" in codes and "920575" in codes
    assert "513300" not in codes and "501225" not in codes  # funds are dropped
    first = next(w for w in watched if w.code == "002724")
    assert first.end >= first.start
    anomalies = eastmoney_risk.parse_anomalies(_json("eastmoney/price_anomaly_list.json"))
    assert len(anomalies) == 5 and anomalies[0].code == "002242" and anomalies[0].day == TODAY
    assert anomalies[0].deviation_pct == pytest.approx(188.84) and anomalies[0].days == 30
    with pytest.raises(SourceError):
        eastmoney_risk.parse_anomalies({"result": 1001, "msg": "unknow team"})
    with pytest.raises(SourceError):
        eastmoney_risk.parse_monitor({"changed": True})


def test_risk_list_refresh_notes_and_watch_window(db, monkeypatch):
    service, _ = make_service(db)
    monkeypatch.setattr(eastmoney_risk, "fetch_monitor", lambda *, timeout_sec: eastmoney_risk.parse_monitor(
        _json("eastmoney/stock_monitor.json")))
    monkeypatch.setattr(eastmoney_risk, "fetch_anomalies", lambda *, timeout_sec: (_ for _ in ()).throw(
        ConnectionError("dycalchis down")))
    summary = service.refresh_risk_lists()
    assert summary["status"] == "partial" and summary["risk_watch"]["status"] == "ok"
    assert summary["price_anomaly"]["status"] == "failed"
    assert service.repo.ingested_days("risk_watch", since=TODAY)[TODAY] == "ok"

    watched = _REAL_WATCHED(service)
    assert "002724" in watched and watched["002724"] >= "2026-11-09"
    assert _REAL_WATCHED(service, as_of=date(2026, 12, 31)) == {}  # windows are about two weeks
    notes = risk_notes_from(parse_risk_watch(eastmoney_risk.parse_monitor(_json("eastmoney/stock_monitor.json")))
                            + parse_price_anomalies(eastmoney_risk.parse_anomalies(
                                _json("eastmoney/price_anomaly_list.json"))), TODAY)
    assert any(n.startswith("在东方财富重点监控名单内") for n in notes)
    assert "2026-11-09 严重异常波动：30 个交易日累计偏离 188.84%，按交易所规则可能停牌核查" in notes


def test_screening_drops_watched_stocks(monkeypatch):
    from src.services.screening.pipeline import drop_risk_watch

    monkeypatch.setattr(StockEventService, "watched_codes", lambda self, as_of=None: {"002724": "2026-11-13"})
    df = pd.DataFrame({"code": ["002724", "600089", "sz002724"], "name": ["海洋王", "特变电工", "海洋王"]})
    kept, note = drop_risk_watch(df, "cn")
    assert list(kept["code"]) == ["600089"] and "002724" in note
    assert drop_risk_watch(df, "us")[0] is df
    monkeypatch.setattr(StockEventService, "watched_codes", lambda self, as_of=None: (_ for _ in ()).throw(
        RuntimeError("db down")))
    assert drop_risk_watch(df, "cn") == (df, "")


def test_market_wide_kinds_do_not_count_as_a_per_stock_fetch(db):
    service, _ = make_service(db)
    service.repo.upsert(parse_hot_rank(_json("tushare/ths_hot_20261106.json"), [], TRADE_DAY))
    assert _REAL_NEEDS_CODE_FETCH(service, "002086") is True


# ----------------------------------------------------------------------
# 开盘啦 / 热股榜
# ----------------------------------------------------------------------
def test_limit_board_and_hot_rank_real_samples():
    boards = parse_limit_board([dict(r, tag="涨停") for r in _json("tushare/kpl_list_20261106.json")], TRADE_DAY)
    assert len(boards) == 12 and boards[0]["code"] == "002088"
    assert boards[0]["title"] == "开盘啦涨停（6天5板）：锂电池，题材 锂电池、储能"
    hot = parse_hot_rank(_json("tushare/ths_hot_20261106.json"), _json("tushare/dc_hot_20261106.json"), TRADE_DAY)
    top = next(e for e in hot if e["code"] == "002086")
    assert top["payload"]["ths_rank"] == 1 and top["payload"]["dc_rank"] == 1
    assert len(top["payload"]["ths_reason"]) <= 300 and "同花顺 AI 摘要" in top["title"]
    assert parse_hot_rank([{"ts_code": "000001.SZ", "rank": 101}], [], TRADE_DAY) == []


def test_daily_run_ingests_sentiment_lists_per_trade_date(db):
    def kpl(params):
        rows = _json("tushare/kpl_list_20261106.json") if params["trade_date"] == "20261106" else []
        return rows if params["tag"] == "涨停" else []

    def on_day(rows):
        return lambda params: rows if params["trade_date"] == "20261106" else []

    service, fake = make_service(db, {
        "kpl_list": kpl, "ths_hot": on_day(_json("tushare/ths_hot_20261106.json")),
        "dc_hot": on_day(_json("tushare/dc_hot_20261106.json")),
    })
    summary = service.run_daily(backfill_days=5)
    assert summary["kinds"]["limit_board"]["fetched"] == 12 and summary["kinds"]["hot_rank"]["fetched"] > 0
    kpl_calls = [p for api, p in fake.calls if api == "kpl_list"]
    assert {p["tag"] for p in kpl_calls} == {"涨停", "炸板", "跌停"}
    assert not any(p["trade_date"] in ("20261107", "20261108") for p in kpl_calls)  # weekend
    ths = next(p for api, p in fake.calls if api == "ths_hot")
    dc = next(p for api, p in fake.calls if api == "dc_hot")
    assert (ths["market"], ths["is_new"]) == ("热股", "Y") and (dc["market"], dc["hot_type"]) == ("A股市场", "人气榜")
    stored = service.events_for("002086", kinds=("hot_rank",), days=10, fetch=False)["events"]
    assert stored and stored[0]["payload"]["ths_rank"] == 1


# ----------------------------------------------------------------------
# 连板梯队 / 晋级率
# ----------------------------------------------------------------------
def _row(code, state, streak=None, st=False):
    return {"code": code, "limit_state": state, "consecutive_up": streak, "is_st": st}


def test_limit_ladder_counts_heights_and_promotion():
    previous = [_row(f"60000{i}", "up", 1) for i in range(5)] + [_row("000001", "up", 2), _row("000002", "none")]
    today = [_row("600000", "up", 2), _row("600001", "one_word_up", 2), _row("600002", "none"),
             _row("000001", "up", 3), _row("300001", "up", 1), _row("300999", "up", 4, st=True)]
    values, flags = limit_ladder(today, previous)
    ladder = json.loads(values["ladder"])
    assert values["max_consecutive"] == 3 and flags == []
    assert ladder["counts"] == {"1": 1, "2": 2, "3": 1}  # ST is left out, like limit_list_d
    assert ladder["promotion"] == {"1": {"prev": 5, "promoted": 2}, "2": {"prev": 1, "promoted": 1}}
    assert values["promotion_rate"] == pytest.approx(50.0)

    values, flags = limit_ladder(today, None)
    assert values["promotion_rate"] is None and flags == ["ladder_previous_missing"]
    values, flags = limit_ladder(today, previous[:2])
    assert values["promotion_rate"] is None and "promotion_sample_low" in flags
    assert limit_ladder([_row("600000", None)], previous)[1] == ["ladder_state_missing"]


def test_market_daily_stores_and_reads_the_ladder(db):
    from src.repositories.board_analysis_repo import BoardAnalysisRepository

    repo = BoardAnalysisRepository(db)
    values, _ = limit_ladder([_row("600000", "up", 2)], [_row(f"60000{i}", "up", 1) for i in range(5)])
    repo.write_market_metrics(TRADE_DAY, values, {})
    stored = repo.get_market_daily(TRADE_DAY)
    assert stored["max_consecutive"] == 2 and stored["promotion_rate"] == pytest.approx(20.0)
    assert stored["ladder"]["counts"] == {"2": 1}


# ----------------------------------------------------------------------
# 财联社直连 / 华尔街见闻 / 长篇新闻
# ----------------------------------------------------------------------
def test_cls_sign_and_real_sample():
    params = {"rn": 20, "appName": "CailianpressWeb", "os": "web", "sv": "7.7.5", "last_time": 1794192886,
              "refresh_type": 1}
    signed = cls_telegraph.signed_params(rn=20, last_time=1794192886)
    assert signed["sign"] == cls_telegraph.sign(dict(reversed(list(params.items())))) == cls_telegraph.sign(signed)
    assert len(signed["sign"]) == 32
    items = cls_telegraph.parse(_json("flash/cls_roll_list.json"))
    assert len(items) == 8 and items[0].item_id == "2528622" and items[0].published_at == datetime(2026, 11, 9, 10, 54, 46)
    assert cls_telegraph.parse(_json("flash/cls_roll_list.json"), important_only=True) == []  # sample is all level C
    with pytest.raises(SourceError):
        cls_telegraph.parse({"errno": 1, "msg": "sign error"})


def test_cls_direct_rows_match_akshare_rows():
    """An item read by AKShare and by the direct call is the same pool row (same synthetic URL)."""
    item = cls_telegraph.parse(_json("flash/cls_roll_list.json"))[0]
    direct = ak_source.cls_direct_records([item])[0]
    akshare = ak_source.parse_cls(pd.DataFrame({"标题": [item.title], "内容": [item.content],
                                                "发布日期": [item.published_at.date()],
                                                "发布时间": [item.published_at.time()]}))[0]
    assert direct.url == akshare.url and direct.title == akshare.title and direct.source_key == "cls"


def test_poll_uses_cls_direct_when_akshare_fails(monkeypatch):
    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_pool_service import NewsPoolService
    from tests.news_fixtures import NOW, install_stub, make_config

    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    stub = install_stub(monkeypatch)
    stub.flash["cls"] = ConnectionError("akshare cls broken")
    sent = {}

    class Http:
        def get(self, url, params=None, **kwargs):
            sent.update(params)
            return type("R", (), {"raise_for_status": lambda self: None,
                                  "json": lambda self: _json("flash/cls_roll_list.json")})()

    monkeypatch.setattr(cls_telegraph, "_session", lambda: Http())
    service = NewsPoolService(IntelligenceRepository(db), make_config(news_akshare_cls_symbol="全部"),
                              is_trading_day=lambda d: True, holdings_provider=lambda: [], sleep_fn=lambda s: None)
    result = service.poll_flash(now=NOW)
    assert result["sources"]["cls"]["status"] == "ok" and result["sources"]["cls"]["via"] == "direct"
    assert sent["appName"] == "CailianpressWeb" and len(sent["sign"]) == 32
    assert result["inserted"] >= 7
    DatabaseManager.reset_instance()


def test_wscn_real_sample_and_fallback_order():
    lives = wscn_lives.parse(_json("flash/wscn_lives.json"))
    assert len(lives) == 6 and lives[0].url == "https://wallstreetcn.com/livenews/3196626"
    assert [live.important for live in lives].count(True) == 1
    records = ak_source.wscn_records(lives)
    assert records[0].source_key == "wscn" and records[0].title.startswith("光纤概念股")
    assert ak_source.FALLBACK_FLASH_SOURCES[-1] == "wscn"
    with pytest.raises(SourceError):
        wscn_lives.parse({"code": 50000})


def test_daily_job_stores_major_news_as_articles(monkeypatch):
    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_digest_service import get_news_health
    from src.services.news_pool_service import NewsPoolService
    from tests.news_fixtures import NOW, install_stub, make_config

    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    install_stub(monkeypatch)
    windows = []
    service = NewsPoolService(
        IntelligenceRepository(db), make_config(stock_list=[]), is_trading_day=lambda d: True,
        holdings_provider=lambda: [], sleep_fn=lambda s: None,
        risk_lists_runner=lambda: {"status": "ok"},
        articles_query=lambda start, end: windows.append((start, end)) or _json("tushare/major_news_20261106.json"),
    )
    result = service.run_daily_fetch(day=NOW.date(), now=NOW)
    assert result["articles"]["status"] == "ok" and result["articles"]["fetched"] == 4
    assert result["risk_lists"] == {"status": "ok"}
    assert windows == [(datetime(2026, 11, 8, 10, 0), NOW)]
    rows = service.repo.list_news_rows(since=datetime(2026, 11, 1), until=NOW)
    articles = [r for r in rows if r.item_kind == "article"]
    assert len(articles) == 4 and all(r.url.startswith("http") for r in articles)
    health = {s["source"]: s for s in get_news_health(repo=service.repo, now=NOW, config=service.config)["sources"]}
    assert health["major"]["last_status"] == "ok"
    DatabaseManager.reset_instance()
