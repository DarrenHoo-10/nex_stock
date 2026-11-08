# -*- coding: utf-8 -*-
"""Followed boards and model-recommended boards (pipeline step, rule fallback, API)."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from api.v1.endpoints import boards as boards_endpoint
from src.repositories.board_preference_repo import BoardPreferenceRepository
from src.services.board_anomaly_service import BoardAnomalyService
from src.services.board_recommendation_service import (
    BoardRecommendationService,
    build_prompt,
    parse_reply,
    rule_ranking,
)
from src.storage import DatabaseManager
from tests.test_board_pipeline import FakeIngest, run, seeded_world


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def _row(ref: str, state: str, excess: float, tags: List[str] | None = None) -> Dict[str, Any]:
    return {"id": ref, "name": ref.split(":")[1], "board_type": ref.split(":")[0], "state": state, "tags": tags or [],
            "change_pct": 1.0, "change_5d_pct": 3.0, "excess_pct": excess, "amount_ratio": 1.8, "breadth_pct": 70.0,
            "limit_up_count": 2, "reason": f"{ref} 理由"}


def test_parse_reply_keeps_only_offered_ids_in_order():
    allowed = {"concept:A": _row("concept:A", "strengthening", 2), "industry:B": _row("industry:B", "first_launch", 1)}
    text = '```json\n[{"board_id": "industry:B", "reason": "量价齐升"}, {"board_id": "concept:X", "reason": "编的"},' \
           ' {"board_id": "industry:B", "reason": "重复"}, {"board_id": "concept:A", "reason": "持续走强"}]\n```'
    picked = parse_reply(text, allowed)
    assert [p["board_ref"] for p in picked] == ["industry:B", "concept:A"]
    assert picked[0]["reason"] == "量价齐升" and picked[0]["board_type"] == "industry"
    assert parse_reply("没有 JSON", allowed) == [] and parse_reply("[not json]", allowed) == []


def test_rule_ranking_prefers_strong_states_and_skips_single_stock_boards():
    rows = [
        _row("concept:Ebb", "ebbing", 9),
        _row("concept:Single", "first_launch", 8, ["single_stock"]),
        _row("concept:Strong", "strengthening", 3),
        _row("industry:Launch", "first_launch", 1),
        _row("concept:Div", "divergence", 5),
    ]
    assert [r["board_ref"] for r in rule_ranking(rows)] == ["industry:Launch", "concept:Strong", "concept:Div"]


def test_prompt_lists_candidates_and_followed_boards():
    prompt = build_prompt(date(2026, 11, 6), [_row("concept:A", "strengthening", 2)], [_row("industry:B", "none", 0)])
    assert "id=concept:A" in prompt and "走强" in prompt and "用户关注的板块" in prompt and "B（状态 无异动" in prompt


def _service(world, db, reply=None, error=None, model="anthropic/claude-opus-5-5"):
    calls: List[str] = []

    def llm(prompt: str) -> str:
        calls.append(prompt)
        if error is not None:
            raise error
        return reply or "[]"

    service = BoardRecommendationService(
        anomaly_service=BoardAnomalyService(repo=world.repo(), calendar_fn=None),
        prefs=BoardPreferenceRepository(db),
        config=SimpleNamespace(litellm_model=model),
        llm_call=llm,
    )
    return service, calls


def _ref(world, key: str) -> str:
    board_id = world.board_ids[key]
    return next(b["ref"] for b in world.repo().list_catalog() if b["id"] == board_id)


def test_pipeline_step_saves_model_picks_and_the_view_returns_today_rows(db):
    world = seeded_world(db)
    ref = _ref(world, "I0")
    reply = json.dumps([{"board_id": ref, "reason": "首次启动，量能放大"}, {"board_id": "concept:BK9999", "reason": "x"}])
    service, calls = _service(world, db, reply=reply)
    BoardPreferenceRepository(db).follow(_ref(world, "I1"), "I1", "industry")

    result = run(world, ingest_service=FakeIngest(), recommender=service)

    step = result["steps"][-1]
    assert step["key"] == "recommend" and step["status"] == "succeeded" and "1 个（模型）" in step["detail"]
    assert len(calls) == 1 and ref in calls[0] and "用户关注的板块" in calls[0]
    view = service.view()
    assert view["status"] == "ready" and view["source"] == "model" and view["model"] == "anthropic/claude-opus-5-5"
    [item] = view["items"]
    assert item["rank"] == 1 and item["reason"] == "首次启动，量能放大"
    assert item["board"]["id"] == ref and item["board"]["state"] == "first_launch"


def test_model_failure_falls_back_to_rules_and_says_why(db):
    world = seeded_world(db)
    service, _ = _service(world, db, error=RuntimeError("gateway down"))
    run(world, ingest_service=FakeIngest(), recommender=service)
    view = service.view()
    assert view["source"] == "rule" and "模型调用失败" in view["note"] and view["model"] is None
    assert view["items"][0]["board"]["id"] == _ref(world, "I0")


def test_no_model_configured_uses_rules_without_calling(db):
    world = seeded_world(db)
    service, calls = _service(world, db, model="")
    service._llm_call = None  # no injected model and no LITELLM_MODEL
    run(world, ingest_service=FakeIngest(), recommender=service)
    assert service.view()["note"] == "没有配置分析模型，按规则排序" and calls == []


def test_pipeline_without_live_ingestion_does_not_recommend(db):
    world = seeded_world(db)
    result = run(world, ingest_service=FakeIngest())
    assert "recommend" not in [s["key"] for s in result["steps"]]


def test_follow_endpoints_round_trip_with_today_rows(db, monkeypatch):
    world = seeded_world(db)
    run(world, ingest_service=FakeIngest())
    service = BoardAnomalyService(repo=world.repo(), calendar_fn=None)
    monkeypatch.setattr(boards_endpoint, "_preference_repo", lambda: BoardPreferenceRepository(db))
    ref = _ref(world, "I0")

    followed = boards_endpoint.follow_board(ref, service=service)
    again = boards_endpoint.follow_board(ref, service=service)
    assert [b.id for b in followed.boards] == [ref] and [b.id for b in again.boards] == [ref]
    assert followed.boards[0].today is not None and followed.boards[0].today.state == "first_launch"
    assert followed.trade_date == world.days[-1].isoformat()

    with pytest.raises(Exception) as missing:
        boards_endpoint.follow_board("concept:BK0000", service=service)
    assert getattr(missing.value, "status_code", None) == 404

    assert boards_endpoint.unfollow_board(ref, service=service).boards == []
    assert boards_endpoint.list_follows(service=service).boards == []
