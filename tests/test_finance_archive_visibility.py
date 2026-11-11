"""Closing views and derived aggregates must follow the reports' verified ownership."""
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from api.deps import get_database_manager
from api.v1.endpoints import closing_analysis
from src.services.closing_report_visibility import record_public_market_review
from src.storage import AnalysisHistory, BacktestResult, BacktestSummary, DailyPlanRun, DatabaseManager


@pytest.fixture
def isolated_db(tmp_path):
    DatabaseManager.reset_instance()
    try:
        yield DatabaseManager(db_url=f"sqlite:///{(tmp_path / 'report-test.db').as_posix()}")
    finally:
        DatabaseManager.reset_instance()


def test_public_review_requires_matching_generator_bytes(tmp_path, monkeypatch, isolated_db):
    day = date(2026, 11, 9)
    monkeypatch.setattr(closing_analysis, "_REPORTS_DIR", tmp_path)
    app = FastAPI()
    app.include_router(closing_analysis.router)
    app.dependency_overrides[get_database_manager] = lambda: isolated_db
    review = tmp_path / "market_review_20261109.md"
    review.write_text("Public market", encoding="utf-8")
    (tmp_path / "report_20261109.md").write_text("private holdings", encoding="utf-8")
    with TestClient(app) as client:
        assert client.get("/public/2026-11-09").status_code == 404
        record_public_market_review(review, day, "Public market")
        assert client.get("/public/2026-11-09").json() == {
            "market_review": "Public market", "report_date": "2026-11-09", "visibility": "public"}
        review.write_text("edited mixed report", encoding="utf-8")
        assert client.get("/public/2026-11-09").status_code == 404


@pytest.mark.skipif(not hasattr(DailyPlanRun, "user_id"), reason="requires Stock user-system P2")
def test_user_view_preserves_own_plan_and_legacy_primary_only(tmp_path, monkeypatch, isolated_db):
    from api import deps
    day, legacy_day = date(2026, 11, 9), date(2026, 11, 8)
    monkeypatch.setattr(closing_analysis, "get_primary_user_id", lambda: "alice")
    monkeypatch.setattr(deps, "is_user_center_mode", lambda: True)
    monkeypatch.setattr(closing_analysis, "_REPORTS_DIR", tmp_path)
    with isolated_db.get_session() as session:
        for owner in ("alice", "bob"):
            session.add(DailyPlanRun(user_id=owner, run_id=owner, trade_date=day, push_preview=owner + " plan"))
        session.commit()
    review = tmp_path / "market_review_20261109.md"
    review.write_text("published market", encoding="utf-8")
    record_public_market_review(review, day, "published market")
    (tmp_path / "report_20261108.md").write_text("alice holdings", encoding="utf-8")
    app = FastAPI()
    @app.middleware("http")
    async def synthetic_identity(request: Request, next_call):
        subject = request.headers.get("test-session", "")
        if subject in {"alice", "bob"}:
            request.state.user_center_session = SimpleNamespace(user_id=subject, role="user", handle=subject, display_name=subject)
        return await next_call(request)
    app.include_router(closing_analysis.router)
    app.dependency_overrides[get_database_manager] = lambda: isolated_db
    with TestClient(app) as client:
        bob = {"test-session": "bob", "X-User-Id": "alice"}
        assert client.get("/" + str(day), headers=bob).json() == {
            "market_review": "published market", "daily_plan": "bob plan", "stock_summary": None}
        assert client.get("/dates", headers=bob).json() == [str(day)]
        assert client.get("/" + str(legacy_day), headers=bob).status_code == 404
        assert client.get("/" + str(legacy_day), headers={"test-session": "alice"}).json()["stock_summary"] == "alice holdings"


@pytest.mark.skipif(not hasattr(AnalysisHistory, "is_private"), reason="requires Stock user-system P2")
def test_shared_backtest_counts_drop_private_sources_and_stale_cached_totals(isolated_db):
    from src.core.backtest_engine import OVERALL_SENTINEL_CODE
    from src.services.backtest_service import BacktestService
    with isolated_db.get_session() as session:
        for owner, private in (("alice", True), ("bob", False)):
            report = AnalysisHistory(code="600519", name=owner, query_id=owner, requester_user_id=owner, is_private=private)
            session.add(report)
            session.flush()
            session.add(BacktestResult(code=report.code, analysis_history_id=report.id, eval_window_days=3,
                                       analysis_date=date(2026, 1, 2), engine_version="v1", eval_status="evaluated"))
        session.add(BacktestSummary(scope="overall", code=OVERALL_SENTINEL_CODE, eval_window_days=3, engine_version="v1", total_evaluations=999))
        session.commit()
    service = BacktestService(isolated_db)
    assert service.get_recent_evaluations(code="600519", eval_window_days=3)["total"] == 1
    assert service.get_summary(scope="overall", code=None, eval_window_days=3)["total_evaluations"] == 1
    with isolated_db.get_session() as session:
        session.query(AnalysisHistory).filter_by(name="bob").update({"is_private": True})
        session.commit()
    assert service.get_recent_evaluations(code="600519", eval_window_days=3)["total"] == 0
    assert service.get_summary(scope="overall", code=None, eval_window_days=3) is None
