# -*- coding: utf-8 -*-
"""Assistant notes: proposals, versions, injection and API (offline)."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from api.v1.endpoints import assistant_notes as notes_endpoint
from src.agent import assistant_memory
from src.agent.assistant_memory import build_memory_injection, fit_pages, split_entries
from src.services.assistant_note_service import (
    AssistantNoteService,
    NoteValidationError,
    NoteVersionConflict,
    ProposalLimitReached,
    ProposalNotPending,
)
from src.storage import AssistantNote, DatabaseManager


@pytest.fixture()
def db(tmp_path):
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'notes.db'}")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def service(db):
    return AssistantNoteService(db, config=SimpleNamespace(
        assistant_note_daily_proposal_limit=2, assistant_note_inject_max_chars=3000,
    ))


@pytest.fixture(autouse=True)
def no_recall(monkeypatch):
    """Notes tests never touch recall: force the disabled service."""
    from src.agent.recall import RecallService
    from src.agent.recall.none_provider import NoneRecallProvider

    monkeypatch.setattr(assistant_memory, "get_recall_service", lambda config=None: RecallService(NoneRecallProvider()))


# -- proposals / versions -------------------------------------------------


def test_propose_does_not_persist_until_confirmed(service):
    proposal = service.propose_edit("global", None, "- 不碰 ST（2026-11-06）", source="user_said")

    assert proposal["base_version"] == 0
    assert proposal["before_md"] == ""
    assert "+- 不碰 ST（2026-11-06）" in proposal["diff"]
    assert service.get_page("global") is None
    assert [item["proposal_id"] for item in service.list_proposals()] == [proposal["proposal_id"]]

    note = service.confirm_proposal(proposal["proposal_id"])
    assert note["version"] == 1 and note["title"] == "偏好" and note["subject"] is None
    assert service.get_page("global")["body_md"] == "- 不碰 ST（2026-11-06）"
    revisions = service.list_revisions(note["id"])
    assert [(r["version"], r["source"]) for r in revisions] == [(1, "user_said")]
    assert service.list_proposals() == []
    with pytest.raises(ProposalNotPending):
        service.confirm_proposal(proposal["proposal_id"])


def test_confirm_conflicts_when_page_changed_after_proposal(service):
    service.manual_edit("stock", "300750", "- 拿着等年报", title="宁德时代 300750")
    proposal = service.propose_edit("stock", "300750", "- 拿着等年报\n- 只在回踩 20 日线加仓", source="suggested")
    assert proposal["base_version"] == 1 and proposal["title"] == "宁德时代 300750"

    service.manual_edit("stock", "300750", "- 改了主意", expected_version=1)

    with pytest.raises(NoteVersionConflict) as exc:
        service.confirm_proposal(proposal["proposal_id"])
    assert exc.value.current_version == 2
    assert service.get_page("stock", "300750")["body_md"] == "- 改了主意"
    # The proposal stays pending so the user can reject it explicitly.
    assert service.reject_proposal(proposal["proposal_id"])["status"] == "rejected"


def test_proposal_for_new_page_conflicts_if_page_appears(service):
    proposal = service.propose_edit("board", "BK1184", "- 板块只看龙头")
    service.manual_edit("board", "BK1184", "- 别的内容")
    with pytest.raises(NoteVersionConflict):
        service.confirm_proposal(proposal["proposal_id"])


def test_manual_edit_expected_version_and_noop(service):
    first = service.manual_edit("global", None, "- a", expected_version=0)
    with pytest.raises(NoteVersionConflict):
        service.manual_edit("global", None, "- b", expected_version=0)
    same = service.manual_edit("global", None, "- a")
    assert same["version"] == first["version"] == 1


def test_revert_writes_new_version_with_old_body(service):
    v1 = service.manual_edit("global", None, "- v1")
    service.manual_edit("global", None, "- v2")
    reverted = service.revert(v1["id"], 1, expected_version=2)

    assert reverted["version"] == 3 and reverted["body_md"] == "- v1"
    assert [r["version"] for r in service.list_revisions(v1["id"])] == [3, 2, 1]
    with pytest.raises(NoteVersionConflict):
        service.revert(v1["id"], 1, expected_version=1)


def test_daily_limit_applies_to_suggested_only(db):
    service = AssistantNoteService(db, config=SimpleNamespace(
        assistant_note_daily_proposal_limit=2, assistant_note_inject_max_chars=3000,
    ))
    service.propose_edit("global", None, "- 1", source="suggested")
    service.propose_edit("global", None, "- 2", source="suggested")
    with pytest.raises(ProposalLimitReached):
        service.propose_edit("global", None, "- 3", source="suggested")
    # A user-requested edit is not limited.
    assert service.propose_edit("global", None, "- 3", source="user_said")["source"] == "user_said"


def test_validation_and_unique_global_page(service, db):
    with pytest.raises(NoteValidationError):
        service.propose_edit("stock", None, "x")
    with pytest.raises(NoteValidationError):
        service.propose_edit("global", "300750", "x")
    with pytest.raises(NoteValidationError):
        service.propose_edit("galaxy", None, "x")
    service.manual_edit("global", None, "- one")
    session = db.get_session()
    try:
        session.add(AssistantNote(scope="global", subject=None, title="偏好", body_md="dup", version=1))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
    finally:
        session.close()


def test_delete_and_clear_all(service):
    note = service.manual_edit("stock", "AAPL", "- x")
    service.manual_edit("global", None, "- y")
    proposal = service.propose_edit("stock", "AAPL", "- z")
    assert service.delete_page("stock", "aapl") is True
    assert service.get_page("stock", "AAPL") is None
    assert service.list_proposals(status="rejected")[0]["proposal_id"] == proposal["proposal_id"]
    assert service.delete_page("stock", "AAPL") is False
    counts = service.clear_all()
    assert counts["notes"] == 1 and service.list_pages() == []
    assert note["id"]


# -- injection --------------------------------------------------------------


def test_injection_includes_global_and_active_stock_page_only(service, db):
    service.manual_edit("global", None, "- 不碰 ST（2026-11-06）")
    service.manual_edit("stock", "300750", "- 年报前不卖（2026-11-06）", title="宁德时代 300750")
    service.manual_edit("stock", "600519", "- 茅台页不应出现")
    service.manual_edit("board", "BK1184", "- 板块页")

    stock_turn = build_memory_injection(
        message="宁德怎么样", effective_context={"stock_code": "300750"}, db_manager=db,
        config=SimpleNamespace(assistant_note_inject_max_chars=3000),
    )
    assert "不碰 ST" in stock_turn.section and "年报前不卖" in stock_turn.section
    assert "茅台页" not in stock_turn.section and "板块页" not in stock_turn.section
    assert stock_turn.note_titles == ["偏好", "宁德时代 300750"]
    assert stock_turn.object_tags == ["stock:300750"]
    assert "优先级固定" in stock_turn.section and "历史数字" in stock_turn.section
    assert stock_turn.needs_tidy is False

    global_turn = build_memory_injection(
        message="大盘", effective_context={}, db_manager=db,
        config=SimpleNamespace(assistant_note_inject_max_chars=3000),
    )
    assert global_turn.note_titles == ["偏好"]

    board_turn = build_memory_injection(
        message="板块", effective_context={"board_id": "BK1184"}, db_manager=db,
        config=SimpleNamespace(assistant_note_inject_max_chars=3000),
    )
    assert board_turn.note_titles == ["偏好", "BK1184"] and "板块页" in board_turn.section
    assert board_turn.object_tags == ["board:BK1184"]


def test_injection_cap_keeps_whole_entries_and_flags_tidy(service, db):
    entries = [f"- 条目{i}：" + "字" * 40 for i in range(10)]
    service.manual_edit("global", None, "\n".join(entries))

    injection = build_memory_injection(
        message="q", effective_context={}, db_manager=db,
        config=SimpleNamespace(assistant_note_inject_max_chars=200),
    )

    assert injection.needs_tidy is True
    assert "整理" in injection.section
    kept = [line for line in injection.section.splitlines() if line.startswith("- 条目")]
    assert 0 < len(kept) < 10
    assert kept == entries[: len(kept)]  # whole leading entries, nothing cut mid-entry


def test_fit_pages_and_split_entries_helpers():
    assert split_entries("- a\n  cont\n- b\n\nplain") == ["- a\n  cont", "- b", "plain"]
    pages = [{"title": "偏好", "body_md": "- a"}, {"title": "X", "body_md": "- " + "b" * 50}]
    rendered, tidy = fit_pages(pages, 30)
    assert [title for title, _ in rendered] == ["偏好"] and tidy is True
    rendered, tidy = fit_pages(pages, 500)
    assert len(rendered) == 2 and tidy is False


def test_memory_section_without_notes_only_states_recall_not_used(db):
    injection = build_memory_injection(
        message="q", effective_context={}, db_manager=db,
        config=SimpleNamespace(assistant_note_inject_max_chars=3000),
    )
    assert injection.recall_status == "disabled"
    assert "未使用长期回忆" in injection.section
    assert "用户笔记" not in injection.section


def test_prepare_agent_chat_injects_notes_into_system_prompt(service, db, monkeypatch):
    from src.agent import executor

    service.manual_edit("global", None, "- 偏好稳健")
    monkeypatch.setattr(executor, "build_visible_chat_history", lambda *a, **k: [])
    prepared = executor.prepare_agent_chat(
        message="你好", session_id="s1", context={"stock_code": "300750"}, config=SimpleNamespace(),
        context_llm_adapter=None, skill_instructions="", default_skill_policy="",
        use_legacy_default_prompt=False, use_codex_prompt=False, include_provider_trace=False,
        db_manager=db,
    )
    assert "偏好稳健" in prepared.system_prompt
    assert "未使用长期回忆" in prepared.system_prompt
    assert prepared.memory is not None and prepared.memory.object_tags == ["stock:300750"]
    assert prepared.memory.context_snapshot["context_object"]["stock_code"] == "300750"


def test_injection_failure_never_breaks_chat(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(assistant_memory, "_collect_pages", boom)
    injection = build_memory_injection(
        message="q", effective_context={}, config=SimpleNamespace(assistant_note_inject_max_chars=3000),
    )
    assert injection.section  # still renders the recall status line
    assert injection.note_titles == []


# -- API ----------------------------------------------------------------------


@pytest.fixture()
def client(service):
    app = FastAPI()
    app.include_router(notes_endpoint.router, prefix="/api/v1/agent/notes")
    app.dependency_overrides[notes_endpoint.get_assistant_note_service] = lambda: service
    return TestClient(app)


def test_api_propose_confirm_conflict_revert_and_clear(client):
    base = "/api/v1/agent/notes"
    assert client.get(f"{base}/page", params={"scope": "global"}).json() == {"note": None}

    proposal = client.post(f"{base}/proposals", json={
        "scope": "stock", "subject": "300750", "title": "宁德时代 300750",
        "body_md": "- 拿着等年报", "source": "suggested", "origin_session_id": "s1", "origin_message_id": 3,
    })
    assert proposal.status_code == 200
    body = proposal.json()
    assert body["status"] == "pending" and body["base_version"] == 0 and "拿着等年报" in body["diff"]

    note = client.post(f"{base}/proposals/{body['proposal_id']}/confirm")
    assert note.status_code == 200 and note.json()["version"] == 1
    note_id = note.json()["id"]

    stale = client.post(f"{base}/proposals", json={"scope": "stock", "subject": "300750", "body_md": "- 新"}).json()
    assert client.put(f"{base}/page", json={
        "scope": "stock", "subject": "300750", "body_md": "- 手改", "expected_version": 1,
    }).json()["version"] == 2
    conflict = client.post(f"{base}/proposals/{stale['proposal_id']}/confirm")
    assert conflict.status_code == 409 and conflict.json()["detail"]["error"] == "note_version_conflict"
    assert client.put(f"{base}/page", json={
        "scope": "stock", "subject": "300750", "body_md": "- x", "expected_version": 1,
    }).status_code == 409

    revisions = client.get(f"{base}/{note_id}/revisions").json()["revisions"]
    assert [r["version"] for r in revisions] == [2, 1]
    assert client.post(f"{base}/{note_id}/revert", json={"version": 1}).json()["body_md"] == "- 拿着等年报"

    listing = client.get(base).json()
    assert len(listing["notes"]) == 1 and listing["usage"]["needs_tidy"] is False

    assert client.delete(base).status_code == 400
    assert client.delete(base, params={"confirm": "true"}).json()["notes"] == 1


def test_api_daily_limit_returns_429_and_bad_scope_422(client):
    base = "/api/v1/agent/notes/proposals"
    for index in range(2):
        assert client.post(base, json={"scope": "global", "body_md": f"- {index}", "source": "suggested"}).status_code == 200
    limited = client.post(base, json={"scope": "global", "body_md": "- 9", "source": "suggested"})
    assert limited.status_code == 429 and limited.json()["detail"]["error"] == "proposal_limit_reached"
    assert client.post(base, json={"scope": "stock", "body_md": "x"}).status_code == 422
    assert client.get("/api/v1/agent/notes/page", params={"scope": "nope"}).status_code == 422
