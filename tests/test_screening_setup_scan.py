# -*- coding: utf-8 -*-
"""Tests for the market-wide surge-pullback setup pre-scan (no network).

A synthetic market exercises every rule path: a waiting setup (with an
ex-dividend day), a peer whose own turnover is too high, a lone-industry
surge, a broken setup, a triggered entry, an ST stock and a superseded setup.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from src.services import screening_service
from src.services import screening_setup_scan as setup_scan
from src.services.screening import pipeline as screening_pipeline
from src.services.screening.config import Config as ScreeningRuntimeConfig
from src.services.screening.models import HardFilterConfig, ScreeningConfig, SetupScanConfig, Strategy
from src.services.screening.scorer import compute_screen_scores
from src.services.screening.strategy import load_strategy

REPO_ROOT = Path(__file__).resolve().parents[1]
DATES = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-08-03", periods=38)]
WINDOW, FUTURE = DATES[:36], DATES[36:]  # 36 published sessions; the scan uses the last 35
S = 31  # surge session (absolute index in WINDOW); index 30 inside the 35-session scan window
DIV_AT = 34  # Alpha pays 0.5/share; ex-dividend session
AS_OF = datetime.strptime(WINDOW[-1], "%Y%m%d").date()
DIVIDEND = 0.5

# (open, high, low, close) after the surge day, per stock
AFTER = {
    "waiting": [(10.75, 10.8, 10.6, 10.7), (10.7, 10.72, 10.5, 10.6), (10.6, 10.65, 10.45, 10.55),
                (10.55, 10.6, 10.4, 10.5)],
    "triggered": [(10.75, 10.8, 10.6, 10.7), (10.5, 10.55, 10.12, 10.3), (10.3, 10.4, 10.2, 10.35),
                  (10.35, 10.4, 10.25, 10.3)],
    "broken": [(10.3, 10.35, 9.85, 9.9), (9.9, 10.0, 9.8, 9.85), (9.85, 9.9, 9.7, 9.8), (9.8, 9.85, 9.7, 9.75)],
}
STOCKS = {
    # ts_code: (name, industry, path, turnover on surge day)
    "000001.SZ": ("Alpha", "机械", "waiting", 5.0),
    "000002.SZ": ("Beta", "机械", "waiting", 11.0),
    "000003.SZ": ("Gamma", "化工", "waiting", 5.0),
    "000004.SZ": ("Delta", "机械", "broken", 5.0),
    "000005.SZ": ("Epsilon", "机械", "triggered", 5.0),
    "600006.SH": ("*ST Zeta", "机械", "waiting", 5.0),
}


def _bars_for(ts_code: str, path: str) -> list[dict]:
    rows, prev = [], 10.0
    for i, d in enumerate(WINDOW):
        vol, amount, pre_close = 1000.0, 50000.0, prev
        if i < S:
            o, h, lo, c = 10.0, 10.1, 9.9, 10.0
        elif i == S:
            o, h, lo, c = 10.1, 10.9, 10.05, 10.8
            vol, amount = 3000.0, 160000.0
        else:
            o, h, lo, c = AFTER[path][i - S - 1]
            if ts_code == "000001.SZ" and i >= DIV_AT:  # ex-dividend: raw prices drop by the dividend
                o, h, lo, c = (round(x - DIVIDEND, 2) for x in (o, h, lo, c))
                if i == DIV_AT:
                    pre_close = round(prev - DIVIDEND, 2)
        rows.append({"ts_code": ts_code, "trade_date": d, "open": o, "high": h, "low": lo, "close": c,
                     "pre_close": pre_close, "pct_chg": (c / pre_close - 1) * 100, "vol": vol, "amount": amount})
        prev = c
    return rows


def _eta_bars() -> list[dict]:
    """Surges at session 27 and again at 33 (alone, turnover too high) -> the older one is superseded."""
    rows, prev = [], 10.0
    for i, d in enumerate(WINDOW):
        c, vol = (10.8, 3000.0) if i == 27 else (11.6, 3000.0) if i == 33 else (prev, 1000.0)
        rows.append({"ts_code": "000007.SZ", "trade_date": d, "open": prev, "high": max(c, prev) + 0.05,
                     "low": min(c, prev) - 0.05, "close": c, "pre_close": prev, "pct_chg": (c / prev - 1) * 100,
                     "vol": vol, "amount": 50000.0})
        prev = c
    return rows


class FakeProvider:
    def __init__(self, *, regime_on: bool = True, publish_last: bool = True) -> None:
        rows = [row for code, (_n, _i, path, _t) in STOCKS.items() for row in _bars_for(code, path)]
        rows += _eta_bars()
        self.bars = pd.DataFrame(rows)
        self.publish_last = publish_last
        # switch: 20-session change 4% on the surge day (off), then 6%..9% (on)
        idx = [1000.0] * S + ([1040.0, 1060.0, 1070.0, 1080.0, 1090.0] if regime_on else [1000.0] * 5)
        self.index = pd.DataFrame({"trade_date": WINDOW, "close": idx})
        self.daily_basic_calls: list[str] = []

    def open_trade_dates(self, start: str, end: str) -> list[str]:
        return [d for d in DATES if start <= d <= end]

    def daily(self, trade_date: str):
        if trade_date not in WINDOW or (not self.publish_last and trade_date == WINDOW[-1]):
            return None
        return self.bars[self.bars["trade_date"] == trade_date].copy()

    def daily_basic(self, trade_date: str):
        self.daily_basic_calls.append(trade_date)
        if trade_date == WINDOW[S]:
            return pd.DataFrame([{"ts_code": c, "turnover_rate": t} for c, (_n, _i, _p, t) in STOCKS.items()])
        if trade_date == WINDOW[33]:
            return pd.DataFrame([{"ts_code": "000007.SZ", "turnover_rate": 12.0}])
        return pd.DataFrame(columns=["ts_code", "turnover_rate"])

    def stock_basic(self) -> pd.DataFrame:
        rows = [{"ts_code": c, "name": n, "industry": ind, "list_date": "20200101"}
                for c, (n, ind, _p, _t) in STOCKS.items()]
        rows.append({"ts_code": "000007.SZ", "name": "Eta", "industry": "机械", "list_date": "20200101"})
        return pd.DataFrame(rows)

    def index_daily(self, ts_code: str, start: str, end: str) -> pd.DataFrame:
        return self.index[(self.index["trade_date"] >= start) & (self.index["trade_date"] <= end)].copy()


def _expected_alpha_lines() -> tuple[float, float]:
    """MA10 and surge low in current (post-dividend) raw prices, computed independently."""
    closes = [10.0] * S + [10.8] + [c for (_o, _h, _l, c) in AFTER["waiting"]]
    k = (closes[DIV_AT - 1] - DIVIDEND) / closes[DIV_AT - 1]  # pre-ex prices scaled into current units
    current = [c * k if i < DIV_AT else c - DIVIDEND for i, c in enumerate(closes)]
    return round(sum(current[-10:]) / 10, 2), round(10.05 * k, 2)


# --------------------------------------------------------------------------- scan rules


def test_scan_applies_every_rule_and_adjusts_for_dividends() -> None:
    result = setup_scan.scan_surge_pullback(FakeProvider(regime_on=True), SetupScanConfig(), today=AS_OF)

    assert result.as_of == WINDOW[-1]
    assert result.next_session == FUTURE[0]
    assert result.regime_on is True
    assert result.regime_change_20d_pct == pytest.approx(9.0)
    assert list(result.candidates.index) == ["000001"]
    assert result.stats == {"surges_in_window": 6, "qualified": 3, "broken": 1, "triggered": 1,
                            "no_line": 0, "waiting": 1}

    alpha = result.candidates.loc["000001"]
    expected_line, expected_cancel = _expected_alpha_lines()
    assert alpha["setup_buy_line"] == pytest.approx(expected_line)
    assert alpha["setup_cancel_line"] == pytest.approx(expected_cancel)
    assert alpha["setup_buy_line"] != pytest.approx(10.22)  # naive raw-price MA10 would be wrong
    assert alpha["setup_last_close"] == pytest.approx(10.0)
    assert alpha["setup_surge_date"] == f"{WINDOW[S][:4]}-{WINDOW[S][4:6]}-{WINDOW[S][6:]}"
    assert alpha["setup_surge_volume_ratio"] == pytest.approx(3.0)
    assert alpha["setup_industry_peers"] == 3  # Beta, Delta, Epsilon; the ST stock is not counted
    assert alpha["setup_session_index"] == 5 and alpha["setup_window_sessions_left"] == 6
    assert bool(alpha["setup_regime_on"]) is True


def test_switch_off_blocks_triggers_and_keeps_setups_waiting() -> None:
    result = setup_scan.scan_surge_pullback(FakeProvider(regime_on=False), SetupScanConfig(), today=AS_OF)

    assert result.regime_on is False
    # no orders while the switch is off, so Epsilon never triggered; Delta still broke its low
    assert sorted(result.candidates.index) == ["000001", "000005"]
    assert result.stats["triggered"] == 0 and result.stats["broken"] == 1


def test_unpublished_latest_session_falls_back_to_previous_day() -> None:
    result = setup_scan.scan_surge_pullback(
        FakeProvider(regime_on=True, publish_last=False), SetupScanConfig(), today=AS_OF,
    )
    assert result.as_of == WINDOW[-2]
    assert result.next_session == WINDOW[-1]


def test_apply_setup_scan_restricts_snapshot_and_reports_switch() -> None:
    snapshot = pd.DataFrame([{"code": "000001", "name": "Alpha"}, {"code": "000009", "name": "Other"}])
    df, notes = setup_scan.apply_setup_scan(
        snapshot, SetupScanConfig(), config=None, provider=FakeProvider(regime_on=False), today=AS_OF,
    )
    assert list(df["code"]) == ["000001"]
    assert notes[0].startswith("市场开关关闭") and "仅供观察" in notes[0]
    assert any("不在本次快照" in note for note in notes)  # Epsilon is a setup but not in this snapshot

    blocked, block_notes = setup_scan.apply_setup_scan(
        snapshot, SetupScanConfig(regime_off_action="block"), config=None,
        provider=FakeProvider(regime_on=False), today=AS_OF,
    )
    assert blocked.empty and "未返回候选" in block_notes[0]


# --------------------------------------------------------------------------- pipeline


def _strategy(**setup_kwargs) -> Strategy:
    return Strategy(
        name="surge_demo", display_name="Demo", description="demo",
        screening=ScreeningConfig(
            enabled=True, market_scope=["cn"], hard_filters=HardFilterConfig(exclude_st=True),
            factor_weights={"small_cap": 1.0}, max_output=10,
            setup_scan=SetupScanConfig(**setup_kwargs),
        ),
    )


def _run_pipeline(monkeypatch, tmp_path: Path, *, regime_on: bool, **setup_kwargs):
    snapshot = pd.DataFrame([
        {"code": "000001", "name": "Alpha", "price": 10.0, "change_pct": -0.5, "amount": 6e7, "circ_mv": 8e9},
        {"code": "000005", "name": "Epsilon", "price": 10.3, "change_pct": -0.3, "amount": 6e7, "circ_mv": 3e9},
        {"code": "000009", "name": "Other", "price": 5.0, "change_pct": 1.0, "amount": 9e7, "circ_mv": 1e9},
    ])
    snapshot.attrs.update({"snapshot_source": "test", "source_errors": [], "fallback_used": False})
    strategy = _strategy(**setup_kwargs)
    monkeypatch.setattr(screening_pipeline, "load_all_strategies_with_extra", lambda _c: {"surge_demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: snapshot.copy())
    monkeypatch.setattr(screening_pipeline, "apply_dsa_provider_context", lambda picks, _ctx: [])
    monkeypatch.setattr(screening_pipeline, "run_post_analyzers", lambda picks, **k: (picks, []))
    monkeypatch.setattr(setup_scan, "build_market_data_provider", lambda _c: FakeProvider(regime_on=regime_on))
    monkeypatch.setattr(setup_scan, "cn_today", lambda: AS_OF)
    config = ScreeningRuntimeConfig(
        strategies_dir=REPO_ROOT / "src" / "services" / "screening" / "strategies",
        post_analyzers=[], risk_enabled=True, portfolio_diversity_enabled=False, data_dir=tmp_path,
    )
    return screening_pipeline.screen("surge_demo", use_llm=False, config=config)


def test_pipeline_lists_setups_small_cap_first_and_flags_switch_off(monkeypatch, tmp_path: Path) -> None:
    result = _run_pipeline(monkeypatch, tmp_path, regime_on=False)

    assert [p.code for p in result.picks] == ["000005", "000001"]  # smaller float cap first
    assert result.degradation[0].startswith("市场开关关闭")
    for pick in result.picks:
        assert setup_scan.REGIME_OFF_FLAG in pick.risk_flags
        assert pick.setup["type"] == "surge_pullback" and pick.setup["regime_on"] is False
        assert isinstance(pick.setup["session_index"], int)
    assert result.picks[0].factor_scores["small_cap"] > result.picks[1].factor_scores["small_cap"]


def test_pipeline_switch_on_and_block_mode(monkeypatch, tmp_path: Path) -> None:
    on = _run_pipeline(monkeypatch, tmp_path, regime_on=True)
    assert [p.code for p in on.picks] == ["000001"]
    assert on.degradation[0].startswith("市场开关开启")
    assert setup_scan.REGIME_OFF_FLAG not in on.picks[0].risk_flags

    blocked = _run_pipeline(monkeypatch, tmp_path, regime_on=False, regime_off_action="block")
    assert blocked.picks == []
    assert blocked.degradation[0].startswith("市场开关关闭")


def test_pipeline_reports_missing_tushare_token(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_API_TOKEN", raising=False)
    snapshot = pd.DataFrame([{"code": "000001", "name": "Alpha", "price": 10.0, "amount": 6e7}])
    strategy = _strategy()
    monkeypatch.setattr(screening_pipeline, "load_all_strategies_with_extra", lambda _c: {"surge_demo": strategy})
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: snapshot.copy())
    config = ScreeningRuntimeConfig(post_analyzers=[], data_dir=tmp_path)
    with pytest.raises(RuntimeError, match="TUSHARE_TOKEN"):
        screening_pipeline.screen("surge_demo", use_llm=False, config=config)


# --------------------------------------------------------------------------- data layer


class FakePro:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.empty_dates: set[str] = set()

    def _record(self, name: str, kwargs: dict) -> None:
        self.calls.append((name, kwargs))

    def daily(self, **kwargs):
        self._record("daily", kwargs)
        if kwargs["trade_date"] in self.empty_dates:
            return pd.DataFrame()
        return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": kwargs["trade_date"], "close": 10.0}])

    def stock_basic(self, **kwargs):
        self._record("stock_basic", kwargs)
        return pd.DataFrame([{"ts_code": "000001.SZ", "name": "Alpha", "industry": "机械", "list_date": "20200101"}])

    def trade_cal(self, **kwargs):
        self._record("trade_cal", kwargs)
        return pd.DataFrame({"cal_date": ["20261105", "20261106", "20261107", "20261109"],
                             "is_open": [1, 1, 0, 1]})


def test_tushare_market_data_caches_per_date_and_skips_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(setup_scan, "_TUSHARE_MIN_INTERVAL_SEC", 0.0)
    pro = FakePro()
    pro.empty_dates.add("20261109")
    data = setup_scan.TushareMarketData(tmp_path, pro=pro)

    assert list(data.daily("20261106")["ts_code"]) == ["000001.SZ"]
    assert data.daily("20261109") is None  # unpublished day: not cached
    again = setup_scan.TushareMarketData(tmp_path, pro=pro)
    assert again.daily("20261106") is not None
    assert again.daily("20261109") is None
    assert [c[1]["trade_date"] for c in pro.calls if c[0] == "daily"] == ["20261106", "20261109", "20261109"]

    assert data.open_trade_dates("20261105", "20261109") == ["20261105", "20261106", "20261109"]
    data.stock_basic()
    again.stock_basic()  # fresh cache file reused across instances
    assert [c[0] for c in pro.calls].count("stock_basic") == 1


def test_tushare_market_data_requires_token(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.delenv("TUSHARE_API_TOKEN", raising=False)
    with pytest.raises(setup_scan.SetupScanDataError, match="TUSHARE_TOKEN"):
        setup_scan.TushareMarketData(tmp_path).daily("20261106")


# --------------------------------------------------------------------------- loader / scorer / service


def _write_strategy(tmp_path: Path, setup_block: str) -> Path:
    path = tmp_path / "demo.yaml"
    path.write_text(
        "name: demo\ndisplay_name: Demo\ndescription: d\nscreening:\n  enabled: true\n"
        f"  setup_scan:\n{setup_block}",
        encoding="utf-8",
    )
    return path


def test_loader_parses_and_validates_setup_scan(tmp_path: Path) -> None:
    strategy = load_strategy(_write_strategy(tmp_path, "    type: surge_pullback\n    lookback_days: 7\n"
                                                       "    amount_20d_avg_min: 50000000\n"))
    assert strategy.screening.setup_scan.lookback_days == 7
    assert strategy.screening.setup_scan.amount_20d_avg_min == pytest.approx(5e7)

    for block, message in (
        ("    type: surge_pullback\n    unknown_key: 1\n", "Unknown keys"),
        ("    type: breakout\n", "Unsupported type"),
        ("    regime_off_action: maybe\n", "regime_off_action"),
        ("    lookback_days: 0\n", "lookback_days"),
        ("    surge_volume_ratio_min: 3\n    surge_volume_ratio_max: 2\n", "surge_volume_ratio_max"),
    ):
        with pytest.raises(ValueError, match=message):
            load_strategy(_write_strategy(tmp_path, block))


def test_small_cap_factor_prefers_smaller_float_cap() -> None:
    df = pd.DataFrame({"code": ["a", "b", "c"], "circ_mv": [9e9, 1e9, 4e9], "total_mv": [1, 1, 1]})
    scored = compute_screen_scores(df, ScreeningConfig(factor_weights={"small_cap": 1.0}))
    assert list(scored.sort_values("screen_score", ascending=False)["code"]) == ["b", "c", "a"]

    fallback = compute_screen_scores(pd.DataFrame({"code": ["x", "y"], "total_mv": [2e9, 5e8]}),
                                     ScreeningConfig(factor_weights={"small_cap": 1.0}))
    assert fallback.loc[1, "factor_small_cap_score"] > fallback.loc[0, "factor_small_cap_score"]


def test_service_explanations_surface_setup_facts() -> None:
    setup = {
        "type": "surge_pullback", "surge_date": "2026-10-28", "surge_change_pct": 10.0,
        "surge_volume_ratio": 2.4, "surge_turnover_rate": 5.1, "prior_20d_change_pct": 3.0,
        "industry": "机械", "industry_peers": 3, "session_index": 9, "window_sessions_left": 2,
        "buy_line": 17.0, "buy_line_ma": 10, "buy_line_gap_pct": 1.8, "cancel_line": 16.4,
        "next_session": "2026-11-09", "regime_on": False, "regime_change_20d_pct": -9.09,
        "regime_index": "000852.SH",
    }
    candidate = screening_service._normalize_candidate({"code": "300817", "name": "Demo", "setup": setup}, 1)
    assert candidate["setup"] == setup
    assert candidate["reason"].startswith("形态：2026-10-28 放量大涨 +10.0%")
    assert candidate["reason_source"] == "setup_scan"

    explained = screening_service._attach_candidate_explanations(candidate)
    selected = [item for item in explained["why_selected"] if item["source"] == "setup_scan"]
    now = {item["code"]: item for item in explained["why_now"]}
    assert len(selected) == 1 and "机械 同日另有 3 只" in selected[0]["text"]
    assert now["setup_buy_line"]["text"].startswith("2026-11-09 买入价格线（MA10）17.00")
    assert "16.40" in now["setup_cancel_line"]["text"]
    assert "关闭" in now["setup_regime"]["text"]
    assert explained["explanation_quality"]["why_now"] == "ok"
