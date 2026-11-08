# -*- coding: utf-8 -*-
"""Long-term recall plugin, context_snapshot migration and forget-on-delete (offline).

The Hindsight provider is exercised through a fake ``requests`` session, so
request shapes, masking, tags, timeouts and queue behaviour are real while no
network call is ever made.
"""

import sqlite3
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests

import src.agent.recall as recall_pkg
from src.agent import assistant_memory
from src.agent.assistant_memory import build_memory_injection, retain_chat_turn
from src.agent.recall import (
    RecallQuery,
    RecallService,
    RecallTurn,
    mask_sensitive_numbers,
)
from src.agent.recall.hindsight_provider import HindsightRecallProvider
from src.agent.recall.none_provider import NoneRecallProvider
from src.agent.recall.registry import create_recall_provider
from src.storage import DatabaseManager


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeSession:
    """Stands in for ``requests.Session``; records every call."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda method, url, kwargs: FakeResponse())

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


BASE = "http://hindsight.test:8888"
BANK_URL = f"{BASE}/v1/default/banks/bank-1"


def make_provider(session, timeout_s=2.0, api_key="k-secret"):
    return HindsightRecallProvider(
        base_url=BASE + "/", bank_id="bank-1", api_key=api_key, timeout_s=timeout_s, session=session,
    )


def make_service(session, **kwargs):
    kwargs.setdefault("retain_retry_delays", (0.0, 0.0))
    kwargs.setdefault("forget_retry_base_s", 0.01)
    return RecallService(make_provider(session), timeout_s=kwargs.pop("timeout_s", 2.0), **kwargs)


def sample_turn(**overrides):
    values = dict(
        session_id="s1", message_id=7, user_text="我持有 1000 股宁德时代，成本 200 元，怎么看？",
        assistant_text="仓位占用 5万元，先看 300750 的年报。",
        occurred_at=datetime(2026, 11, 7, 10, 30), tags=["stock:300750"], market_phase="intraday",
    )
    values.update(overrides)
    return RecallTurn(**values)


@pytest.fixture(autouse=True)
def fresh_recall_service():
    recall_pkg.reset_recall_service()
    yield
    recall_pkg.reset_recall_service()


# -- masking ------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("持有 1000 股，成本 12.5 元", "持有 [数量]股，成本 [价格]"),
    ("买入均价是 ¥1,234.5，共 3万股", "买入均价 [价格]，共 [数量]股"),
    ("账户还有 20万元 和 $500 现金", "账户还有 [金额] 和 [金额] 现金"),
    ("300750 上涨 5.2%，2026-11-07 收盘", "300750 上涨 5.2%，2026-11-07 收盘"),
])
def test_masking(raw, expected):
    assert mask_sensitive_numbers(raw) == expected


# -- registry / none provider ---------------------------------------------------


def test_default_provider_is_none_and_hindsight_is_lazy():
    sys.modules.pop("src.agent.recall.hindsight_provider", None)
    assert isinstance(create_recall_provider(SimpleNamespace()), NoneRecallProvider)
    assert isinstance(create_recall_provider(SimpleNamespace(assistant_recall_provider="none")), NoneRecallProvider)
    assert isinstance(create_recall_provider(SimpleNamespace(assistant_recall_provider="bogus")), NoneRecallProvider)
    assert "src.agent.recall.hindsight_provider" not in sys.modules

    provider = create_recall_provider(SimpleNamespace(
        assistant_recall_provider="hindsight", hindsight_base_url=BASE, hindsight_bank_id="b",
        hindsight_api_key="k", assistant_recall_timeout_s=2.0,
    ))
    assert provider.provider_id == "hindsight" and provider.configured


def test_none_provider_path_is_inert():
    service = RecallService(NoneRecallProvider())
    outcome = service.recall(RecallQuery(text="q"))
    assert outcome.status == "disabled" and outcome.items == []
    assert service.retain_async(sample_turn()) is False
    assert service.forget_async(["s1:7"]) is False
    assert service.health().status == "disabled"
    assert service.pending_counts() == (0, 0)


# -- hindsight request shapes -----------------------------------------------------


def test_hindsight_recall_request_shape_and_parsing():
    def handler(method, url, kwargs):
        return FakeResponse(200, {"results": [
            {"id": "1", "text": "旧的看法", "tags": ["date:2026-10-01"], "document_id": "s0:1"},
            {"id": "2", "text": "较新的看法", "mentioned_at": "2026-11-05T09:00:00Z", "document_id": "s0:2",
             "scores": {"final": 0.9}},
            {"id": "3", "text": "  "},
        ]})

    session = FakeSession(handler)
    service = make_service(session)
    outcome = service.recall(RecallQuery(
        text="我持有 1000 股，成本 200 元", tags=["stock:300750"], limit=5, now=datetime(2026, 11, 7, 9, 0),
    ))

    call = session.calls[0]
    assert call["method"] == "POST" and call["url"] == f"{BANK_URL}/memories/recall"
    assert call["headers"]["Authorization"] == "Bearer k-secret"
    assert call["timeout"] == 2.0
    body = call["json"]
    assert body["query"] == "我持有 [数量]股，成本 [价格]"  # masked before leaving
    assert body["tags"] == ["stock:300750"] and body["tags_match"] == "any"
    assert body["budget"] == "low" and body["query_timestamp"] == "2026-11-07T09:00:00"
    assert outcome.status == "used"
    assert [(item.date, item.text) for item in outcome.items] == [
        ("2026-11-05", "较新的看法"), ("2026-10-01", "旧的看法"),
    ]


def test_hindsight_recall_omits_tags_without_context_object_and_unmasked_when_off():
    session = FakeSession(lambda m, u, k: FakeResponse(200, {"results": []}))
    service = make_service(session, mask_amounts=False)
    outcome = service.recall(RecallQuery(text="持有 1000 股"))
    body = session.calls[0]["json"]
    assert "tags" not in body and body["query"] == "持有 1000 股"
    assert outcome.status == "used" and outcome.items == []


def test_hindsight_retain_request_shape_with_masking_tags_and_document_id():
    session = FakeSession()
    service = make_service(session)
    assert service.retain_async(sample_turn()) is True
    assert service.wait_idle(5)

    call = session.calls[0]
    assert call["method"] == "POST" and call["url"] == f"{BANK_URL}/memories"
    assert call["headers"]["Authorization"] == "Bearer k-secret"
    assert call["json"]["async"] is True
    item = call["json"]["items"][0]
    assert item["document_id"] == "s1:7"
    assert item["tags"] == ["stock:300750", "date:2026-11-07", "phase:intraday"]
    assert item["timestamp"] == "2026-11-07T10:30:00"
    assert item["metadata"] == {
        "session_id": "s1", "message_id": "7", "date": "2026-11-07", "market_phase": "intraday",
    }
    assert "[数量]股" in item["content"] and "[价格]" in item["content"] and "[金额]" in item["content"]
    for secret in ("1000", "200 元", "5万元"):
        assert secret not in item["content"]
    assert "300750" in item["content"]  # codes are not masked


def test_hindsight_retain_keeps_numbers_when_masking_disabled():
    session = FakeSession()
    service = make_service(session, mask_amounts=False)
    service.retain_async(sample_turn())
    assert service.wait_idle(5)
    assert "1000 股" in session.calls[0]["json"]["items"][0]["content"]


def test_hindsight_forget_and_health_requests():
    def handler(method, url, kwargs):
        if url.endswith("s1:8") or url.endswith("s1%3A8"):
            return FakeResponse(404)
        return FakeResponse(200, {"status": "healthy"})

    session = FakeSession(handler)
    provider = make_provider(session)
    provider.forget(["s1:7", "s1:8"])  # 404 counts as already deleted

    assert [(c["method"], c["url"]) for c in session.calls] == [
        ("DELETE", f"{BANK_URL}/documents/s1%3A7"),
        ("DELETE", f"{BANK_URL}/documents/s1%3A8"),
    ]
    health = provider.health()
    assert session.calls[-1]["method"] == "GET" and session.calls[-1]["url"] == f"{BASE}/health"
    assert health.status == "ok"


def test_hindsight_not_configured_and_unreachable_health():
    assert HindsightRecallProvider(base_url="", bank_id="").health().status == "not_configured"

    def boom(method, url, kwargs):
        raise requests.ConnectionError("down")

    health = make_provider(FakeSession(boom)).health()
    assert health.status == "error" and "ConnectionError" in health.message
    assert "k-secret" not in str(health)


# -- timeouts / failures never break chat ----------------------------------------


def test_recall_timeout_is_skipped_and_marks_not_used():
    def slow(method, url, kwargs):
        time.sleep(1.0)
        return FakeResponse(200, {"results": [{"text": "late"}]})

    service = make_service(FakeSession(slow), timeout_s=0.2)
    started = time.monotonic()
    outcome = service.recall(RecallQuery(text="q"))
    assert time.monotonic() - started < 0.8
    assert outcome.status == "unavailable" and outcome.reason == "timeout"


def test_injection_with_failing_recall_notes_not_used_and_answers_normally(monkeypatch):
    def down(method, url, kwargs):
        return FakeResponse(503)

    service = make_service(FakeSession(down))
    monkeypatch.setattr(assistant_memory, "get_recall_service", lambda config=None: service)
    injection = build_memory_injection(
        message="宁德怎么样", effective_context={"stock_code": "300750"},
        config=SimpleNamespace(assistant_note_inject_max_chars=3000),
    )
    assert injection.recall_status == "unavailable" and injection.recall_reason == "error"
    assert "未使用长期回忆" in injection.section


def test_injection_with_recall_items_renders_dated_past_conversations(monkeypatch):
    session = FakeSession(lambda m, u, k: FakeResponse(200, {"results": [
        {"text": "上次说年报前不卖", "tags": ["date:2026-11-04"]},
    ]}))
    service = make_service(session)
    monkeypatch.setattr(assistant_memory, "get_recall_service", lambda config=None: service)
    injection = build_memory_injection(
        message="宁德怎么样", effective_context={"stock_code": "300750"},
        config=SimpleNamespace(assistant_note_inject_max_chars=3000),
    )
    assert injection.recall_status == "used"
    assert "- [2026-11-04] 上次说年报前不卖" in injection.section
    assert "过往对话" in injection.section and "历史数字" in injection.section
    assert "未使用长期回忆" not in injection.section
    assert session.calls[0]["json"]["tags"] == ["stock:300750"]


def test_retain_failure_is_retried_then_succeeds():
    attempts = []

    def flaky(method, url, kwargs):
        attempts.append(1)
        return FakeResponse(500) if len(attempts) < 3 else FakeResponse(200)

    service = make_service(FakeSession(flaky))
    service.retain_async(sample_turn())
    assert service.wait_idle(5)
    assert len(attempts) == 3 and service.dropped_retains == 0
    assert service.pending_counts() == (0, 0)


def test_retain_gives_up_after_max_attempts_without_raising():
    session = FakeSession(lambda m, u, k: FakeResponse(500))
    service = make_service(session)
    assert service.retain_async(sample_turn()) is True
    assert service.wait_idle(5)
    assert len(session.calls) == 3 and service.dropped_retains == 1


def test_retain_chat_turn_queues_document_with_object_tags_and_never_raises(monkeypatch):
    session = FakeSession()
    service = make_service(session)
    monkeypatch.setattr(assistant_memory, "get_recall_service", lambda config=None: service)
    memory = assistant_memory.MemoryInjection(object_tags=["stock:300750"], market_phase="postmarket")

    retain_chat_turn(session_id="s9", user_message_id=42, user_text="q", assistant_text="a", memory=memory)
    assert service.wait_idle(5)
    item = session.calls[0]["json"]["items"][0]
    assert item["document_id"] == "s9:42"
    assert "stock:300750" in item["tags"] and "phase:postmarket" in item["tags"]

    retain_chat_turn(session_id="s9", user_message_id=None, user_text="q", assistant_text="a", memory=None)
    monkeypatch.setattr(assistant_memory, "get_recall_service", lambda config=None: 1 / 0)
    retain_chat_turn(session_id="s9", user_message_id=1, user_text="q", assistant_text="a", memory=None)


def test_forget_cancels_pending_retain_for_same_document():
    gate = {"open": False}
    session = FakeSession()
    real_request = session.request

    def blocking(method, url, **kwargs):
        while not gate["open"]:
            time.sleep(0.01)
        return real_request(method, url, **kwargs)

    session.request = blocking
    service = make_service(session)
    service.retain_async(sample_turn(message_id=1))  # picked up, blocks in flight
    time.sleep(0.1)
    service.retain_async(sample_turn(message_id=2))  # still queued
    service.forget_async(["s1:2"])
    gate["open"] = True
    assert service.wait_idle(5)
    methods = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in session.calls]
    assert ("POST", "memories") in methods and ("DELETE", "s1%3A2") in methods
    assert [c for c in session.calls if c["method"] == "POST"][0]["json"]["items"][0]["document_id"] == "s1:1"
    assert len([c for c in session.calls if c["method"] == "POST"]) == 1


# -- forget when a session is deleted -----------------------------------------------


@pytest.fixture()
def db(tmp_path):
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'recall.db'}")
    yield manager
    DatabaseManager.reset_instance()


def _seed_session(db, session_id="sess-del"):
    ids = []
    for index in range(2):
        ids.append(db.save_conversation_message(session_id, "user", f"q{index}"))
        db.save_conversation_message(session_id, "assistant", f"a{index}")
    return ids


def test_session_delete_forgets_retained_documents(db, monkeypatch):
    session = FakeSession()
    service = make_service(session)
    monkeypatch.setattr(recall_pkg, "get_recall_service", lambda config=None: service)
    user_ids = _seed_session(db)

    assert db.delete_conversation_session("sess-del") == 4
    assert service.wait_idle(5)

    deleted = sorted(c["url"].rsplit("/", 1)[-1] for c in session.calls if c["method"] == "DELETE")
    assert deleted == sorted(f"sess-del%3A{uid}" for uid in user_ids)
    assert db.get_conversation_messages("sess-del") == []


def test_forget_failure_stays_pending_and_is_retried_after_recovery(db, monkeypatch):
    state = {"down": True}
    session = FakeSession(lambda m, u, k: FakeResponse(503 if state["down"] else 200))
    service = make_service(session)
    monkeypatch.setattr(recall_pkg, "get_recall_service", lambda config=None: service)
    user_ids = _seed_session(db)

    db.delete_conversation_session("sess-del")  # must not raise although the service is down
    deadline = time.monotonic() + 3
    while len(session.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.pending_counts()[1] == len(user_ids)
    assert service.health(use_cache=False).pending_forget == len(user_ids)

    state["down"] = False
    assert service.wait_idle(5)
    assert service.pending_counts() == (0, 0)


def test_session_delete_without_provider_is_a_noop(db, monkeypatch):
    monkeypatch.setattr(recall_pkg, "get_recall_service", lambda config=None: RecallService(NoneRecallProvider()))
    _seed_session(db)
    assert db.delete_conversation_session("sess-del") == 4


def test_session_delete_survives_recall_layer_errors(db, monkeypatch):
    def boom(config=None):
        raise RuntimeError("recall exploded")

    monkeypatch.setattr(recall_pkg, "get_recall_service", boom)
    _seed_session(db)
    assert db.delete_conversation_session("sess-del") == 4


# -- context_snapshot -------------------------------------------------------------------


def test_context_snapshot_migration_on_existing_database(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id VARCHAR(100) NOT NULL, role VARCHAR(20) NOT NULL, content TEXT NOT NULL, created_at DATETIME)"
    )
    connection.execute(
        "INSERT INTO conversation_messages (session_id, role, content, created_at) "
        "VALUES ('old', 'user', 'legacy question', '2026-01-01 10:00:00')"
    )
    connection.commit()
    connection.close()

    DatabaseManager.reset_instance()
    try:
        manager = DatabaseManager(db_url=f"sqlite:///{path}")
        columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(conversation_messages)")}
        assert "context_snapshot" in columns
        # old rows stay readable, new rows can carry a snapshot
        assert [m["content"] for m in manager.get_conversation_messages("old")] == ["legacy question"]
        snapshot = {"market_phase": "intraday", "context_object": {"stock_code": "300750"}}
        message_id = manager.save_conversation_message("old", "assistant", "answer", context_snapshot=snapshot)
        plain_id = manager.save_conversation_message("old", "assistant", "no snapshot")
        raw = dict(sqlite3.connect(path).execute(
            "SELECT id, context_snapshot FROM conversation_messages").fetchall())
        assert '"market_phase": "intraday"' in raw[message_id] and raw[plain_id] is None and raw[1] is None
        # re-initialising is idempotent
        DatabaseManager.reset_instance()
        DatabaseManager(db_url=f"sqlite:///{path}")
    finally:
        DatabaseManager.reset_instance()


def test_user_turn_commit_persists_context_snapshot(db):
    session_id = "snap"
    generation = db.ensure_chat_session_generation(session_id)
    snapshot = db.read_chat_session_snapshot(session_id)
    assert snapshot["session_generation"] == generation
    accepted = db.commit_legacy_chat_user_turn(
        snapshot, "hello", state_disposition="legacy-unaware",
        context_snapshot={"market_phase": "postmarket", "data_time": "2026-11-07T16:00:00+08:00"},
    )
    with db.session_scope() as s:
        from src.storage import ConversationMessage

        row = s.get(ConversationMessage, accepted["user_message_id"])
        assert '"postmarket"' in row.context_snapshot


def test_build_context_snapshot_uses_market_phase_and_object():
    snapshot, phase = assistant_memory.build_context_snapshot(
        {"stock_code": "300750", "stock_name": "宁德时代", "board_id": "BK1184",
         "market_phase_context": {"phase": "intraday", "market": "cn",
                                  "market_local_time": "2026-11-07T10:30:00+08:00",
                                  "effective_daily_bar_date": "2026-11-06"}},
        datetime(2026, 11, 7, 10, 30),
    )
    assert phase == "intraday"
    assert snapshot["data_time"] == "2026-11-07T10:30:00+08:00"
    assert snapshot["context_object"] == {"stock_code": "300750", "stock_name": "宁德时代", "board_id": "BK1184"}
