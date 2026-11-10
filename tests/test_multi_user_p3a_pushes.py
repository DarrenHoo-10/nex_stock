# -*- coding: utf-8 -*-
"""Multi-user P3a: every push path goes to the owner's own channels - and only there.

Wired paths: next-day plan (manual and scheduled), pre-open push, alert notifications, single-stock analysis push
(``notify``), position analysis, ``/agent/chat/send``. For each: a non-primary user with a channel reaches only that
channel, a non-primary user without one reaches nothing (never the ``.env`` channels or bot apps), and the primary user
is unchanged. Real services over SQLite; only HTTP (``FakeHTTP``), DNS, the model and the clock are doubled.
"""

from __future__ import annotations

import json
import smtplib
import sys
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.config import Config, get_config
from src.core.pipeline import StockAnalysisPipeline
from src.notification import ChannelAttemptResult, NotificationDispatchResult, NotificationService
from src.repositories.user_repo import UserRepository
from src.services import daily_plan_service as plan_module
from src.services.alert_service import AlertService
from src.services.alert_worker import AlertWorker
from src.services.daily_plan_service import DailyPlanService
from src.services.user_notification_service import (
    NO_MODEL_NOTICE_KEY,
    UserNotificationService,
    build_user_notifier,
)
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    stub_dns,
    teardown_multi_user_env,
)
from tests.p3a_helpers import (
    CUSTOM,
    SYSTEM_CHANNEL_ENV,
    SYSTEM_HOSTS,
    FakeHTTP,
    create_channel,
)
from tests.test_daily_plan_user_isolation import _holding, _seed_market

USER_HOST = "hooks.example.org"  # CUSTOM["webhook_url"]
SYSTEM_HOSTS_SET = {host for host in SYSTEM_HOSTS.values() if host != "www.pushplus.plus"}
NOW = datetime(2026, 11, 6, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def app(tmp_path, monkeypatch):
    plan_module._ACTIVE.clear()
    built = build_multi_user_app(tmp_path, monkeypatch)
    built.dns = stub_dns(monkeypatch)
    built.http = FakeHTTP().install(monkeypatch)
    # the system's channels and bot app exist in the environment: nothing of a non-primary user may reach them
    for key, value in SYSTEM_CHANNEL_ENV.items():
        monkeypatch.setenv(key, value)
    Config.reset_instance()
    smtp = MagicMock(side_effect=AssertionError("the system SMTP account must never be used here"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", smtp)
    monkeypatch.setattr(smtplib, "SMTP", smtp)
    monkeypatch.setattr("src.utils.safe_smtp.connect_pinned_smtp", MagicMock(side_effect=AssertionError("no SMTP expected")))
    repo = UserRepository()
    for user in (USER_A, USER_B):
        repo.touch_app_user(user, user, user, "user")
    _seed_market(built.db)
    # reports must not be written into the repository's reports/ directory
    monkeypatch.setattr(NotificationService, "save_report_to_file", lambda self, content, filename=None: str(tmp_path / "r.md"))
    yield built
    plan_module._ACTIVE.clear()
    teardown_multi_user_env()


def _channel(app, user_id, **config):
    response = create_channel(app.as_user(user_id), "custom", {**CUSTOM, **config})
    assert response.status_code == 201, response.text
    return response.json()


def _assert_nothing_reached_the_system(app, *, allowed=()):
    assert not set(app.http.hosts()) & SYSTEM_HOSTS_SET, app.http.hosts()
    assert set(app.http.hosts()) <= set(allowed), app.http.hosts()


def _plan_service(app, user_id, *, model=False, pushed=None):
    config = SimpleNamespace(
        litellm_model="model-x" if model else "", max_workers=1, stock_list=[], daily_plan_enabled=True,
        position_stop_loss_pct=10.0, position_max_total_pct=100.0, position_max_holdings=5,
        position_max_single_pct=None, position_stop_mode="fixed", schedule_time="18:00", schedule_times=["18:00"],
    )
    anomalies = SimpleNamespace(repo=SimpleNamespace(latest_completed_run=lambda trade_date=None: None))
    kwargs = {}
    if model:
        kwargs["llm_call"] = lambda prompt, system: ""  # no usable answer: stock plans fall back to the rules
    if pushed is not None:
        kwargs["pusher"] = pushed.append  # the primary user's system pusher
    return DailyPlanService(
        db_manager=app.db, config=config, spawn=lambda fn: fn(), anomaly_service=anomalies, calendar_fn=None,
        now_fn=lambda: NOW, workers=1, user_id=user_id, **kwargs,
    )


def _step(run, key):
    return {step["key"]: step for step in run["steps"]}[key]


# ---------------------------------------------------------------------------- next-day plan: manual run


def test_manual_plan_run_of_a_non_primary_user_goes_to_their_own_channel_only(app):
    _channel(app, USER_A)
    service = _plan_service(app, USER_A, model=True)
    service.set_holdings_snapshot(100_000.0, [_holding()])

    run = service.trigger_run(wait=True)

    assert run["push"]["channel"] == "personal" and run["push"]["status"] == "sent" and run["push"]["configured"] is True
    assert run["push"]["channels"] == [{"channel": "custom", "success": True}]
    assert _step(run, "push")["status"] == "succeeded" and _step(run, "push")["label"] == "个人渠道推送"
    [call] = app.http.calls
    assert call.host == USER_HOST and "次日计划" in json.loads(call.body)["text"]  # the plan summary, via the channel's template
    _assert_nothing_reached_the_system(app, allowed={USER_HOST})


def test_non_primary_user_without_a_channel_gets_no_push_and_never_the_system_channels(app):
    service = _plan_service(app, USER_B, model=True)
    service.set_holdings_snapshot(100_000.0, [_holding()])

    run = service.trigger_run(wait=True)

    assert run["push"]["status"] == "skipped" and run["push"]["configured"] is False
    assert "个人推送渠道" in _step(run, "push")["detail"]
    assert run["push_preview_markdown"] and run["stock_succeeded"] == 1
    assert app.http.calls == []


def test_a_failing_personal_channel_never_fails_the_run_and_is_recorded_per_channel(app):
    _channel(app, USER_A, webhook_url="https://bad.example.org/in")
    _channel(app, USER_A, webhook_url="https://fine.example.org/in")
    app.http.routes["bad.example.org"] = (500, {}, b"nope")
    service = _plan_service(app, USER_A, model=True)
    service.set_holdings_snapshot(100_000.0, [_holding()])

    run = service.trigger_run(wait=True)

    assert run["status"] in ("succeeded", "partial") and run["stock_succeeded"] == 1
    assert run["push"]["status"] == "sent"  # one of the two delivered
    assert app.http.hosts() == ["bad.example.org", "fine.example.org"]

    # every channel failing: the push step fails, the run does not
    app.http.routes["fine.example.org"] = (500, {}, b"nope")
    app.http.reset()
    again = _plan_service(app, USER_A, model=True).trigger_run(wait=True)
    assert again["push"]["status"] == "failed" and _step(again, "push")["status"] == "failed"
    assert again["status"] in ("succeeded", "partial") and again["stock_succeeded"] == 1


def test_primary_plan_run_still_uses_the_injected_system_pusher(app):
    pushed = []
    _channel(app, USER_A)  # a personal channel exists, but it is not the primary user's route
    service = _plan_service(app, PRIMARY_ID, model=True, pushed=pushed)
    service.set_holdings_snapshot(100_000.0, [_holding()])
    service._push_configured = lambda: True

    run = service.trigger_run(wait=True)

    assert len(pushed) == 1 and run["push"]["channel"] == "feishu" and run["push"]["status"] == "sent"
    assert app.http.calls == []
    assert _step(run, "push")["label"] == "飞书推送"


def test_an_injected_system_pusher_cannot_serve_a_non_primary_user(app):
    pushed = []
    _channel(app, USER_A)
    service = _plan_service(app, USER_A, model=True, pushed=pushed)
    service.set_holdings_snapshot(100_000.0, [_holding()])

    service.trigger_run(wait=True)

    assert pushed == []  # the user's own channel was used instead
    assert app.http.hosts() == [USER_HOST]


# ---------------------------------------------------------------------------- next-day plan: scheduled, no model


def test_scheduled_run_without_a_model_marks_the_run_and_reminds_once(app):
    _channel(app, USER_A)
    service = _plan_service(app, USER_A, model=False)
    service.set_holdings_snapshot(100_000.0, [_holding()])

    first = service.trigger_run("schedule", wait=True)
    assert first["push"]["status"] == "skipped" and first["push"]["reason"] == "no_model"
    assert "未配置模型" in _step(first, "push")["detail"] and "已提醒一次" in _step(first, "push")["detail"]
    assert any("未配置模型" in gap for gap in first["data_gaps"])
    assert first["stock_succeeded"] == 1  # the rule plan is still made and readable on the web
    [reminder] = app.http.calls
    assert reminder.host == USER_HOST and "NexStock" in reminder.body
    assert UserRepository().has_notice(USER_A, NO_MODEL_NOTICE_KEY)

    app.http.reset()
    second = _plan_service(app, USER_A, model=False).trigger_run("schedule", wait=True)
    assert second["push"]["reason"] == "no_model" and "已提醒一次" not in _step(second, "push")["detail"]
    assert app.http.calls == []  # not every day

    # a manual run is a request: it still pushes the rule plan
    manual = _plan_service(app, USER_A, model=False).trigger_run("manual", wait=True)
    assert manual["push"]["status"] == "sent" and len(app.http.calls) == 1


def test_no_model_reminder_needs_a_channel_and_resets_when_a_model_is_back(app):
    no_channel = _plan_service(app, USER_B, model=False)
    no_channel.set_holdings_snapshot(100_000.0, [_holding()])
    run = no_channel.trigger_run("schedule", wait=True)
    assert run["push"]["reason"] == "no_model" and app.http.calls == []
    assert not UserRepository().has_notice(USER_B, NO_MODEL_NOTICE_KEY)

    _channel(app, USER_B)
    _plan_service(app, USER_B, model=False).trigger_run("schedule", wait=True)
    assert UserRepository().has_notice(USER_B, NO_MODEL_NOTICE_KEY) and len(app.http.calls) == 1

    # a scheduled run with a model pushes the plan and re-arms the reminder for a later loss of the model
    app.http.reset()
    with_model = _plan_service(app, USER_B, model=True).trigger_run("schedule", wait=True)
    assert with_model["push"]["status"] == "sent" and len(app.http.calls) == 1
    assert not UserRepository().has_notice(USER_B, NO_MODEL_NOTICE_KEY)


# ---------------------------------------------------------------------------- pre-open push


class _FakePreopenPipeline:
    """Replaces the analysis pipeline: records how it was built; its notifier is whatever the real pipeline would pick."""

    built = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakePreopenPipeline.built.append(kwargs)
        user_id = kwargs.get("user_id")
        self.real_notifier = build_user_notifier(user_id) if user_id else None
        self.notifier = self

    def is_available(self):
        return self.real_notifier.is_available() if self.real_notifier else True

    def generate_aggregate_report(self, results, report_type):
        return "仪表盘:" + ",".join(r.code for r in results)

    def run(self, **kwargs):
        return [SimpleNamespace(code=code) for code in kwargs["stock_codes"]]

    def send(self, content, **kwargs):
        if self.real_notifier is None:
            return True
        return self.real_notifier.send(content, **kwargs)


def _preopen(app, user_id, monkeypatch):
    from src.services.preopen_push import NewsPart, build_shared_preopen, run_preopen_push
    from src.services.user_config_service import get_user_config
    from src.services.watchlist_service import WatchlistService
    from tests.test_preopen_push import FakeRepo, FakeUsService

    WatchlistService().add(user_id, "600519")
    _FakePreopenPipeline.built = []
    config = get_config() if user_id == PRIMARY_ID else get_user_config(user_id)
    with patch("src.core.pipeline.StockAnalysisPipeline", _FakePreopenPipeline):
        shared = build_shared_preopen(
            config, day=date(2026, 11, 9), now=datetime(2026, 11, 9, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
            us_service=FakeUsService(), repo=FakeRepo(),
        )
        return run_preopen_push(
            config, day=date(2026, 11, 9), now=datetime(2026, 11, 9, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
            news_loader=lambda *a: NewsPart(False), shared=shared, user_id=None if user_id == PRIMARY_ID else user_id,
        )


def test_preopen_push_of_a_non_primary_user_uses_their_watchlist_model_and_own_channel(app, monkeypatch):
    give_user_model(USER_A)
    _channel(app, USER_A)

    result = _preopen(app, USER_A, monkeypatch)

    [built] = _FakePreopenPipeline.built
    assert built["user_id"] == USER_A and built["config"].user_view_id == USER_A
    assert result["analysis"]["pushed"] is True and result["analysis"]["codes"] == ["600519"]
    [call] = app.http.calls
    assert call.host == USER_HOST and "盘前版" in json.loads(call.body)["text"]
    _assert_nothing_reached_the_system(app, allowed={USER_HOST})


def test_preopen_push_without_a_channel_pushes_nothing_anywhere(app, monkeypatch):
    give_user_model(USER_B)
    result = _preopen(app, USER_B, monkeypatch)
    assert result["analysis"]["pushed"] is False
    assert app.http.calls == []


def test_primary_preopen_push_is_unchanged(app, monkeypatch):
    from src.services.preopen_push import _load_watchlist

    result = _preopen(app, PRIMARY_ID, monkeypatch)
    [built] = _FakePreopenPipeline.built
    assert "user_id" not in built  # same pipeline construction as before multi-user
    assert result["analysis"]["pushed"] is True and app.http.calls == []
    assert _load_watchlist(get_config()) == ["600519"]


# ---------------------------------------------------------------------------- alerts


RULE = {
    "name": "Moutai breakout", "target_scope": "single_symbol", "target": "600519", "alert_type": "price_cross",
    "parameters": {"direction": "above", "price": 1800}, "severity": "warning", "enabled": True,
}


def _rule(app, user_id):
    response = app.as_user(user_id).post("/api/v1/alerts/rules", json=RULE)
    assert response.status_code == 200, response.text
    return response.json()


async def _quote(_monitor, _stock_code):
    return SimpleNamespace(price=1810.0)


def _worker(system_notifier):
    config = SimpleNamespace(agent_event_monitor_enabled=True, agent_event_alert_rules_json="", trading_day_check_enabled=False)
    return AlertWorker(config_provider=lambda: config, service=AlertService(), notifier=system_notifier)


def _system_notifier():
    notifier = MagicMock()
    notifier.send_with_results.return_value = NotificationDispatchResult(
        dispatched=True, success=True, status="sent", channel_results=[ChannelAttemptResult(channel="feishu", success=True)],
    )
    return notifier


def test_alert_of_a_non_primary_rule_goes_to_the_owners_channel_and_is_recorded(app):
    rule = _rule(app, USER_A)
    _channel(app, USER_A)
    system = _system_notifier()

    with patch("src.agent.events.EventMonitor._get_realtime_quote", new=_quote):
        stats = _worker(system).run_once()

    assert stats["triggered"] == 1 and stats["notified"] == 1
    system.send_with_results.assert_not_called()  # the primary user's notifier is not theirs
    [call] = app.http.calls
    assert call.host == USER_HOST and "Event Alert" in call.body
    _assert_nothing_reached_the_system(app, allowed={USER_HOST})
    notifications = app.as_user(USER_A).get("/api/v1/alerts/notifications").json()["items"]
    assert [(n["channel"], n["success"]) for n in notifications] == [("custom", True)]
    assert app.as_user(USER_A).get(f"/api/v1/alerts/rules/{rule['id']}").json()["cooldown_active"] is True
    assert app.as_user(USER_B).get("/api/v1/alerts/notifications").json()["items"] == []


def test_alert_of_a_user_without_a_channel_still_records_no_channel(app):
    _rule(app, USER_B)
    system = _system_notifier()
    with patch("src.agent.events.EventMonitor._get_realtime_quote", new=_quote):
        stats = _worker(system).run_once()
    assert stats["triggered"] == 1 and stats["notified"] == 0
    system.send_with_results.assert_not_called()
    assert app.http.calls == []
    notifications = app.as_user(USER_B).get("/api/v1/alerts/notifications").json()["items"]
    assert [(n["channel"], n["success"]) for n in notifications] == [("__no_channel__", False)]


def test_alert_channel_failure_is_recorded_and_other_users_are_unaffected(app):
    _rule(app, USER_A)
    _rule(app, PRIMARY_ID)
    _channel(app, USER_A)
    app.http.routes[USER_HOST] = (500, {}, b"down")
    system = _system_notifier()

    with patch("src.agent.events.EventMonitor._get_realtime_quote", new=_quote):
        stats = _worker(system).run_once()

    assert stats["triggered"] == 2 and stats["notified"] == 1  # the primary rule still went through its notifier
    system.send_with_results.assert_called_once()
    notifications = app.as_user(USER_A).get("/api/v1/alerts/notifications").json()["items"]
    assert [(n["channel"], n["success"]) for n in notifications] == [("custom", False)]
    cooldown = app.as_user(USER_A).get("/api/v1/alerts/rules").json()["items"][0]["cooldown_active"]
    assert cooldown is False  # a failed delivery does not start the cooldown, the next cycle retries


# ---------------------------------------------------------------------------- analysis push (notify) and position analysis


def _analysis_result():
    from src.analyzer import AnalysisResult

    return AnalysisResult(
        code="600519", name="贵州茅台", sentiment_score=61, trend_prediction="震荡", operation_advice="观望",
        analysis_summary="摘要", report_language="zh", success=True,
    )


def test_pipeline_notifier_is_the_users_personal_one_and_pushes_to_their_channel_only(app):
    from src.enums import ReportType
    from src.services.user_config_service import get_user_config

    give_user_model(USER_A)
    _channel(app, USER_A)
    pipeline = StockAnalysisPipeline(config=get_user_config(USER_A), user_id=USER_A)
    assert isinstance(pipeline.notifier, UserNotificationService) and pipeline.notifier.is_available()

    pipeline._send_single_stock_notification(_analysis_result(), report_type=ReportType.SIMPLE)

    [call] = app.http.calls
    assert call.host == USER_HOST
    _assert_nothing_reached_the_system(app, allowed={USER_HOST})

    system_pipeline = StockAnalysisPipeline(config=get_config())
    assert type(system_pipeline.notifier) is NotificationService  # the primary user: the .env channels, as before


def test_pipeline_of_a_user_without_channels_has_an_unavailable_notifier(app):
    from src.services.user_config_service import get_user_config

    give_user_model(USER_B)
    pipeline = StockAnalysisPipeline(config=get_user_config(USER_B), user_id=USER_B)
    assert pipeline.notifier.is_available() is False
    pipeline._send_single_stock_notification(_analysis_result())
    assert app.http.calls == []


def test_analysis_notify_reaches_the_pipeline_for_non_primary_users(app):
    from src.services.analysis_service import AnalysisService

    give_user_model(USER_A)
    seen = []

    class Spy:
        def __init__(self, **kwargs):
            seen.append({"init": kwargs})

        def process_single_stock(self, **kwargs):
            seen[-1]["process"] = kwargs

    with patch("src.core.pipeline.StockAnalysisPipeline", Spy):
        AnalysisService().analyze_stock("600519", user_id=USER_A, send_notification=True)
        AnalysisService().analyze_stock("600519", user_id=USER_A, send_notification=False)
        AnalysisService().analyze_stock("600519", send_notification=True)

    assert [item["process"]["single_stock_notify"] for item in seen] == [True, False, True]
    assert seen[0]["init"]["user_id"] == USER_A and "user_id" not in seen[2]["init"]


def test_async_analysis_batch_no_longer_forces_notify_off_for_a_non_primary_requester(app):
    from api.v1.endpoints import analysis as analysis_endpoint

    queue = MagicMock()
    queue.submit_tasks_batch.return_value = ([], [])
    request = SimpleNamespace(
        stock_name=None, original_query=None, selection_source=None, report_type="detailed", analysis_phase="auto",
        force_refresh=False, notify=True, skills=None, report_language=None,
    )
    with patch.object(analysis_endpoint, "get_task_queue", return_value=queue):
        analysis_endpoint._handle_async_analysis_batch(["600519"], request, user_id=USER_A)
    kwargs = queue.submit_tasks_batch.call_args.kwargs
    assert kwargs["notify"] is True and kwargs["user_id"] == USER_A


# ---------------------------------------------------------------------------- /agent/chat/send


def test_chat_send_goes_to_the_requesters_own_channel(app):
    _channel(app, USER_A)
    client = app.as_user(USER_A)

    sent = client.post("/api/v1/agent/chat/send", json={"content": "A 的对话摘要"})
    assert sent.json() == {"success": True}
    [call] = app.http.calls
    assert call.host == USER_HOST
    _assert_nothing_reached_the_system(app, allowed={USER_HOST})

    # B has no channel: told so, and nothing is sent anywhere (neither A's nor the system's)
    app.http.reset()
    refused = app.as_user(USER_B).post("/api/v1/agent/chat/send", json={"content": "B text"})
    assert refused.json()["success"] is False and refused.json()["error"] == "no_channels"
    assert app.http.calls == []

    # a failing channel is reported as a send failure, not as "no channels"
    app.http.routes[USER_HOST] = (500, {}, b"x")
    failed = app.as_user(USER_A).post("/api/v1/agent/chat/send", json={"content": "again"})
    assert failed.json()["success"] is False and failed.json()["error"] == "send_failed"


def test_chat_send_of_the_primary_user_is_unchanged(app):
    with patch("src.notification.NotificationService") as notification:
        notification.return_value.send.return_value = True
        primary = app.as_user(PRIMARY_ID).post("/api/v1/agent/chat/send", json={"content": "P text"})
    assert primary.json() == {"success": True}
    notification.return_value.send.assert_called_once_with("P text")
    assert app.http.calls == []


def test_position_analysis_asks_for_a_push_for_every_requester(app):
    """The request carries notify=True for everyone; whose channels it reaches is decided by the pipeline's notifier."""
    from api.v1.endpoints import portfolio as portfolio_endpoint  # noqa: F401  (route import)

    for user_id in (USER_A, PRIMARY_ID):
        client = app.as_user(user_id)
        account = client.post("/api/v1/portfolio/accounts", json={"name": "Held", "base_currency": "CNY"}).json()["id"]
        client.post("/api/v1/portfolio/trades", json={
            "account_id": account, "symbol": "600519", "trade_date": "2026-01-02", "side": "buy",
            "quantity": 10, "price": 100,
        })
        queue = MagicMock()
        queue.submit_tasks_batch.return_value = (
            [SimpleNamespace(task_id="t", trace_id="tr", stock_code="600519", analysis_phase="postmarket")], [],
        )
        give_user_model(user_id) if user_id != PRIMARY_ID else None
        with patch("src.services.portfolio_service.PortfolioService._fetch_realtime_position_price", return_value=(None, None)), \
                patch("api.v1.endpoints.portfolio.get_task_queue", return_value=queue):
            response = client.post("/api/v1/portfolio/positions/600519/analysis", json={"account_id": account})
        assert response.status_code == 202, response.text
        kwargs = queue.submit_tasks_batch.call_args.kwargs
        assert kwargs["notify"] is True
        assert ("user_id" in kwargs) is (user_id != PRIMARY_ID)
