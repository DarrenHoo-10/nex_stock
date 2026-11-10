# -*- coding: utf-8 -*-
"""Multi-user P2b-2: fair analysis queue, per-user task visibility and per-user duplicate detection (offline).

* Scheduling: pending tasks wait in one queue per user and idle workers take the user served least recently;
  every user may run at most ``ANALYSIS_MAX_CONCURRENT_PER_USER`` tasks at once, ``MAX_WORKERS`` stays global.
  The fairness tests run real worker threads but synchronise on events (the analysis is a gated fake), never on
  sleeps.
* Visibility: ``/analysis/tasks``, the task stream, ``/analysis/status/{id}`` and ``/analysis/tasks/{id}/flow``
  only show the requester's own tasks; another user's task id behaves exactly like a missing one.
* Duplicate detection is per user, so there is no 409 that reveals another user's ``existing_task_id``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections import Counter
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.services.task_queue import AnalysisTaskQueue, task_owner
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    make_user,
    stub_dns,
    teardown_multi_user_env,
)

USER_C = "uid-user-c"
WAIT = 10.0


class Harness:
    """A real task queue whose analysis is a fake gated per stock code (no model, no network, no sleeps)."""

    def __init__(self, *, workers: int, per_user: int):
        self.workers = workers
        self.per_user = per_user
        self.lock = threading.Lock()
        self.order: list[str] = []
        self.owner_of: dict[str, str] = {}
        self.running: Counter = Counter()
        self.max_running: Counter = Counter()
        self.started: dict[str, threading.Event] = {}
        self.gates: dict[str, threading.Event] = {}
        self.finished: dict[str, threading.Event] = {}
        self.open = threading.Event()  # teardown: let every still-gated analysis through

    def _wait_gate(self, code):
        gate = self._event(self.gates, code)
        waited = 0.0
        while not (gate.wait(0.01) or self.open.is_set()) and waited < WAIT:
            waited += 0.01

    def _event(self, table, code):
        with self.lock:
            return table.setdefault(code, threading.Event())

    def __enter__(self):
        self._original_instance = AnalysisTaskQueue._instance
        AnalysisTaskQueue._instance = None
        self.queue = AnalysisTaskQueue(max_workers=self.workers)
        self.queue._per_user_limit = self.per_user
        harness = self

        class FakeAnalysisService:
            last_error = None

            def analyze_stock(self, stock_code, user_id=None, **kwargs):
                owner = task_owner(user_id)
                with harness.lock:
                    harness.order.append(stock_code)
                    harness.owner_of[stock_code] = owner
                    harness.running[owner] += 1
                    harness.max_running[owner] = max(harness.max_running[owner], harness.running[owner])
                harness._event(harness.started, stock_code).set()
                harness._wait_gate(stock_code)
                with harness.lock:
                    harness.running[owner] -= 1
                harness._event(harness.finished, stock_code).set()
                return {"stock_name": stock_code}

        self._patch = patch("src.services.analysis_service.AnalysisService", FakeAnalysisService)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self.open.set()
        self.queue.shutdown()
        self._patch.stop()
        AnalysisTaskQueue._instance = self._original_instance

    # --- driving
    def submit(self, codes, user_id=None):
        kwargs = {"user_id": user_id} if user_id else {}
        accepted, duplicates = self.queue.submit_tasks_batch(codes, report_type="detailed", notify=False, **kwargs)
        assert not duplicates and len(accepted) == len(codes)
        return accepted

    def wait_started(self, code):
        assert self._event(self.started, code).wait(WAIT), f"{code} never started; started={self.order}"

    def wait_finished(self, code):
        assert self._event(self.finished, code).wait(WAIT), f"{code} never finished"

    def release(self, code):
        self._event(self.gates, code).set()

    def drain(self, codes):
        """Open every gate up front and wait until all given codes have run."""
        for code in codes:
            self.release(code)
        for code in codes:
            self.wait_finished(code)


# ===================================================================== fairness


def test_second_user_starts_while_a_fifty_stock_backlog_waits():
    a_codes = [f"6000{n:02d}" for n in range(1, 51)]
    with Harness(workers=2, per_user=1) as h:
        h.submit(a_codes, USER_A)
        h.wait_started(a_codes[0])
        h.wait_started(a_codes[1])  # alone, A uses every worker (work-conserving)
        h.submit(["000858"], USER_B)
        assert "000858" not in h.order

        h.release(a_codes[0])
        h.wait_started("000858")  # the first freed worker goes to B, not to A's backlog
        # A's first two started in parallel threads (any order); 48 of A's tasks are still queued
        assert set(h.order[:2]) == set(a_codes[:2]) and h.order[2:] == ["000858"]

        h.drain(["000858"] + a_codes)


def test_the_next_free_worker_goes_to_the_user_served_least_recently():
    with Harness(workers=1, per_user=1) as h:
        h.submit(["600001", "600002", "600003"], USER_A)
        h.wait_started("600001")
        h.submit(["000858"], USER_B)
        h.submit(["300750"], USER_C)

        h.release("600001")
        h.wait_started("000858")  # B, not A's next task
        assert h.order == ["600001", "000858"]
        h.release("000858")
        h.wait_started("300750")  # then C, still not A
        h.release("300750")
        h.wait_started("600002")
        h.drain(["600002", "600003"])
        assert h.order == ["600001", "000858", "300750", "600002", "600003"]


def test_rotation_alternates_between_two_backlogs():
    with Harness(workers=1, per_user=1) as h:
        h.submit(["600001", "600002", "600003"], USER_A)
        h.wait_started("600001")
        h.submit(["000001", "000002", "000003"], USER_B)
        for code in ["600001", "000001", "600002", "000002", "600003"]:
            h.wait_started(code)
            h.release(code)
        h.drain(["000003"])
        assert h.order == ["600001", "000001", "600002", "000002", "600003", "000003"]


def test_a_user_alone_uses_every_worker():
    with Harness(workers=3, per_user=1) as h:
        h.submit(["600001", "600002", "600003", "600004"], USER_A)
        for code in ("600001", "600002", "600003"):
            h.wait_started(code)
        assert "600004" not in h.order  # MAX_WORKERS stays the global cap
        h.drain(["600001", "600002", "600003", "600004"])
        assert h.max_running[USER_A] == 3


def test_freed_workers_go_to_users_below_the_cap_while_others_wait():
    with Harness(workers=3, per_user=1) as h:
        a_codes = ["600001", "600002", "600003", "600004", "600005"]
        h.submit(a_codes, USER_A)
        for code in a_codes[:3]:
            h.wait_started(code)
        h.submit(["000858"], USER_B)
        h.submit(["300750"], USER_C)

        h.release("600001")
        h.wait_started("000858")  # A still holds two slots, so B comes first
        h.release("600002")
        h.wait_started("300750")  # A still holds one (its cap), so C comes next
        assert set(h.order[:3]) == set(a_codes[:3]) and h.order[3:] == ["000858", "300750"]

        h.drain(["600003", "000858", "300750", "600004", "600005"])


def test_a_higher_cap_guarantees_more_slots_under_contention():
    with Harness(workers=3, per_user=2) as h:
        a_codes = ["600001", "600002", "600003", "600004"]
        h.submit(a_codes, USER_A)
        for code in a_codes[:3]:
            h.wait_started(code)
        h.submit(["000858"], USER_B)

        h.release("600001")
        h.wait_started("000858")  # A holds 2 = its cap, so B gets the freed worker
        h.release("600002")
        h.wait_started("600004")  # A is back below its cap of 2
        assert set(h.order[:3]) == set(a_codes[:3]) and h.order[3:] == ["000858", "600004"]
        h.drain(["600003", "000858", "600004"])


def test_the_cap_can_be_changed_at_runtime():
    with Harness(workers=3, per_user=1) as h:
        h.submit(["600001"], USER_A)
        h.wait_started("600001")
        assert h.queue.sync_per_user_limit(3, log=False) is True
        assert h.queue.sync_per_user_limit(3, log=False) is False
        assert h.queue.sync_per_user_limit("bogus", log=False) is False and h.queue.per_user_limit == 3
        h.drain(["600001"])


def test_system_background_tasks_count_as_the_primary_user_but_not_against_the_analysis_cap():
    background_started = threading.Event()
    background_gate = threading.Event()

    def background():
        background_started.set()
        background_gate.wait(WAIT)
        return {"ok": True}

    with Harness(workers=2, per_user=1) as h:
        h.submit(["600001"], None)  # the primary user's own analysis, at its cap of 1
        h.wait_started("600001")
        h.submit(["000858", "000002"], USER_B)  # B takes the second worker and has one more waiting
        h.wait_started("000858")
        h.submit(["600002"], None)
        task = h.queue.submit_background_task(background, stock_code="market_review", stock_name="x")

        h.release("000858")
        # The primary user is at its cap with an analysis waiting, yet its uncounted background task is
        # startable under the cap and the primary user was served least recently.
        assert background_started.wait(WAIT)
        assert "600002" not in h.order and "000002" not in h.order
        from src.config import get_primary_user_id

        assert task_owner(h.queue.get_task(task.task_id).user_id) == get_primary_user_id()
        background_gate.set()
        h.drain(["600001", "600002", "000002"])


# ===================================================================== per-user duplicate detection


class _NeverRuns:
    def submit(self, *args, **kwargs):
        return Future()


@pytest.fixture
def stub_queue():
    original = AnalysisTaskQueue._instance
    AnalysisTaskQueue._instance = None
    queue = AnalysisTaskQueue(max_workers=5)
    queue._executor = _NeverRuns()
    yield queue
    AnalysisTaskQueue._instance = original


def test_duplicate_detection_is_per_user_and_primary_keys_are_unchanged(stub_queue):
    queue = stub_queue
    first, _ = queue.submit_tasks_batch(["600519"], user_id=USER_A)
    other, other_dups = queue.submit_tasks_batch(["600519"], user_id=USER_B)
    primary, primary_dups = queue.submit_tasks_batch(["600519"])
    again, dups = queue.submit_tasks_batch(["600519"], user_id=USER_A)

    assert other and not other_dups and primary and not primary_dups  # nobody collides with somebody else
    assert not again and [d.existing_task_id for d in dups] == [first[0].task_id]  # only with themselves
    assert queue.is_analyzing("600519", USER_A) and queue.is_analyzing("600519", USER_B) and queue.is_analyzing("600519")
    assert queue.get_analyzing_task_id("600519", USER_B) == other[0].task_id
    assert "600519" in queue._analyzing_stocks  # the primary user keeps the bare key it always had
    assert not queue.is_analyzing("000858", USER_A)


def test_finishing_frees_only_the_owners_duplicate_key(stub_queue):
    queue = stub_queue
    a, _ = queue.submit_tasks_batch(["600519"], user_id=USER_A)
    b, _ = queue.submit_tasks_batch(["600519"], user_id=USER_B)

    with patch("src.services.analysis_service.AnalysisService") as service:
        service.return_value.analyze_stock.return_value = {"stock_name": "x"}
        queue._run_analysis_task(a[0].task_id, "600519", "detailed", False)

    assert not queue.is_analyzing("600519", USER_A) and queue.is_analyzing("600519", USER_B)
    assert queue.get_task(b[0].task_id).status.value == "pending"


def test_a_worker_start_failure_fails_only_that_task_and_frees_its_slot(stub_queue):
    queue = stub_queue
    queue._max_workers = 1
    (running,), _ = queue.submit_tasks_batch(["600001"], user_id=USER_A)
    (queued,), _ = queue.submit_tasks_batch(["000858"], user_id=USER_B)
    assert queue.get_task(queued.task_id).status.value == "pending"

    class _Broken:
        def submit(self, *args, **kwargs):
            raise RuntimeError("pool down")

    queue._executor = _Broken()
    queue._on_task_finished(running.task_id)  # the worker of the first task finished; starting the next fails

    failed = queue.get_task(queued.task_id)
    assert failed.status.value == "failed" and "pool down" in failed.error
    assert not queue.is_analyzing("000858", USER_B) and queue._running == {}


def test_task_listing_and_stats_are_filtered_before_the_limit(stub_queue):
    queue = stub_queue
    queue.submit_tasks_batch(["600519"], user_id=USER_A)
    for code in ("000001", "000002", "000858", "300750", "002594"):
        queue.submit_tasks_batch([code], user_id=USER_B)
    queue.submit_background_task(lambda: {}, stock_code="screening_screen", user_id=USER_A)

    mine = queue.list_all_tasks(limit=3, viewer_user_id=USER_A)
    assert {t.stock_code for t in mine} == {"600519", "screening_screen"}  # B's five newer tasks did not crowd them out
    assert queue.get_task_stats(viewer_user_id=USER_B)["total"] == 5
    assert queue.get_task_stats(viewer_user_id=PRIMARY_ID)["total"] == 0
    assert len(queue.list_pending_tasks(viewer_user_id=USER_A)) == 2
    assert len(queue.list_all_tasks(limit=50)) == 7  # unfiltered internal view


# ===================================================================== API visibility

ANALYZE = "/api/v1/analysis/analyze"
TASKS = "/api/v1/analysis/tasks"
STATUS = "/api/v1/analysis/status/"


@pytest.fixture
def api(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    stub_dns(monkeypatch)
    give_user_model(USER_A)
    give_user_model(USER_B)
    original = AnalysisTaskQueue._instance
    AnalysisTaskQueue._instance = None
    queue = AnalysisTaskQueue(max_workers=5)
    queue._executor = _NeverRuns()
    app.queue = queue
    with patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
        yield app
    AnalysisTaskQueue._instance = original
    teardown_multi_user_env()


def _submit(api, user, code):
    response = api.as_user(user).post(ANALYZE, json={"stock_code": code, "async_mode": True})
    return response


def _ids(response):
    assert response.status_code == 200, response.text
    return {task["task_id"] for task in response.json()["tasks"]}


def test_task_list_and_counts_show_only_the_requesters_tasks(api):
    ids = {user: _submit(api, user, code).json()["task_id"]
           for user, code in ((PRIMARY_ID, "000001"), (USER_A, "600519"), (USER_B, "000858"))}

    for user in ids:
        listed = api.as_user(user).get(TASKS)
        assert _ids(listed) == {ids[user]}
        body = listed.json()
        assert body["total"] == 1 and body["pending"] + body["processing"] == 1


def test_a_task_of_another_user_is_missing_in_status_and_flow(api):
    task_id = _submit(api, USER_A, "600519").json()["task_id"]
    unknown = "0" * 32

    for path in (STATUS + "{}", TASKS + "/{}/flow"):
        foreign = api.as_user(USER_B).get(path.format(task_id))
        missing = api.as_user(USER_B).get(path.format(unknown))
        assert foreign.status_code == missing.status_code == 404, path
        assert foreign.json()["error"] == missing.json()["error"]
        assert foreign.json()["message"].replace(task_id, "X") == missing.json()["message"].replace(unknown, "X")
        assert api.as_user(USER_A).get(path.format(task_id)).status_code == 200
    # the primary user has no special view of other users' tasks either
    assert api.as_user(PRIMARY_ID).get(STATUS + task_id).status_code == 404


def test_duplicate_conflict_never_reveals_another_users_task(api):
    mine = _submit(api, USER_A, "600519")
    assert mine.status_code == 202

    again = _submit(api, USER_A, "600519")
    assert again.status_code == 409 and again.json()["existing_task_id"] == mine.json()["task_id"]

    for user in (USER_B, PRIMARY_ID):
        response = _submit(api, user, "600519")
        assert response.status_code == 202, (user, response.text)
        assert response.json()["task_id"] != mine.json()["task_id"]

    batch = api.as_user(USER_B).post(ANALYZE, json={"stock_codes": ["600519", "000858"], "async_mode": True})
    assert batch.status_code == 202
    body = batch.json()
    assert [item["stock_code"] for item in body["duplicates"]] == ["600519"]  # B's own earlier task, nobody else's
    own = api.as_user(USER_B).get(TASKS).json()["tasks"]
    assert {t["task_id"] for t in own} >= {body["duplicates"][0]["existing_task_id"]}


def test_market_review_and_screening_style_background_tasks_belong_to_their_submitter(api):
    task = api.queue.submit_background_task(lambda: {}, stock_code="market_review", user_id=USER_A)

    assert _ids(api.as_user(USER_A).get(TASKS)) == {task.task_id}
    assert _ids(api.as_user(USER_B).get(TASKS)) == set()
    assert _ids(api.as_user(PRIMARY_ID).get(TASKS)) == set()
    assert api.as_user(USER_B).get(STATUS + task.task_id).status_code == 404


def test_task_stream_only_carries_the_requesters_events(api):
    from api.v1.endpoints.analysis import task_stream

    a_task = api.queue.submit_tasks_batch(["600519"], user_id=USER_A)[0][0]
    b_task = api.queue.submit_tasks_batch(["000858"], user_id=USER_B)[0][0]

    async def scenario():
        response = await task_stream(user=make_user(USER_B))
        stream = response.body_iterator
        seen = [await stream.__anext__()]  # connected
        seen.append(await stream.__anext__())  # B's pending task, replayed on connect
        assert b_task.task_id in seen[1] and a_task.task_id not in seen[1]

        next_event = asyncio.ensure_future(stream.__anext__())
        for _ in range(200):  # let the generator subscribe (event-loop turns, no timers)
            if api.queue._subscribers:
                break
            await asyncio.sleep(0)
        api.queue._broadcast_event("task_progress", {"task_id": a_task.task_id, "progress": 50})
        api.queue._broadcast_event("task_progress", {"task_id": b_task.task_id, "progress": 60})
        frame = await asyncio.wait_for(next_event, WAIT)
        await stream.aclose()
        return frame

    frame = asyncio.run(scenario())
    assert b_task.task_id in frame and a_task.task_id not in frame and '"progress": 60' in frame


def test_primary_user_flow_is_unchanged(api):
    queue = MagicMock()
    queue.submit_tasks_batch.return_value = (
        [MagicMock(task_id="t1", trace_id="t1", stock_code="600519", analysis_phase="auto", asset_type=None)], [],
    )
    with patch("api.v1.endpoints.analysis.get_task_queue", return_value=queue):
        response = api.as_user(PRIMARY_ID).post(ANALYZE, json={"stock_code": "600519", "async_mode": True})

    kwargs = queue.submit_tasks_batch.call_args.kwargs
    assert response.status_code == 202
    assert "user_id" not in kwargs and "allow_reuse" not in kwargs and kwargs["notify"] is True


# ===================================================================== config


def test_per_user_cap_config_default_range_and_scope(monkeypatch):
    from src.config import Config
    from src.core.config_registry import SCOPE_SYSTEM, get_config_scope

    monkeypatch.delenv("ANALYSIS_MAX_CONCURRENT_PER_USER", raising=False)
    assert Config._load_from_env({}).analysis_max_concurrent_per_user == 1
    assert Config._load_from_env({"ANALYSIS_MAX_CONCURRENT_PER_USER": "3"}).analysis_max_concurrent_per_user == 3
    assert Config._load_from_env({"ANALYSIS_MAX_CONCURRENT_PER_USER": "0"}).analysis_max_concurrent_per_user == 1
    assert get_config_scope("ANALYSIS_MAX_CONCURRENT_PER_USER") == SCOPE_SYSTEM


def test_get_task_queue_applies_the_configured_cap(monkeypatch):
    from src.services import task_queue as module

    original = AnalysisTaskQueue._instance
    AnalysisTaskQueue._instance = None
    try:
        config = MagicMock(max_workers=3, analysis_max_concurrent_per_user=2)
        with patch("src.config.get_config", return_value=config):
            queue = module.get_task_queue()
        assert queue.per_user_limit == 2 and queue.max_workers == 3
    finally:
        AnalysisTaskQueue._instance = original
