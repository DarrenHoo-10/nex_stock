# -*- coding: utf-8 -*-
"""Storage for macro and index series (``macro_series``): one row per series and period, value as JSON."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import desc, func, or_, select

from src.storage import DatabaseManager, MacroSeries


class MacroRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert(self, series: str, rows: Iterable[Dict[str, Any]], *, fetched_at: datetime, source: str = "tushare") -> int:
        """Insert or replace rows (``{"period", "value"}``, optionally their own ``series``); returns rows written."""
        written = 0
        with self.db.get_session() as session:
            for row in rows:
                name, period = str(row.get("series") or series), str(row.get("period") or "")
                if not period:
                    continue
                record = session.get(MacroSeries, (name, period))
                if record is None:
                    record = MacroSeries(series=name, period=period)
                    session.add(record)
                record.value = json.dumps(row.get("value") or {}, ensure_ascii=False, sort_keys=True)
                record.source = source
                record.fetched_at = fetched_at
                written += 1
            if written == 0:
                # Remember the attempt so an empty answer is not re-requested on every run.
                record = session.get(MacroSeries, (series, "_fetch")) or MacroSeries(series=series, period="_fetch")
                record.value = "{}"
                record.source = source
                record.fetched_at = fetched_at
                session.merge(record)
            session.commit()
        return written

    def latest(self, series: str, *, limit: int) -> List[Dict[str, Any]]:
        """Newest ``limit`` periods of one series, newest first."""
        with self.db.get_session() as session:
            rows = session.execute(
                select(MacroSeries).where(MacroSeries.series == series, MacroSeries.period != "_fetch")
                .order_by(desc(MacroSeries.period)).limit(max(1, int(limit)))
            ).scalars().all()
            return [{"period": r.period, "value": json.loads(r.value or "{}"),
                     "fetched_at": r.fetched_at, "source": r.source} for r in rows]

    def last_fetched(self, series: str) -> Optional[datetime]:
        """Latest fetch of ``series`` or of its per-key series (``series:<key>``)."""
        with self.db.get_session() as session:
            return session.execute(
                select(func.max(MacroSeries.fetched_at)).where(
                    or_(MacroSeries.series == series, MacroSeries.series.like(f"{series}:%")))
            ).scalar()
