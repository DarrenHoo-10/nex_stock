"""Read daily Markdown reports produced by the analysis pipeline."""
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_database_manager
from src.repositories.daily_plan_repo import DailyPlanRepository
from src.storage import DatabaseManager

router = APIRouter()
_REPORTS_DIR = Path(__file__).resolve().parents[3] / "reports"
_DAILY_FILENAME = re.compile(r"(?:market_review|report)_([0-9]{8})\.md")


class ClosingAnalysis(BaseModel):
    daily_plan: str | None
    market_review: str | None
    stock_summary: str | None


@router.get("/dates")
def list_report_dates(db: DatabaseManager = Depends(get_database_manager)) -> list[date]:
    days = set(DailyPlanRepository(db).closing_report_dates())
    if not _REPORTS_DIR.exists():
        return sorted(days, reverse=True)
    for path in _REPORTS_DIR.iterdir():
        match = _DAILY_FILENAME.fullmatch(path.name)
        if match and not path.is_symlink() and path.is_file():
            stamp = match[1]
            try:
                days.add(date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:])))
            except ValueError:
                continue
    return sorted(days, reverse=True)


def _read_daily_report(filename: str) -> str | None:
    path = _REPORTS_DIR / filename
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


@router.get("/{report_date}")
def get_closing_analysis(report_date: date, db: DatabaseManager = Depends(get_database_manager)) -> ClosingAnalysis:
    stamp = report_date.strftime("%Y%m%d")
    market = _read_daily_report(f"market_review_{stamp}.md")
    stocks = _read_daily_report(f"report_{stamp}.md")
    plan = DailyPlanRepository(db).closing_report(report_date)
    if plan is None and market is None and stocks is None:
        raise HTTPException(status_code=404, detail="No daily report for this date")
    return ClosingAnalysis(daily_plan=plan, market_review=market, stock_summary=stocks)
