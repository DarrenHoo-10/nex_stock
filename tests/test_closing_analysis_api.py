from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as ApiClient

from api.deps import get_current_user, get_database_manager
from api.v1.endpoints import closing_analysis
from src.repositories.daily_plan_repo import DailyPlanRepository
from src.services.closing_report_visibility import record_public_market_review
from src.storage import DatabaseManager
from tests.multi_user_helpers import PRIMARY_ID, USER_A, USER_B, UserSwitch


@pytest.fixture(autouse=True)
def _primary(monkeypatch):
    monkeypatch.setenv("PRIMARY_USER_ID", PRIMARY_ID)
    from src.config import Config

    Config.reset_instance()
    yield
    Config.reset_instance()


def _client(db, switch):
    app = FastAPI()
    app.include_router(closing_analysis.router, prefix="/closing-analysis")
    app.dependency_overrides[get_database_manager] = lambda: db
    app.dependency_overrides[get_current_user] = switch
    return ApiClient(app)


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
    client = _client(db, UserSwitch(PRIMARY_ID))
    assert client.get("/closing-analysis/dates").json() == []

    repo = DailyPlanRepository(db)
    for run_id, preview in [("first", "# Old"), ("newest", "# Plan"), ("retry", None)]:
        repo.create_run(PRIMARY_ID, run_id, date(2026, 11, 4), "schedule", [], {})
        repo.update_run(PRIMARY_ID, run_id, push_preview=preview)
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


def test_plan_previews_and_the_stock_summary_are_private(tmp_path, monkeypatch, db):
    """Plan previews hold each user's holdings; reports/report_*.md is the primary user's stock summary."""
    directory = tmp_path / "reports"
    directory.mkdir()
    (directory / "report_20261102.md").write_text("# Primary stocks", encoding="utf-8")
    market_file = directory / "market_review_20261102.md"
    market_file.write_text("# Market", encoding="utf-8")
    record_public_market_review(market_file, date(2026, 11, 2), "# Market")
    monkeypatch.setattr(closing_analysis, "_REPORTS_DIR", directory)
    repo = DailyPlanRepository(db)
    repo.create_run(USER_A, "a-run", date(2026, 11, 4), "schedule", [], {})
    repo.update_run(USER_A, "a-run", push_preview="# A holds 600519 x 300")
    switch = UserSwitch(USER_A)
    client = _client(db, switch)

    assert client.get("/closing-analysis/2026-11-04").json()["daily_plan"] == "# A holds 600519 x 300"

    switch.user_id = USER_B
    assert "2026-11-04" not in client.get("/closing-analysis/dates").json()
    assert client.get("/closing-analysis/2026-11-04").status_code == 404
    other = client.get("/closing-analysis/2026-11-02").json()
    assert other == {"daily_plan": None, "market_review": "# Market", "stock_summary": None}

    switch.user_id = PRIMARY_ID
    assert client.get("/closing-analysis/2026-11-04").status_code == 404  # A's preview is not the primary's
    assert client.get("/closing-analysis/2026-11-02").json()["stock_summary"] == "# Primary stocks"
