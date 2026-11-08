# -*- coding: utf-8 -*-
"""Offline tests for scheduling the news pool: config keys, registry, job builders, run gating, both scheduler owners."""

from __future__ import annotations

import os
import sys
import threading
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import src.services.news_schedule as schedule_module
from src.config import Config
from src.core.config_registry import WEB_SETTINGS_HIDDEN_FROM_UI, get_field_definition
from src.services.news_schedule import (
    NEWS_CLASSIFY_TASK_NAME,
    NEWS_DAILY_JOB_NAME,
    NEWS_POLL_TASK_NAME,
    build_news_background_tasks,
    build_news_daily_jobs,
    run_scheduled_news_classify,
    run_scheduled_news_daily,
    run_scheduled_news_poll,
)
from src.services.runtime_scheduler import RuntimeSchedulerService
from tests.news_fixtures import make_config

MONDAY = date(2026, 11, 9)
SATURDAY = date(2026, 11, 7)


class RecordingService:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or {"status": "ok"}
        self.error = error

    def poll_tick(self):
        self.calls.append("poll_tick")
        if self.error:
            raise self.error
        return self.result

    def run_daily_fetch(self, day=None):
        self.calls.append(("daily", day))
        return self.result

    def classify_pending(self):
        self.calls.append("classify")
        return self.result


# ----------------------------------------------------------------------
# config keys
# ----------------------------------------------------------------------
class ConfigKeysTestCase(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    def _load(self, env):
        with patch("src.config.setup_env"), patch.object(Config, "_parse_litellm_yaml", return_value=[]), \
                patch.object(Config, "_parse_stock_email_groups", return_value=[]), \
                patch.dict(os.environ, env, clear=True):
            return Config._load_from_env()

    def test_defaults_match_the_design(self):
        config = self._load({})
        self.assertFalse(config.news_intel_auto_fetch_enabled)  # the existing switch stays opt-in
        self.assertEqual(config.news_poll_interval_minutes, 10)
        self.assertEqual(config.news_poll_offhours_interval_minutes, 30)
        self.assertEqual(config.news_flash_retention_days, 3)
        self.assertEqual(config.news_company_retention_days, 30)
        self.assertEqual(config.news_intel_retention_days, 30)  # legacy key untouched
        self.assertEqual(config.news_daily_fetch_time, "08:05")
        self.assertEqual(config.news_akshare_cls_symbol, "全部")
        self.assertEqual(config.news_announcement_mode, "auto")
        self.assertEqual(config.news_board_driver_min_importance, 4)
        self.assertTrue(config.news_classify_enabled)
        self.assertEqual(config.litellm_light_model, "")

    def test_env_overrides_and_invalid_values_fall_back(self):
        config = self._load({
            "NEWS_INTEL_AUTO_FETCH_ENABLED": "true", "NEWS_POLL_INTERVAL_MINUTES": "5",
            "NEWS_POLL_OFFHOURS_INTERVAL_MINUTES": "60", "NEWS_FLASH_RETENTION_DAYS": "2",
            "NEWS_COMPANY_RETENTION_DAYS": "45", "NEWS_DAILY_FETCH_TIME": "07:55", "NEWS_AKSHARE_CLS_SYMBOL": "重点",
            "NEWS_ANNOUNCEMENT_MODE": "market", "NEWS_CLASSIFY_BATCH_SIZE": "10", "NEWS_CLASSIFY_ENABLED": "false",
            "NEWS_BOARD_DRIVER_MIN_IMPORTANCE": "5",
        })
        self.assertTrue(config.news_intel_auto_fetch_enabled)
        self.assertEqual((config.news_poll_interval_minutes, config.news_poll_offhours_interval_minutes), (5, 60))
        self.assertEqual((config.news_flash_retention_days, config.news_company_retention_days), (2, 45))
        self.assertEqual((config.news_daily_fetch_time, config.news_akshare_cls_symbol), ("07:55", "重点"))
        self.assertEqual((config.news_announcement_mode, config.news_classify_batch_size), ("market", 10))
        self.assertFalse(config.news_classify_enabled)
        self.assertEqual(config.news_board_driver_min_importance, 5)
        bad = self._load({
            "NEWS_DAILY_FETCH_TIME": "99:99", "NEWS_AKSHARE_CLS_SYMBOL": "everything", "NEWS_ANNOUNCEMENT_MODE": "x",
            "NEWS_POLL_INTERVAL_MINUTES": "0", "NEWS_FLASH_RETENTION_DAYS": "abc",
        })
        self.assertEqual((bad.news_daily_fetch_time, bad.news_akshare_cls_symbol, bad.news_announcement_mode),
                         ("08:05", "全部", "auto"))
        self.assertEqual((bad.news_poll_interval_minutes, bad.news_flash_retention_days), (1, 3))  # clamped to the minimum / invalid -> default

    def test_light_model_env_uses_the_litellm_model_value_format(self):
        self.assertEqual(self._load({"LITELLM_LIGHT_MODEL": "deepseek/deepseek-v4-flash"}).litellm_light_model,
                         "deepseek/deepseek-v4-flash")
        self.assertEqual(self._load({"LITELLM_LIGHT_MODEL": "  "}).litellm_light_model, "")
        # a bare name is normalised like AGENT_LITELLM_MODEL
        self.assertEqual(self._load({"LITELLM_LIGHT_MODEL": "gpt-4o-mini"}).litellm_light_model, "openai/gpt-4o-mini")

    def test_channel_model_names_are_kept_as_declared(self):
        channels = {
            "LLM_CHANNELS": "relay", "LLM_RELAY_PROTOCOL": "openai", "LLM_RELAY_BASE_URL": "https://relay.example.test/v1",
            "LLM_RELAY_API_KEY": "k", "LLM_RELAY_MODELS": "cheap-flash", "LITELLM_LIGHT_MODEL": "openai/cheap-flash",
            "LITELLM_MODEL": "openai/cheap-flash",
        }
        with patch("src.config.setup_env"), patch.object(Config, "_parse_stock_email_groups", return_value=[]), \
                patch.dict(os.environ, channels, clear=True):
            config = Config._load_from_env()
        self.assertEqual(config.litellm_light_model, "openai/cheap-flash")


def test_light_model_is_registered_visibly_like_agent_model_and_news_ops_keys_are_hidden():
    field = get_field_definition("LITELLM_LIGHT_MODEL")
    agent = get_field_definition("AGENT_LITELLM_MODEL")
    assert field["category"] == agent["category"] == "ai_model"
    assert field["help_key"] == "settings.ai_model.LITELLM_LIGHT_MODEL"
    assert field["ui_control"] == "text" and field["is_editable"] is True and field["is_sensitive"] is False
    assert field["examples"] and field["docs"] and "LITELLM_LIGHT_MODEL" not in WEB_SETTINGS_HIDDEN_FROM_UI
    assert {
        "NEWS_POLL_INTERVAL_MINUTES", "NEWS_POLL_OFFHOURS_INTERVAL_MINUTES", "NEWS_FLASH_RETENTION_DAYS",
        "NEWS_COMPANY_RETENTION_DAYS", "NEWS_DAILY_FETCH_TIME", "NEWS_CLASSIFY_ENABLED", "NEWS_BOARD_DRIVER_MIN_IMPORTANCE",
        "NEWS_CLASSIFY_MAX_ATTEMPTS", "NEWS_ANNOUNCEMENT_MODE",
    } <= WEB_SETTINGS_HIDDEN_FROM_UI


def test_light_model_help_exists_in_both_languages():
    text = open("apps/dsa-web/src/locales/settingsHelp.ts", encoding="utf-8").read()
    assert text.count("'settings.ai_model.LITELLM_LIGHT_MODEL': {") == 2  # zh + en


def test_documented_in_env_example_and_both_full_guides():
    keys = [
        "LITELLM_LIGHT_MODEL", "NEWS_POLL_INTERVAL_MINUTES", "NEWS_POLL_OFFHOURS_INTERVAL_MINUTES", "NEWS_DAILY_FETCH_TIME",
        "NEWS_FLASH_RETENTION_DAYS", "NEWS_COMPANY_RETENTION_DAYS", "NEWS_AKSHARE_CLS_SYMBOL", "NEWS_DEDUP_WINDOW_MINUTES",
        "NEWS_FETCH_TIMEOUT_SEC", "NEWS_ANNOUNCEMENT_MODE", "NEWS_ANNOUNCEMENT_BY_CODE_MAX", "NEWS_CLASSIFY_ENABLED",
        "NEWS_CLASSIFY_BATCH_SIZE", "NEWS_CLASSIFY_MAX_ITEMS_PER_RUN", "NEWS_CLASSIFY_MAX_ATTEMPTS",
        "NEWS_BOARD_DRIVER_MIN_IMPORTANCE",
    ]
    for path in (".env.example", "docs/full-guide.md", "docs/full-guide_EN.md"):
        content = open(path, encoding="utf-8").read()
        assert [k for k in keys if k not in content] == [], path


# ----------------------------------------------------------------------
# builders
# ----------------------------------------------------------------------
def test_nothing_is_registered_while_the_switch_is_off():
    off = make_config(news_intel_auto_fetch_enabled=False)
    assert build_news_background_tasks(off, config_provider=lambda: off) == []
    assert build_news_daily_jobs(off, config_provider=lambda: off) == []
    assert build_news_background_tasks(SimpleNamespace(), config_provider=lambda: SimpleNamespace()) == []
    flaky = SimpleNamespace(news_intel_auto_fetch_enabled="yes please")  # only a real True counts
    assert build_news_background_tasks(flaky, config_provider=lambda: flaky) == []


def test_poll_task_ticks_at_the_shorter_interval_and_classification_has_its_own_task():
    config = make_config()
    tasks = {t["name"]: t for t in build_news_background_tasks(config, config_provider=lambda: config)}
    assert set(tasks) == {NEWS_POLL_TASK_NAME, NEWS_CLASSIFY_TASK_NAME}
    assert tasks[NEWS_POLL_TASK_NAME]["interval_seconds"] == 600 and tasks[NEWS_POLL_TASK_NAME]["run_immediately"] is True
    assert tasks[NEWS_CLASSIFY_TASK_NAME]["interval_seconds"] == 600

    config = make_config(news_poll_interval_minutes=15, news_poll_offhours_interval_minutes=5, news_classify_enabled=False)
    tasks = {t["name"]: t for t in build_news_background_tasks(config, config_provider=lambda: config)}
    assert set(tasks) == {NEWS_POLL_TASK_NAME} and tasks[NEWS_POLL_TASK_NAME]["interval_seconds"] == 300


def test_daily_job_runs_at_the_configured_time_and_follows_changes():
    config = make_config(news_daily_fetch_time="07:50")
    [job] = build_news_daily_jobs(config, config_provider=lambda: config)
    assert job["name"] == NEWS_DAILY_JOB_NAME and job["schedule_time"] == "07:50"
    config.news_daily_fetch_time = "08:10"
    assert job["schedule_time_provider"]() == "08:10"
    config.news_daily_fetch_time = ""
    assert job["schedule_time_provider"]() == "08:05"


def test_task_callables_run_one_occurrence_through_the_latest_config(monkeypatch):
    seen = []
    monkeypatch.setattr(schedule_module, "run_scheduled_news_poll", lambda provider: seen.append(("poll", provider())))
    monkeypatch.setattr(schedule_module, "run_scheduled_news_classify", lambda provider: seen.append(("classify", provider())))
    monkeypatch.setattr(schedule_module, "run_scheduled_news_daily", lambda provider: seen.append(("daily", provider())))
    config = make_config()
    for task in build_news_background_tasks(config, config_provider=lambda: config):
        task["task"]()
    build_news_daily_jobs(config, config_provider=lambda: config)[0]["task"]()
    assert seen == [("poll", config), ("classify", config), ("daily", config)]


# ----------------------------------------------------------------------
# one occurrence
# ----------------------------------------------------------------------
def test_poll_occurrence_delegates_to_the_interval_gate_and_never_raises():
    service = RecordingService()
    assert run_scheduled_news_poll(lambda: make_config(), service=service) == {"status": "ok"}
    assert service.calls == ["poll_tick"]
    off = RecordingService()
    assert run_scheduled_news_poll(lambda: make_config(news_intel_auto_fetch_enabled=False), service=off)["reason"] == "disabled"
    assert off.calls == []
    crashed = run_scheduled_news_poll(lambda: make_config(), service=RecordingService(error=RuntimeError("db gone")))
    assert crashed["status"] == "failed" and "db gone" in crashed["error"]

    def broken_provider():
        raise RuntimeError("config unreadable")

    assert run_scheduled_news_poll(broken_provider)["status"] == "failed"


def test_classify_occurrence_respects_both_switches():
    service = RecordingService()
    assert run_scheduled_news_classify(lambda: make_config(), classifier=service)["status"] == "ok"
    assert service.calls == ["classify"]
    for override in ({"news_intel_auto_fetch_enabled": False}, {"news_classify_enabled": False}):
        silent = RecordingService()
        assert run_scheduled_news_classify(lambda: make_config(**override), classifier=silent)["status"] == "skipped"
        assert silent.calls == []


def test_daily_occurrence_skips_non_trading_days_and_runs_for_the_day_otherwise():
    service = RecordingService()
    assert run_scheduled_news_daily(lambda: make_config(), today=MONDAY, is_trading_day=lambda d: True, service=service)["status"] == "ok"
    assert service.calls == [("daily", MONDAY)]
    closed = RecordingService()
    result = run_scheduled_news_daily(lambda: make_config(), today=SATURDAY, is_trading_day=lambda d: d.weekday() < 5, service=closed)
    assert result == {"status": "skipped", "reason": "not_trading_day", "day": "2026-11-07"} and closed.calls == []
    off = RecordingService()
    assert run_scheduled_news_daily(lambda: make_config(news_intel_auto_fetch_enabled=False), today=MONDAY, service=off)["reason"] == "disabled"


def test_real_calendar_treats_a_saturday_as_closed_for_the_daily_job():
    service = RecordingService()
    result = run_scheduled_news_daily(lambda: make_config(), today=SATURDAY, service=service)
    assert result["reason"] == "not_trading_day" and service.calls == []


# ----------------------------------------------------------------------
# scheduler owners
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


def _scheduler_config(**overrides):
    values = dict(schedule_enabled=True, schedule_time="18:00", schedule_times=["09:20"], agent_event_monitor_enabled=False)
    values.update(overrides)
    return make_config(**values)


def test_runtime_scheduler_registers_news_tasks_and_the_daily_job_and_rebuilds_on_reconcile(fake_schedule):
    config = _scheduler_config()
    service = _runtime_service(config)
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    scheduler = service._scheduler
    background = {entry["name"]: entry["interval_seconds"] for entry in scheduler._background_tasks}
    assert background == {NEWS_POLL_TASK_NAME: 600, NEWS_CLASSIFY_TASK_NAME: 600}
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["08:05", "09:20"]  # analysis time untouched

    config.news_poll_interval_minutes = 20  # settings saved -> reconcile rebuilds without duplicates
    config.news_daily_fetch_time = "08:10"
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert {e["name"]: e["interval_seconds"] for e in service._scheduler._background_tasks}[NEWS_POLL_TASK_NAME] == 1200
    assert sorted(job.at_time for job in fake_schedule.get_jobs()) == ["08:10", "09:20"]

    config.news_intel_auto_fetch_enabled = False
    with patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert service._scheduler._background_tasks == []
    assert [job.at_time for job in fake_schedule.get_jobs()] == ["09:20"]


def test_runtime_scheduler_news_failure_to_build_does_not_block_analysis(fake_schedule):
    config = _scheduler_config()
    service = _runtime_service(config)
    with patch("src.services.news_schedule.build_news_background_tasks", side_effect=RuntimeError("boom")), \
            patch("src.services.news_schedule.build_news_daily_jobs", side_effect=RuntimeError("boom")), \
            patch("src.services.runtime_scheduler.threading.Thread", _NoopThread):
        service.reconcile_from_config()
    assert [job.at_time for job in fake_schedule.get_jobs()] == ["09:20"]
    assert service._scheduler._background_tasks == []


def test_scheduler_background_task_skips_an_occurrence_while_the_previous_one_runs(fake_schedule):
    from src.scheduler import Scheduler

    scheduler = Scheduler(schedule_time="18:00", register_signals=False)
    release, started, runs = threading.Event(), threading.Event(), []

    def slow_poll():
        runs.append(1)
        started.set()
        release.wait(timeout=5)

    scheduler.add_background_task(slow_poll, interval_seconds=30, run_immediately=True, name=NEWS_POLL_TASK_NAME)
    assert started.wait(timeout=5)
    entry = scheduler._background_tasks[0]
    worker = entry["thread"]
    entry["last_run"] = 0.0  # the interval has elapsed, but the previous run is still alive
    scheduler._run_background_tasks()
    release.set()
    worker.join(timeout=5)
    assert runs == [1]
