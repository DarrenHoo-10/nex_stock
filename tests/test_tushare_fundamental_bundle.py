# -*- coding: utf-8 -*-
"""Tushare 基本面捆绑兜底（get_fundamental_bundle）的回归测试。"""

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from data_provider.tushare_fetcher import TushareFetcher


def _make_fetcher() -> TushareFetcher:
    with patch.object(TushareFetcher, "_init_api", return_value=None):
        fetcher = TushareFetcher()
    fetcher._api = MagicMock()
    fetcher.priority = 2
    return fetcher


def _fin_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000938.SZ", "000938.SZ"],
            "end_date": ["20260331", "20260630"],
            "or_yoy": [20.1, 33.93],
            "netprofit_yoy": [15.0, 108.18],
            "roe": [6.1, 13.29],
            "grossprofit_margin": [12.0, 13.41],
        }
    )


def _income_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000938.SZ", "000938.SZ"],
            "end_date": ["20260331", "20260630"],
            "total_revenue": [3.0e10, 6.35e10],
            "n_income_attr_p": [1.0e9, 2.17e9],
        }
    )


def _cashflow_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000938.SZ", "000938.SZ"],
            "end_date": ["20260331", "20260630"],
            "n_cashflow_act": [-1.0e9, -4.2e9],
        }
    )


def _forecast_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000938.SZ"],
            "ann_date": ["20260710"],
            "end_date": ["20260630"],
            "type": ["预增"],
            "p_change_min": [80.0],
            "p_change_max": [110.0],
            "summary": [""],
        }
    )


def _dividend_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000938.SZ"] * 4,
            "end_date": ["20251231", "20250630", "20241231", "20251231"],
            "div_proc": ["实施", "实施", "实施", "预案"],
            "cash_div": [0.5, 0.3, 0.4, 9.9],
            "cash_div_tax": [0.45, 0.27, 0.36, 8.9],
            "record_date": ["20260701", "20251101", "20250102", ""],
            "ex_date": ["20260702", "20251103", "20250103", ""],
            "pay_date": ["", "", "", ""],
        }
    )


def _top10_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": ["000938.SZ"] * 2,
            "ann_date": ["20260820", "20260820"],
            "end_date": ["20260630", "20260630"],
            "holder_name": ["甲", "乙"],
            "hold_change": [-120.5, 30.0],
        }
    )


class TestTushareFundamentalBundle(unittest.TestCase):
    """覆盖字段映射、fail-open 与预算门控。"""

    def _bundle(self, fetcher: TushareFetcher, **kwargs) -> dict:
        return fetcher.get_fundamental_bundle("000938.SZ", **kwargs)

    def test_happy_path_maps_all_blocks_and_marks_partial(self) -> None:
        fetcher = _make_fetcher()

        def fake_call(api_name: str, fields: str = "", **kwargs) -> pd.DataFrame:
            return {
                "fina_indicator": _fin_df(),
                "income": _income_df(),
                "cashflow": _cashflow_df(),
                "forecast": _forecast_df(),
                "dividend": _dividend_df(),
                "top10_floatholders": _top10_df(),
            }[api_name]

        with patch.object(fetcher, "_call_api_with_rate_limit", side_effect=fake_call):
            result = self._bundle(fetcher)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["growth"],
            {
                "revenue_yoy": 33.93,
                "net_profit_yoy": 108.18,
                "roe": 13.29,
                "gross_margin": 13.41,
            },
        )
        report = result["earnings"]["financial_report"]
        self.assertEqual(report["report_date"], "2026-06-30")
        self.assertAlmostEqual(report["revenue"], 6.35e10)
        self.assertAlmostEqual(report["net_profit_parent"], 2.17e9)
        self.assertAlmostEqual(report["operating_cash_flow"], -4.2e9)
        self.assertAlmostEqual(report["roe"], 13.29)
        self.assertIn("预增", result["earnings"]["forecast_summary"])
        dividend = result["earnings"]["dividend"]
        self.assertEqual(dividend["ttm_event_count"], 2)
        self.assertAlmostEqual(dividend["ttm_cash_dividend_per_share"], 0.8)
        self.assertAlmostEqual(
            result["institution"]["top10_holder_change"], -120.5
        )
        chain = " ".join(result["source_chain"])
        self.assertIn("growth:tushare.fina_indicator", chain)
        self.assertIn("earnings_report:tushare.income,cashflow", chain)

    def test_fail_open_when_all_apis_raise(self) -> None:
        fetcher = _make_fetcher()
        with patch.object(
            fetcher,
            "_call_api_with_rate_limit",
            side_effect=Exception("抱歉，您没有接口访问权限"),
        ):
            result = self._bundle(fetcher)

        self.assertEqual(result["status"], "not_supported")
        self.assertEqual(result["growth"], {})
        self.assertEqual(result["earnings"], {})
        self.assertTrue(result["errors"])

    def test_budget_gate_skips_optional_apis(self) -> None:
        fetcher = _make_fetcher()
        called = []

        def fake_call(api_name: str, fields: str = "", **kwargs) -> pd.DataFrame:
            called.append(api_name)
            return {
                "fina_indicator": _fin_df(),
                "income": _income_df(),
                "cashflow": _cashflow_df(),
            }.get(api_name, pd.DataFrame())

        with patch.object(fetcher, "_call_api_with_rate_limit", side_effect=fake_call):
            result = self._bundle(fetcher, budget_seconds=0)

        self.assertEqual(result["status"], "partial")
        self.assertNotIn("forecast", called)
        self.assertNotIn("dividend", called)
        self.assertNotIn("top10_floatholders", called)

    def test_etf_code_short_circuits_without_api_calls(self) -> None:
        fetcher = _make_fetcher()
        with patch.object(fetcher, "_call_api_with_rate_limit") as api_mock:
            result = fetcher.get_fundamental_bundle("510050.SH")

        api_mock.assert_not_called()
        self.assertEqual(result["status"], "not_supported")


if __name__ == "__main__":
    unittest.main()
