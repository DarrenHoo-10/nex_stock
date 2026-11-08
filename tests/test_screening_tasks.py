# -*- coding: utf-8 -*-
"""Persisted screening tasks: lifecycle rows, interruption after restart, list and detail endpoints."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from api.v1.endpoints import screening as screening_endpoint
from src.services import screening_task_service
from src.services.task_queue import TaskStatus as QueueTaskStatus
from src.storage import DatabaseManager

SCOPE = {"boards": [], "topics": [{"name": "半导体", "source": "concept"}], "topic_match": "any"}


class ScreeningTaskStorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def _create(self, task_id: str) -> None:
        self.db.create_screening_task(
            task_id=task_id, strategy="volume_breakout", market="cn", max_results=10, scope=SCOPE, message="已提交",
        )

    def test_tracker_records_progress_completion_and_links_the_run(self) -> None:
        self._create("t1")
        tracker = screening_task_service.ScreeningTaskTracker(self.db, "t1")
        tracker.started(20, "开始")
        self.db.save_screening_run({"run_id": "run-1", "strategy": "volume_breakout", "market": "cn",
                                    "candidate_count": 2, "candidates": [{"code": "000988"}, {"code": "600519"}],
                                    "scope": SCOPE})

        def job():
            tracker.progress(60, "排序中")
            self.assertEqual(self.db.get_screening_task("t1")["progress"], 60)
            return {"run_id": "run-1", "candidate_count": 2}

        tracker.run(job)
        task = screening_task_service.get_task(self.db, "t1", is_live=lambda _id: False)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["progress"], 100)
        self.assertEqual(task["run_id"], "run-1")
        self.assertEqual(task["candidate_count"], 2)
        self.assertEqual(task["scope"]["topics"][0]["name"], "半导体")
        self.assertTrue(task["created_at"].endswith("Z"))
        self.assertIsNotNone(task["finished_at"])
        self.assertEqual(len(task["result"]["candidates"]), 2)

    def test_tracker_records_failure_text_and_reraises(self) -> None:
        self._create("t2")
        tracker = screening_task_service.ScreeningTaskTracker(self.db, "t2")

        def job():
            raise HTTPException(424, detail={"error": "screening_screen_failed", "message": "数据源不可用"})

        with self.assertRaises(HTTPException):
            tracker.run(job)
        task = self.db.get_screening_task("t2")
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"], "数据源不可用")

    def test_active_task_missing_from_queue_becomes_interrupted(self) -> None:
        self._create("old")
        self._create("live")
        tasks, total = screening_task_service.list_tasks(
            self.db, limit=10, offset=0, is_live=lambda task_id: task_id == "live",
        )
        self.assertEqual(total, 2)
        by_id = {task["task_id"]: task for task in tasks}
        self.assertEqual(by_id["old"]["status"], "interrupted")
        self.assertEqual(by_id["live"]["status"], "pending")
        self.assertEqual(self.db.get_screening_task("old")["status"], "interrupted")

    def test_list_is_newest_first_and_paged(self) -> None:
        for index in range(3):
            self._create(f"t{index}")
        tasks, total = self.db.list_screening_tasks(limit=2, offset=0)
        self.assertEqual(total, 3)
        self.assertEqual([task["task_id"] for task in tasks], ["t2", "t1"])
        tasks, _ = self.db.list_screening_tasks(limit=2, offset=2)
        self.assertEqual([task["task_id"] for task in tasks], ["t0"])


class ScreeningTaskEndpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url="sqlite:///:memory:")

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()

    def test_start_persists_the_task_and_the_job_records_completion(self) -> None:
        fake_queue = MagicMock()
        fake_queue.submit_background_task.return_value = SimpleNamespace(
            task_id="ignored", trace_id=None, status=QueueTaskStatus.PENDING, message="选股任务已提交",
        )
        request = screening_endpoint.ScreeningScreenRequest(strategy="volume_breakout", max_results=5, scope=SCOPE)
        service = MagicMock()
        service.screen.return_value = {"run_id": "run-9", "candidate_count": 1, "candidates": []}
        with patch("api.v1.endpoints.screening.get_task_queue", return_value=fake_queue), \
                patch("api.v1.endpoints.screening._service", return_value=service):
            screening_endpoint.screening_start_screen_task(request, MagicMock(), config=MagicMock(), db_manager=self.db)
            task_id = fake_queue.submit_background_task.call_args.kwargs["task_id"]
            stored = self.db.get_screening_task(task_id)
            self.assertEqual(stored["status"], "pending")
            self.assertEqual(stored["strategy"], "volume_breakout")
            self.assertEqual(stored["max_results"], 5)
            fake_queue.submit_background_task.call_args.args[0]()

        stored = self.db.get_screening_task(task_id)
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["run_id"], "run-9")

    def test_list_and_detail_fall_back_to_the_stored_task_after_restart(self) -> None:
        self.db.create_screening_task(task_id="gone", strategy="volume_breakout", market="cn", max_results=10, scope=SCOPE)
        self.db.create_screening_task(task_id="done", strategy="dual_low", market="cn", max_results=10, scope={})
        self.db.save_screening_run({"run_id": "run-d", "strategy": "dual_low", "market": "cn",
                                    "candidate_count": 1, "candidates": [{"code": "000988"}]})
        self.db.update_screening_task("done", status="completed", progress=100, run_id="run-d", candidate_count=1)
        empty_queue = MagicMock()
        empty_queue.get_task.return_value = None
        with patch("api.v1.endpoints.screening.get_task_queue", return_value=empty_queue):
            listing = screening_endpoint.screening_list_screen_tasks(limit=20, offset=0, db_manager=self.db)
            detail = screening_endpoint.screening_screen_task_status("done", db_manager=self.db)
            gone = screening_endpoint.screening_screen_task_status("gone", db_manager=self.db)
            with self.assertRaises(HTTPException) as missing:
                screening_endpoint.screening_screen_task_status("nope", db_manager=self.db)

        self.assertEqual(listing.total, 2)
        self.assertEqual({task.task_id: task.status for task in listing.tasks}, {"gone": "interrupted", "done": "completed"})
        self.assertTrue(all(task.result is None for task in listing.tasks))
        self.assertEqual(detail.status, "completed")
        self.assertEqual(detail.result["candidates"][0]["code"], "000988")
        self.assertEqual(gone.status, "interrupted")
        self.assertEqual(missing.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
