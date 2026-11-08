# -*- coding: utf-8 -*-
"""Offline tests for scheduling the daily board pipeline (config, job builder, Scheduler, runtime service).

The ``schedule`` library is replaced by an in-memory fake and the pipeline itself by a recording stub:
nothing runs on a clock and no external request is made.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import src.services.board_pipeline_schedule as schedule_module
from src.config import Config
from src.services.board_pipeline_schedule import (
    BOARD_PIPELINE_JOB_NAME,
    build_board_pipeline_jobs,
    run_scheduled_board_pipeline,
)
from src.services.runtime_scheduler import RuntimeSchedulerService

MONDAY = date(2026, 11, 9)
SATURDAY = date(2026, 11, 7)


def make_config(**overrides):
    values = dict(board_pipeline_enabled=True, board_pipeline_schedule_time="17:00", tushare_token="token-for-tests")
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingPipeline:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result if result is not None else {"status": "succeeded", "run_id": 7, "steps": []}
        self.error = error

    def __call__(self, day):
        self.calls.append(day)
        if self.error is not None:
            raise self.error
        return self.result


# ----------------------------------------------------------------------
# One scheduled occurrence
# ----------------------------------------------------------------------
def test_trading_day_with_token_runs_the_pipeline_for_that_day():
    pipeline = RecordingPipeline()
    result = run_scheduled_board_pipeline(
        lambda: make_config(), today=MONDAY, is_trading_day=lambda d: True, pipeline=pipeline
    )
    assert pipeline.calls == [MONDAY] and result["status"] == "succeeded"


def test_non_trading_day_is_skipped_without_running_anything():
    pipeline = RecordingPipeline()
    result = run_scheduled_board_pipeline(
        lambda: make_config(), today=SATURDAY, is_trading_day=lambda d: d.weekday() < 5, pipeline=pipeline
    )
    assert result == {"status": "skipped", "reason": "not_trading_day", "trade_date": "2026-11-07"}
    assert pipeline.calls == []


def test_real_calendar_treats_a_saturday_as_closed():
    pytest.importorskip("exchange_calendars")
    pipeline = RecordingPipeline()
    result = run_scheduled_board_pipeline(lambda: make_config(), today=SATURDAY, pipeline=pipeline)
    assert result["reason"] == "not_trading_day" and pipeline.calls == []


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_tushare_token_skips_with_a_log_line(token, caplog):
    pipeline = RecordingPipeline()
    with caplog.at_level(logging.INFO, logger=schedule_module.logger.name):
        result = run_scheduled_board_pipeline(
            lambda: make_config(tushare_token=token), today=MONDAY, is_trading_day=lambda d: True, pipeline=pipeline
        )
    assert result["status"] == "skipped" and result["reason"] == "no_tushare_token"
    assert pipeline.calls == []
    assert any("TUSHARE_TOKEN" in r.getMessage() for r in caplog.records)
    assert "token-for-tests" not in caplog.text


def test_disabled_switch_skips():
    pipeline = RecordingPipeline()
    result = run_scheduled_board_pipeline(
        lambda: make_config(board_pipeline_enabled=False), today=MONDAY, is_trading_day=lambda d: True,
        pipeline=pipeline,
    )
    assert result["reason"] == "disabled" and pipeline.calls == []


def test_partial_run_is_a_warning_and_failures_never_raise(caplog):
    partial = RecordingPipeline(result={
        "status": "partial", "run_id": 8,
        "steps": [{"key": "membership", "status": "partial"}, {"key": "signals", "status": "succeeded"}],
    })
    with caplog.at_level(logging.WARNING, logger=schedule_module.logger.name):
        result = run_scheduled_board_pipeline(
            lambda: make_config(), today=MONDAY, is_trading_day=lambda d: True, pipeline=partial
        )
    assert result["status"] == "partial"
    warning = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning and "membership:partial" in warning[0].getMessage()

    failed = run_scheduled_board_pipeline(
        lambda: make_config(), today=MONDAY, is_trading_day=lambda d: True,
        pipeline=RecordingPipeline(result={"status": "failed", "run_id": 9, "error": "没有行情数据"}),
    )
    assert failed["status"] == "failed"

    crashed = run_scheduled_board_pipeline(
        lambda: make_config(), today=MONDAY, is_trading_day=lambda d: True,
        pipeline=RecordingPipeline(error=RuntimeError("db is gone")),
    )
    assert crashed["status"] == "failed" and "db is gone" in crashed["error"]

    def broken_provider():
        raise RuntimeError("config unreadable")

    assert run_scheduled_board_pipeline(broken_provider)["status"] == "failed"


def test_overlapping_occurrences_do_not_run_twice():
    release = threading.Event()
    entered = threading.Event()

    def slow_pipeline(day):
        entered.set()
        release.wait(timeout=5)
        return {"status": "succeeded", "run_id": 1, "steps": []}

    worker = threading.Thread(target=lambda: run_scheduled_board_pipeline(
        lambda: make_config(), today=MONDAY, is_trading_day=lambda d: True, pipeline=slow_pipeline))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        second = run_scheduled_board_pipeline(
            lambda: make_config(), today=MONDAY, is_trading_day=lambda d: True, pipeline=RecordingPipeline()
        )
        assert second["status"] == "skipped" and second["reason"] == "already_running"
    finally:
        release.set()
        worker.join(timeout=5)


# ----------------------------------------------------------------------
# Job builder and config keys
# ----------------------------------------------------------------------
def test_job_builder_defaults_to_17_00_and_reads_the_latest_time():
    config = make_config()
    jobs = build_board_pipeline_jobs(config, config_provider=lambda: config)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["name"] == BOARD_PIPELINE_JOB_NAME and job["schedule_time"] == "17:00"
    config.board_pipeline_schedule_time = "17:30"
    assert job["schedule_time_provider"]() == "17:30"


def test_job_builder_registers_nothing_when_disabled_and_keeps_the_job_without_a_token(caplog):
    off = make_config(board_pipeline_enabled=False)
    assert build_board_pipeline_jobs(off, config_provider=lambda: off) == []
    # a missing token does not unregister the job: the token is re-read on every run
    no_token = make_config(tushare_token=None)
    with caplog.at_level(logging.INFO, logger=schedule_module.logger.name):
        jobs = build_board_pipeline_jobs(no_token, config_provider=lambda: no_token)
    assert len(jobs) == 1 and "TUSHARE_TOKEN" in caplog.text
    # configs without the keys at all (older test doubles) register nothing
    assert build_board_pipeline_jobs(SimpleNamespace(), config_provider=lambda: SimpleNamespace()) == []


def test_job_task_runs_one_scheduled_occurrence(monkeypatch):
    seen = []
    monkeypatch.setattr(schedule_module, "run_scheduled_board_pipeline", lambda provider: seen.append(provider()))
    config = make_config()
    [job] = build_board_pipeline_jobs(config, config_provider=lambda: config)
    job["task"]()
    assert seen == [config]


class ConfigKeysTestCase(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    def _load(self, env):
        with patch("src.config.setup_env"), patch.object(Config, "_parse_litellm_yaml", return_value=[]), \
                patch.object(Config, "_parse_stock_email_groups", return_value=[]), \
                patch.dict(os.environ, env, clear=True):
            return Config._load_from_env()

    def test_defaults_enable_the_job_at_17_00(self):
        config = self._load({})
        self.assertTrue(config.board_pipeline_enabled)
        self.assertEqual(config.board_pipeline_schedule_time, "17:00")
        self.assertEqual(config.schedule_time, "18:00")  # the analysis schedule is untouched

    def test_env_overrides_and_invalid_time_falls_back(self):
        config = self._load({"BOARD_PIPELINE_ENABLED": "false", "BOARD_PIPELINE_SCHEDULE_TIME": "17:45"})
        self.assertFalse(config.board_pipeline_enabled)
        self.assertEqual(config.board_pipeline_schedule_time, "17:45")
        invalid = self._load({"BOARD_PIPELINE_SCHEDULE_TIME": "25:99"})
        self.assertEqual(invalid.board_pipeline_schedule_time, "17:00")
        blank = self._load({"BOARD_PIPELINE_SCHEDULE_TIME": "  "})
        self.assertEqual(blank.board_pipeline_schedule_time, "17:00")


# ----------------------------------------------------------------------
# Scheduler.add_daily_job
# ----------------------------------------------------------------------
class _FakeJob:
    def __init__(self, module):
        self.module = module
        self.at_time = None
        self.job_func = None
        self.next_run = datetime(2026, 1, 1, 18, 0, 0)

    @property
    def day(self):
        return self

    def at(self, value):
        self.at_time = value
        hour, minute = [int(part) for part in value.split(":")]
        self.next_run = datetime(2026, 1, 1, hour, minute, 0)
        return self

    def do(self, fn):
        self.job_func = fn
        self.module.jobs.append(self)
        return self


class _FakeScheduleModule:
    def __init__(self):
        self.jobs = []

    def every(self):
        return _FakeJob(self)

    def get_jobs(self):
        return list(self.jobs)

    def run_pending(self):
        return None

    def cancel_job(self, job):
        if job in self.jobs:
            self.jobs.remove(job)


@pytest.fixture()
def fake_schedule():
    module = _FakeScheduleModule()
    with patch.dict(sys.modules, {"schedule": module}):
        yield module


def _new_scheduler():
    from src.scheduler import Scheduler

    return Scheduler(schedule_time="18:00", register_signals=False)


def test_add_daily_job_registers_its_own_time_next_to_the_main_task(fake_schedule):
    scheduler = _new_scheduler()
    scheduler.set_daily_task(lambda: None, run_immediately=False)
    assert scheduler.add_daily_job(lambda: None, "17:00", name="board_pipeline") is True
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["17:00", "18:00"]
    assert scheduler.schedule_times == ["18:00"]  # the analysis schedule is not touched


def test_add_daily_job_rejects_invalid_times(fake_schedule):
    scheduler = _new_scheduler()
    assert scheduler.add_daily_job(lambda: None, "25:00", name="x") is False
    assert scheduler.add_daily_job(lambda: None, "", name="x") is False
    assert fake_schedule.get_jobs() == []


def test_daily_job_runs_off_the_scheduler_thread_and_failures_are_contained(fake_schedule):
    scheduler = _new_scheduler()
    done = threading.Event()
    threads = []

    def task():
        threads.append(threading.current_thread())
        done.set()
        raise RuntimeError("boom")  # must not escape into the scheduler loop

    scheduler.add_daily_job(task, "17:00", name="board_pipeline")
    [job] = fake_schedule.get_jobs()
    job.job_func()  # what ``run_pending`` does at 17:00
    assert done.wait(timeout=5)
    assert threads[0] is not threading.current_thread() and threads[0].name == "scheduler-daily-board_pipeline"
    entry = scheduler._extra_daily_jobs[0]
    entry["thread"].join(timeout=5)


def test_daily_job_occurrence_is_skipped_while_the_previous_one_still_runs(fake_schedule):
    scheduler = _new_scheduler()
    release = threading.Event()
    started = threading.Event()
    runs = []

    def task():
        runs.append(1)
        started.set()
        release.wait(timeout=5)

    scheduler.add_daily_job(task, "17:00", name="board_pipeline")
    [job] = fake_schedule.get_jobs()
    job.job_func()
    assert started.wait(timeout=5)
    job.job_func()  # next occurrence while the first is still busy
    release.set()
    scheduler._extra_daily_jobs[0]["thread"].join(timeout=5)
    assert runs == [1]


def test_daily_job_follows_a_changed_time_and_stop_cancels_it(fake_schedule):
    scheduler = _new_scheduler()
    current = {"time": "17:00"}
    scheduler.add_daily_job(lambda: None, "17:00", name="board_pipeline", schedule_time_provider=lambda: current["time"])
    scheduler.set_daily_task(lambda: None, run_immediately=False)

    scheduler._refresh_extra_daily_jobs_if_needed()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["17:00", "18:00"]  # unchanged: no churn

    current["time"] = "17:30"
    scheduler._refresh_extra_daily_jobs_if_needed()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["17:30", "18:00"]

    current["time"] = "99:99"  # invalid: keep the last good time
    scheduler._refresh_extra_daily_jobs_if_needed()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["17:30", "18:00"]

    scheduler.stop()
    assert fake_schedule.get_jobs() == []


def test_run_with_schedule_registers_extra_daily_jobs(fake_schedule):
    from src import scheduler as scheduler_module

    captured = {}

    class StopImmediately(scheduler_module.Scheduler):
        def run(self):
            captured["times"] = sorted(job.at_time for job in fake_schedule.get_jobs())

    with patch.object(scheduler_module, "Scheduler", StopImmediately):
        scheduler_module.run_with_schedule(
            lambda: None, schedule_time="18:00", run_immediately=False,
            extra_daily_jobs=[{"task": lambda: None, "schedule_time": "17:00", "name": "board_pipeline"}],
        )
    assert captured["times"] == ["17:00", "18:00"]


# ----------------------------------------------------------------------
# RuntimeSchedulerService (Web / API / Desktop owner)
# ----------------------------------------------------------------------
class _NoopThread:
    def __init__(self, target=None, **kwargs):
        self.target = target

    def start(self):
        return None

    def is_alive(self):
        return False


def _runtime_service(config):
    service = RuntimeSchedulerService(config_provider=lambda: config, task_runner=lambda *a: None)
    service._reload_config = lambda: config
    return service


def test_runtime_scheduler_registers_the_board_job_at_17_00_without_touching_analysis(fake_schedule):
    config = SimpleNamespace(
        schedule_enabled=True, schedule_time="18:00", schedule_times=["09:20"],
        board_pipeline_enabled=True, board_pipeline_schedule_time="17:00", tushare_token="token-for-tests",
    )
    service = _runtime_service(config)
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["09:20", "17:00"]

    # a reconcile (settings saved) rebuilds the schedule: no duplicate board job is left behind
    config.board_pipeline_schedule_time = "17:15"
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["09:20", "17:15"]

    config.board_pipeline_enabled = False
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert [job.at_time for job in fake_schedule.get_jobs()] == ["09:20"]

    config.schedule_enabled = False  # no scheduler, no board job
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert fake_schedule.get_jobs() == []


def test_runtime_scheduler_board_job_failure_to_build_does_not_block_analysis(fake_schedule):
    config = SimpleNamespace(schedule_enabled=True, schedule_time="18:00", schedule_times=["09:20"])
    service = _runtime_service(config)
    with patch("src.services.board_pipeline_schedule.build_board_pipeline_jobs", side_effect=RuntimeError("boom")), \
            patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert [job.at_time for job in fake_schedule.get_jobs()] == ["09:20"]

# ----------------------------------------------------------------------
# Weekly industry-board membership job
# ----------------------------------------------------------------------
INDUSTRY_JOB = schedule_module.INDUSTRY_MEMBERSHIP_JOB_NAME


def industry_config(**overrides):
    values = dict(
        board_pipeline_enabled=True, board_pipeline_schedule_time="17:00", tushare_token="token-for-tests",
        board_industry_membership_enabled=True, board_industry_membership_weekday=6,
        board_industry_membership_time="10:00",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingRefresher:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result if result is not None else {
            "status": "succeeded", "trade_date": "2026-11-06", "detail": "行业板块成分 ok", "result": {}}
        self.error = error

    def __call__(self, *, today):
        self.calls.append(today)
        if self.error is not None:
            raise self.error
        return self.result


def test_weekly_job_runs_on_the_configured_weekday_regardless_of_trading_days():
    refresher = RecordingRefresher()
    result = schedule_module.run_scheduled_industry_membership(
        lambda: industry_config(), today=SATURDAY, refresher=refresher)  # a Saturday is never a trading day
    assert result["status"] == "succeeded" and refresher.calls == [SATURDAY]


def test_weekly_job_skips_every_other_weekday_without_calling_the_refresher():
    refresher = RecordingRefresher()
    for offset in range(7):
        day = date(2026, 11, 2 + offset)  # Mon .. Sun
        result = schedule_module.run_scheduled_industry_membership(
            lambda: industry_config(), today=day, refresher=refresher)
        if day == SATURDAY:
            assert result["status"] == "succeeded"
        else:
            assert result == {"status": "skipped", "reason": "not_scheduled_weekday", "run_date": day.isoformat()}
    assert refresher.calls == [SATURDAY]


def test_weekly_job_weekday_is_iso_and_read_from_the_live_config():
    refresher = RecordingRefresher()
    run = schedule_module.run_scheduled_industry_membership
    assert run(lambda: industry_config(board_industry_membership_weekday=1), today=MONDAY, refresher=refresher)["status"] == "succeeded"
    assert run(lambda: industry_config(board_industry_membership_weekday=7), today=date(2026, 11, 8), refresher=refresher)["status"] == "succeeded"
    assert run(lambda: industry_config(board_industry_membership_weekday=7), today=SATURDAY, refresher=refresher)["status"] == "skipped"
    assert refresher.calls == [MONDAY, date(2026, 11, 8)]


def test_weekly_job_skips_with_a_log_when_the_token_is_missing(caplog):
    refresher = RecordingRefresher()
    with caplog.at_level(logging.INFO, logger=schedule_module.logger.name):
        for token in (None, "", "   "):
            result = schedule_module.run_scheduled_industry_membership(
                lambda: industry_config(tushare_token=token), today=SATURDAY, refresher=refresher)
            assert result["status"] == "skipped" and result["reason"] == "no_tushare_token"
    assert "TUSHARE_TOKEN" in caplog.text and refresher.calls == []


def test_weekly_job_skips_when_disabled_by_either_switch():
    refresher = RecordingRefresher()
    run = schedule_module.run_scheduled_industry_membership
    assert run(lambda: industry_config(board_industry_membership_enabled=False), today=SATURDAY, refresher=refresher)["reason"] == "disabled"
    assert run(lambda: industry_config(board_pipeline_enabled=False), today=SATURDAY, refresher=refresher)["reason"] == "disabled"
    assert refresher.calls == []


def test_weekly_job_reports_an_already_folded_day_as_skipped():
    already_done = {"status": "skipped", "trade_date": "2026-11-06", "detail": "2026-11-06 行业板块该日成分已折叠入拉链表", "result": {}}
    refresher = RecordingRefresher(result=already_done)
    result = schedule_module.run_scheduled_industry_membership(lambda: industry_config(), today=SATURDAY, refresher=refresher)
    assert result["status"] == "skipped" and "已折叠" in result["detail"]


def test_weekly_job_contains_failures_and_overlapping_runs():
    boom = schedule_module.run_scheduled_industry_membership(
        lambda: industry_config(), today=SATURDAY, refresher=RecordingRefresher(error=RuntimeError("boom")))
    assert boom["status"] == "failed" and "boom" in boom["error"]

    entered, release = threading.Event(), threading.Event()

    def slow(*, today):
        entered.set()
        release.wait(timeout=5)
        return {"status": "succeeded", "trade_date": "2026-11-06", "detail": "ok", "result": {}}

    worker = threading.Thread(target=lambda: schedule_module.run_scheduled_industry_membership(
        lambda: industry_config(), today=SATURDAY, refresher=slow))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        second = schedule_module.run_scheduled_industry_membership(
            lambda: industry_config(), today=SATURDAY, refresher=RecordingRefresher())
        assert second["status"] == "skipped" and second["reason"] == "already_running"
    finally:
        release.set()
        worker.join(timeout=5)


def test_industry_job_builder_defaults_to_10_00_and_follows_the_live_time():
    config = industry_config()
    [job] = schedule_module.build_industry_membership_jobs(config, config_provider=lambda: config)
    assert job["name"] == INDUSTRY_JOB and job["schedule_time"] == "10:00"
    config.board_industry_membership_time = "09:30"
    assert job["schedule_time_provider"]() == "09:30"
    config.board_industry_membership_time = ""
    assert job["schedule_time_provider"]() == "10:00"


def test_industry_job_builder_registers_only_when_both_switches_are_on(caplog):
    for overrides in ({"board_pipeline_enabled": False}, {"board_industry_membership_enabled": False}):
        off = industry_config(**overrides)
        assert schedule_module.build_industry_membership_jobs(off, config_provider=lambda: off) == []
    bare = SimpleNamespace()
    assert schedule_module.build_industry_membership_jobs(bare, config_provider=lambda: bare) == []
    no_token = industry_config(tushare_token=None)  # still registered: the token is re-read on every firing
    with caplog.at_level(logging.INFO, logger=schedule_module.logger.name):
        assert len(schedule_module.build_industry_membership_jobs(no_token, config_provider=lambda: no_token)) == 1
    assert "TUSHARE_TOKEN" in caplog.text


def test_industry_job_task_runs_one_scheduled_occurrence(monkeypatch):
    seen = []
    monkeypatch.setattr(schedule_module, "run_scheduled_industry_membership", lambda provider: seen.append(provider()))
    config = industry_config()
    [job] = schedule_module.build_industry_membership_jobs(config, config_provider=lambda: config)
    job["task"]()
    assert seen == [config]


class IndustryConfigKeysTestCase(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    def _load(self, env):
        with patch("src.config.setup_env"), patch.object(Config, "_parse_litellm_yaml", return_value=[]), \
                patch.object(Config, "_parse_stock_email_groups", return_value=[]), \
                patch.dict(os.environ, env, clear=True):
            return Config._load_from_env()

    def test_defaults(self):
        config = self._load({})
        self.assertTrue(config.board_industry_membership_enabled)
        self.assertEqual(config.board_industry_membership_weekday, 6)  # Saturday (ISO)
        self.assertEqual(config.board_industry_membership_time, "10:00")
        self.assertFalse(config.board_membership_full_day_fallback)  # the all-boards query is opt-in
        self.assertEqual(config.market_snapshot_members_requests_per_minute, 200)
        self.assertEqual(config.market_snapshot_requests_per_minute, 60)  # other ingestion stays conservative

    def test_env_overrides(self):
        config = self._load({
            "BOARD_INDUSTRY_MEMBERSHIP_ENABLED": "false", "BOARD_INDUSTRY_MEMBERSHIP_WEEKDAY": "7",
            "BOARD_INDUSTRY_MEMBERSHIP_TIME": "09:15", "BOARD_MEMBERSHIP_FULL_DAY_FALLBACK": "true",
            "MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE": "120",
        })
        self.assertFalse(config.board_industry_membership_enabled)
        self.assertEqual(config.board_industry_membership_weekday, 7)
        self.assertEqual(config.board_industry_membership_time, "09:15")
        self.assertTrue(config.board_membership_full_day_fallback)
        self.assertEqual(config.market_snapshot_members_requests_per_minute, 120)

    def test_invalid_values_fall_back_to_defaults(self):
        for bad in ("0", "8", "-1", "sat", "6.5"):
            self.assertEqual(self._load({"BOARD_INDUSTRY_MEMBERSHIP_WEEKDAY": bad}).board_industry_membership_weekday, 6, bad)
        self.assertEqual(self._load({"BOARD_INDUSTRY_MEMBERSHIP_TIME": "25:99"}).board_industry_membership_time, "10:00")
        self.assertEqual(self._load({"MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE": "0"}).market_snapshot_members_requests_per_minute, 1)
        self.assertEqual(self._load({"MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE": "x"}).market_snapshot_members_requests_per_minute, 200)


def test_runtime_scheduler_registers_the_weekly_job_next_to_the_board_job(fake_schedule):
    config = SimpleNamespace(
        schedule_enabled=True, schedule_time="18:00", schedule_times=["09:20"],
        board_pipeline_enabled=True, board_pipeline_schedule_time="17:00", tushare_token="token-for-tests",
        board_industry_membership_enabled=True, board_industry_membership_weekday=6,
        board_industry_membership_time="10:00",
    )
    service = _runtime_service(config)
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["09:20", "10:00", "17:00"]

    config.board_industry_membership_time = "09:45"  # a saved setting rebuilds the schedule, no duplicate left behind
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["09:20", "09:45", "17:00"]

    config.board_industry_membership_enabled = False
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["09:20", "17:00"]


def test_runtime_scheduler_weekly_job_build_failure_does_not_block_other_jobs(fake_schedule):
    config = SimpleNamespace(
        schedule_enabled=True, schedule_time="18:00", schedule_times=["09:20"],
        board_pipeline_enabled=True, board_pipeline_schedule_time="17:00", tushare_token="token-for-tests",
        board_industry_membership_enabled=True,
    )
    service = _runtime_service(config)
    with patch("src.services.board_pipeline_schedule.build_industry_membership_jobs", side_effect=RuntimeError("boom")), \
            patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["09:20", "17:00"]
