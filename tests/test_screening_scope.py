"""Scope contracts through the real filtering pipeline, API and persisted results."""
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.v1.endpoints import screening as routes
from src.config import Config
from src.services.screening import pipeline
from src.services.screening.config import Config as RuntimeConfig
from src.services.screening.models import HardFilterConfig, ScreeningConfig, Strategy
from src.services.screening.scope import ScreeningScope, apply_scope
from src.services import screening_service as service_module
from src.services.screening_service import DsaEastMoneyHotspotProvider, ScreeningService
from src.storage import DatabaseManager


TOPICS = [{"name": "机器人", "source": "concept"}, {"name": "电子", "source": "industry"}]


@pytest.fixture
def scope_runtime(monkeypatch):
    frame = pd.DataFrame([
        {"code": code, "name": name, "price": price, "amount": 200_000_000, "change_pct": 1}
        for code, name, price in [
            ("600001", "主板甲", 10), ("000001", "主板乙", 20),
            ("300001", "创业甲", 10), ("688001", "科创甲", 10),
            ("920001", "北交甲", 10), ("600002", "其他题材", 10),
        ]
    ])
    strategy = Strategy(name="scope_demo", display_name="范围示例", description="test", screening=ScreeningConfig(
        hard_filters=HardFilterConfig(price_max=15), factor_weights={"value": 1}, max_output=10,
    ))
    runtime = RuntimeConfig(post_analyzers=[], risk_enabled=False, portfolio_diversity_enabled=False)
    monkeypatch.setattr(pipeline, "load_all_strategies_with_extra", lambda _: {"scope_demo": strategy})
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: frame.copy())
    monkeypatch.setattr(service_module, "_get_screening_status_snapshot", lambda: ({}, True, None))
    monkeypatch.setattr(service_module, "_build_screening_context", lambda *a, **k: {})
    monkeypatch.setattr(service_module.ScreeningPipelineConfig, "from_env", lambda: runtime)
    monkeypatch.setattr(service_module, "_enrich_candidates_with_dsa", lambda candidates, **k: (candidates, {}))
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "screening_topic_catalog", lambda self, source: [t for t in TOPICS if t["source"] == source])
    def constituents(self, topic, *, source):
        codes = ["600001", "000001", "300001"] if source == "concept" else ["600001", "688001"]
        return pd.DataFrame({"代码": codes})
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_eastmoney_constituents", constituents)
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_ths_constituents", lambda *a, **k: pd.DataFrame())
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_url="sqlite:///:memory:")
    service = ScreeningService(Config(screening_enabled=True), db_manager=db)
    yield service, db, frame, runtime
    DatabaseManager.reset_instance()


@pytest.mark.parametrize("match,expected", [("any", {"600001", "300001", "688001"}), ("all", {"600001"})])
def test_scope_runs_before_strategy_and_persists_evidence(scope_runtime, match, expected):
    service, db, _, _ = scope_runtime
    scope = ScreeningScope(topics=TOPICS, topic_match=match)
    result = service.screen(strategy="scope_demo", market="cn", max_results=10, scope=scope)
    assert {item["code"] for item in result["candidates"]} == expected
    assert all(item["matched_topics"] for item in result["candidates"])
    assert result["scope_counts"]["board_count"] == 6
    assert result["scope_counts"]["topic_count"] == (4 if match == "any" else 1)
    assert result["after_filter_count"] == len(expected)  # price=20 removed by real strategy
    assert result["scope"] == scope.model_dump()
    assert result["topic_evidence"][0]["retrieved_at"]
    assert db.get_screening_run(result["run_id"])["result"]["topic_evidence"] == result["topic_evidence"]
    assert db.list_screening_runs()[0]["scope"] == scope.model_dump()


def test_sync_and_background_api_share_scope(scope_runtime, monkeypatch):
    service, _, _, _ = scope_runtime
    monkeypatch.setattr(routes, "_service", lambda *a: service)
    request = routes.ScreeningScreenRequest(strategy="scope_demo", scope={"boards": ["main_board"], "topics": [TOPICS[0]]})
    result = routes.screening_screen(request, Mock(), service.config, service.db_manager)
    assert [item["code"] for item in result["candidates"]] == ["600001"]
    assert result["scope_counts"] == {"board_count": 3, "topic_count": 2}
    queue = Mock()
    results = []
    def submit(fn, **kwargs):
        results.append(fn())
        return Mock(task_id=kwargs["task_id"], trace_id=kwargs["task_id"], status="completed", message="done")
    queue.submit_background_task.side_effect = submit
    monkeypatch.setattr(routes, "get_task_queue", lambda: queue)
    routes.screening_start_screen_task(request, Mock(), service.config, service.db_manager)
    assert results[0]["scope"] == result["scope"]
    assert [item["code"] for item in results[0]["candidates"]] == ["600001"]


def test_missing_membership_never_widens_or_uses_preview(scope_runtime, monkeypatch):
    service, db, _, _ = scope_runtime
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_eastmoney_constituents", lambda *a, **k: pd.DataFrame())
    with pytest.raises(HTTPException) as error:
        service.screen(strategy="scope_demo", market="cn", max_results=10, scope=ScreeningScope(topics=TOPICS))
    assert error.value.status_code == 424
    assert db.list_screening_runs() == []


def test_unrecognized_topic_rejected_before_constituent_fetch(scope_runtime, monkeypatch):
    service, _, _, _ = scope_runtime
    fetch = Mock()
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_eastmoney_constituents", fetch)
    with pytest.raises(HTTPException):
        service.screen(strategy="scope_demo", market="cn", max_results=10,
                       scope=ScreeningScope(topics=[{"name": "机器人新闻"}]))
    fetch.assert_not_called()


def test_empty_intersection_is_saved_without_relaxing(scope_runtime):
    service, db, _, _ = scope_runtime
    result = service.screen(strategy="scope_demo", market="cn", max_results=10,
                            scope=ScreeningScope(boards=["bse"], topics=TOPICS))
    assert result["candidates"] == []
    assert result["scope_counts"] == {"board_count": 1, "topic_count": 0}
    assert db.get_screening_run(result["run_id"])["result"]["scope"] == result["scope"]


def test_no_scope_preserves_full_universe(scope_runtime):
    service, _, _, _ = scope_runtime
    result = service.screen(strategy="scope_demo", market="cn", max_results=10)
    assert len(result["candidates"]) == 5
    assert result["topic_evidence"] == []


@pytest.mark.parametrize("scope", [
    {"boards": ["unknown"]}, {"topics": [TOPICS[0], TOPICS[0]]},
    {"topics": [{"name": " "}]}, {"topic_match": "anything"},
    {"topics": [{"name": str(i)} for i in range(6)]}, {"allow_missing": True},
])
def test_invalid_scopes_rejected(scope):
    with pytest.raises(ValidationError):
        routes.ScreeningScreenRequest(scope=scope)


def test_non_cn_scope_rejected():
    with pytest.raises(ValidationError):
        routes.ScreeningScreenRequest(market="us", scope={"boards": ["main_board"]})


def test_partial_directory_search_is_labelled(scope_runtime, monkeypatch):
    service, _, _, _ = scope_runtime
    def catalog(self, source):
        if source == "industry":
            raise TimeoutError()
        return [TOPICS[0]]
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "screening_topic_catalog", catalog)
    response = service.search_topics("机器")
    assert response["items"] == [TOPICS[0]]
    assert response["warnings"]


def test_invalid_codes_and_board_scope_cannot_pass():
    frame = pd.DataFrame({"code": ["600001", "300001", "688001", "920001", "oops"]})
    selected, counts = apply_scope(frame, ScreeningScope(boards=["main_board"]), [])
    assert selected["code"].tolist() == ["600001"]
    assert counts == {"board_count": 1, "topic_count": 1}


def test_partial_fallback_is_exact_and_has_visible_evidence(scope_runtime, monkeypatch):
    service, db, _, _ = scope_runtime
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_eastmoney_constituents", Mock(side_effect=TimeoutError()))
    fallback = Mock(return_value=pd.DataFrame({"code": ["600001", "300001"]}))
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_ths_constituents", fallback)
    result = service.screen(strategy="scope_demo", market="cn", max_results=10,
                            scope=ScreeningScope(boards=["main_board"], topics=[TOPICS[0]]))
    fallback.assert_called_once_with("机器人", exact=True)
    assert [item["code"] for item in result["candidates"]] == ["600001"]
    assert result["topic_evidence"][0]["coverage"] == "partial"
    assert result["topic_evidence"][0]["provider"] == "ths"
    assert any("覆盖不完整" in warning for warning in result["warnings"])
    assert db.get_screening_run(result["run_id"])["result"]["warnings"] == result["warnings"]


def test_strict_ths_membership_uses_table_only(monkeypatch):
    provider = DsaEastMoneyHotspotProvider()
    monkeypatch.setattr(provider, "_fetch_ths_concept_names", lambda: pd.DataFrame({"name": ["机器人概念"], "code": ["123"]}))
    response = Mock(content=b'<a>600999</a><a>Sidebar</a><table class="m-table"><tbody><tr><td>1</td><td>600001</td><td>Member</td></tr></tbody></table>')
    monkeypatch.setattr("requests.get", lambda *a, **k: response)
    assert provider._fetch_ths_constituents("机器人概念", exact=True)["code"].tolist() == ["600001"]
    assert provider._fetch_ths_constituents("机器人", exact=True).empty


@pytest.mark.parametrize("bad_frame", [
    pd.DataFrame(), pd.DataFrame({"name": ["无代码"]}), pd.DataFrame({"code": ["invalid", "AAPL"]}),
])
def test_invalid_eastmoney_payload_uses_verified_ths_members(scope_runtime, monkeypatch, bad_frame):
    service, _, _, _ = scope_runtime
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_eastmoney_constituents", Mock(return_value=bad_frame))
    fallback = Mock(return_value=pd.DataFrame({"code": ["600001", "300001"]}))
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_ths_constituents", fallback)
    result = service.screen(strategy="scope_demo", market="cn", max_results=10,
                            scope=ScreeningScope(boards=["main_board"], topics=[TOPICS[0]]))
    fallback.assert_called_once_with("机器人", exact=True)
    assert [item["code"] for item in result["candidates"]] == ["600001"]
    assert result["topic_evidence"][0]["provider"] == "ths"
    assert result["topic_evidence"][0]["coverage"] == "partial"


def test_valid_eastmoney_members_do_not_call_ths(scope_runtime, monkeypatch):
    service, _, _, _ = scope_runtime
    fallback = Mock(side_effect=AssertionError("primary succeeded"))
    monkeypatch.setattr(DsaEastMoneyHotspotProvider, "_fetch_ths_constituents", fallback)
    memberships, evidence = service._resolve_scope(ScreeningScope(topics=[TOPICS[0]]))
    assert memberships == [{"600001", "000001", "300001"}]
    assert evidence[0]["provider"] == "eastmoney"
    fallback.assert_not_called()


def test_both_invalid_sources_fail_without_saving_unscoped_result(scope_runtime, monkeypatch):
    service, db, _, _ = scope_runtime
    for method in ("_fetch_eastmoney_constituents", "_fetch_ths_constituents"):
        monkeypatch.setattr(DsaEastMoneyHotspotProvider, method, Mock(return_value=pd.DataFrame({"code": ["invalid"]})))
    with pytest.raises(HTTPException) as error:
        service.screen(strategy="scope_demo", market="cn", max_results=10,
                       scope=ScreeningScope(boards=["main_board"], topics=[TOPICS[0]]))
    assert error.value.status_code == 424
    assert db.list_screening_runs() == []


def test_conflicting_strategy_board_does_not_get_overridden(scope_runtime, monkeypatch):
    _, _, _, runtime = scope_runtime
    strategy = Strategy(name="conflict", display_name="Conflict", description="test", screening=ScreeningConfig(
        hard_filters=HardFilterConfig(board_whitelist=["创业板"]),
    ))
    monkeypatch.setattr(pipeline, "load_all_strategies_with_extra", lambda _: {"conflict": strategy})
    snapshot = Mock()
    monkeypatch.setattr(pipeline, "fetch_snapshot_with_fallback", snapshot)
    with pytest.raises(ValueError, match="没有交集"):
        pipeline.screen("conflict", scope=ScreeningScope(boards=["main_board"]), config=runtime)
    snapshot.assert_not_called()
