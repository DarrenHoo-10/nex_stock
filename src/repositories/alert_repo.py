# -*- coding: utf-8 -*-
"""Alert repository.

Provides DB access helpers for alert-center P1 API tables.

Multi-user (docs/design/multi-user.md): rules are personal data, so every method takes the owning
``user_id``. Triggers, notification attempts and cooldowns carry no owner column and are reached
only through an owned rule. ``list_all_enabled_rules`` is the one system-wide read: the alert worker
evaluates every user's rules, at most ``per_user_limit`` per user so one user cannot push another's out.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, delete, desc, func, or_, select

from src.config import get_primary_user_id
from src.storage import (
    AlertCooldownRecord,
    AlertNotificationRecord,
    AlertRuleRecord,
    AlertTriggerRecord,
    DatabaseManager,
)


class AlertRuleLimitExceeded(ValueError):
    """The user already has the maximum number of enabled alert rules."""


def _rule_ids_of(user_id: str):
    return select(AlertRuleRecord.id).where(AlertRuleRecord.user_id == user_id)


def _owned_trigger_clause(user_id: str):
    """Triggers of ``user_id``'s rules; rule-less rows (legacy ``.env`` rules) belong to the primary user."""
    clause = AlertTriggerRecord.rule_id.in_(_rule_ids_of(user_id))
    if user_id == get_primary_user_id():
        clause = or_(clause, AlertTriggerRecord.rule_id.is_(None))
    return clause


def _owned_notification_clause(user_id: str):
    """Notification attempts of ``user_id``'s triggers (trigger-less rows belong to the primary user)."""
    clause = AlertNotificationRecord.trigger_id.in_(
        select(AlertTriggerRecord.id).where(_owned_trigger_clause(user_id))
    )
    if user_id == get_primary_user_id():
        clause = or_(clause, AlertNotificationRecord.trigger_id.is_(None))
    return clause


class AlertRepository:
    """DB access layer for alert rules and alert history (scoped by ``user_id``)."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    @staticmethod
    def _owns_rule(session, user_id: str, rule_id: Optional[int]) -> bool:
        if rule_id is None:
            return False
        return session.execute(
            select(AlertRuleRecord.id)
            .where(AlertRuleRecord.id == rule_id, AlertRuleRecord.user_id == user_id)
            .limit(1)
        ).scalar_one_or_none() is not None

    def _require_owned_rule(self, session, user_id: str, rule_id: Optional[int]) -> None:
        if not self._owns_rule(session, user_id, rule_id):
            raise ValueError("alert rule not found for user")

    @staticmethod
    def _enforce_enabled_limit(
        session, user_id: str, max_enabled: Optional[int], *, exclude_rule_id: Optional[int] = None,
    ) -> None:
        """Raise when enabling one more rule would exceed ``max_enabled`` (``None`` = unlimited)."""
        if max_enabled is None:
            return
        query = select(func.count(AlertRuleRecord.id)).where(
            AlertRuleRecord.user_id == user_id, AlertRuleRecord.enabled.is_(True),
        )
        if exclude_rule_id is not None:
            query = query.where(AlertRuleRecord.id != exclude_rule_id)
        if (session.execute(query).scalar() or 0) >= max_enabled:
            raise AlertRuleLimitExceeded(f"enabled alert rule limit reached ({max_enabled})")

    def create_rule(
        self, user_id: str, fields: Dict[str, Any], *, max_enabled: Optional[int] = None,
    ) -> AlertRuleRecord:
        with self.db.get_session() as session:
            if fields.get("enabled", True):
                self._enforce_enabled_limit(session, user_id, max_enabled)
            row = AlertRuleRecord(**{**fields, "user_id": user_id})
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_rule(self, user_id: str, rule_id: int) -> Optional[AlertRuleRecord]:
        with self.db.get_session() as session:
            return session.execute(
                select(AlertRuleRecord)
                .where(AlertRuleRecord.id == rule_id, AlertRuleRecord.user_id == user_id)
                .limit(1)
            ).scalar_one_or_none()

    def update_rule(
        self, user_id: str, rule_id: int, fields: Dict[str, Any], *, max_enabled: Optional[int] = None,
    ) -> Optional[AlertRuleRecord]:
        fields = {key: value for key, value in fields.items() if key not in {"id", "user_id"}}
        with self.db.get_session() as session:
            row = session.execute(
                select(AlertRuleRecord)
                .where(AlertRuleRecord.id == rule_id, AlertRuleRecord.user_id == user_id)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            if fields.get("enabled") and not row.enabled:
                self._enforce_enabled_limit(session, user_id, max_enabled, exclude_rule_id=rule_id)
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
            return row

    def delete_rule(self, user_id: str, rule_id: int) -> bool:
        """Delete an owned rule together with its trigger/notification/cooldown history.

        SQLite may reuse a deleted rule's id, so history left behind would attach to another
        user's next rule.
        """
        with self.db.get_session() as session:
            if not self._owns_rule(session, user_id, rule_id):
                return False
            trigger_ids = select(AlertTriggerRecord.id).where(AlertTriggerRecord.rule_id == rule_id)
            session.execute(delete(AlertNotificationRecord).where(AlertNotificationRecord.trigger_id.in_(trigger_ids)))
            session.execute(delete(AlertTriggerRecord).where(AlertTriggerRecord.rule_id == rule_id))
            session.execute(delete(AlertCooldownRecord).where(AlertCooldownRecord.rule_id == rule_id))
            result = session.execute(
                delete(AlertRuleRecord).where(AlertRuleRecord.id == rule_id, AlertRuleRecord.user_id == user_id)
            )
            session.commit()
            return bool(result.rowcount)

    def list_rules(
        self,
        user_id: str,
        *,
        enabled: Optional[bool] = None,
        alert_type: Optional[str] = None,
        target_scope: Optional[str] = None,
        target: Optional[str] = None,
        source: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[AlertRuleRecord], int]:
        conditions = [AlertRuleRecord.user_id == user_id]
        if enabled is not None:
            conditions.append(AlertRuleRecord.enabled.is_(enabled))
        if alert_type:
            conditions.append(AlertRuleRecord.alert_type == alert_type)
        if target_scope:
            conditions.append(AlertRuleRecord.target_scope == target_scope)
        if target:
            if target_scope == "single_symbol":
                conditions.append(func.lower(AlertRuleRecord.target) == target.strip().lower())
            else:
                conditions.append(AlertRuleRecord.target == target)
        if source:
            conditions.append(AlertRuleRecord.source == source)

        where_clause = and_(*conditions)
        offset = (page - 1) * page_size
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(AlertRuleRecord.id)).select_from(AlertRuleRecord).where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(AlertRuleRecord)
                .where(where_clause)
                .order_by(desc(AlertRuleRecord.updated_at), desc(AlertRuleRecord.id))
                .offset(offset)
                .limit(page_size)
            ).scalars().all()
            return list(rows), int(total)

    def list_all_enabled_rules(self, *, per_user_limit: int = 1000) -> List[AlertRuleRecord]:
        """Enabled rules of EVERY user (alert worker only; each row carries its ``user_id``).

        Each user contributes at most ``per_user_limit`` rules (their most recently updated), so a user
        with many rules can never push another user's rules out of the selection.
        """
        safe_limit = max(1, int(per_user_limit))
        ranked = (
            select(
                AlertRuleRecord.id.label("rule_id"),
                func.row_number().over(
                    partition_by=AlertRuleRecord.user_id,
                    order_by=(desc(AlertRuleRecord.updated_at), desc(AlertRuleRecord.id)),
                ).label("rank"),
            )
            .where(AlertRuleRecord.enabled.is_(True))
            .subquery()
        )
        with self.db.get_session() as session:
            rows = session.execute(
                select(AlertRuleRecord)
                .join(ranked, ranked.c.rule_id == AlertRuleRecord.id)
                .where(ranked.c.rank <= safe_limit)
                .order_by(desc(AlertRuleRecord.updated_at), desc(AlertRuleRecord.id))
            ).scalars().all()
            return list(rows)

    def _check_trigger_owner(self, session, user_id: str, fields: Dict[str, Any]) -> None:
        rule_id = fields.get("rule_id")
        if rule_id is None:
            if user_id != get_primary_user_id():
                raise ValueError("rule-less alert triggers belong to the primary user")
            return
        self._require_owned_rule(session, user_id, rule_id)

    def create_trigger(self, user_id: str, fields: Dict[str, Any]) -> AlertTriggerRecord:
        self._validate_trigger_fields(fields)

        with self.db.get_session() as session:
            self._check_trigger_owner(session, user_id, fields)
            row = AlertTriggerRecord(**fields)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def create_trigger_if_absent(self, user_id: str, fields: Dict[str, Any]) -> Tuple[AlertTriggerRecord, bool]:
        """Create a triggered history row unless the same DB signal already exists.

        Callers must use this only after they have decided the trigger is safe to
        deduplicate. Non-triggered or timestamp-less history should use
        ``create_trigger`` so audit rows are not silently reclassified as deduped.
        """
        self._validate_trigger_fields(fields)

        rule_id = fields.get("rule_id")
        data_timestamp = fields.get("data_timestamp")
        if fields.get("status") != "triggered" or rule_id is None or data_timestamp is None:
            raise ValueError(
                "create_trigger_if_absent requires triggered status, rule_id, and data_timestamp"
            )

        with self.db.get_session() as session:
            self._check_trigger_owner(session, user_id, fields)
            query = select(AlertTriggerRecord).where(
                AlertTriggerRecord.rule_id == rule_id,
                AlertTriggerRecord.target == fields.get("target"),
                AlertTriggerRecord.status == "triggered",
                AlertTriggerRecord.data_timestamp == data_timestamp,
            )
            data_source = fields.get("data_source")
            if data_source is None:
                query = query.where(AlertTriggerRecord.data_source.is_(None))
            else:
                query = query.where(AlertTriggerRecord.data_source == data_source)

            existing = session.execute(
                query.order_by(AlertTriggerRecord.id.asc()).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False

            row = AlertTriggerRecord(**fields)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row, True

    @staticmethod
    def _validate_trigger_fields(fields: Dict[str, Any]) -> None:
        if not fields.get("target"):
            raise ValueError("alert trigger target is required")
        if not fields.get("status"):
            raise ValueError("alert trigger status is required")

    def record_notification_attempt(self, user_id: str, fields: Dict[str, Any]) -> AlertNotificationRecord:
        if not fields.get("channel"):
            raise ValueError("alert notification channel is required")

        with self.db.get_session() as session:
            trigger_id = fields.get("trigger_id")
            if trigger_id is None:
                if user_id != get_primary_user_id():
                    raise ValueError("trigger-less notification attempts belong to the primary user")
            elif session.execute(
                select(AlertTriggerRecord.id)
                .where(AlertTriggerRecord.id == trigger_id, _owned_trigger_clause(user_id))
                .limit(1)
            ).scalar_one_or_none() is None:
                raise ValueError("alert trigger not found for user")
            row = AlertNotificationRecord(**fields)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def get_active_cooldown(
        self,
        user_id: str,
        *,
        rule_id: int,
        target: str,
        severity: Optional[str],
        now: Optional[datetime] = None,
    ) -> Optional[AlertCooldownRecord]:
        now_value = now or datetime.now()
        with self.db.get_session() as session:
            if not self._owns_rule(session, user_id, rule_id):
                return None
            return session.execute(
                select(AlertCooldownRecord)
                .where(
                    AlertCooldownRecord.rule_id == rule_id,
                    AlertCooldownRecord.target == target,
                    AlertCooldownRecord.severity == severity,
                    AlertCooldownRecord.state == "active",
                    AlertCooldownRecord.cooldown_until > now_value,
                )
                .order_by(desc(AlertCooldownRecord.cooldown_until), desc(AlertCooldownRecord.id))
                .limit(1)
            ).scalar_one_or_none()

    def upsert_cooldown(
        self,
        user_id: str,
        *,
        rule_id: int,
        rule_key: Optional[str],
        target: str,
        severity: Optional[str],
        last_triggered_at: datetime,
        cooldown_until: datetime,
        reason: Optional[str] = None,
        state: str = "active",
    ) -> AlertCooldownRecord:
        with self.db.get_session() as session:
            self._require_owned_rule(session, user_id, rule_id)
            row = session.execute(
                select(AlertCooldownRecord)
                .where(
                    AlertCooldownRecord.rule_id == rule_id,
                    AlertCooldownRecord.target == target,
                    AlertCooldownRecord.severity == severity,
                )
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                row = AlertCooldownRecord(
                    rule_id=rule_id,
                    rule_key=rule_key,
                    target=target,
                    severity=severity,
                )
                session.add(row)
            row.rule_key = rule_key
            row.last_triggered_at = last_triggered_at
            row.cooldown_until = cooldown_until
            row.reason = reason
            row.state = state
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
            return row

    def get_rule_cooldown_summary(
        self,
        user_id: str,
        *,
        rule_id: int,
        target: str,
        severity: Optional[str],
    ) -> Optional[AlertCooldownRecord]:
        with self.db.get_session() as session:
            if not self._owns_rule(session, user_id, rule_id):
                return None
            return session.execute(
                select(AlertCooldownRecord)
                .where(
                    AlertCooldownRecord.rule_id == rule_id,
                    AlertCooldownRecord.target == target,
                    AlertCooldownRecord.severity == severity,
                )
                .order_by(desc(AlertCooldownRecord.updated_at), desc(AlertCooldownRecord.id))
                .limit(1)
            ).scalar_one_or_none()

    def list_triggers(
        self,
        user_id: str,
        *,
        rule_id: Optional[int] = None,
        target: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[AlertTriggerRecord], int]:
        conditions = [_owned_trigger_clause(user_id)]
        if rule_id is not None:
            conditions.append(AlertTriggerRecord.rule_id == rule_id)
        if target:
            conditions.append(AlertTriggerRecord.target == target)
        if status:
            conditions.append(AlertTriggerRecord.status == status)

        where_clause = and_(*conditions)
        offset = (page - 1) * page_size
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(AlertTriggerRecord.id)).select_from(AlertTriggerRecord).where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(AlertTriggerRecord)
                .where(where_clause)
                .order_by(desc(AlertTriggerRecord.triggered_at), desc(AlertTriggerRecord.id))
                .offset(offset)
                .limit(page_size)
            ).scalars().all()
            return list(rows), int(total)

    def list_notifications(
        self,
        user_id: str,
        *,
        trigger_id: Optional[int] = None,
        channel: Optional[str] = None,
        success: Optional[bool] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[AlertNotificationRecord], int]:
        conditions = [_owned_notification_clause(user_id)]
        if trigger_id is not None:
            conditions.append(AlertNotificationRecord.trigger_id == trigger_id)
        if channel:
            conditions.append(AlertNotificationRecord.channel == channel)
        if success is not None:
            conditions.append(AlertNotificationRecord.success.is_(success))

        where_clause = and_(*conditions)
        offset = (page - 1) * page_size
        with self.db.get_session() as session:
            total = session.execute(
                select(func.count(AlertNotificationRecord.id))
                .select_from(AlertNotificationRecord)
                .where(where_clause)
            ).scalar() or 0
            rows = session.execute(
                select(AlertNotificationRecord)
                .where(where_clause)
                .order_by(desc(AlertNotificationRecord.created_at), desc(AlertNotificationRecord.id))
                .offset(offset)
                .limit(page_size)
            ).scalars().all()
            return list(rows), int(total)
