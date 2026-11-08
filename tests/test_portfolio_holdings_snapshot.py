# -*- coding: utf-8 -*-
"""Offline tests for the manual holdings snapshot written into the portfolio ledger."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.config import Config
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager

ENTRY = date(2026, 11, 6)
LATER = date(2026, 11, 9)


class HoldingsSnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        env_path = Path(self.temp_dir.name) / ".env"
        db_path = Path(self.temp_dir.name) / "snapshot_test.db"
        env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(env_path)
        os.environ["DATABASE_PATH"] = str(db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.service = PortfolioService()
        # Name lookup must stay offline in tests.
        patcher = patch.object(
            PortfolioService,
            "_resolve_snapshot_stock_name",
            staticmethod(lambda code: {"600519": "贵州茅台", "000001": "平安银行"}.get(code, code)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    # helpers ----------------------------------------------------------
    def _set(self, cash, holdings, entry=ENTRY, **kwargs):
        return self.service.set_holdings_snapshot(cash, holdings, entry_date=entry, **kwargs)

    def _trades(self, account_id):
        return self.service.list_trade_events(account_id=account_id, page=1, page_size=100)["items"]

    def _cash_entries(self, account_id):
        return self.service.list_cash_ledger_events(account_id=account_id, page=1, page_size=100)["items"]

    def _position(self, account_id, code, as_of=LATER, method="fifo"):
        snap = self.service.get_portfolio_snapshot(
            account_id=account_id, as_of=as_of, cost_method=method, include_realtime=False, read_only=True
        )
        for pos in snap["accounts"][0]["positions"]:
            if pos["symbol"] == code:
                return pos
        return None

    def _cash(self, account_id, as_of=LATER):
        snap = self.service.get_portfolio_snapshot(
            account_id=account_id, as_of=as_of, include_realtime=False, read_only=True
        )
        return snap["accounts"][0]["total_cash"]

    def _save_close(self, symbol: str, on_date: date, close: float) -> None:
        df = pd.DataFrame(
            [{"date": on_date, "open": close, "high": close, "low": close, "close": close,
              "volume": 1.0, "amount": close, "pct_chg": 0.0}]
        )
        self.db.save_daily_data(df, code=symbol, data_source="unit-test")

    # first snapshot ---------------------------------------------------
    def test_first_snapshot_writes_opening_trade_and_cash_difference(self):
        result = self._set(
            50000,
            [{"code": "600519", "shares": 150, "cost": 100.5, "buy_date": "2026-10-20", "horizon": "swing"}],
        )
        aid = result["account_id"]

        trades = self._trades(aid)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual((trade["symbol"], trade["side"], trade["quantity"], trade["price"]), ("600519", "buy", 150.0, 100.5))
        self.assertEqual(trade["trade_date"], "2026-10-20")
        self.assertEqual(trade["note"], "期初持仓|horizon=swing")  # odd lot is accepted

        cash_entries = self._cash_entries(aid)
        self.assertEqual(len(cash_entries), 1)
        self.assertEqual(cash_entries[0]["direction"], "in")
        self.assertAlmostEqual(cash_entries[0]["amount"], 50000 + 150 * 100.5)

        pos = self._position(aid, "600519")
        self.assertAlmostEqual(pos["quantity"], 150)
        self.assertAlmostEqual(pos["avg_cost"], 100.5)
        self.assertAlmostEqual(self._cash(aid), 50000)

        self.assertAlmostEqual(result["cash"], 50000)
        holding = result["holdings"][0]
        self.assertEqual((holding["code"], holding["name"], holding["shares"]), ("600519", "贵州茅台", 150))
        self.assertAlmostEqual(holding["cost"], 100.5)
        self.assertEqual(holding["buy_date"], "2026-10-20")
        self.assertTrue(holding["buy_date_confirmed"])
        self.assertEqual(holding["horizon"], "swing")
        # No quote available offline: market value falls back to cost basis.
        self.assertFalse(holding["price_available"])
        self.assertAlmostEqual(result["market_value"], 150 * 100.5)
        self.assertAlmostEqual(result["total_capital"], 50000 + 150 * 100.5)

    def test_missing_buy_date_uses_entry_date_and_is_marked_unconfirmed(self):
        result = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10}])
        trade = self._trades(result["account_id"])[0]
        self.assertEqual(trade["trade_date"], ENTRY.isoformat())
        self.assertEqual(trade["note"], "期初持仓|日期未确认")
        holding = result["holdings"][0]
        self.assertFalse(holding["buy_date_confirmed"])
        self.assertEqual(holding["buy_date"], ENTRY.isoformat())
        self.assertIsNone(holding["horizon"])

    def test_market_value_uses_history_close_when_available(self):
        self._save_close("600519", ENTRY, 12.0)
        result = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-11-01"}])
        holding = result["holdings"][0]
        self.assertTrue(holding["price_available"])
        self.assertAlmostEqual(holding["market_value"], 1200.0)
        self.assertAlmostEqual(result["total_capital"], 1000 + 1200.0)

    def test_default_account_created_then_reused(self):
        self.assertEqual(self.service.list_accounts(), [])
        first = self._set(100, [])
        second = self._set(200, [])
        self.assertEqual(first["account_id"], second["account_id"])
        accounts = self.service.list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual((accounts[0]["market"], accounts[0]["base_currency"]), ("cn", "CNY"))

    def test_picks_existing_cn_cny_account_and_rejects_foreign_explicit_account(self):
        us = self.service.create_account(name="us", broker=None, market="us", base_currency="USD")
        cn = self.service.create_account(name="mine", broker=None, market="cn", base_currency="CNY")
        result = self._set(100, [])
        self.assertEqual(result["account_id"], cn["id"])
        with self.assertRaises(ValueError):
            self._set(100, [], account_id=us["id"])

    # validation -------------------------------------------------------
    def test_validation_errors(self):
        bad_inputs = [
            (-1, []),
            (True, []),
            (100, [{"code": "AAPL", "shares": 1, "cost": 1}]),
            (100, [{"code": "600519", "shares": 0, "cost": 1}]),
            (100, [{"code": "600519", "shares": 1.5, "cost": 1}]),
            (100, [{"code": "600519", "shares": 10, "cost": 0}]),
            (100, [{"code": "600519", "shares": 10, "cost": float("nan")}]),
            (100, [{"code": "600519", "shares": 10, "cost": 1, "buy_date": "2026/10/01"}]),
            (100, [{"code": "600519", "shares": 10, "cost": 1, "buy_date": "2026-12-01"}]),
            (100, [{"code": "600519", "shares": 10, "cost": 1, "horizon": "long"}]),
            (100, [{"code": "600519", "shares": 10, "cost": 1}, {"code": "600519", "shares": 5, "cost": 1}]),
        ]
        for cash, holdings in bad_inputs:
            with self.subTest(cash=cash, holdings=holdings):
                with self.assertRaises(ValueError):
                    self._set(cash, holdings)
        self.assertEqual(self.service.list_accounts(), [])  # nothing written for invalid input

    def test_integral_float_shares_are_accepted(self):
        result = self._set(0, [{"code": "600519", "shares": 300.0, "cost": 5}])
        self.assertEqual(result["holdings"][0]["shares"], 300)

    # re-snapshot ------------------------------------------------------
    def test_same_snapshot_twice_is_idempotent(self):
        holdings = [{"code": "600519", "shares": 150, "cost": 100.123, "buy_date": "2026-10-20", "horizon": "short"}]
        first = self._set(50000, holdings)
        aid = first["account_id"]
        second = self._set(50000, holdings)
        self.assertFalse(second["changed"])
        self.assertEqual(second["trades"], [])
        self.assertIsNone(second["cash_entry"])
        self.assertEqual(len(self._trades(aid)), 1)
        self.assertEqual(len(self._cash_entries(aid)), 1)
        # Even on a later day (entry date moves), nothing new is written.
        third = self._set(50000, holdings, entry=LATER)
        self.assertFalse(third["changed"])
        self.assertEqual(len(self._trades(aid)), 1)

    def test_rounded_cost_within_tolerance_does_not_rewrite(self):
        aid = self._set(0, [{"code": "600519", "shares": 100, "cost": 27.85333}])["account_id"]
        again = self._set(0, [{"code": "600519", "shares": 100, "cost": 27.853}])
        self.assertFalse(again["changed"])
        self.assertEqual(len(self._trades(aid)), 1)

    def test_increase_writes_single_buy_for_difference_and_keeps_old_lot_dates(self):
        aid = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-10-01"}])["account_id"]
        result = self._set(500, [{"code": "600519", "shares": 300, "cost": 12, "buy_date": "2026-11-06"}], entry=LATER)
        self.assertEqual([t["side"] for t in result["trades"]], ["buy"])
        trade = result["trades"][0]
        self.assertAlmostEqual(trade["quantity"], 200)
        self.assertAlmostEqual(trade["price"], (300 * 12 - 100 * 10) / 200)  # 13.0
        self.assertEqual(trade["trade_date"], "2026-11-06")
        self.assertTrue(trade["note"].startswith("持仓调整"))
        pos = self._position(aid, "600519", method="fifo")
        self.assertAlmostEqual(pos["quantity"], 300)
        self.assertAlmostEqual(pos["avg_cost"], 12)
        self.assertAlmostEqual(self._position(aid, "600519", method="avg")["avg_cost"], 12)
        self.assertAlmostEqual(self._cash(aid), 500)
        self.assertEqual(result["holdings"][0]["buy_date"], "2026-11-06")  # latest lot

    def test_decrease_with_same_cost_writes_cost_neutral_sell(self):
        aid = self._set(1000, [{"code": "600519", "shares": 300, "cost": 10, "buy_date": "2026-10-01"}])["account_id"]
        result = self._set(2000, [{"code": "600519", "shares": 100, "cost": 10}], entry=LATER)
        self.assertEqual([t["side"] for t in result["trades"]], ["sell"])
        self.assertAlmostEqual(result["trades"][0]["quantity"], 200)
        self.assertAlmostEqual(result["trades"][0]["price"], 10)
        pos = self._position(aid, "600519")
        self.assertAlmostEqual(pos["quantity"], 100)
        self.assertAlmostEqual(pos["avg_cost"], 10)
        self.assertAlmostEqual(self._cash(aid), 2000)
        snap = self.service.get_portfolio_snapshot(
            account_id=aid, as_of=LATER, include_realtime=False, read_only=True
        )
        self.assertAlmostEqual(snap["realized_pnl"], 0.0, places=4)

    def test_cost_correction_closes_and_reopens_without_realized_pnl(self):
        aid = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-10-01"}])["account_id"]
        result = self._set(1000, [{"code": "600519", "shares": 100, "cost": 11, "buy_date": "2026-10-15"}], entry=LATER)
        self.assertEqual([t["side"] for t in result["trades"]], ["sell", "buy"])
        self.assertEqual({t["trade_date"] for t in result["trades"]}, {"2026-10-15"})
        self.assertEqual(result["warnings"], [])
        pos = self._position(aid, "600519")
        self.assertAlmostEqual(pos["quantity"], 100)
        self.assertAlmostEqual(pos["avg_cost"], 11)
        self.assertAlmostEqual(self._cash(aid), 1000)
        snap = self.service.get_portfolio_snapshot(
            account_id=aid, as_of=LATER, include_realtime=False, read_only=True
        )
        self.assertAlmostEqual(snap["realized_pnl"], 0.0, places=4)
        self.assertTrue(result["holdings"][0]["buy_date_confirmed"])

    def test_reopen_buy_date_before_existing_event_is_clamped_and_reported(self):
        aid = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-10-01"}])["account_id"]
        result = self._set(1000, [{"code": "600519", "shares": 100, "cost": 11, "buy_date": "2026-09-20"}], entry=LATER)
        # The reopened lot cannot predate the ledger event it replaces: date moves, warning, unconfirmed.
        self.assertEqual({t["trade_date"] for t in result["trades"]}, {"2026-10-01"})
        self.assertEqual(len(result["warnings"]), 1)
        self.assertFalse(result["holdings"][0]["buy_date_confirmed"])
        self.assertAlmostEqual(self._position(aid, "600519")["avg_cost"], 11)

    def test_decrease_with_different_lot_costs_falls_back_to_reopen(self):
        aid = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-10-01"}])["account_id"]
        self._set(1000, [{"code": "600519", "shares": 200, "cost": 15, "buy_date": "2026-10-10"}], entry=ENTRY)
        # Average cost stays 15 but FIFO would leave the 20 yuan lot (cost 20 != 15): reopen at 15.
        result = self._set(1000, [{"code": "600519", "shares": 100, "cost": 15}], entry=LATER)
        self.assertEqual([t["side"] for t in result["trades"]], ["sell", "buy"])
        pos = self._position(aid, "600519")
        self.assertAlmostEqual(pos["quantity"], 100)
        self.assertAlmostEqual(pos["avg_cost"], 15)

    def test_removed_holding_becomes_cost_neutral_sell(self):
        aid = self._set(
            1000,
            [
                {"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-10-01"},
                {"code": "000001", "shares": 500, "cost": 8, "buy_date": "2026-10-02"},
            ],
        )["account_id"]
        result = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10}], entry=LATER)
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual((result["trades"][0]["symbol"], result["trades"][0]["side"]), ("000001", "sell"))
        self.assertIsNone(self._position(aid, "000001"))
        self.assertAlmostEqual(self._position(aid, "600519")["quantity"], 100)
        self.assertEqual([h["code"] for h in result["holdings"]], ["600519"])
        self.assertAlmostEqual(self._cash(aid), 1000)

    def test_adding_a_new_holding_uses_opening_tag(self):
        aid = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-10-01"}])["account_id"]
        result = self._set(
            1000,
            [
                {"code": "600519", "shares": 100, "cost": 10},
                {"code": "000001", "shares": 700, "cost": 9.9, "buy_date": "2026-11-02", "horizon": "short"},
            ],
            entry=LATER,
        )
        self.assertEqual(len(result["trades"]), 1)
        self.assertEqual(result["trades"][0]["note"], "期初持仓|horizon=short")
        self.assertAlmostEqual(self._position(aid, "000001")["avg_cost"], 9.9)
        self.assertAlmostEqual(self._cash(aid), 1000)

    def test_cash_only_change_writes_only_a_ledger_entry(self):
        aid = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10, "buy_date": "2026-10-01"}])["account_id"]
        up = self._set(1500, [{"code": "600519", "shares": 100, "cost": 10}], entry=LATER)
        self.assertEqual(up["trades"], [])
        self.assertEqual((up["cash_entry"]["direction"], round(up["cash_entry"]["amount"], 6)), ("in", 500.0))
        down = self._set(400, [{"code": "600519", "shares": 100, "cost": 10}], entry=LATER)
        self.assertEqual((down["cash_entry"]["direction"], round(down["cash_entry"]["amount"], 6)), ("out", 1100.0))
        self.assertAlmostEqual(self._cash(aid), 400)
        self.assertEqual(len(self._trades(aid)), 1)

    def test_empty_snapshot_clears_positions(self):
        aid = self._set(1000, [{"code": "600519", "shares": 100, "cost": 10}])["account_id"]
        result = self._set(1000, [], entry=LATER)
        self.assertEqual(result["holdings"], [])
        self.assertIsNone(self._position(aid, "600519"))
        self.assertAlmostEqual(self._cash(aid), 1000)

    # interaction with normal trades ----------------------------------
    def test_interaction_with_existing_normal_trade(self):
        account = self.service.create_account(name="main", broker=None, market="cn", base_currency="CNY")
        aid = account["id"]
        self.service.record_cash_ledger(account_id=aid, event_date=date(2026, 10, 1), direction="in", amount=100000)
        self.service.record_trade(
            account_id=aid, symbol="600519", trade_date=date(2026, 10, 5), side="buy", quantity=100, price=10, fee=5
        )
        # Replayed cost includes the fee: (1000 + 5) / 100 = 10.05; cash = 100000 - 1005.
        self.assertAlmostEqual(self._position(aid, "600519", as_of=ENTRY)["avg_cost"], 10.05)

        # Snapshot matching reality is a no-op for the position; only the cash difference is written.
        same = self._set(98000, [{"code": "600519", "shares": 100, "cost": 10.05}])
        self.assertEqual(same["account_id"], aid)
        self.assertEqual(same["trades"], [])
        self.assertAlmostEqual(same["cash_entry"]["amount"], 995.0)
        self.assertEqual(same["cash_entry"]["direction"], "out")
        self.assertAlmostEqual(self._cash(aid, as_of=ENTRY), 98000)

        # A later normal trade dated after the snapshot keeps working with the replay.
        self.service.record_trade(
            account_id=aid, symbol="600519", trade_date=LATER, side="sell", quantity=40, price=12
        )
        self.assertAlmostEqual(self._position(aid, "600519", as_of=LATER)["quantity"], 60)

        # Re-snapshot with a cost correction after a later normal event: reopen is dated after it.
        result = self._set(98480, [{"code": "600519", "shares": 60, "cost": 11, "buy_date": "2026-11-06"}], entry=ENTRY)
        self.assertEqual([t["side"] for t in result["trades"]], ["sell", "buy"])
        self.assertEqual({t["trade_date"] for t in result["trades"]}, {LATER.isoformat()})
        self.assertEqual(len(result["warnings"]), 1)
        self.assertAlmostEqual(self._position(aid, "600519", as_of=LATER)["avg_cost"], 11)
        self.assertAlmostEqual(self._cash(aid, as_of=LATER), 98480)

    def test_snapshot_does_not_touch_other_market_positions(self):
        account = self.service.create_account(name="main", broker=None, market="cn", base_currency="CNY")
        self.service.record_trade(
            account_id=account["id"], symbol="AAPL", trade_date=date(2026, 10, 5), side="buy",
            quantity=5, price=100, market="us", currency="USD",
        )
        result = self._set(0, [])
        self.assertEqual(result["trades"], [])
        snap = self.service.get_portfolio_snapshot(
            account_id=account["id"], as_of=ENTRY, include_realtime=False, read_only=True
        )
        self.assertEqual([p["symbol"] for p in snap["accounts"][0]["positions"]], ["AAPL"])


if __name__ == "__main__":
    unittest.main()
