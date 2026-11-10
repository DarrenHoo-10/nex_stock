# -*- coding: utf-8 -*-
"""Multi-user P2a: board follows are per user; board data stays shared (offline)."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.deps import get_board_anomaly_service, get_current_user
from api.v1.endpoints import boards as boards_endpoint
from src.repositories.board_preference_repo import BoardPreferenceRepository
from tests.multi_user_helpers import PRIMARY_ID, USER_A, USER_B, build_multi_user_app, teardown_multi_user_env

BOARDS = {
    "concept:BK1": {"ref": "concept:BK1", "name": "板块一", "board_type": "concept"},
    "industry:BK2": {"ref": "industry:BK2", "name": "板块二", "board_type": "industry"},
}


class FakeBoardService:
    """Shared board data: the same catalog and rows for every user."""

    def __init__(self):
        self.repo = SimpleNamespace(find_boards=lambda ref: [BOARDS[ref]] if ref in BOARDS else [])

    def board_rows(self, refs, trade_date=None):
        return None, {ref: {
            "id": ref, "name": BOARDS[ref]["name"], "board_type": BOARDS[ref]["board_type"], "state": "none",
            "tags": [], "change_pct": None, "change_5d_pct": None, "excess_pct": None, "amount_ratio": None,
            "breadth_pct": None, "limit_up_count": None, "broken_rate_pct": None, "reason": "",
        } for ref in refs}


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    built.client.app.dependency_overrides[get_board_anomaly_service] = FakeBoardService
    yield built
    teardown_multi_user_env()


def _ids(resp):
    assert resp.status_code == 200, resp.text
    return [board["id"] for board in resp.json()["boards"]]


def test_every_follow_route_depends_on_current_user():
    follow_routes = [
        route for route in boards_endpoint.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/follows")
    ]
    assert len(follow_routes) == 3
    for route in follow_routes:
        calls = [dep.call for dep in route.dependant.dependencies]
        assert get_current_user in calls, f"{route.path} {route.methods} does not depend on get_current_user"


def test_follows_are_invisible_to_other_users(app):
    assert _ids(app.as_user(USER_A).put("/api/v1/boards/follows/concept:BK1")) == ["concept:BK1"]

    assert _ids(app.as_user(USER_A).get("/api/v1/boards/follows")) == ["concept:BK1"]
    assert _ids(app.as_user(USER_B).get("/api/v1/boards/follows")) == []
    assert _ids(app.as_user(PRIMARY_ID).get("/api/v1/boards/follows")) == []


def test_same_board_followed_independently_and_unfollow_only_affects_own(app):
    _ids(app.as_user(USER_A).put("/api/v1/boards/follows/concept:BK1"))
    _ids(app.as_user(USER_B).put("/api/v1/boards/follows/concept:BK1"))
    _ids(app.as_user(USER_B).put("/api/v1/boards/follows/industry:BK2"))
    # repeated follow stays idempotent per user
    assert _ids(app.as_user(USER_A).put("/api/v1/boards/follows/concept:BK1")) == ["concept:BK1"]

    assert _ids(app.as_user(USER_A).delete("/api/v1/boards/follows/concept:BK1")) == []

    assert _ids(app.as_user(USER_A).get("/api/v1/boards/follows")) == []
    assert _ids(app.as_user(USER_B).get("/api/v1/boards/follows")) == ["concept:BK1", "industry:BK2"]


def test_unknown_board_is_404_for_everyone(app):
    resp = app.as_user(USER_A).put("/api/v1/boards/follows/concept:NOPE")
    assert resp.status_code == 404


def test_repository_scopes_every_follow_operation_by_user(app):
    repo = BoardPreferenceRepository(app.db)
    assert repo.follow(USER_A, "concept:BK1", "板块一", "concept") is True
    assert repo.follow(USER_B, "concept:BK1", "板块一", "concept") is True
    assert repo.unfollow(USER_B, "industry:BK2") is False

    assert [row["board_ref"] for row in repo.list_follows(USER_A)] == ["concept:BK1"]
    assert repo.unfollow(USER_A, "concept:BK1") is True
    assert repo.list_follows(USER_A) == []
    assert [row["board_ref"] for row in repo.list_follows(USER_B)] == ["concept:BK1"]


def test_shared_recommendations_are_generated_without_anyones_follows(app):
    """Recommendations are shared output: no user's follows (the primary user's included) may shape them."""
    import json
    from datetime import date

    from src.services.board_recommendation_service import BoardRecommendationService

    class NoFollowReads(BoardPreferenceRepository):
        def list_follows(self, *_args, **_kwargs):  # pragma: no cover - failing here is the assertion
            raise AssertionError("generating shared recommendations must not read anyone's follows")

    candidate = {
        "id": "concept:BK7", "name": "候选板块", "board_type": "concept", "state": "strengthening", "tags": [],
        "change_pct": 1.0, "change_5d_pct": 3.0, "excess_pct": 2.0, "amount_ratio": 1.5, "breadth_pct": 60.0,
        "limit_up_count": 1, "reason": "走强",
    }
    prompts = []

    class Anomalies:
        repo = SimpleNamespace(latest_completed_run=lambda _d: {"id": 1, "trade_date": date(2026, 11, 6)})

        @staticmethod
        def list_anomalies(*_args):
            return {"boards": [candidate]}

        @staticmethod
        def board_rows(refs, _day):
            return None, {ref: dict(candidate, id=ref) for ref in refs}

    def generate(follows):
        repo = NoFollowReads(app.db)
        seeding = BoardPreferenceRepository(app.db)
        for user_id, ref, name, kind in follows:
            seeding.follow(user_id, ref, name, kind)

        def llm(prompt):
            prompts.append(prompt)
            return json.dumps([{"board_id": "concept:BK7", "reason": "持续走强"}])

        service = BoardRecommendationService(
            anomaly_service=Anomalies(), prefs=repo, config=SimpleNamespace(litellm_model="m"), llm_call=llm,
        )
        assert service.generate()["status"] == "succeeded"
        return service.view()

    without_follows = generate([])
    with_follows = generate([
        (PRIMARY_ID, "industry:BK2", "板块二", "industry"), (USER_A, "concept:BK1", "板块一", "concept"),
    ])

    assert prompts[0] == prompts[1]
    assert "板块一" not in prompts[1] and "板块二" not in prompts[1] and "用户关注" not in prompts[1]
    # the stored / served recommendation is the same whoever follows what, and carries no follow trace
    assert {**without_follows, "generated_at": None} == {**with_follows, "generated_at": None}
    assert "BK1" not in json.dumps(with_follows) and "BK2" not in json.dumps(with_follows)
