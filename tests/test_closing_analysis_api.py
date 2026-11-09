from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as ApiClient

from api.deps import get_database_manager
from api.v1.endpoints import closing_analysis
from src.repositories.daily_plan_repo import DailyPlanRepository
from src.storage import DatabaseManager


@pytest.fixture
def db(tmp_path):
    DatabaseManager.reset_instance()
    try:
        yield DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'plan.db').as_posix()}")
    finally:
        DatabaseManager.reset_instance()


def test_daily_reports_by_date(tmp_path, monkeypatch, db):
    directory = tmp_path / "reports"
    monkeypatch.setattr(closing_analysis, "_REPORTS_DIR", directory)
    app = FastAPI()
    app.include_router(closing_analysis.router, prefix="/closing-analysis")
    app.dependency_overrides[get_database_manager] = lambda: db
    client = ApiClient(app)
    assert client.get("/closing-analysis/dates").json() == []

    repo = DailyPlanRepository(db)
    for run_id, preview in [("first", "# Old"), ("newest", "# Plan"), ("retry", None)]:
        repo.create_run(run_id, date(2026, 11, 4), "schedule", [], {})
        repo.update_run(run_id, push_preview=preview)
    assert client.get("/closing-analysis/2026-11-04").json()["daily_plan"] == "# Plan"

    directory.mkdir()
    files = {
        "report_20261102.md": "# Stocks",
        "market_review_20261102.md": "# Market",
        "market_review_20261030.md": "# Previous",
        "report_20261103_ABC.md": "Individual stock",
        "report_20260230.md": "Invalid date",
    }
    for name, body in files.items():
        (directory / name).write_text(body, encoding="utf-8")

    assert client.get("/closing-analysis/dates").json() == ["2026-11-04", "2026-11-02", "2026-10-30"]
    assert client.get("/closing-analysis/2026-11-02").json() == {
        "daily_plan": None, "market_review": "# Market", "stock_summary": "# Stocks",
    }
    assert client.get("/closing-analysis/2026-10-30").json()["stock_summary"] is None
    assert client.get("/closing-analysis/2026-11-03").status_code == 404
    assert client.get("/closing-analysis/invalid").status_code == 422
