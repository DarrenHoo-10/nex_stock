"""Directory caching tested at the external I/O boundary; no live provider calls."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pandas as pd
import pytest

from data_provider import akshare_fetcher
from src.services import screening_service
from src.services.screening_service import DsaEastMoneyHotspotProvider as Provider


@pytest.fixture(autouse=True)
def isolated_catalogs(monkeypatch):
    monkeypatch.setattr(Provider, "_screening_catalog_cache", {})
    monkeypatch.setattr(Provider, "_screening_catalog_failures", {})
    monkeypatch.setattr(Provider, "_ths_names_cache", None)
    monkeypatch.setattr(Provider, "_ths_names_failure_at", None)
    # Block both requests and the multiprocessing AkShare gateway unless mocked by the test.
    monkeypatch.setattr("requests.sessions.Session.request", Mock(side_effect=AssertionError("No live HTTP in tests")))
    monkeypatch.setattr(akshare_fetcher, "_akshare_call_with_timeout", Mock(side_effect=AssertionError("Mock external I/O first")))
    clock = [100.0]
    monkeypatch.setattr(screening_service.time, "monotonic", lambda: clock[0])
    return clock


def test_search_and_member_lookup_reuse_ths_directory_across_instances(monkeypatch):
    calls = []
    def fetch(fn, **kwargs):
        calls.append(fn.__name__)
        if fn.__name__ == "stock_board_concept_name_em":
            raise TimeoutError("fixture timeout")
        assert fn.__name__ == "stock_board_concept_name_ths"
        return pd.DataFrame({"name": [" 机器人概念 ", None], "code": ["301558", None]})
    monkeypatch.setattr(akshare_fetcher, "_akshare_call_with_timeout", fetch)
    directory = Provider().screening_topic_catalog("concept")
    assert directory == [{"name": "机器人概念", "source": "concept"}]
    assert Provider()._resolve_ths_concept_code("机器人概念", exact=True) == "301558"
    assert Provider()._resolve_ths_concept_code("机器人", exact=True) == ""
    directory[0]["name"] = "caller changed result"
    names = Provider()._fetch_ths_concept_names()
    names.loc[0, "code"] = "changed"
    assert Provider().screening_topic_catalog("concept")[0]["name"] == "机器人概念"
    assert Provider()._resolve_ths_concept_code("机器人概念", exact=True) == "301558"
    assert calls == ["stock_board_concept_name_em", "stock_board_concept_name_ths"]


def test_concurrent_directory_requests_coalesce_at_external_boundary(monkeypatch):
    fetch = Mock(return_value=pd.DataFrame({"name": ["机器人概念"], "code": ["301558"]}))
    monkeypatch.setattr(akshare_fetcher, "_akshare_call_with_timeout", fetch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: Provider()._fetch_ths_concept_names(), range(8)))
    assert all(result["code"].tolist() == ["301558"] for result in results)
    fetch.assert_called_once()


def test_industry_failure_cooldown_and_recovery(monkeypatch, isolated_catalogs):
    fetch = Mock(side_effect=[TimeoutError(), pd.DataFrame({"板块名称": ["电子"]})])
    monkeypatch.setattr(akshare_fetcher, "_akshare_call_with_timeout", fetch)
    with pytest.raises(TimeoutError):
        Provider().screening_topic_catalog("industry")
    with pytest.raises(ValueError, match="稍后重试"):
        Provider().screening_topic_catalog("industry")
    assert fetch.call_count == 1
    isolated_catalogs[0] += Provider._CATALOG_FAILURE_COOLDOWN_SECONDS + 1
    assert Provider().screening_topic_catalog("industry") == [{"name": "电子", "source": "industry"}]
    assert fetch.call_count == 2


def test_expired_ths_directory_failure_does_not_reuse_old_membership_map(monkeypatch, isolated_catalogs):
    fetch = Mock(side_effect=[pd.DataFrame({"name": ["机器人概念"], "code": ["301558"]}), TimeoutError()])
    monkeypatch.setattr(akshare_fetcher, "_akshare_call_with_timeout", fetch)
    Provider()._fetch_ths_concept_names()
    isolated_catalogs[0] += Provider._CATALOG_TTL_SECONDS + 1
    with pytest.raises(TimeoutError):
        Provider()._fetch_ths_concept_names()
    with pytest.raises(ValueError, match="稍后重试"):
        Provider()._fetch_ths_concept_names()
    assert fetch.call_count == 2


@pytest.mark.parametrize("frame", [pd.DataFrame({"name": ["机器人"]}), pd.DataFrame({"name": [" "], "code": [" "]})])
def test_invalid_ths_directory_is_not_cached_as_success(monkeypatch, frame):
    fetch = Mock(return_value=frame)
    monkeypatch.setattr(akshare_fetcher, "_akshare_call_with_timeout", fetch)
    with pytest.raises(ValueError):
        Provider()._fetch_ths_concept_names()
    assert Provider._ths_names_cache is None
    with pytest.raises(ValueError, match="稍后重试"):
        Provider()._fetch_ths_concept_names()
    fetch.assert_called_once()
