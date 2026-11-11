"""Offline report feed isolation and real Feishu consumer/sender handoff gates."""
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from fastapi import HTTPException

from src.services import finance_channel_ownership as channel
from src.services.finance_identity import FinanceIdentityStore, user_system_available
from src.services.finance_report_events import FinanceReportEvents


@pytest.fixture
def managed(tmp_path, monkeypatch):
    store = FinanceIdentityStore(tmp_path / "ledger.db")
    monkeypatch.setattr(channel, "get_config", lambda: SimpleNamespace(database_path=str(tmp_path / "stock.db")))
    monkeypatch.setattr(channel, "setting", lambda key: {"FINANCE_FEISHU_MANAGED": "true", "FINANCE_FEISHU_APP_ID": "test-app"}.get(key, ""))
    # All participants share the same durable ownership database.
    monkeypatch.setattr(channel, "FinanceIdentityStore", lambda path: store)
    return store


def test_report_events_are_versioned_replayable_and_owner_filtered(managed, monkeypatch):
    import src.services.finance_report_events as module
    monkeypatch.setattr(module, "configuration", lambda: {"INSTANCE_ID": "test-instance"})
    feed = FinanceReportEvents(managed)
    feed.record("shared", "market_review", "2026-11-10", "market only", owner=None, public=True)
    feed.record("a", "daily_plan", "2026-11-10", "alice holdings", owner="alice")
    feed.record("b", "daily_plan", "2026-11-10", "bob holdings", owner="bob")
    feed.record("a", "daily_plan", "2026-11-10", "alice holdings", owner="alice")
    alice = feed.read("alice")
    assert [event["reportId"] for event in alice["events"]] == ["shared", "a"]
    assert [event["reportId"] for event in feed.read("bob")["events"]] == ["shared", "b"]
    assert [event["reportId"] for event in feed.read("alice", audience="public")["events"]] == ["shared"]
    assert feed.read("alice", alice["nextCursor"])["events"] == []
    feed.record("a", "daily_plan", "2026-11-10", "updated alice holdings", owner="alice")
    assert len(feed.read("alice", alice["nextCursor"])["events"]) == 1


def test_existing_archive_ledger_upgrade_separates_report_date_from_trade_date(managed):
    with managed.db() as db:
        db.execute("""CREATE TABLE report_events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL,
            version TEXT NOT NULL, kind TEXT NOT NULL, trading_day TEXT, owner TEXT, audience TEXT NOT NULL,
            content TEXT NOT NULL, UNIQUE(report_id,version,audience))""")
        db.execute("""CREATE TABLE public_report_events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL,
            version TEXT NOT NULL, kind TEXT NOT NULL, trading_day TEXT, content TEXT NOT NULL, UNIQUE(report_id,version))""")
        db.execute("INSERT INTO report_events(report_id,version,kind,trading_day,audience,content) VALUES('market-review:2026-11-08','old','market_review','2026-11-08','public','old')")
        db.execute("INSERT INTO public_report_events(report_id,version,kind,trading_day,content) VALUES('market-review:2026-11-08','old','market_review','2026-11-08','old')")
    FinanceReportEvents(managed)
    with managed.db() as db:
        for table in ("report_events", "public_report_events"):
            row = db.execute(f"SELECT * FROM {table}").fetchone()
            assert row["trading_day"] is None
            assert row["report_date"] == "2026-11-08"


def test_verified_weekend_archive_is_report_date_and_unknown_trade_day_keeps_cursor(managed, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from datetime import date
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from src.storage import AnalysisHistory, DailyPlanRun
    from src.services.closing_report_visibility import record_public_market_review
    from api.v1.endpoints import closing_analysis
    import src.services.finance_report_events as module
    monkeypatch.setattr(module, "configuration", lambda: {"INSTANCE_ID": "test"})
    monkeypatch.setattr(closing_analysis, "_REPORTS_DIR", tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'report-sources.db'}")
    AnalysisHistory.__table__.create(engine)
    DailyPlanRun.__table__.create(engine)
    class Sources:
        @contextmanager
        def get_session(self):
            with Session(engine) as session:
                yield session
    try:
        path = tmp_path / "market_review_20261108.md"
        content = "Weekend report using explicitly dated earlier market evidence"
        path.write_text(content, encoding="utf-8")
        record_public_market_review(path, date(2026, 11, 8), content)
        feed = FinanceReportEvents(managed)
        feed.reconcile(Sources(), tmp_path, public_only=True)
        report = feed.read_public()["events"][0]
        assert report["tradingDay"] is None and report["reportDate"] == "2026-11-08"
        feed.record("unknown-date", "stock_analysis", "not-a-date", "unknown", owner="alice")
        result = feed.read("alice")
        assert result["events"][-1]["tradingDay"] is None
        assert result["nextCursor"] == result["events"][-1]["cursor"]
    finally:
        engine.dispose()


def test_channel_transition_requires_pause_and_no_live_stock_receiver(managed):
    assert channel.channel_state()["owner"] == "stock"
    with pytest.raises(HTTPException):
        channel.transition(1, "core")
    assert channel.transition(1, "paused")["version"] == 2
    with channel.receiver_lease(managed):
        with pytest.raises(HTTPException) as exc:
            channel.transition(2, "core")
        assert exc.value.detail["error"] == "FINANCE_STOCK_RECEIVER_STILL_RUNNING"
    assert channel.transition(2, "core")["owner"] == "core"
    with pytest.raises(HTTPException):
        channel.transition(2, "paused")
    assert channel.transition(3, "paused")["version"] == 4
    assert channel.transition(4, "stock")["owner"] == "stock"


def test_real_sender_entry_points_do_no_network_when_core_owns_channel(managed, monkeypatch, tmp_path):
    from src.notification_sender.feishu_sender import FeishuSender
    from bot.platforms.feishu_stream import FeishuReplyClient
    channel.transition(1, "paused")
    channel.transition(2, "core")
    def unexpected(*args, **kwargs):
        pytest.fail("managed legacy sender reached network")
    monkeypatch.setattr("requests.post", unexpected)
    sender = FeishuSender(SimpleNamespace(feishu_webhook_url="https://example.invalid/hook"))
    assert sender._send_via_webhook("private") is False
    assert sender._app_send_raw("{}") is False
    path = tmp_path / "private.md"
    path.write_text("private holdings")
    assert sender._send_file_via_app_bot(path) is False
    reply = object.__new__(FeishuReplyClient)
    assert reply.patch_card("id", "private") is False
    assert reply.reply_text("id", "private") is False
    assert reply.send_to_chat("chat", "private") is False


def test_receive_and_registration_stop_before_processing(managed, monkeypatch):
    from bot.platforms.feishu_stream import FeishuStreamHandler
    from src.services.feishu_registration_service import FeishuRegistrationService
    channel.transition(1, "paused")
    handler = object.__new__(FeishuStreamHandler)
    # Invalid event intentionally proves the gate precedes parsing/dispatch.
    assert handler.handle_message(None) is None
    assert handler._process_message(None) is None
    with pytest.raises(HTTPException) as exc:
        FeishuRegistrationService().start()
    assert exc.value.status_code == 409


def test_disabling_flag_cannot_restore_legacy_sender(managed, monkeypatch):
    channel.transition(1, "paused")
    channel.transition(2, "core")
    monkeypatch.setattr(channel, "setting", lambda key: "test-app" if key == "FINANCE_FEISHU_APP_ID" else "")
    # managed_store checks the existing database marker even after flag removal.
    path = Path(channel.get_config().database_path).with_suffix(".finance.db")
    path.touch()
    assert channel.stock_allowed() is False


def test_other_feishu_app_is_not_taken_over(managed):
    channel.transition(1, "paused")
    channel.transition(2, "core")
    assert channel.stock_allowed(SimpleNamespace(_app_id="other-app")) is True
    assert channel.stock_allowed(SimpleNamespace(_app_id="test-app")) is False


def test_live_receiver_lease_survives_enabling_management_by_hot_reload(managed, monkeypatch):
    enabled = {"value": False}
    monkeypatch.setattr(channel, "setting", lambda key: {"FINANCE_FEISHU_MANAGED": "true" if enabled["value"] else "false",
                                                         "FINANCE_FEISHU_APP_ID": "test-app"}.get(key, ""))
    started, release = threading.Event(), threading.Event()
    class Receiver:
        _app_id = "test-app"
        _running = True

        @channel.stock_receiver
        def run(self):
            started.set()
            release.wait(3)
    worker = threading.Thread(target=Receiver().run)
    worker.start()
    try:
        assert started.wait(1)
        enabled["value"] = True
        channel.transition(1, "paused")
        with pytest.raises(HTTPException) as exc:
            channel.transition(2, "core")
        assert exc.value.detail["error"] == "FINANCE_STOCK_RECEIVER_STILL_RUNNING"
    finally:
        release.set()
        worker.join(5)
    assert channel.transition(2, "core")["owner"] == "core"


@pytest.mark.skipif(not user_system_available(), reason="Requires P1/P2 source ownership")
def test_committed_sources_catch_up_without_cross_user_or_deleted_event(managed, monkeypatch, tmp_path):
    from datetime import date
    import src.services.finance_report_events as module
    from src.storage import DatabaseManager, AnalysisHistory, DailyPlanRun
    from tests.multi_user_helpers import prepare_multi_user_env, teardown_multi_user_env
    prepare_multi_user_env(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "configuration", lambda: {"INSTANCE_ID": "test-instance"})
    db = DatabaseManager.get_instance()
    try:
        with db.get_session() as session:
            report = AnalysisHistory(query_id="private-source", code="TEST", analysis_summary="alice private",
                                     raw_result="{}", requester_user_id="alice", is_private=True)
            session.add(report)
            session.add(DailyPlanRun(run_id="bob-plan", user_id="bob", trade_date=date(2026, 11, 10),
                                    status="succeeded", trigger="manual", push_preview="bob personal plan"))
            session.add(DailyPlanRun(run_id="web-only", user_id="bob", trade_date=date(2026, 11, 10),
                                     status="succeeded", trigger="schedule", push_preview="private rule plan",
                                     push_json='{"status":"skipped","reason":"no_model"}'))
            session.commit()
            report_id = report.id
        feed = FinanceReportEvents(managed)
        # An event saved before the P3 upgrade must also respect the current source policy on replay.
        feed.record("plan:web-only", "daily_plan", date(2026, 11, 10), "private rule plan", owner="bob")
        feed.reconcile(db, tmp_path / "missing-reports")
        feed.reconcile(db, tmp_path / "missing-reports")
        assert [event["reportId"] for event in feed.read("alice")["events"]] == [f"analysis:{report_id}"]
        assert [event["reportId"] for event in feed.read("bob")["events"]] == ["plan:bob-plan"]
        assert feed.read("alice", audience="public")["events"] == []
        with db.get_session() as session:
            session.delete(session.get(AnalysisHistory, report_id))
            session.commit()
        assert feed.read("alice")["events"] == []
    finally:
        teardown_multi_user_env()
