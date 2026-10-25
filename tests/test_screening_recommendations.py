"""Offline pool aggregation and cache tests: no market-data requests."""
from datetime import date
from unittest.mock import Mock

import pandas as pd
import pytest

from src.config import Config
from src.services import screening_service as service_module
from src.services.screening.config import Config as RuntimeConfig
from src.services.screening.recommendations import aggregate_limit_up_topics
from src.services.screening_service import ScreeningService


def test_counts_unique_pool_codes_not_change_percentage_or_label_duplicates():
    pool = pd.DataFrame([
        {"代码": "600001", "名称": "甲", "所属行业": "机械", "涨跌幅": 5},
        {"代码": "600001", "名称": "甲", "所属行业": "机械", "涨跌幅": 5},
        {"代码": "300001", "名称": "乙", "所属行业": "电子", "涨跌幅": 20},
        {"代码": "invalid", "名称": "无效", "所属行业": "错误"},
    ])
    mapping = {"600001": {"concepts": "机器人;机器人"}, "300001": {"concepts": "机器人;芯片"}}
    rows = aggregate_limit_up_topics(pool, trade_date="20261023", mapping=mapping)
    assert rows[0]["topic"] == "机器人"
    assert rows[0]["limit_up_count"] == 2
    assert rows[0]["count_coverage"] == "mapped_pool"
    assert {row["topic"]: row["limit_up_count"] for row in rows} == {"机器人": 2, "机械": 1, "电子": 1, "芯片": 1}
    assert all(row["trade_date"] == "20261023" for row in rows)


@pytest.fixture
def recommendations(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENING_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(service_module, "_LIMIT_UP_RECOMMENDATIONS_LAST_ATTEMPT", 0)
    monkeypatch.setattr("src.core.trading_calendar.get_effective_trading_date", lambda _: date(2026, 10, 23))
    monkeypatch.setattr(service_module.ScreeningPipelineConfig, "from_env", lambda: RuntimeConfig(industry_provider_cache_dir=None))
    pool = pd.DataFrame([{"代码": "600001", "名称": "甲", "所属行业": "机械"}])
    fetch = Mock(return_value=pool)
    monkeypatch.setattr("data_provider.akshare_fetcher._akshare_call_with_timeout", fetch)
    return ScreeningService(Config(screening_enabled=True)), fetch


def test_read_and_repeated_refresh_do_not_refetch_and_empty_concepts_are_visible(recommendations):
    service, fetch = recommendations
    assert service.limit_up_hotspots()["hotspots"] == []
    fetch.assert_not_called()
    result = service.limit_up_hotspots(refresh=True)
    assert result["hotspots"][0]["limit_up_count"] == 1
    assert result["hotspots"][0]["trade_date"] == "20261023"
    assert any("仅按涨停池所属行业" in note for note in result["source_errors"])
    assert service.limit_up_hotspots()["cache_used"]
    assert service.limit_up_hotspots(refresh=True)["cache_used"]
    fetch.assert_called_once()
    assert fetch.call_args.kwargs["date"] == "20261023"


def test_failed_refresh_retains_original_date_with_warning(recommendations, monkeypatch):
    service, fetch = recommendations
    original = service.limit_up_hotspots(refresh=True)
    monkeypatch.setattr(service_module, "_LIMIT_UP_RECOMMENDATIONS_LAST_ATTEMPT", 0)
    fetch.side_effect = TimeoutError()
    result = service.limit_up_hotspots(refresh=True)
    assert result["hotspots"] == original["hotspots"]
    assert result["stale"] and result["fallback_used"]
    assert any("刷新失败" in error for error in result["source_errors"])


def test_old_heat_cache_never_appears_as_limit_up_recommendations(recommendations, tmp_path):
    service, fetch = recommendations
    (tmp_path / "hotspots.json").write_text('{"hotspots":[{"topic":"假榜","heat_score":99}]}', encoding="utf-8")
    assert service.limit_up_hotspots()["hotspots"] == []
    fetch.assert_not_called()
