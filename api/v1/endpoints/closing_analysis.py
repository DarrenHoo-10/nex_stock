"""Read daily Markdown reports produced by the analysis pipeline.

Multi-user (docs/design/multi-user.md): the market review is shared; the daily plan preview holds the
caller's own holdings, so it is read for the caller only; ``reports/report_*.md`` is the primary user's
local stock summary (user runs never write there) and is only returned to the primary user.
"""
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import CurrentUser, get_current_user, get_database_manager
from src.config import get_primary_user_id
from src.repositories.daily_plan_repo import DailyPlanRepository
from src.services.closing_report_visibility import read_public_market_review
from src.storage import DatabaseManager

router = APIRouter()
_REPORTS_DIR = Path(__file__).resolve().parents[3] / "reports"
_DAILY_FILENAME = re.compile(r"(?:market_review|report)_([0-9]{8})\.md")


class ClosingAnalysis(BaseModel):
    daily_plan: str | None
    market_review: str | None
    stock_summary: str | None


@router.get("/dates")
def list_report_dates(
    db: DatabaseManager = Depends(get_database_manager),
    user: CurrentUser = Depends(get_current_user),
) -> list[date]:
    days = set(DailyPlanRepository(db).closing_report_dates(user.user_id))
    is_primary = user.user_id == get_primary_user_id()
    if not _REPORTS_DIR.exists():
        return sorted(days, reverse=True)
    for path in _REPORTS_DIR.iterdir():
        match = _DAILY_FILENAME.fullmatch(path.name)
        if match and not path.is_symlink() and path.is_file():
            stamp = match[1]
            if not is_primary and path.name.startswith("report_"):
                continue  # the primary user's stock summary
            try:
                day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))
                if is_primary or read_public_market_review(_REPORTS_DIR, day) is not None:
                    days.add(day)
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


@router.get("/public/{report_date}")
def public_closing_review(report_date: date):
    content = read_public_market_review(_REPORTS_DIR, report_date)
    if content is None:
        raise HTTPException(status_code=404, detail="No public review for this date")
    return {"market_review": content, "report_date": report_date, "visibility": "public"}


@router.get("/{report_date}")
def get_closing_analysis(
    report_date: date,
    db: DatabaseManager = Depends(get_database_manager),
    user: CurrentUser = Depends(get_current_user),
) -> ClosingAnalysis:
    stamp = report_date.strftime("%Y%m%d")
    is_primary = user.user_id == get_primary_user_id()
    market = _read_daily_report(f"market_review_{stamp}.md") if is_primary else read_public_market_review(_REPORTS_DIR, report_date)
    stocks = _read_daily_report(f"report_{stamp}.md") if is_primary else None
    plan = DailyPlanRepository(db).closing_report(user.user_id, report_date)
    if plan is None and market is None and stocks is None:
        raise HTTPException(status_code=404, detail="No daily report for this date")
    return ClosingAnalysis(daily_plan=plan, market_review=market, stock_summary=stocks)
