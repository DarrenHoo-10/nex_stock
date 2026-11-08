# -*- coding: utf-8 -*-
"""Repository helpers for persisted market / symbol intelligence."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, delete, desc, func, or_, select
from sqlalchemy.exc import IntegrityError

from src.storage import (
    DatabaseManager,
    IntelligenceItem,
    IntelligenceSource,
    INTELLIGENCE_ITEM_NULL_SCOPE_VALUE,
    NewsFetchGap,
    NewsFetchState,
)

AKSHARE_SOURCE_TYPE = "akshare"
FLASH_KINDS = ("flash", "morning_brief")  # kept for NEWS_FLASH_RETENTION_DAYS
COMPANY_KINDS = ("announcement",)  # kept for NEWS_COMPANY_RETENTION_DAYS
CLASSIFIABLE_SOURCE_TYPES = (AKSHARE_SOURCE_TYPE, "newsnow")
_NORMALIZE_TITLE_RE = re.compile(r"[^0-9A-Za-z一-鿿]+")
_NEAR_DUP_MIN_CONTAINED_CHARS = 12
_IN_CHUNK = 400


def normalize_title_key(title: Optional[str]) -> str:
    """Punctuation / whitespace / case-insensitive title key used for cross-source near-dup checks."""
    return _NORMALIZE_TITLE_RE.sub("", str(title or "")).lower()


def _titles_near_duplicate(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    short, long_ = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= _NEAR_DUP_MIN_CONTAINED_CHARS and short in long_


class IntelligenceRepository:
    """DB access layer for configurable intelligence sources and items."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    def create_source(self, fields: Dict[str, Any]) -> IntelligenceSource:
        with self.db.get_session() as session:
            row = IntelligenceSource(**fields)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_source(self, source_id: int) -> Optional[IntelligenceSource]:
        with self.db.get_session() as session:
            return session.execute(
                select(IntelligenceSource).where(IntelligenceSource.id == source_id).limit(1)
            ).scalar_one_or_none()

    def get_source_by_name(self, name: str) -> Optional[IntelligenceSource]:
        with self.db.get_session() as session:
            return session.execute(
                select(IntelligenceSource).where(IntelligenceSource.name == name).limit(1)
            ).scalar_one_or_none()

    def update_source_enabled(self, source_id: int, enabled: bool) -> None:
        with self.db.get_session() as session:
            row = session.execute(
                select(IntelligenceSource).where(IntelligenceSource.id == source_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            row.enabled = bool(enabled)
            row.updated_at = datetime.now()
            session.commit()

    def list_sources(
        self,
        *,
        enabled: Optional[bool] = None,
        source_type: Optional[str] = None,
        scope_type: Optional[str] = None,
        market: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[IntelligenceSource], int]:
        conditions = []
        if enabled is not None:
            conditions.append(IntelligenceSource.enabled.is_(enabled))
        if source_type:
            conditions.append(IntelligenceSource.source_type == source_type)
        if scope_type:
            conditions.append(IntelligenceSource.scope_type == scope_type)
        if market:
            conditions.append(IntelligenceSource.market == market)
        where_clause = and_(*conditions) if conditions else True
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 100))
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(IntelligenceSource.id)).select_from(IntelligenceSource).where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(IntelligenceSource)
                .where(where_clause)
                .order_by(desc(IntelligenceSource.updated_at), desc(IntelligenceSource.id))
                .offset((safe_page - 1) * safe_size)
                .limit(safe_size)
            ).scalars().all()
            return list(rows), int(total)

    def update_source_status(
        self,
        source_id: int,
        *,
        status: str,
        error: Optional[str] = None,
        fetched_at: Optional[datetime] = None,
    ) -> None:
        with self.db.get_session() as session:
            row = session.execute(
                select(IntelligenceSource).where(IntelligenceSource.id == source_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return
            row.last_status = status
            row.last_error = error
            if fetched_at is not None:
                row.last_fetched_at = fetched_at
            row.updated_at = datetime.now()
            session.commit()

    def upsert_items(self, items: Iterable[Dict[str, Any]]) -> int:
        saved = 0
        with self.db.get_session() as session:
            for fields in items:
                url = (fields.get("url") or "").strip()
                title = (fields.get("title") or "").strip()
                if not url or not title:
                    continue
                item_fields = dict(fields)
                scope_value = self._normalize_scope_value(item_fields.get("scope_value"))
                item_fields["scope_value"] = scope_value
                source_id = item_fields.get("source_id")
                conditions = [
                    IntelligenceItem.url == url,
                    IntelligenceItem.source_type == (item_fields.get("source_type") or "rss"),
                    IntelligenceItem.scope_type == (item_fields.get("scope_type") or "market"),
                    IntelligenceItem.market == (item_fields.get("market") or "cn"),
                ]
                if source_id is None:
                    conditions.append(IntelligenceItem.source_id.is_(None))
                    conditions.append(IntelligenceItem.source_name == item_fields.get("source_name"))
                else:
                    conditions.append(IntelligenceItem.source_id == source_id)
                conditions.append(IntelligenceItem.scope_value == scope_value)
                existing = session.execute(
                    select(IntelligenceItem).where(and_(*conditions)).limit(1)
                ).scalar_one_or_none()
                if existing is not None:
                    existing.summary = item_fields.get("summary") or existing.summary
                    existing.source = item_fields.get("source") or existing.source
                    existing.published_at = item_fields.get("published_at") or existing.published_at
                    existing.fetched_at = item_fields.get("fetched_at") or datetime.now()
                    existing.raw_payload = item_fields.get("raw_payload") or existing.raw_payload
                    continue
                try:
                    with session.begin_nested():
                        session.add(IntelligenceItem(**item_fields))
                        session.flush()
                    saved += 1
                except IntegrityError:
                    continue
            session.commit()
        return saved

    def list_items(
        self,
        *,
        scope_type: Optional[str] = None,
        scope_value: Optional[str] = None,
        market: Optional[str] = None,
        query: Optional[str] = None,
        days: Optional[int] = None,
        published_days: Optional[int] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Tuple[List[IntelligenceItem], int]:
        conditions = []
        if scope_type:
            conditions.append(IntelligenceItem.scope_type == scope_type)
        if scope_value:
            normalized_scope = self._normalize_scope_value(scope_value)
            if scope_type == "symbol":
                conditions.append(func.lower(IntelligenceItem.scope_value) == normalized_scope.lower())
            else:
                conditions.append(IntelligenceItem.scope_value == normalized_scope)
        if market:
            conditions.append(IntelligenceItem.market == market)
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(or_(IntelligenceItem.title.like(pattern), IntelligenceItem.summary.like(pattern)))
        if days is not None:
            conditions.append(IntelligenceItem.fetched_at >= datetime.now() - timedelta(days=max(1, int(days))))
        if published_days is not None:
            published_cutoff = datetime.now() - timedelta(days=max(1, int(published_days)))
            conditions.append(IntelligenceItem.published_at >= published_cutoff)
        where_clause = and_(*conditions) if conditions else True
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 100))
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(IntelligenceItem.id)).select_from(IntelligenceItem).where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(IntelligenceItem)
                .where(where_clause)
                .order_by(desc(func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at)), desc(IntelligenceItem.id))
                .offset((safe_page - 1) * safe_size)
                .limit(safe_size)
            ).scalars().all()
            return list(rows), int(total)

    @staticmethod
    def _normalize_scope_value(value: Any) -> str:
        normalized = str(value or "").strip()
        return normalized or INTELLIGENCE_ITEM_NULL_SCOPE_VALUE

    def apply_retention(self, retention_days: int) -> int:
        """Purge old rss / atom / newsnow items (``NEWS_INTEL_RETENTION_DAYS``).

        AKShare items have their own tiers (``apply_news_retention``) and are never touched here.
        """
        cutoff = datetime.now() - timedelta(days=max(1, int(retention_days)))
        with self.db.get_session() as session:
            result = session.execute(
                delete(IntelligenceItem).where(
                    IntelligenceItem.fetched_at < cutoff,
                    IntelligenceItem.source_type != AKSHARE_SOURCE_TYPE,
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    def apply_news_retention(self, *, flash_days: int, company_days: int, now: datetime) -> Dict[str, int]:
        """Tiered purge of AKShare items by publish time (``now`` is naive China time).

        Market flashes and 财经早餐 keep ``flash_days``; announcements / company events keep
        ``company_days``. Gap records older than the flash window are dropped with the flashes.
        """
        age = func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at)
        flash_cutoff = now - timedelta(days=max(1, int(flash_days)))
        company_cutoff = now - timedelta(days=max(1, int(company_days)))
        with self.db.get_session() as session:
            flash = session.execute(
                delete(IntelligenceItem).where(
                    IntelligenceItem.source_type == AKSHARE_SOURCE_TYPE,
                    or_(IntelligenceItem.item_kind.in_(FLASH_KINDS), IntelligenceItem.item_kind.is_(None)),
                    age < flash_cutoff,
                )
            )
            company = session.execute(
                delete(IntelligenceItem).where(
                    IntelligenceItem.source_type == AKSHARE_SOURCE_TYPE,
                    IntelligenceItem.item_kind.notin_(FLASH_KINDS),
                    IntelligenceItem.item_kind.isnot(None),
                    age < company_cutoff,
                )
            )
            gaps = session.execute(delete(NewsFetchGap).where(NewsFetchGap.gap_to < flash_cutoff))
            session.commit()
            return {
                "flash": int(flash.rowcount or 0),
                "company": int(company.rowcount or 0),
                "gaps": int(gaps.rowcount or 0),
            }

    # ------------------------------------------------------------------
    # AKShare news pool
    # ------------------------------------------------------------------
    def upsert_news_records(
        self,
        records: Iterable[Any],
        *,
        fetched_at: datetime,
        dedup_window_minutes: int = 30,
    ) -> Dict[str, Any]:
        """Insert normalised news records with dedup.

        * same-source key: ``(source_name, url, scope)`` already stored -> counted as ``same_source``;
        * cross-source near-dup (flash kinds only): normalised title equal (or one contained in the other,
          >= 12 chars) to an item published within ``dedup_window_minutes`` -> counted as ``near_duplicate``.
          Records are handled in the given order, so the earlier source in a merged batch wins.

        Returns ``{"inserted": [ids], "same_source": n, "near_duplicate": n}``.
        """
        items = [r for r in records if (getattr(r, "url", "") or "").strip() and (getattr(r, "title", "") or "").strip()]
        result: Dict[str, Any] = {"inserted": [], "same_source": 0, "near_duplicate": 0}
        if not items:
            return result
        window = timedelta(minutes=max(1, int(dedup_window_minutes)))
        published = [r.published_at for r in items if r.published_at is not None]
        with self.db.get_session() as session:
            existing_keys = set()
            urls = list({r.url for r in items})
            for start in range(0, len(urls), _IN_CHUNK):
                chunk = urls[start:start + _IN_CHUNK]
                rows = session.execute(
                    select(
                        IntelligenceItem.source_name, IntelligenceItem.url,
                        IntelligenceItem.scope_type, IntelligenceItem.scope_value,
                    ).where(
                        IntelligenceItem.source_type == AKSHARE_SOURCE_TYPE,
                        IntelligenceItem.url.in_(chunk),
                    )
                ).all()
                existing_keys.update((row[0], row[1], row[2], row[3]) for row in rows)

            recent: List[Tuple[str, Optional[datetime]]] = []
            if published:
                rows = session.execute(
                    select(IntelligenceItem.title, IntelligenceItem.published_at).where(
                        IntelligenceItem.item_kind.in_(FLASH_KINDS),
                        IntelligenceItem.published_at >= min(published) - window,
                        IntelligenceItem.published_at <= max(published) + window,
                    )
                ).all()
                recent = [(normalize_title_key(row[0]), row[1]) for row in rows]

            for record in items:
                scope_value = self._normalize_scope_value(record.scope_value)
                key = (record.source_name, record.url, record.scope_type, scope_value)
                if key in existing_keys:
                    result["same_source"] += 1
                    continue
                title_key = normalize_title_key(record.title)
                if record.kind in FLASH_KINDS and record.published_at is not None and title_key:
                    if any(
                        other_at is not None
                        and abs(other_at - record.published_at) <= window
                        and _titles_near_duplicate(title_key, other_key)
                        for other_key, other_at in recent
                    ):
                        result["near_duplicate"] += 1
                        continue
                row = IntelligenceItem(
                    source_id=None,
                    source_name=record.source_name,
                    source_type=AKSHARE_SOURCE_TYPE,
                    title=record.title,
                    summary=record.summary,
                    url=record.url,
                    source=record.source_label,
                    published_at=record.published_at,
                    fetched_at=fetched_at,
                    scope_type=record.scope_type,
                    scope_value=scope_value,
                    market="cn",
                    raw_payload=json.dumps(record.raw_payload or {}, ensure_ascii=False),
                    item_kind=record.kind,
                    news_scope="stock" if record.scope_type == "symbol" else None,
                    stock_codes=json.dumps(record.stock_codes, ensure_ascii=False) if record.stock_codes else None,
                )
                try:
                    with session.begin_nested():
                        session.add(row)
                        session.flush()
                except IntegrityError:
                    result["same_source"] += 1
                    continue
                existing_keys.add(key)
                if record.kind in FLASH_KINDS:
                    recent.append((title_key, record.published_at))
                result["inserted"].append(int(row.id))
            session.commit()
        return result

    def get_fetch_state(self, source_key: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(NewsFetchState, source_key)
            return self._state_to_dict(row) if row is not None else None

    def list_fetch_states(self) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(select(NewsFetchState).order_by(NewsFetchState.source_key)).scalars().all()
            return [self._state_to_dict(row) for row in rows]

    def save_fetch_state(self, source_key: str, **fields: Any) -> None:
        """Create / update one checkpoint row; only the given fields change."""
        with self.db.get_session() as session:
            row = session.get(NewsFetchState, source_key)
            if row is None:
                row = NewsFetchState(source_key=source_key)
                session.add(row)
            for name, value in fields.items():
                setattr(row, name, value)
            session.commit()

    def newest_flash_published_at(self) -> Optional[datetime]:
        with self.db.get_session() as session:
            return session.execute(
                select(func.max(IntelligenceItem.published_at)).where(
                    IntelligenceItem.source_type == AKSHARE_SOURCE_TYPE,
                    IntelligenceItem.item_kind == "flash",
                )
            ).scalar()

    def add_fetch_gap(self, source_key: str, gap_from: datetime, gap_to: datetime, *, covered: bool,
                      detected_at: datetime) -> None:
        with self.db.get_session() as session:
            session.add(NewsFetchGap(
                source_key=source_key, gap_from=gap_from, gap_to=gap_to, covered=bool(covered),
                detected_at=detected_at,
            ))
            session.commit()

    def list_fetch_gaps(self, *, since: Optional[datetime] = None, covered: Optional[bool] = None) -> List[Dict[str, Any]]:
        conditions = []
        if since is not None:
            conditions.append(NewsFetchGap.gap_to >= since)
        if covered is not None:
            conditions.append(NewsFetchGap.covered.is_(covered))
        with self.db.get_session() as session:
            stmt = select(NewsFetchGap)
            if conditions:
                stmt = stmt.where(and_(*conditions))
            rows = session.execute(stmt.order_by(desc(NewsFetchGap.gap_from), desc(NewsFetchGap.id))).scalars().all()
            return [
                {
                    "id": row.id, "source_key": row.source_key, "gap_from": row.gap_from, "gap_to": row.gap_to,
                    "covered": bool(row.covered), "detected_at": row.detected_at,
                }
                for row in rows
            ]

    @staticmethod
    def _state_to_dict(row: NewsFetchState) -> Dict[str, Any]:
        return {
            "source_key": row.source_key,
            "checkpoint_at": row.checkpoint_at,
            "last_fetch_at": row.last_fetch_at,
            "last_success_at": row.last_success_at,
            "last_status": row.last_status,
            "last_error": row.last_error,
            "last_count": row.last_count,
            "gap_check": row.gap_check,
        }

    # ------------------------------------------------------------------
    # classification
    # ------------------------------------------------------------------
    def list_unclassified_items(
        self, *, limit: int, max_attempts: int, published_since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Newest unclassified news items (AKShare + NewsNow) still under the attempt cap."""
        conditions = [
            IntelligenceItem.classified_at.is_(None),
            IntelligenceItem.source_type.in_(CLASSIFIABLE_SOURCE_TYPES),
            func.coalesce(IntelligenceItem.classify_attempts, 0) < max(1, int(max_attempts)),
        ]
        if published_since is not None:
            conditions.append(func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at) >= published_since)
        with self.db.get_session() as session:
            rows = session.execute(
                select(IntelligenceItem)
                .where(and_(*conditions))
                .order_by(desc(func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at)),
                          desc(IntelligenceItem.id))
                .limit(max(1, int(limit)))
            ).scalars().all()
            return [
                {
                    "id": row.id, "title": row.title, "summary": row.summary, "item_kind": row.item_kind,
                    "source": row.source, "scope_type": row.scope_type, "scope_value": row.scope_value,
                    "stock_codes": self.loads_list(row.stock_codes), "attempts": int(row.classify_attempts or 0),
                    "published_at": row.published_at,
                }
                for row in rows
            ]

    def save_classification(self, item_id: int, fields: Dict[str, Any]) -> None:
        with self.db.get_session() as session:
            row = session.get(IntelligenceItem, item_id)
            if row is None:
                return
            for name, value in fields.items():
                setattr(row, name, value)
            session.commit()

    def bump_classify_attempts(self, item_ids: Iterable[int]) -> None:
        ids = [int(i) for i in item_ids]
        if not ids:
            return
        with self.db.get_session() as session:
            for start in range(0, len(ids), _IN_CHUNK):
                for row in session.execute(
                    select(IntelligenceItem).where(IntelligenceItem.id.in_(ids[start:start + _IN_CHUNK]))
                ).scalars():
                    row.classify_attempts = int(row.classify_attempts or 0) + 1
            session.commit()

    def count_unclassified(self, *, max_attempts: int) -> int:
        with self.db.get_session() as session:
            return int(session.execute(
                select(func.count(IntelligenceItem.id)).where(
                    IntelligenceItem.classified_at.is_(None),
                    IntelligenceItem.source_type.in_(CLASSIFIABLE_SOURCE_TYPES),
                    func.coalesce(IntelligenceItem.classify_attempts, 0) < max(1, int(max_attempts)),
                )
            ).scalar() or 0)

    # ------------------------------------------------------------------
    # digest
    # ------------------------------------------------------------------
    def list_news_rows(
        self,
        *,
        since: datetime,
        until: datetime,
        min_importance: Optional[int] = None,
    ) -> List[IntelligenceItem]:
        """Candidate rows for the digest in a (padded) time window; callers refine in Python."""
        age = func.coalesce(IntelligenceItem.published_at, IntelligenceItem.fetched_at)
        conditions = [age >= since, age <= until]
        if min_importance is not None:
            conditions.append(IntelligenceItem.importance >= int(min_importance))
        with self.db.get_session() as session:
            rows = session.execute(
                select(IntelligenceItem)
                .where(and_(*conditions))
                .order_by(desc(age), desc(IntelligenceItem.id))
            ).scalars().all()
            return list(rows)

    @staticmethod
    def loads_list(raw: Any) -> List[Any]:
        if isinstance(raw, list):
            return raw
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return value if isinstance(value, list) else []
