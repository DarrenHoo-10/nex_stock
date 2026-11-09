# -*- coding: utf-8 -*-
"""Storage for company events (``stock_event``) and per-day ingest bookkeeping (``stock_event_ingest``)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import and_, delete, desc, select

from src.storage import DatabaseManager, StockEvent, StockEventIngest, utc_naive_now

_IN_CHUNK = 400


class StockEventRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert(self, records: Iterable[Dict[str, Any]], *, fetched_at=None) -> int:
        """Insert events not stored yet (identity: code, kind, event_date, payload_hash); returns inserted count."""
        rows = [r for r in records if r.get("code") and r.get("kind") and r.get("event_date") and r.get("title")]
        if not rows:
            return 0
        stamp = fetched_at or utc_naive_now()
        inserted = 0
        with self.db.get_session() as session:
            existing = set()
            codes = sorted({r["code"] for r in rows})
            for start in range(0, len(codes), _IN_CHUNK):
                chunk = codes[start:start + _IN_CHUNK]
                for code, kind, event_date, payload_hash in session.execute(
                    select(StockEvent.code, StockEvent.kind, StockEvent.event_date, StockEvent.payload_hash)
                    .where(StockEvent.code.in_(chunk))
                ).all():
                    existing.add((code, kind, event_date, payload_hash))
            for r in rows:
                key = (r["code"], r["kind"], r["event_date"], r["payload_hash"])
                if key in existing:
                    continue
                existing.add(key)
                session.add(StockEvent(
                    code=r["code"], kind=r["kind"], event_date=r["event_date"], title=str(r["title"])[:300],
                    payload=json.dumps(r.get("payload") or {}, ensure_ascii=False, default=str),
                    payload_hash=r["payload_hash"], source=r.get("source") or "tushare", fetched_at=stamp,
                ))
                inserted += 1
            session.commit()
        return inserted

    def list_for_code(
        self, code: str, *, since: date, until: Optional[date] = None, kinds: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        conditions = [StockEvent.code == code, StockEvent.event_date >= since]
        if until is not None:
            conditions.append(StockEvent.event_date <= until)
        if kinds:
            conditions.append(StockEvent.kind.in_(list(kinds)))
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockEvent).where(and_(*conditions)).order_by(desc(StockEvent.event_date), desc(StockEvent.id))
            ).scalars().all()
            return [self._to_dict(row) for row in rows]

    def list_by_kind(self, kind: str, *, since: date, until: Optional[date] = None) -> List[Dict[str, Any]]:
        """Events of one kind across all stocks (market-wide lists such as 重点监控名单)."""
        conditions = [StockEvent.kind == kind, StockEvent.event_date >= since]
        if until is not None:
            conditions.append(StockEvent.event_date <= until)
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockEvent).where(and_(*conditions)).order_by(desc(StockEvent.event_date), desc(StockEvent.id))
            ).scalars().all()
            return [self._to_dict(row) for row in rows]

    def latest_fetch_for_code(self, code: str, *, kinds: Optional[Sequence[str]] = None) -> Optional[Any]:
        conditions = [StockEvent.code == code]
        if kinds:
            conditions.append(StockEvent.kind.in_(list(kinds)))
        with self.db.get_session() as session:
            return session.execute(
                select(StockEvent.fetched_at).where(and_(*conditions))
                .order_by(desc(StockEvent.fetched_at)).limit(1)
            ).scalar()

    def mark_ingested(self, kind: str, ann_date: date, *, rows: int, status: str, error: Optional[str] = None) -> None:
        with self.db.get_session() as session:
            row = session.execute(
                select(StockEventIngest).where(StockEventIngest.kind == kind, StockEventIngest.ann_date == ann_date)
            ).scalar_one_or_none()
            if row is None:
                row = StockEventIngest(kind=kind, ann_date=ann_date)
                session.add(row)
            row.rows, row.status, row.error, row.fetched_at = rows, status, (error or None) and error[:500], utc_naive_now()
            session.commit()

    def ingested_days(self, kind: str, *, since: date) -> Dict[date, str]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockEventIngest.ann_date, StockEventIngest.status)
                .where(StockEventIngest.kind == kind, StockEventIngest.ann_date >= since)
            ).all()
            return {day: status for day, status in rows}

    def ingest_status(self) -> List[Dict[str, Any]]:
        """Latest ingest row per kind, for diagnostics."""
        with self.db.get_session() as session:
            rows = session.execute(select(StockEventIngest).order_by(desc(StockEventIngest.ann_date))).scalars().all()
            latest: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                latest.setdefault(row.kind, {
                    "kind": row.kind, "ann_date": row.ann_date.isoformat(), "status": row.status,
                    "rows": row.rows, "error": row.error,
                    "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
                })
            return list(latest.values())

    def prune(self, *, before: date) -> int:
        """Drop past events older than ``before`` (future-dated 解禁 rows are kept)."""
        with self.db.get_session() as session:
            result = session.execute(delete(StockEvent).where(StockEvent.event_date < before))
            session.execute(delete(StockEventIngest).where(StockEventIngest.ann_date < before))
            session.commit()
            return int(result.rowcount or 0)

    @staticmethod
    def _to_dict(row: StockEvent) -> Dict[str, Any]:
        try:
            payload = json.loads(row.payload) if row.payload else {}
        except (TypeError, ValueError):
            payload = {}
        return {
            "code": row.code, "kind": row.kind, "event_date": row.event_date.isoformat(), "title": row.title,
            "payload": payload, "source": row.source,
            "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        }
