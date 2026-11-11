"""Provenance for market-only Markdown produced by the public review pipeline."""
import hashlib
import logging
import os
import tempfile
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


def _digest(content: str) -> str:
    return hashlib.sha256(content.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def record_public_market_review(path: Path, trading_day: date, content: str) -> None:
    """Bind the archived bytes to the actual market-only generator output."""
    report = Path(path)
    if report.name != f"market_review_{trading_day:%Y%m%d}.md" or report.is_symlink() or not report.is_file():
        return
    tmp = None
    try:
        digest = _digest(content)
        if _digest(report.read_text(encoding="utf-8")) != digest:
            return
        with tempfile.NamedTemporaryFile(mode="w", dir=report.parent, delete=False, encoding="ascii") as handle:
            tmp = handle.name
            handle.write(digest)
        os.replace(tmp, report.with_suffix(".public.sha256"))
    except (OSError, UnicodeError):
        logger.warning("Public market review provenance could not be recorded for %s", trading_day)
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def read_public_market_review(reports_dir: Path, trading_day: date) -> str | None:
    report = reports_dir / f"market_review_{trading_day:%Y%m%d}.md"
    marker = report.with_suffix(".public.sha256")
    if report.is_symlink() or marker.is_symlink():
        return None
    try:
        content = report.read_text(encoding="utf-8")
        expected = marker.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    return content if _digest(content) == expected else None
