# -*- coding: utf-8 -*-
"""US leaders config: parsing / defaults / validation, symbol mapping, board mapping (offline)."""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.config import Config
from src.services.us_leaders import (
    DEFAULT_US_LEADERS,
    INDEX_SYMBOLS,
    UsLeader,
    UsMoveThresholds,
    load_us_leaders,
    match_board_name,
    normalize_board_name,
    normalize_symbol,
    resolve_leader_boards,
    thresholds_from_config,
)


def catalog(*names, status="active"):
    return [
        {"id": index + 1, "ref": f"concept:BK{index}", "name": name, "board_type": "concept", "status": status}
        for index, name in enumerate(names)
    ]


# ----------------------------------------------------------------------
# config parsing / defaults / validation
# ----------------------------------------------------------------------
def test_empty_value_uses_the_default_list():
    loaded = load_us_leaders("")
    assert loaded.source == "default" and loaded.warnings == []
    symbols = [leader.symbol for leader in loaded.leaders]
    assert 12 <= len(symbols) <= 15
    assert {"NVDA", "AVGO", "AMD", "TSM", "SOXX", "TSLA", "AAPL", "MSFT", "META", "GOOGL", "AMZN", "PGJ", "SPX", "NDX"} == set(symbols)
    nvda = next(l for l in loaded.leaders if l.symbol == "NVDA")
    assert nvda.kind == "stock" and nvda.boards == ("算力", "CPO", "半导体")
    # market-wide context indices carry no boards
    assert next(l for l in loaded.leaders if l.symbol == "SPX").boards == ()


def test_default_list_carries_eastmoney_codes_and_etf_proxies():
    by_symbol = {l.symbol: l for l in load_us_leaders("").leaders}
    for symbol in ("NVDA", "AVGO", "AMD", "TSLA", "AAPL", "MSFT", "META", "GOOGL", "AMZN"):
        assert by_symbol[symbol].em == f"105.{symbol}" and by_symbol[symbol].em_api == "stock_us_hist"
    assert by_symbol["TSM"].em == "106.TSM"  # NYSE
    # Eastmoney has neither the SOX nor the Golden Dragon index: SOXX / PGJ stand in, with the same boards
    soxx, pgj = by_symbol["SOXX"], by_symbol["PGJ"]
    assert (soxx.kind, soxx.em, soxx.boards) == ("etf", "105.SOXX", ("半导体", "算力", "CPO"))
    assert (pgj.kind, pgj.em, pgj.boards) == ("etf", "105.PGJ", ("互联网服务", "互联网电商"))  # 中概互联 is no East Money board
    assert "费城半导体指数" in soxx.name and "纳斯达克中国金龙指数" in pgj.name
    assert not soxx.is_index and soxx.em_api == "stock_us_hist"
    # market context indices come from the one index_global_spot_em snapshot
    for symbol, code in (("SPX", "SPX"), ("NDX", "NDX")):
        assert by_symbol[symbol].is_index and by_symbol[symbol].em == code and by_symbol[symbol].em_api == "index_global_spot_em"
    assert by_symbol["NDX"].name == "纳斯达克100指数" and by_symbol["NDX"].boards == ()
    assert "SOX" not in by_symbol and "HXC" not in by_symbol and "IXIC" not in by_symbol


def test_pgj_boards_resolve_against_the_eastmoney_catalog_names():
    pgj = next(l for l in DEFAULT_US_LEADERS if l.symbol == "PGJ")
    boards = [
        {"id": 1, "ref": "BK0447.DC", "name": "互联网服务", "board_type": "concept", "status": "active"},
        {"id": 2, "ref": "BK1268.DC", "name": "互联网电商", "board_type": "industry", "status": "active"},
        {"id": 3, "ref": "BK0000.DC", "name": "互联网金融", "board_type": "concept", "status": "active"},
    ]
    resolution = resolve_leader_boards([pgj], boards)
    assert [(m.name, m.tier) for m in resolution.by_leader["PGJ"]] == [("互联网服务", "exact"), ("互联网电商", "exact")]
    assert resolution.unresolved == {}
    assert "纳斯达克中国金龙指数" in pgj.name


def test_etf_proxies_keep_the_tushare_and_yfinance_mapping():
    for symbol in ("SOXX", "PGJ"):
        leader = next(l for l in DEFAULT_US_LEADERS if l.symbol == symbol)
        assert (leader.tushare_api, leader.tushare_code, leader.yfinance_symbol) == ("us_daily", symbol, symbol)
    ndx = next(l for l in DEFAULT_US_LEADERS if l.symbol == "NDX")
    assert ndx.tushare_api is None and ndx.yfinance_symbol == "^NDX"  # not on Tushare index_global: yfinance fallback
    # the etf proxy of an index is judged with the index threshold
    thresholds = UsMoveThresholds(4.0, 1.5)
    soxx = next(l for l in DEFAULT_US_LEADERS if l.symbol == "SOXX")
    assert thresholds.for_leader(soxx) == 1.5
    assert thresholds.for_leader(next(l for l in DEFAULT_US_LEADERS if l.symbol == "NVDA")) == 4.0


def test_user_config_em_field_is_optional_and_validated():
    raw = json.dumps([
        {"symbol": "NVDA", "kind": "stock"},                      # no em: Tushare / yfinance only, as before
        {"symbol": "tsm", "em": " 106.tsm "},
        {"symbol": "SPX", "kind": "index", "em": "SPX"},
        {"symbol": "AMD", "em": "bad code!"},
        {"symbol": "AVGO", "em": 105},
        {"symbol": "SOXX", "kind": "etf", "em": ""},
    ])
    loaded = load_us_leaders(raw)
    by_symbol = {l.symbol: l for l in loaded.leaders}
    assert by_symbol["NVDA"].em is None and by_symbol["NVDA"].em_api is None
    assert by_symbol["TSM"].em == "106.TSM"
    assert by_symbol["SPX"].em_api == "index_global_spot_em"
    assert by_symbol["AMD"].em is None and by_symbol["AVGO"].em is None and by_symbol["SOXX"].em is None
    assert by_symbol["SOXX"].kind == "etf"
    assert len(loaded.warnings) == 2 and all("em" in w for w in loaded.warnings)


def test_explicit_empty_array_disables_the_leaders():
    loaded = load_us_leaders("[]")
    assert loaded.leaders == [] and loaded.warnings == [] and loaded.source == "config"


def test_valid_json_is_normalised():
    raw = json.dumps([
        {"symbol": " nvda ", "name": "英伟达", "kind": "stock", "boards": [" 算力 ", "算力", "CPO"]},
        {"symbol": "^gspc"},                      # alias -> SPX, kind inferred as index, name defaults to the symbol
        {"symbol": "XYZ", "kind": "index", "boards": []},
    ])
    loaded = load_us_leaders(raw)
    assert loaded.warnings == []
    nvda, spx, xyz = loaded.leaders
    assert (nvda.symbol, nvda.boards) == ("NVDA", ("算力", "CPO"))
    assert (spx.symbol, spx.kind, spx.name) == ("SPX", "index", "SPX")
    assert xyz.kind == "index" and xyz.yfinance_symbol == "^XYZ" and xyz.tushare_code is None


def test_invalid_entries_are_skipped_with_warnings_not_exceptions():
    raw = json.dumps([
        "NVDA",
        {"symbol": "bad symbol!"},
        {"symbol": "AMD", "kind": "fund"},
        {"symbol": "TSLA", "boards": "特斯拉概念"},
        {"symbol": "TSLA", "name": "dup"},
        {"symbol": "AAPL", "name": "苹果", "boards": ["消费电子"]},
    ])
    loaded = load_us_leaders(raw)
    assert [l.symbol for l in loaded.leaders] == ["TSLA", "AAPL"]
    assert loaded.leaders[0].boards == ()  # boards of the wrong type become empty
    assert len(loaded.warnings) == 5


def test_broken_json_or_wrong_container_falls_back_to_defaults():
    for raw in ("{not json", '{"symbol": "NVDA"}'):
        loaded = load_us_leaders(raw)
        assert len(loaded.leaders) == len(DEFAULT_US_LEADERS)
        assert loaded.warnings and "默认名单" in loaded.warnings[0]


def test_thresholds_default_and_override():
    default = thresholds_from_config(SimpleNamespace())
    assert default == UsMoveThresholds(4.0, 1.5)
    custom = thresholds_from_config(SimpleNamespace(us_move_stock_pct=6, us_move_index_pct="2.5"))
    assert (custom.stock_pct, custom.index_pct) == (6.0, 2.5)
    assert thresholds_from_config(SimpleNamespace(us_move_stock_pct=-1, us_move_index_pct="x")) == UsMoveThresholds(4.0, 1.5)
    stock, index = DEFAULT_US_LEADERS[0], DEFAULT_US_LEADERS[4]
    assert default.for_leader(stock) == 4.0 and default.for_leader(index) == 1.5


class ConfigKeysTestCase(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    def _load(self, env):
        with patch("src.config.setup_env"), patch.object(Config, "_parse_litellm_yaml", return_value=[]), \
                patch.object(Config, "_parse_stock_email_groups", return_value=[]), \
                patch.dict(os.environ, env, clear=True):
            return Config._load_from_env()

    def test_defaults(self):
        config = self._load({})
        self.assertEqual(config.us_leaders, "")
        self.assertEqual((config.us_move_stock_pct, config.us_move_index_pct), (4.0, 1.5))
        self.assertFalse(config.preopen_push_enabled)
        self.assertEqual(config.preopen_push_time, "08:15")

    def test_overrides_and_invalid_values(self):
        config = self._load({
            "US_LEADERS": ' [{"symbol":"NVDA"}] ', "US_MOVE_STOCK_PCT": "5", "US_MOVE_INDEX_PCT": "0",
            "PREOPEN_PUSH_ENABLED": "true", "PREOPEN_PUSH_TIME": "8:15",
        })
        self.assertEqual(config.us_leaders, '[{"symbol":"NVDA"}]')
        self.assertEqual(config.us_move_stock_pct, 5.0)
        self.assertEqual(config.us_move_index_pct, 0.1)  # below the 0.1 minimum -> clamped (parse_env_float semantics)
        self.assertTrue(config.preopen_push_enabled)
        self.assertEqual(config.preopen_push_time, "08:15")  # invalid HH:MM -> default


# ----------------------------------------------------------------------
# symbol mapping (Tushare <-> yfinance, one place)
# ----------------------------------------------------------------------
def test_symbol_mapping_between_tushare_and_yfinance():
    nvda = UsLeader("NVDA", "英伟达", "stock")
    assert (nvda.tushare_api, nvda.tushare_code, nvda.yfinance_symbol) == ("us_daily", "NVDA", "NVDA")
    spx = UsLeader("SPX", "标普", "index")
    assert (spx.tushare_api, spx.tushare_code, spx.yfinance_symbol) == ("index_global", "SPX", "^GSPC")
    assert INDEX_SYMBOLS["IXIC"].yfinance == "^IXIC" and INDEX_SYMBOLS["DJI"].tushare == "DJI"
    for symbol, yf_symbol in (("SOX", "^SOX"), ("HXC", "^HXC"), ("NDX", "^NDX")):
        leader = UsLeader(symbol, symbol, "index")
        assert leader.tushare_api is None and leader.tushare_code is None  # indices not on Tushare go to yfinance
        assert leader.yfinance_symbol == yf_symbol
    assert normalize_symbol("^GSPC") == "SPX" and normalize_symbol("brk.b") == "BRK.B" and normalize_symbol("a b") == ""


def test_yfinance_fetcher_passes_caret_index_symbols_through():
    from data_provider.yfinance_fetcher import YfinanceFetcher

    fetcher = YfinanceFetcher.__new__(YfinanceFetcher)
    assert fetcher._convert_stock_code("^SOX") == "^SOX"
    assert fetcher._convert_stock_code("^HXC") == "^HXC"
    assert fetcher._convert_stock_code("SPX") == "^GSPC"  # existing mapping unchanged
    assert fetcher._convert_stock_code("NVDA") == "NVDA"


# ----------------------------------------------------------------------
# board mapping
# ----------------------------------------------------------------------
def test_board_name_matching_tiers():
    boards = catalog("算力", "算力租赁", "半导体概念", "CPO概念", "AI 应用", "云计算", "汽车零部件", "半导体设备")
    assert [(m.name, m.tier) for m in match_board_name("算力", boards)] == [("算力", "exact")]  # exact beats contains
    assert [(m.name, m.tier) for m in match_board_name("半导体", boards)] == [("半导体概念", "normalized")]
    assert [(m.name, m.tier) for m in match_board_name("cpo", boards)] == [("CPO概念", "normalized")]
    assert [(m.name, m.tier) for m in match_board_name("AI应用", boards)] == [("AI 应用", "normalized")]
    assert [(m.name, m.tier) for m in match_board_name("汽车", boards)] == [("汽车零部件", "contains")]
    assert match_board_name("中概互联", boards) == []
    assert match_board_name("", boards) == []
    assert normalize_board_name("AI 应用概念") == "ai应用"


def test_retired_boards_are_ignored_and_contains_is_capped():
    assert match_board_name("算力", catalog("算力", status="retired")) == []
    many = catalog(*[f"算力主题{i:02d}" for i in range(20)])
    assert len(match_board_name("算力", many)) == 5


def test_resolution_reports_unresolved_names_per_leader():
    leaders = [
        UsLeader("NVDA", "英伟达", "stock", ("算力", "CPO", "半导体")),
        UsLeader("HXC", "金龙", "index", ("中概互联",)),
        UsLeader("SPX", "标普", "index", ()),
    ]
    boards = catalog("算力", "CPO概念")
    resolution = resolve_leader_boards(leaders, boards)
    assert [m.name for m in resolution.by_leader["NVDA"]] == ["算力", "CPO概念"]
    assert resolution.unresolved == {"NVDA": ["半导体"], "HXC": ["中概互联"]}
    assert resolution.by_leader["HXC"] == [] and resolution.by_leader["SPX"] == []
    assert resolution.unresolved_names() == ["半导体", "中概互联"]
    assert resolution.board_ids_for(["NVDA", "SPX"]) == {1: ["NVDA"], 2: ["NVDA"]}
