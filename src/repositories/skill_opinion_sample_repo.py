# -*- coding: utf-8 -*-
"""Repository for immutable skill opinion samples."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.storage import AnalysisHistory, DatabaseManager, SkillOpinionSampleRecord


class SkillOpinionSampleRepository:
    """Persist low-sensitivity samples without mutating an existing sample.

    Samples (and the outcomes / skill weights derived from them) are shared system output: a private report
    (``AnalysisHistory.is_private``, docs/design/multi-user.md 17) never yields a sample.
    """

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    @staticmethod
    def shared_sample_condition():
        """SQL predicate on ``SkillOpinionSampleRecord``: its source report is not private."""
        private_source = (
            select(AnalysisHistory.id)
            .where(
                AnalysisHistory.id == SkillOpinionSampleRecord.analysis_history_id,
                AnalysisHistory.is_private.is_(True),
            )
            .correlate(SkillOpinionSampleRecord)
            .exists()
        )
        return ~private_source

    def insert_missing(self, rows: Iterable[Dict[str, Any]]) -> int:
        values = list(rows)
        if not values:
            return 0

        def _insert(session):
            history_ids = {
                row.get("analysis_history_id")
                for row in values
                if row.get("analysis_history_id") is not None
            }
            if not history_ids:
                return 0
            existing_history_ids = set(
                session.execute(
                    select(AnalysisHistory.id).where(
                        AnalysisHistory.id.in_(history_ids),
                        or_(AnalysisHistory.is_private.is_(None), AnalysisHistory.is_private.is_(False)),
                    )
                ).scalars()
            )
            eligible_values = [
                row
                for row in values
                if row.get("analysis_history_id") in existing_history_ids
            ]
            if not eligible_values:
                return 0

            statement = sqlite_insert(SkillOpinionSampleRecord).values(eligible_values)
            statement = statement.on_conflict_do_nothing(
                index_elements=[
                    "analysis_history_id",
                    "skill_id",
                    "sample_schema_version",
                ]
            )
            result = session.execute(statement)
            return result.rowcount or 0

        return self.db._run_write_transaction(
            "insert skill opinion samples",
            _insert,
        )

    def list_for_history(self, analysis_history_id: int) -> List[SkillOpinionSampleRecord]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(SkillOpinionSampleRecord)
                .where(SkillOpinionSampleRecord.analysis_history_id == analysis_history_id)
                .order_by(SkillOpinionSampleRecord.id)
            ).scalars().all()
            return list(rows)
