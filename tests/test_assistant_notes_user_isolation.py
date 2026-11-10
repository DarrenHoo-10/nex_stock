# -*- coding: utf-8 -*-
"""Multi-user P2a: assistant notes, revisions and proposals are per user (offline)."""

from __future__ import annotations

import pytest

from src.agent import assistant_memory
from src.agent.assistant_memory import build_memory_injection
from src.services.assistant_note_service import AssistantNoteService
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    teardown_multi_user_env,
)

BASE = "/api/v1/agent/notes"


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    yield built
    teardown_multi_user_env()


@pytest.fixture(autouse=True)
def no_recall(monkeypatch):
    from src.agent.recall import RecallService
    from src.agent.recall.none_provider import NoneRecallProvider

    monkeypatch.setattr(
        assistant_memory, "get_recall_service", lambda config=None: RecallService(NoneRecallProvider()),
    )


def _write_page(app, user, body="note", scope="global", subject=None, **extra):
    payload = {"scope": scope, "subject": subject, "body_md": body, **extra}
    response = app.as_user(user).put(f"{BASE}/page", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _propose(app, user, body="proposal body", scope="global", subject=None, source="user_said"):
    response = app.as_user(user).post(
        f"{BASE}/proposals", json={"scope": scope, "subject": subject, "body_md": body, "source": source},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_same_scope_and_subject_page_is_independent_per_user(app):
    a = _write_page(app, USER_A, "A global")
    b = _write_page(app, USER_B, "B global")
    p = _write_page(app, PRIMARY_ID, "primary global")
    assert len({a["id"], b["id"], p["id"]}) == 3

    for user, expected in ((USER_A, "A global"), (USER_B, "B global"), (PRIMARY_ID, "primary global")):
        page = app.as_user(user).get(f"{BASE}/page", params={"scope": "global"}).json()["note"]
        assert page["body_md"] == expected
        listed = app.as_user(user).get(BASE).json()
        assert [n["body_md"] for n in listed["notes"]] == [expected]
        assert listed["usage"]["total_chars"] == len(expected)

    _write_page(app, USER_A, "A stock", scope="stock", subject="600519")
    assert app.as_user(USER_B).get(f"{BASE}/page", params={"scope": "stock", "subject": "600519"}).json()["note"] is None


def test_editing_with_expected_version_of_another_users_page_does_not_conflict_or_leak(app):
    _write_page(app, USER_B, "B v1")
    _write_page(app, USER_B, "B v2", expected_version=1)

    created = _write_page(app, USER_A, "A v1", expected_version=0)

    assert created["version"] == 1 and created["body_md"] == "A v1"
    assert app.as_user(USER_B).get(f"{BASE}/page", params={"scope": "global"}).json()["note"]["body_md"] == "B v2"


def test_delete_page_only_deletes_own_and_rejects_only_own_pending_proposals(app):
    _write_page(app, USER_A, "A")
    _write_page(app, USER_B, "B")
    proposal_b = _propose(app, USER_B, "B changed")

    assert app.as_user(USER_A).delete(f"{BASE}/page", params={"scope": "global"}).json() == {"deleted": True}

    assert app.as_user(USER_B).get(f"{BASE}/page", params={"scope": "global"}).json()["note"]["body_md"] == "B"
    pending = app.as_user(USER_B).get(f"{BASE}/proposals").json()["proposals"]
    assert [p["proposal_id"] for p in pending] == [proposal_b["proposal_id"]]


def test_foreign_note_revisions_and_revert_are_404(app):
    note = _write_page(app, USER_B, "B v1")
    _write_page(app, USER_B, "B v2")

    assert app.as_user(USER_A).get(f"{BASE}/{note['id']}/revisions").status_code == 404
    foreign_revert = app.as_user(USER_A).post(f"{BASE}/{note['id']}/revert", json={"version": 1})
    assert foreign_revert.status_code == 404
    assert foreign_revert.json()["error"] == "note_not_found"
    # same body as a note id that does not exist at all
    missing = app.as_user(USER_A).post(f"{BASE}/99999/revert", json={"version": 1})
    assert missing.status_code == 404 and missing.json() == foreign_revert.json()

    own = app.as_user(USER_B).get(f"{BASE}/{note['id']}/revisions")
    assert own.status_code == 200 and [r["version"] for r in own.json()["revisions"]] == [2, 1]
    assert app.as_user(USER_B).get(f"{BASE}/page", params={"scope": "global"}).json()["note"]["body_md"] == "B v2"


def test_proposals_are_private_and_foreign_confirm_reject_are_404(app):
    proposal = _propose(app, USER_B, "B proposal")

    assert app.as_user(USER_A).get(f"{BASE}/proposals").json()["proposals"] == []
    confirm = app.as_user(USER_A).post(f"{BASE}/proposals/{proposal['proposal_id']}/confirm")
    reject = app.as_user(USER_A).post(f"{BASE}/proposals/{proposal['proposal_id']}/reject")
    assert confirm.status_code == 404 and confirm.json()["error"] == "proposal_not_found"
    assert reject.status_code == 404 and reject.json()["error"] == "proposal_not_found"
    missing = app.as_user(USER_A).post(f"{BASE}/proposals/not-a-proposal/confirm")
    assert missing.status_code == 404 and missing.json() == confirm.json()

    # untouched for its owner, who can still confirm it
    still = app.as_user(USER_B).get(f"{BASE}/proposals").json()["proposals"]
    assert [p["status"] for p in still] == ["pending"]
    confirmed = app.as_user(USER_B).post(f"{BASE}/proposals/{proposal['proposal_id']}/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["body_md"] == "B proposal"
    assert app.as_user(USER_A).get(f"{BASE}/page", params={"scope": "global"}).json()["note"] is None


def test_confirming_a_proposal_writes_the_page_for_its_owner_only(app):
    _write_page(app, PRIMARY_ID, "primary page")
    proposal = _propose(app, USER_A, "A page")

    app.as_user(USER_A).post(f"{BASE}/proposals/{proposal['proposal_id']}/confirm")

    assert app.as_user(USER_A).get(f"{BASE}/page", params={"scope": "global"}).json()["note"]["body_md"] == "A page"
    assert app.as_user(PRIMARY_ID).get(f"{BASE}/page", params={"scope": "global"}).json()["note"]["body_md"] == "primary page"


def test_daily_suggested_proposal_limit_is_counted_per_user(app):
    limit = AssistantNoteService(app.db).daily_proposal_limit
    for i in range(limit):
        _propose(app, USER_A, f"A suggestion {i}", scope="stock", subject=f"60000{i}", source="suggested")

    exhausted = app.as_user(USER_A).post(
        f"{BASE}/proposals",
        json={"scope": "stock", "subject": "600099", "body_md": "one more", "source": "suggested"},
    )
    assert exhausted.status_code == 429

    # another user (and the primary user) still have their full allowance
    assert _propose(app, USER_B, "B suggestion", source="suggested")["status"] == "pending"
    assert _propose(app, PRIMARY_ID, "P suggestion", source="suggested")["status"] == "pending"


def test_clear_all_only_clears_the_callers_data(app):
    note_a = _write_page(app, USER_A, "A")
    _write_page(app, USER_B, "B")
    _write_page(app, PRIMARY_ID, "P")
    _propose(app, USER_A, "A proposal 2")
    proposal_b = _propose(app, USER_B, "B proposal")

    cleared = app.as_user(USER_A).delete(BASE, params={"confirm": "true"}).json()
    assert cleared == {"notes": 1, "revisions": 1, "proposals": 1}

    assert app.as_user(USER_A).get(BASE).json()["notes"] == []
    assert app.as_user(USER_A).get(f"{BASE}/{note_a['id']}/revisions").status_code == 404
    assert [n["body_md"] for n in app.as_user(USER_B).get(BASE).json()["notes"]] == ["B"]
    assert [n["body_md"] for n in app.as_user(PRIMARY_ID).get(BASE).json()["notes"]] == ["P"]
    kept = app.as_user(USER_B).get(f"{BASE}/proposals").json()["proposals"]
    assert [p["proposal_id"] for p in kept] == [proposal_b["proposal_id"]]
    b_note = app.as_user(USER_B).get(BASE).json()["notes"][0]
    assert len(app.as_user(USER_B).get(f"{BASE}/{b_note['id']}/revisions").json()["revisions"]) == 1


def test_memory_injection_reads_the_session_owners_notes(app):
    AssistantNoteService(app.db, user_id=PRIMARY_ID).manual_edit("global", None, "PRIMARY PREFERENCE")
    AssistantNoteService(app.db, user_id=USER_A).manual_edit("global", None, "A PREFERENCE")
    AssistantNoteService(app.db, user_id=USER_B).manual_edit("stock", "600519", "B STOCK NOTE")

    def injected(user_id):
        return build_memory_injection(
            message="hi", effective_context={"stock_code": "600519"}, db_manager=app.db, user_id=user_id,
        ).section

    section_a = injected(USER_A)
    assert "A PREFERENCE" in section_a
    assert "PRIMARY PREFERENCE" not in section_a and "B STOCK NOTE" not in section_a
    section_b = injected(USER_B)
    assert "B STOCK NOTE" in section_b and "A PREFERENCE" not in section_b and "PRIMARY PREFERENCE" not in section_b
    # no user given = the primary user (bots, CLI)
    default = build_memory_injection(message="hi", effective_context={}, db_manager=app.db).section
    assert "PRIMARY PREFERENCE" in default and "A PREFERENCE" not in default


def test_primary_user_notes_flow_is_unchanged(app):
    page = _write_page(app, PRIMARY_ID, "- 不碰 ST")
    proposal = _propose(app, PRIMARY_ID, "- 不碰 ST\n- 只做主板")
    confirmed = app.as_user(PRIMARY_ID).post(f"{BASE}/proposals/{proposal['proposal_id']}/confirm").json()

    assert confirmed["id"] == page["id"] and confirmed["version"] == 2
    revisions = app.as_user(PRIMARY_ID).get(f"{BASE}/{page['id']}/revisions").json()["revisions"]
    assert [r["version"] for r in revisions] == [2, 1]
