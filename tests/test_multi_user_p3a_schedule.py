# -*- coding: utf-8 -*-
"""Multi-user P3a: per-user schedules (times, constraints, job registry, parallel runs), the shared news pool, migration.

No sleeps longer than a fraction of a second: timeouts are tiny and every blocked runner waits on an ``Event`` the test
releases, so the suite is deterministic.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace
from typing import Dict, List
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.config import Config, get_config
from src.repositories.user_repo import UserRepository
from src.scheduler import Scheduler
from src.services import daily_plan_service as plan_module
from src.services import user_scheduled_runs as runs
from src.services.daily_plan_service import DailyPlanService
from src.services.news_pool_service import NewsPoolService
from src.services.runtime_scheduler import RuntimeSchedulerService
from src.services.user_notification_service import NO_MODEL_NOTICE_KEY
from src.services.user_schedule_service import (
    earliest_plan_time,
    effective_plan_times,
    effective_preopen_time,
    plan_times_in_use,
    plan_users_due,
    preopen_times_in_use,
    preopen_users_due,
)
from src.services.user_settings_service import UserSettingsService
from src.services.watchlist_service import WatchlistService
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    stub_dns,
    teardown_multi_user_env,
)
from tests.p3a_helpers import CUSTOM, FakeHTTP, create_channel
from tests.test_board_pipeline_schedule import _NoopThread, fake_schedule  # noqa: F401
from tests.test_daily_plan_user_isolation import _holding, _seed_market

USER_C = "uid-user-c"
SETTINGS = "/api/v1/me/settings"
NOW = datetime(2026, 11, 6, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
PREOPEN_NOW = datetime(2026, 11, 9, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai"))  # inside the allowed pre-open window


@pytest.fixture
def app(tmp_path, monkeypatch):
    plan_module._ACTIVE.clear()
    built = build_multi_user_app(tmp_path, monkeypatch)
    built.dns = stub_dns(monkeypatch)
    built.http = FakeHTTP().install(monkeypatch)
    repo = UserRepository()
    for user in (USER_A, USER_B, USER_C):
        repo.touch_app_user(user, user, user, "user")
    monkeypatch.setattr(runs, "multi_user_scheduling_enabled", lambda: True)  # (the API itself keeps AUTH_MODE=local)
    monkeypatch.setattr("src.core.trading_calendar.is_market_open", lambda *a, **k: True)
    runs.reset_shared_state()
    yield built
    runs.reset_shared_state()
    plan_module._ACTIVE.clear()
    teardown_multi_user_env()


def _put(app, user_id, **values):
    return app.as_user(user_id).put(SETTINGS, json={"items": [{"key": k, "value": v} for k, v in values.items()]})


def _set(user_id, **values):
    UserSettingsService().set_settings(user_id, values)


# ---------------------------------------------------------------------------- time constraints


def test_plan_time_must_be_after_the_systems_data_ready_time(app):
    schedule = app.as_user(USER_A).get("/api/v1/me/schedule").json()
    assert schedule["daily_plan"]["earliest_time"] == "17:30"  # BOARD_PIPELINE_SCHEDULE_TIME 17:00 + 30 min
    assert schedule["preopen_push"]["latest_time"] == "09:00"
    assert schedule["is_primary"] is False and schedule["managed_in_system_settings"] is False

    for early in ("09:00", "17:00", "17:29"):
        refused = _put(app, USER_A, DAILY_PLAN_TIME=early)
        assert refused.status_code == 400, early
        body = refused.json()
        assert body["error"] == "plan_time_too_early"
        issue = body["detail"]["issues"][0]
        assert issue["key"] == "DAILY_PLAN_TIME" and issue["earliest_time"] == "17:30"
    assert _put(app, USER_A, DAILY_PLAN_TIME="17:30").status_code == 200
    assert _put(app, USER_A, DAILY_PLAN_TIME="23:59").status_code == 200
    assert _put(app, USER_A, DAILY_PLAN_TIME="").status_code == 200  # blank = the system default again

    invalid = _put(app, USER_A, DAILY_PLAN_TIME="25:61")
    assert invalid.status_code == 400 and invalid.json()["error"] == "invalid_value"
    assert _put(app, USER_A, DAILY_PLAN_TIME="6pm").status_code == 400


def test_a_rejected_time_stores_nothing_and_the_batch_is_all_or_nothing(app):
    refused = _put(app, USER_A, DAILY_PLAN_TIME="09:00", DAILY_PLAN_ENABLED="true")
    assert refused.status_code == 400
    assert UserSettingsService().get_plain_values(USER_A) == {}


def test_preopen_time_must_be_before_the_market_opens(app):
    for late in ("09:01", "10:00", "23:00"):
        refused = _put(app, USER_A, PREOPEN_PUSH_TIME=late)
        assert refused.status_code == 400 and refused.json()["error"] == "preopen_time_too_late"
        assert refused.json()["detail"]["issues"][0]["latest_time"] == "09:00"
    for fine in ("09:00", "08:15", "05:30"):
        assert _put(app, USER_A, PREOPEN_PUSH_TIME=fine).status_code == 200


def test_earliest_plan_time_follows_the_board_pipeline_schedule(app, monkeypatch):
    config = get_config()
    assert earliest_plan_time() == "17:30"
    monkeypatch.setattr(config, "board_pipeline_schedule_time", "18:00")
    monkeypatch.setattr(config, "daily_plan_data_ready_buffer_minutes", 15)
    assert earliest_plan_time() == "18:15"
    assert app.as_user(USER_A).get("/api/v1/me/schedule").json()["daily_plan"]["earliest_time"] == "18:15"
    assert _put(app, USER_A, DAILY_PLAN_TIME="18:10").json()["error"] == "plan_time_too_early"
    assert _put(app, USER_A, DAILY_PLAN_TIME="18:15").status_code == 200
    monkeypatch.setattr(config, "board_pipeline_enabled", False)  # no pipeline: the close (15:00) + buffer
    assert earliest_plan_time() == "15:15"


def test_the_primary_users_schedule_is_the_env_and_cannot_be_set_here(app):
    assert _put(app, PRIMARY_ID, DAILY_PLAN_TIME="18:00").json()["error"] == "primary_user_uses_env"
    schedule = app.as_user(PRIMARY_ID).get("/api/v1/me/schedule").json()
    assert schedule["is_primary"] is True and schedule["managed_in_system_settings"] is True


def test_effective_times_fall_back_to_the_system_times_and_never_run_before_the_data_is_ready(app, monkeypatch):
    from src.services.user_config_service import get_user_config

    monkeypatch.setenv("SCHEDULE_TIMES", "09:20,18:00,21:00")
    Config.reset_instance()
    config = get_config()
    _set(USER_A, DAILY_PLAN_ENABLED="true")

    assert effective_plan_times(get_user_config(USER_A)) == ["18:00", "21:00"]  # 09:20 is before the data is ready
    monkeypatch.setenv("SCHEDULE_TIMES", "09:20")
    Config.reset_instance()
    config = get_config()
    assert effective_plan_times(get_user_config(USER_A)) == ["17:30"]  # nothing valid left: the earliest allowed time
    _set(USER_A, DAILY_PLAN_TIME="19:30")
    assert effective_plan_times(get_user_config(USER_A)) == ["19:30"]
    # the administrator later moves the board pipeline: the saved time is clamped, not obeyed
    monkeypatch.setattr(config, "board_pipeline_schedule_time", "20:00")
    assert effective_plan_times(get_user_config(USER_A)) == ["20:30"]

    _set(USER_A, PREOPEN_PUSH_TIME="08:40")
    assert effective_preopen_time(get_user_config(USER_A)) == "08:40"
    monkeypatch.setenv("PREOPEN_PUSH_TIME", "10:00")  # a system default after the open must not leak to users
    Config.reset_instance()
    assert effective_preopen_time(get_user_config(USER_B)) == "09:00"


def test_dedicated_plan_time_key_also_applies_to_the_primary_users_env_schedule(monkeypatch):
    from src.services.daily_plan_schedule import build_daily_plan_jobs, configured_times

    base = SimpleNamespace(schedule_times=["18:00"], schedule_time="18:00", daily_plan_enabled=True)
    assert configured_times(base) == ["18:00"]  # unchanged without DAILY_PLAN_TIME
    base.daily_plan_time = "19:00"
    assert configured_times(base) == ["19:00"]
    assert [job["schedule_time"] for job in build_daily_plan_jobs(base, config_provider=lambda: base)] == ["19:00"]


# ---------------------------------------------------------------------------- who is due when


def test_users_are_selected_by_their_own_enabled_flag_and_time(app):
    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00", PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:10")
    _set(USER_B, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="19:30", PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:10")
    _set(USER_C, DAILY_PLAN_ENABLED="false", DAILY_PLAN_TIME="18:00", PREOPEN_PUSH_ENABLED="false")

    assert plan_times_in_use() == ["18:00", "19:30"]  # de-duplicated, C (disabled) contributes nothing
    assert preopen_times_in_use() == ["08:10"]
    assert plan_users_due("18:00") == [USER_A] and plan_users_due("19:30") == [USER_B] and plan_users_due("20:00") == []
    assert preopen_users_due("08:10") == [USER_A, USER_B]


def test_a_user_with_unreadable_settings_does_not_keep_the_others_from_being_scheduled(app, monkeypatch):
    import src.services.user_schedule_service as schedule_service

    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00")
    _set(USER_B, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="19:00")
    real = schedule_service.user_plan_schedule

    def read(user_id):
        if user_id == USER_B:
            raise RuntimeError("broken settings")
        return real(user_id)

    monkeypatch.setattr(schedule_service, "user_plan_schedule", read)
    assert plan_times_in_use() == ["18:00"] and plan_users_due("18:00") == [USER_A] and plan_users_due("19:00") == []


def test_only_known_non_primary_users_are_scheduled(app):
    _set(USER_A, DAILY_PLAN_ENABLED="true")
    assert PRIMARY_ID not in plan_users_due("18:00")  # the primary user runs through the .env jobs
    assert "uid-never-seen" not in plan_users_due("18:00")  # only users registered in app_user


# ---------------------------------------------------------------------------- job registry (the scheduler)


def _group_times(schedule_module):
    return sorted(job.at_time for job in schedule_module.get_jobs())


def test_the_registry_holds_the_deduplicated_times_of_all_users_and_follows_setting_changes(app, fake_schedule):  # noqa: F811
    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00", PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:10")
    _set(USER_B, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="19:30", PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:10")
    _set(USER_C, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="19:30")
    scheduler = Scheduler(schedule_time="18:30", register_signals=False)
    for job in runs.build_user_schedule_jobs():
        scheduler.register_job_definition(job)

    assert _group_times(fake_schedule) == ["08:10", "18:00", "19:30"]  # one job per time, not per user
    names = sorted(entry["name"] for entry in scheduler._extra_daily_jobs)
    assert names == ["user_daily_plan_1800", "user_daily_plan_1930", "user_preopen_push_0810"]

    # user B moves their plan, C turns theirs off: the next re-sync registers / cancels exactly the difference
    _set(USER_B, DAILY_PLAN_TIME="20:15")
    _set(USER_C, DAILY_PLAN_ENABLED="false")
    for group in scheduler._dynamic_job_groups:
        group["last_sync"] = 0.0
    scheduler._refresh_extra_daily_jobs_if_needed()
    assert _group_times(fake_schedule) == ["08:10", "18:00", "20:15"]
    assert len(scheduler._extra_daily_jobs) == 3

    # a provider failure keeps what is registered
    group = scheduler._dynamic_job_groups[0]
    group["times_provider"] = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    group["last_sync"] = 0.0
    scheduler._refresh_extra_daily_jobs_if_needed()
    assert _group_times(fake_schedule) == ["08:10", "18:00", "20:15"]


def test_each_registered_job_fires_only_the_users_whose_setting_matches_its_time(app, fake_schedule, monkeypatch):  # noqa: F811
    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00")
    _set(USER_B, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="19:30")
    calls: List[tuple] = []

    def record(user_id, shared_board_cache):
        calls.append((user_id, id(shared_board_cache)))
        return {"status": "succeeded"}

    monkeypatch.setattr(runs, "run_user_daily_plan", record)
    scheduler = Scheduler(schedule_time="18:30", register_signals=False)
    for job in runs.build_user_schedule_jobs():
        scheduler.register_job_definition(job)

    by_time = {entry["name"]: entry for entry in scheduler._extra_daily_jobs}
    by_time["user_daily_plan_1800"]["task"]()
    assert [user for user, _ in calls] == [USER_A]
    by_time["user_daily_plan_1930"]["task"]()
    assert [user for user, _ in calls] == [USER_A, USER_B]


def test_runtime_scheduler_registers_the_user_job_groups_only_in_multi_user_mode(app, fake_schedule, monkeypatch):  # noqa: F811
    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="19:00")
    config = SimpleNamespace(
        schedule_enabled=True, schedule_time="18:00", schedule_times=["09:20"], board_pipeline_enabled=False,
    )
    service = RuntimeSchedulerService(config_provider=lambda: config, task_runner=lambda *a: None)
    service._reload_config = lambda: config
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert _group_times(fake_schedule) == ["09:20", "19:00"]

    monkeypatch.setattr(runs, "multi_user_scheduling_enabled", lambda: False)  # single-user mode: nothing extra
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert _group_times(fake_schedule) == ["09:20"]


def test_multi_user_scheduling_follows_the_auth_mode(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr("src.auth.get_auth_mode", lambda: "local")
    assert runs.multi_user_scheduling_enabled() is False and runs.build_user_schedule_jobs() == []
    monkeypatch.setattr("src.auth.get_auth_mode", lambda: "user_center")
    assert runs.multi_user_scheduling_enabled() is True


def test_cli_schedule_path_collects_the_same_job_groups(app, monkeypatch):
    jobs = runs.build_user_schedule_jobs(get_config(), config_provider=get_config)
    assert [job["name"] for job in jobs] == ["user_daily_plan", "user_preopen_push"]
    assert all(job["dynamic"] and callable(job["times_provider"]) and callable(job["task_factory"]) for job in jobs)
    import main

    source = open(main.__file__, encoding="utf-8").read()
    assert "build_user_schedule_jobs" in source  # main.py --schedule registers them next to the .env jobs


# ---------------------------------------------------------------------------- bounded parallel execution


def test_a_hanging_user_does_not_delay_the_others_and_is_abandoned_after_its_own_timeout(app):
    release = threading.Event()
    done: Dict[str, float] = {}
    timeouts: List[str] = []
    started = time.monotonic()

    def runner(user_id):
        if user_id == "hang":
            release.wait(10)
            return {"status": "succeeded"}
        done[user_id] = time.monotonic() - started
        return {"status": "succeeded", "user": user_id}

    try:
        results = runs.run_for_users(
            ["hang", "u1", "u2", "u3"], runner, max_workers=2, timeout_seconds=0.6, on_timeout=timeouts.append,
        )
    finally:
        release.set()

    assert results["hang"] == {"status": "timeout"} and timeouts == ["hang"]
    assert {user: results[user]["status"] for user in ("u1", "u2", "u3")} == {"u1": "succeeded", "u2": "succeeded", "u3": "succeeded"}
    # the others finished long before the hanging user's timeout: they were never queued behind it
    assert max(done.values()) < 0.5


def test_a_hung_slot_is_freed_at_the_timeout_so_waiting_users_still_run(app):
    release = threading.Event()
    order: List[str] = []

    def runner(user_id):
        order.append(user_id)
        if user_id == "hang":
            release.wait(10)
        return {"status": "succeeded"}

    try:
        results = runs.run_for_users(["hang", "u1"], runner, max_workers=1, timeout_seconds=0.3)
    finally:
        release.set()
    assert results["hang"]["status"] == "timeout" and results["u1"]["status"] == "succeeded"
    assert order == ["hang", "u1"]


def test_a_failing_user_is_only_their_own_failure_and_concurrency_is_bounded(app):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def runner(user_id):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            if user_id == "boom":
                raise RuntimeError("model key revoked")
            time.sleep(0.05)
            return {"status": "succeeded"}
        finally:
            with lock:
                active["now"] -= 1

    results = runs.run_for_users(["boom"] + [f"u{i}" for i in range(6)], runner, max_workers=3, timeout_seconds=5)
    assert results["boom"] == {"status": "failed", "error": "RuntimeError"}  # the message (a key may be in it) is dropped
    assert all(results[f"u{i}"]["status"] == "succeeded" for i in range(6))
    assert active["max"] <= 3


def test_the_batch_runs_due_users_in_parallel_with_the_configured_limits_and_records_timeouts(app, monkeypatch):
    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00")
    _set(USER_B, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00")
    _set(USER_C, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00")
    release = threading.Event()
    caches = set()
    recorded: List[tuple] = []

    def runner(user_id, cache):
        caches.add(id(cache))
        if user_id == USER_B:
            release.wait(10)
        if user_id == USER_C:
            raise RuntimeError("boom")
        return {"status": "succeeded"}

    monkeypatch.setattr(runs, "_record_plan_timeout", lambda user_id, seconds: recorded.append((user_id, seconds)))
    try:
        result = runs.run_scheduled_user_daily_plans("18:00", runner=runner, max_workers=4, timeout_seconds=0.4)
    finally:
        release.set()

    assert result["status"] == "ok"
    assert {u: o["status"] for u, o in result["users"].items()} == {USER_A: "succeeded", USER_B: "timeout", USER_C: "failed"}
    assert recorded == [(USER_B, 0.4)]
    assert len(caches) == 1  # one shared board context for the whole batch
    assert id(runs._DAY_STATE["plan_cache"]) in caches  # ... and it is the trading day's, reused by later batches

    # configured limits are read from the system config
    monkeypatch.setattr(get_config(), "user_schedule_max_workers", 7)
    monkeypatch.setattr(get_config(), "user_schedule_run_timeout_seconds", 99)
    assert runs._limits(get_config()) == {"max_workers": 7, "timeout_seconds": 99}


def test_batches_skip_non_trading_days_and_never_raise(app, monkeypatch):
    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00", PREOPEN_PUSH_ENABLED="true")
    runner = MagicMock(return_value={"status": "succeeded"})
    off = runs.run_scheduled_user_daily_plans("18:00", today=date(2026, 11, 7), is_trading_day=lambda d: False, runner=runner)
    assert off["reason"] == "not_trading_day" and not runner.called
    off_pre = runs.run_scheduled_user_preopens("08:15", today=date(2026, 11, 7), is_trading_day=lambda d: False, runner=runner)
    assert off_pre["reason"] == "not_trading_day"
    nobody = runs.run_scheduled_user_daily_plans("12:00", runner=runner)
    assert nobody["reason"] == "no_users_due" and not runner.called

    monkeypatch.setattr("src.services.user_schedule_service.plan_users_due", MagicMock(side_effect=RuntimeError("db")))
    crashed = runs.run_scheduled_user_daily_plans("18:00", runner=runner)
    assert crashed["status"] == "failed"


# ---------------------------------------------------------------------------- real plan runs of scheduled users


def _board_service_factory(app, listing_calls):
    real = DailyPlanService
    anomalies = SimpleNamespace(
        repo=SimpleNamespace(latest_completed_run=lambda trade_date=None: {
            "id": 7, "trade_date": date(2026, 11, 6), "status": "succeeded"}),
        list_anomalies=lambda *a, **k: listing_calls.append(1) or {"boards": []},
    )

    def factory(**kwargs):
        config = kwargs["config"]
        from src.services.user_config_service import has_usable_model

        extra = {"llm_call": (lambda prompt, system: "")} if has_usable_model(config) else {}
        return real(
            db_manager=app.db, anomaly_service=anomalies, calendar_fn=None, now_fn=lambda: NOW, workers=1,
            spawn=lambda fn: fn(), **kwargs, **extra,
        )

    return factory


def test_scheduled_plans_of_two_users_share_one_board_context_and_push_to_their_own_channels(app, monkeypatch):
    _seed_market(app.db)
    listing_calls: List[int] = []
    for user, host in ((USER_A, "a.example.org"), (USER_B, "b.example.org")):
        give_user_model(user)
        assert create_channel(app.as_user(user), "custom", {**CUSTOM, "webhook_url": f"https://{host}/in"}).status_code == 201
        DailyPlanService(db_manager=app.db, user_id=user, config=SimpleNamespace(
            litellm_model="", max_workers=1, stock_list=[], position_stop_loss_pct=10.0, position_max_total_pct=100.0,
            position_max_holdings=5, position_max_single_pct=None, position_stop_mode="fixed",
        )).set_holdings_snapshot(100_000.0, [_holding()])
        _set(user, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00")

    with monkeypatch.context() as patched:  # only the scheduled batch gets the test doubles, not the API
        patched.setattr("src.services.daily_plan_service.DailyPlanService", _board_service_factory(app, listing_calls))
        result = runs.run_scheduled_user_daily_plans("18:00")

    assert set(result["users"]) == {USER_A, USER_B}
    assert all(o["status"] in ("succeeded", "partial") for o in result["users"].values())
    assert sorted(app.http.hosts()) == ["a.example.org", "b.example.org"]  # each plan to its owner's channel only
    assert len(listing_calls) == 1  # the board listing of the trading day was computed once for both users
    for user in (USER_A, USER_B):
        latest = app.as_user(user).get("/api/v1/plan/runs/latest").json()["run"]
        assert latest["trigger"] == "schedule" and latest["push"]["status"] == "sent"


def test_a_scheduled_user_with_nothing_to_plan_is_skipped(app, monkeypatch):
    _set(USER_A, DAILY_PLAN_ENABLED="true", DAILY_PLAN_TIME="18:00")
    with monkeypatch.context() as patched:
        patched.setattr("src.services.daily_plan_service.DailyPlanService", _board_service_factory(app, []))
        result = runs.run_scheduled_user_daily_plans("18:00")
    assert result["users"][USER_A] == {"status": "skipped", "reason": "nothing_to_plan"}
    assert app.http.calls == []
    assert app.as_user(USER_A).get("/api/v1/plan/runs/latest").json()["run"] is None


def test_a_timed_out_plan_run_is_marked_failed_for_that_user_only(app):
    _seed_market(app.db)
    from src.repositories.daily_plan_repo import DailyPlanRepository

    repo = DailyPlanRepository(app.db)
    for user, run_id in ((USER_A, "plan-a"), (USER_B, "plan-b")):
        repo.create_run(user, run_id, date(2026, 11, 6), "schedule", [], {"channel": "personal", "status": "pending"})
    runs._record_plan_timeout(USER_A, 1800)
    failed = repo.get_run(USER_A, "plan-a")
    assert failed["status"] == "failed" and "1800" in (failed.get("error") or "")
    assert repo.get_run(USER_B, "plan-b")["status"] not in ("failed", "succeeded")  # B's run is untouched


# ---------------------------------------------------------------------------- pre-open batch


def test_preopen_batch_computes_the_shared_inputs_once_and_only_when_someone_needs_them(app):
    _set(USER_A, PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:15")
    _set(USER_B, PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:15")
    built = []

    def shared_builder():
        built.append(1)
        return "shared"

    seen = []

    def runner(user_id, shared, *, day, now):
        seen.append((user_id, shared()))
        return {"status": "succeeded"}

    result = runs.run_scheduled_user_preopens("08:15", now=PREOPEN_NOW, runner=runner, shared_builder=shared_builder)
    assert {u: o["status"] for u, o in result["users"].items()} == {USER_A: "succeeded", USER_B: "succeeded"}
    assert sorted(seen) == [(USER_A, "shared"), (USER_B, "shared")] and built == [1]

    # a second time slot of the same trading day reuses it: one overnight fetch per day, not per batch
    _set(USER_B, PREOPEN_PUSH_TIME="08:40")
    later = runs.run_scheduled_user_preopens("08:40", now=PREOPEN_NOW, runner=runner, shared_builder=shared_builder)
    assert later["users"][USER_B]["status"] == "succeeded" and built == [1]

    # a new trading day starts empty; nobody due -> nothing is fetched; users that never ask for it never trigger it
    runs.reset_shared_state()
    built.clear()
    nobody = runs.run_scheduled_user_preopens("07:00", now=PREOPEN_NOW, runner=runner, shared_builder=shared_builder)
    assert nobody["reason"] == "no_users_due" and built == []
    never_asked = runs.run_scheduled_user_preopens("08:15", now=PREOPEN_NOW, runner=lambda *a, **k: {"status": "skipped"}, shared_builder=shared_builder)
    assert never_asked["status"] == "ok" and built == []  # (no model / nothing to push) never trigger the fetch


def test_preopen_run_of_a_user_without_a_model_is_marked_and_reminded_once(app):
    WatchlistService().add(USER_A, "600519")
    assert create_channel(app.as_user(USER_A), "custom").status_code == 201
    day = date(2026, 11, 9)
    now = datetime(2026, 11, 9, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    shared = MagicMock(side_effect=AssertionError("no overnight work for a user who cannot be served"))

    first = runs.run_user_preopen(USER_A, shared, day=day, now=now)
    assert first["status"] == "skipped" and first["reason"] == "no_model" and first["label"] == "未配置模型"
    assert first["reminder"] == "sent" and [c.host for c in app.http.calls] == ["hooks.example.org"]
    assert UserRepository().has_notice(USER_A, NO_MODEL_NOTICE_KEY)

    app.http.reset()
    second = runs.run_user_preopen(USER_A, shared, day=day, now=now)
    assert second["reminder"] == "already_sent" and app.http.calls == []

    # nothing on the watchlist: nothing to push at all, and no reminder either
    nothing = runs.run_user_preopen(USER_B, shared, day=day, now=now)
    assert nothing == {"status": "skipped", "reason": "nothing_to_plan"}


def test_a_failed_reminder_is_retried_next_time(app):
    WatchlistService().add(USER_A, "600519")
    create_channel(app.as_user(USER_A), "custom")
    app.http.routes["hooks.example.org"] = (500, {}, b"down")
    day, now = date(2026, 11, 9), datetime(2026, 11, 9, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    first = runs.run_user_preopen(USER_A, MagicMock(), day=day, now=now)
    assert first["reminder"] == "failed" and not UserRepository().has_notice(USER_A, NO_MODEL_NOTICE_KEY)
    app.http.routes.clear()
    assert runs.run_user_preopen(USER_A, MagicMock(), day=day, now=now)["reminder"] == "sent"


# ---------------------------------------------------------------------------- news pool


class _Cfg(SimpleNamespace):
    pass


def _pool(app, *, per_user=50, total=300, holdings=None, users=None):
    config = SimpleNamespace(
        news_pool_max_codes_per_user=per_user, news_pool_max_codes_total=total, stock_list=["600519"],
        news_announcement_mode="auto", news_announcement_by_code_max=500,
    )
    return NewsPoolService(
        repo=MagicMock(), config=config, holdings_provider=lambda: [("cn", "000001")],
        other_users_provider=lambda: users if users is not None else [USER_A, USER_B, USER_C],
        user_holdings_provider=lambda uid: (holdings or {}).get(uid, []),
    )


def test_news_pool_covers_every_users_watchlist_and_holdings_without_attribution(app):
    for code in ("600036", "000858"):
        WatchlistService().add(USER_A, code)
    WatchlistService().add(USER_B, "300750")
    holdings = {USER_B: [("cn", "002594"), ("us", "AAPL")], USER_C: [("cn", "600036")]}

    codes = _pool(app, holdings=holdings).wanted_codes()

    # the primary user's codes (watchlist + holdings, as before) plus everyone else's, one sorted de-duplicated list
    assert codes == sorted({"600519", "000001", "600036", "000858", "300750", "002594"})
    assert codes == sorted(codes) and len(codes) == len(set(codes))
    # nothing in the result or in the pool's state says which user a code came from
    pool = _pool(app, holdings=holdings)
    assert isinstance(pool.wanted_codes(), list) and all(isinstance(code, str) for code in pool.wanted_codes())


def test_news_pool_extra_coverage_is_capped_per_user_and_in_total_without_touching_the_primary_users(app):
    for index in range(40):
        WatchlistService().add(USER_A, f"60{index:04d}")
    for index in range(40):
        WatchlistService().add(USER_B, f"30{index:04d}")
    primary = {"600519", "000001"}

    per_user = _pool(app, per_user=5, total=1000).wanted_codes()
    extras = set(per_user) - primary
    assert len(extras) == 10 and len([c for c in extras if c.startswith("60")]) == 5  # 5 from each of A and B

    total = _pool(app, per_user=40, total=7).wanted_codes()
    extras = set(total) - primary
    assert len(extras) == 7
    # round-robin: neither user crowds the other out of a small total
    assert len([c for c in extras if c.startswith("60")]) in (3, 4) and len([c for c in extras if c.startswith("30")]) in (3, 4)

    assert _pool(app, per_user=0).wanted_codes() == ["000001", "600519"]  # 0 switches the extra coverage off
    assert primary <= set(_pool(app, per_user=1, total=1).wanted_codes())  # the primary user's set is never trimmed


def test_news_pool_survives_a_broken_user_lookup(app):
    def broken():
        raise RuntimeError("db")

    config = SimpleNamespace(news_pool_max_codes_per_user=5, news_pool_max_codes_total=5, stock_list=["600519"])
    pool = NewsPoolService(repo=MagicMock(), config=config, holdings_provider=lambda: [], other_users_provider=broken)
    assert pool.wanted_codes() == ["600519"]


# ---------------------------------------------------------------------------- migration


def test_p3a_migration_adds_the_tables_is_idempotent_and_backs_up_an_existing_database(tmp_path, monkeypatch):
    import sqlite3

    from sqlalchemy import create_engine, text

    from src.storage_multi_user_migration import (
        MULTI_USER_P3A_SCHEMA_VERSION,
        backup_before_multi_user_migration,
        run_multi_user_migration,
    )
    from src.storage import Base

    db_path = tmp_path / "p3a.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    assert run_multi_user_migration(engine, "primary") is True
    assert run_multi_user_migration(engine, "primary") is False  # idempotent

    with engine.connect() as conn:
        tables = {name for (name,) in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"user_notification_channel", "user_notice_marker"} <= tables
        assert conn.execute(text("SELECT 1 FROM schema_migrations WHERE version = :v"), {"v": MULTI_USER_P3A_SCHEMA_VERSION}).fetchone()
        assert backup_before_multi_user_migration(engine) is None  # nothing pending: no new restore point

    # a P2-era database: tables missing and the version unrecorded -> backed up first, then upgraded without data loss
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE user_notification_channel")
        conn.exec_driver_sql("DROP TABLE user_notice_marker")
        conn.execute(text("DELETE FROM schema_migrations WHERE version = :v"), {"v": MULTI_USER_P3A_SCHEMA_VERSION})
        conn.exec_driver_sql("INSERT INTO app_user (user_id, handle, display_name, role, first_seen_at, last_seen_at) "
                             "VALUES ('keep', 'k', 'k', 'user', datetime('now'), datetime('now'))")
    backup = backup_before_multi_user_migration(engine)
    assert backup is not None and backup.is_file()
    assert sqlite3.connect(str(backup)).execute("SELECT COUNT(*) FROM app_user").fetchone()[0] == 1
    assert run_multi_user_migration(engine, "primary") is True
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM app_user").scalar() == 1
        tables = {name for (name,) in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"user_notification_channel", "user_notice_marker"} <= tables
    engine.dispose()
