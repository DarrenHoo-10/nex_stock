# -*- coding: utf-8 -*-
"""Chat turns carry market phase + time; in-progress daily bars are never persisted."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent import executor
from src.agent.stock_scope import StockScopeResolution
from src.agent.tools import data_tools
from src.services.history_loader import filter_persistable_daily_bars

CN = timezone(timedelta(hours=8))
INTRADAY = datetime(2026, 11, 6, 10, 32, tzinfo=CN)  # Friday, trading session
POSTMARKET = datetime(2026, 11, 6, 16, 5, tzinfo=CN)
SATURDAY = datetime(2026, 11, 7, 11, 0, tzinfo=CN)


def _freeze(moment):
    """Make trading_calendar's wall clock deterministic."""
    from src.core import trading_calendar

    real = trading_calendar.build_market_phase_context

    def build(**kwargs):
        kwargs.setdefault("current_time", None)
        if kwargs["current_time"] is None:
            kwargs["current_time"] = moment
        return real(**kwargs)

    return patch.object(trading_calendar, "build_market_phase_context", build)


class TestChatPhaseInjection(unittest.TestCase):
    def test_section_has_phase_time_and_freshness_rule(self):
        with _freeze(INTRADAY):
            section = executor.build_chat_market_phase_section("600519", "zh")
        self.assertIn("市场阶段上下文", section)
        self.assertIn("盘中", section)
        self.assertIn("2026-11-06T10:32", section)
        self.assertIn("历史数字", section)
        self.assertIn("截至", section)

    def test_english_variant_and_default_market(self):
        with _freeze(SATURDAY):
            section = executor.build_chat_market_phase_section("", "en")  # no stock -> A-share clock
        self.assertIn("Market Phase Context", section)
        self.assertIn("non-trading day", section)
        self.assertIn("as of", section)

    def _prepare(self, stock_code="600519", use_codex=False, use_legacy=False):
        context = {"stock_code": stock_code} if stock_code else {}
        with patch.object(executor, "build_agent_chat_context_bundle") as bundle:
            bundle.return_value = SimpleNamespace(context_messages=[])
            return executor.prepare_agent_chat(
                message="现在涨了多少",
                session_id="s1",
                context=context,
                config=SimpleNamespace(),
                context_llm_adapter=MagicMock(),
                skill_instructions="",
                default_skill_policy="",
                use_legacy_default_prompt=use_legacy,
                use_codex_prompt=use_codex,
                include_provider_trace=True,
                scope_resolution=StockScopeResolution(context, None),
            )

    def test_every_prompt_variant_gets_phase_each_turn(self):
        for kwargs in ({}, {"use_codex": True}, {"use_legacy": True}):
            with _freeze(INTRADAY):
                prompt = self._prepare(**kwargs).system_prompt
            self.assertIn("市场阶段上下文", prompt, kwargs)
            self.assertIn("2026-11-06T10:32", prompt, kwargs)
        # the next turn is built from the clock again, not from a cached prompt
        with _freeze(POSTMARKET):
            later = self._prepare().system_prompt
        self.assertIn("2026-11-06T16:05", later)
        self.assertIn("盘后", later)

    def test_drawer_daily_plan_object_reaches_the_prompt(self):
        context = {
            "stock_code": "000938", "stock_name": "紫光股份",
            "daily_plan": {"trade_date": "2026-11-06", "kind": "stock", "board_id": None,
                           "stock_code": "000938", "horizon": "short"},
        }
        with patch.object(executor, "build_agent_chat_context_bundle") as bundle, _freeze(SATURDAY):
            bundle.return_value = SimpleNamespace(context_messages=[])
            prepared = executor.prepare_agent_chat(
                message="你对明天的走势怎么看呢？", session_id="s1", context=context,
                config=SimpleNamespace(), context_llm_adapter=MagicMock(), skill_instructions="",
                default_skill_policy="", use_legacy_default_prompt=False, use_codex_prompt=False,
                include_provider_trace=True,
                scope_resolution=StockScopeResolution(context, None),
            )
        injected = "\n".join(m["content"] for m in prepared.history_messages if m["role"] == "user")
        self.assertIn("股票代码: 000938", injected)
        self.assertIn("紫光股份", injected)
        self.assertIn("2026-11-06 收盘数据，短线 1–5 天", injected)
        self.assertIn("问题没说明对象时，都指这只股票", injected)

    def test_daily_plan_line_for_board_and_none(self):
        board = executor.format_daily_plan_context_line(
            {"trade_date": "2026-11-06", "kind": "board", "board_id": "concept:BK1184", "stock_code": None})
        self.assertIn("板块 concept:BK1184", board)
        self.assertIn("get_board_signal", board)
        self.assertEqual(executor.format_daily_plan_context_line({"kind": "none", "trade_date": "2026-11-06"}), "")
        self.assertEqual(executor.format_daily_plan_context_line(None), "")

    def test_phase_failure_never_breaks_chat(self):
        with patch("src.core.trading_calendar.build_market_phase_context", side_effect=RuntimeError("boom")):
            prompt = self._prepare().system_prompt
        self.assertNotIn("市场阶段上下文", prompt)


class TestPartialBarGuard(unittest.TestCase):
    @staticmethod
    def _df():
        return pd.DataFrame({
            "date": ["2026-11-04", "2026-11-05", "2026-11-06"],
            "open": [1.0, 1.0, 1.0], "high": [1.0, 1.0, 1.0], "low": [1.0, 1.0, 1.0],
            "close": [1.0, 1.0, 1.0], "volume": [1, 1, 1], "amount": [1, 1, 1], "pct_chg": [0, 0, 0],
        })

    def test_today_bar_dropped_while_market_open(self):
        out, dropped = filter_persistable_daily_bars(self._df(), "600519", current_time=INTRADAY)
        self.assertTrue(dropped)
        self.assertEqual(list(out["date"]), ["2026-11-04", "2026-11-05"])

    def test_today_bar_kept_after_close_and_on_non_trading_day(self):
        out, dropped = filter_persistable_daily_bars(self._df(), "600519", current_time=POSTMARKET)
        self.assertFalse(dropped)
        self.assertEqual(len(out), 3)
        out, dropped = filter_persistable_daily_bars(self._df(), "600519", current_time=SATURDAY)
        self.assertFalse(dropped)
        self.assertEqual(len(out), 3)

    def test_handler_does_not_persist_partial_bar_but_still_returns_it(self):
        db = MagicMock()
        db.save_daily_data.return_value = 2
        with _freeze(INTRADAY), \
                patch("src.services.history_loader.load_history_df", return_value=(self._df(), "EfinanceFetcher")), \
                patch.object(data_tools, "_get_db", return_value=db):
            result = data_tools._handle_get_daily_history("600519", days=60)
        saved_df = db.save_daily_data.call_args.args[0]
        self.assertEqual(list(saved_df["date"]), ["2026-11-04", "2026-11-05"])
        self.assertEqual(result["total_records"], 3)
        self.assertTrue(result["last_bar_incomplete"])

    def test_handler_persists_full_frame_after_close(self):
        db = MagicMock()
        with _freeze(POSTMARKET), \
                patch("src.services.history_loader.load_history_df", return_value=(self._df(), "EfinanceFetcher")), \
                patch.object(data_tools, "_get_db", return_value=db):
            result = data_tools._handle_get_daily_history("600519", days=60)
        self.assertEqual(len(db.save_daily_data.call_args.args[0]), 3)
        self.assertNotIn("last_bar_incomplete", result)


if __name__ == "__main__":
    unittest.main()
