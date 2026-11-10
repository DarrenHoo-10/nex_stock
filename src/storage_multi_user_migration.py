# -*- coding: utf-8 -*-
"""
Multi-user P1 schema migration (docs/design/multi-user.md section 4).

* adds ``user_id`` (and ``analysis_history.requester_user_id`` /
  ``model_channel_fingerprint``) to the personal-data tables and backfills every
  existing row with ``PRIMARY_USER_ID``;
* rebuilds ``plan_holding`` / ``board_follow`` / ``assistant_note`` so their unique
  constraints include ``user_id`` (SQLite cannot alter a constraint in place);
* P2a follow-up (``MULTI_USER_P2A_SCHEMA_VERSION``): adds ``assistant_note_proposal.user_id``
  (proposals have no ``note_id``, so they need their own owner column) and deletes orphan alert
  history (triggers / notifications / cooldowns whose rule no longer exists: alert history is owned
  through its rule, and a new rule reusing a deleted rule's id would inherit the orphans);
* P2b-2 follow-up (``MULTI_USER_P2B2_SCHEMA_VERSION``): adds ``analysis_history.is_private`` /
  ``report_trade_date`` / ``report_language`` and ``decision_signals.owner_user_id`` (report privacy and
  same-day report reuse). Backfill: a report produced with portfolio / position context is private to the
  primary user (and so are the signals extracted from it), everything else stays shared;
* P2b-2 security follow-up (``MULTI_USER_P2B2_PURGE_SCHEMA_VERSION``): deletes the backtest results, skill
  opinion samples / outcomes derived from private reports, and the backtest rollups of every (window, engine)
  bucket that lost rows. Backtests and skill weights are shared system output; a result carries the report's
  advice, stop-loss and take-profit, so a private report's must not stay in it (the repositories also exclude
  private reports from new candidates and reads, this removes what earlier code already produced);
* P2b-4 follow-up (``MULTI_USER_P2B4_SCHEMA_VERSION`` / ``MULTI_USER_P2B4_AGENT_PRIVACY_VERSION``): adds
  ``analysis_history.skill_fingerprint`` (the effective strategy set a report was produced with; old rows stay NULL
  and are never reused directly) and marks the legacy Agent-mode reports private. Before the privacy marker an Agent
  run's tool calls were not recorded, so it cannot be told whether a personal-data tool (the portfolio snapshot) ran;
  an Agent-mode report without ``report_trade_date`` (written before that marker existed) is therefore private to its
  requester (the primary user for legacy rows) and so are its decision signals; the derived data of private reports
  is purged again. Holdings must never be shared, so undeterminable means private;
* P3a follow-up (``MULTI_USER_P3A_SCHEMA_VERSION``): adds the ``user_notification_channel`` (personal push
  channels, config encrypted with the owner's data key) and ``user_notice_marker`` (once-only reminders) tables;
  no existing table or row is touched;
* is idempotent and recorded in ``schema_migrations``; everything runs in one
  transaction, a failure rolls back and is re-raised so startup is refused;
* the database file is backed up with the SQLite backup API (WAL-safe) before the
  first run against an old-schema database.

Called from ``DatabaseManager.__init__`` (SQLite only; other engines rely on
``metadata.create_all``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set

from sqlalchemy import MetaData, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

MULTI_USER_SCHEMA_VERSION = "2026-11-09-multi-user-p1-user-id"
MULTI_USER_SCHEMA_DESCRIPTION = (
    "Add user_id to personal-data tables, requester_user_id/model_channel_fingerprint to "
    "analysis_history; backfill PRIMARY_USER_ID; per-user unique constraints"
)

MULTI_USER_P2A_SCHEMA_VERSION = "2026-11-09-multi-user-p2a-note-proposal-user-id"
MULTI_USER_P2A_SCHEMA_DESCRIPTION = (
    "Add user_id to assistant_note_proposal (personal data isolation); backfill PRIMARY_USER_ID; "
    "delete orphan alert history"
)

MULTI_USER_P2B2_SCHEMA_VERSION = "2026-11-10-multi-user-p2b2-report-privacy"
MULTI_USER_P2B2_SCHEMA_DESCRIPTION = (
    "Add analysis_history.is_private/report_trade_date/report_language and decision_signals.owner_user_id; "
    "backfill: position-aware reports and their signals are private to the primary user"
)
MULTI_USER_P2B2_PURGE_SCHEMA_VERSION = "2026-11-10-multi-user-p2b2-private-derived-purge"
MULTI_USER_P2B2_PURGE_SCHEMA_DESCRIPTION = (
    "Delete backtest results / skill opinion samples and outcomes derived from private reports, and the "
    "backtest rollups of the buckets they were in (recomputed by the next backtest run)"
)
MULTI_USER_P2B4_SCHEMA_VERSION = "2026-11-10-multi-user-p2b4-skill-fingerprint"
MULTI_USER_P2B4_SCHEMA_DESCRIPTION = (
    "Add analysis_history.skill_fingerprint (effective strategy set of a report; NULL = never reused directly)"
)
MULTI_USER_P2B4_AGENT_PRIVACY_VERSION = "2026-11-10-multi-user-p2b4-legacy-agent-reports-private"
MULTI_USER_P2B4_AGENT_PRIVACY_DESCRIPTION = (
    "Mark legacy Agent-mode reports (no report_trade_date, so no recorded tool calls) private to their requester, "
    "with their decision signals; purge derived backtest / skill-opinion data again"
)
MULTI_USER_P3A_SCHEMA_VERSION = "2026-11-10-multi-user-p3a-notification-channels"
MULTI_USER_P3A_SCHEMA_DESCRIPTION = (
    "Add user_notification_channel (personal push channels, encrypted config) and user_notice_marker "
    "(once-only reminders)"
)
_P3A_TABLES = ("user_notification_channel", "user_notice_marker")
_PURGE_TABLES = ("backtest_results", "backtest_summaries", "skill_opinion_samples", "skill_opinion_outcomes")
_P2B2_COLUMNS = (
    ("analysis_history", "is_private", "BOOLEAN"),
    ("analysis_history", "report_trade_date", "VARCHAR(10)"),
    ("analysis_history", "report_language", "VARCHAR(8)"),
    ("decision_signals", "owner_user_id", "VARCHAR(64)"),
)
# ``holding_state`` values that prove the signal was extracted from a position-aware analysis.
_POSITION_AWARE_HOLDING_STATES = frozenset({"holding", "empty"})

# (table, column, parent table, parent column): rows whose non-NULL ``column`` points at no parent row.
# Order matters: notifications are checked after the triggers that orphaned them were deleted.
_ORPHAN_ALERT_HISTORY = (
    ("alert_triggers", "rule_id", "alert_rules", "id"),
    ("alert_notifications", "trigger_id", "alert_triggers", "id"),
    ("alert_cooldowns", "rule_id", "alert_rules", "id"),
)

_ANALYSIS_HISTORY_TABLE = "analysis_history"
_ANALYSIS_HISTORY_COLUMNS = {
    "requester_user_id": "VARCHAR(64)",
    "model_channel_fingerprint": "VARCHAR(64)",
}
_USER_ID_COLUMN_SQL = "VARCHAR(64)"


def _models():
    """ORM models touched by the migration (imported lazily: storage imports this module)."""
    from src import storage

    user_id_models = (
        storage.DailyPlanAccount,
        storage.DailyPlanHolding,
        storage.DailyPlanRun,
        storage.DailyPlanStock,
        storage.PortfolioAccount,
        storage.BoardFollow,
        storage.AlertRuleRecord,
        storage.ConversationSessionState,
        storage.AssistantNote,
        storage.LLMUsage,
    )
    rebuild_models = (storage.DailyPlanHolding, storage.BoardFollow, storage.AssistantNote)
    return user_id_models, rebuild_models


def _p2a_models():
    """Tables that gained ``user_id`` after P1 (own migration record)."""
    from src import storage

    return (storage.AssistantNoteProposal,)


def _p2b2_models():
    """Tables that gained report-privacy columns in P2b-2 (own migration record)."""
    from src import storage

    return (storage.AnalysisHistory, storage.DecisionSignalRecord)


def _table_columns(conn: Connection, table: str) -> Set[str]:
    return {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()}


def _existing_tables(conn: Connection) -> Set[str]:
    return {name for (name,) in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}


def _has_user_scoped_unique(conn: Connection, table: str) -> bool:
    """True when a plain (non-partial) UNIQUE index/constraint on ``table`` includes ``user_id``."""
    for row in conn.exec_driver_sql(f'PRAGMA index_list("{table}")').fetchall():
        # row: (seq, name, unique, origin, partial)
        if int(row[2]) != 1 or int(row[4]) == 1:
            continue
        columns = [info[2] for info in conn.exec_driver_sql(f'PRAGMA index_info("{row[1]}")').fetchall()]
        if "user_id" in columns:
            return True
    return False


def _is_recorded(conn: Connection, version: str = MULTI_USER_SCHEMA_VERSION) -> bool:
    if not conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone():
        return False
    return conn.execute(
        text("SELECT 1 FROM schema_migrations WHERE version = :version"),
        {"version": version},
    ).fetchone() is not None


def _record_version(conn: Connection, version: str, description: str) -> None:
    from src.storage import DatabaseSchemaMigration

    conn.execute(
        sqlite_insert(DatabaseSchemaMigration)
        .values(version=version, description=description, applied_at=datetime.now())
        .on_conflict_do_nothing(index_elements=["version"])
    )


def backup_before_multi_user_migration(engine: Engine) -> Optional[Path]:
    """Back up an old-schema SQLite database file beside itself; no-op when no migration is pending.

    Uses the SQLite online backup API (consistent with WAL). Raises on failure so that
    startup is refused instead of migrating without a restore point.
    """
    if engine.url.get_backend_name() != "sqlite":
        return None
    database = (engine.url.database or "").strip()
    if not database or database.lower() == ":memory:":
        return None
    db_path = Path(database)
    if not db_path.is_file() or db_path.stat().st_size == 0:
        return None

    user_id_models, _ = _models()
    affected = {model.__table__.name for model in user_id_models} | {_ANALYSIS_HISTORY_TABLE}
    p2a_affected = {model.__table__.name for model in _p2a_models()} | {
        table for table, _column, _parent, _parent_column in _ORPHAN_ALERT_HISTORY
    }
    p2b2_affected = {table for table, _column, _type in _P2B2_COLUMNS}
    purge_affected = set(_PURGE_TABLES)
    p2b4_affected = {_ANALYSIS_HISTORY_TABLE}
    with engine.connect() as conn:
        existing = {name for (name,) in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        p1_pending = bool(existing & affected) and not _is_recorded(conn)
        p2a_pending = bool(existing & p2a_affected) and not _is_recorded(conn, MULTI_USER_P2A_SCHEMA_VERSION)
        p2b2_pending = bool(existing & p2b2_affected) and not _is_recorded(conn, MULTI_USER_P2B2_SCHEMA_VERSION)
        purge_pending = bool(existing & purge_affected) and not _is_recorded(conn, MULTI_USER_P2B2_PURGE_SCHEMA_VERSION)
        p2b4_pending = bool(existing & p2b4_affected) and not (
            _is_recorded(conn, MULTI_USER_P2B4_SCHEMA_VERSION)
            and _is_recorded(conn, MULTI_USER_P2B4_AGENT_PRIVACY_VERSION)
        )
        # P3a only adds tables (create_all makes them); the restore point is still taken once for an existing database.
        p3a_pending = bool(existing) and not _is_recorded(conn, MULTI_USER_P3A_SCHEMA_VERSION)
        if not (p1_pending or p2a_pending or p2b2_pending or purge_pending or p2b4_pending or p3a_pending):
            return None

    backup_path = db_path.with_name(f"{db_path.name}.pre-multi-user-{datetime.now():%Y%m%d-%H%M%S}.bak")
    source = sqlite3.connect(str(db_path))
    try:
        target = sqlite3.connect(str(backup_path))
        try:
            source.backup(target)
            verdict = target.execute("PRAGMA integrity_check").fetchone()
        finally:
            target.close()
    finally:
        source.close()
    if not verdict or verdict[0] != "ok":
        raise RuntimeError(f"Database backup {backup_path} failed integrity check: {verdict}")
    logger.info("Multi-user migration: database backed up to %s", backup_path)
    return backup_path


def _rebuild_table(conn: Connection, model) -> None:
    """Recreate ``model``'s table with its current ORM shape, preserving every row, id and index."""
    table = model.__table__.name
    tmp = f"{table}__multi_user_new"
    conn.exec_driver_sql(f'DROP TABLE IF EXISTS "{tmp}"')
    tmp_table = model.__table__.to_metadata(MetaData(), name=tmp)
    # Indexes are recreated on the final table after the rename (their names must not collide).
    for index in list(tmp_table.indexes):
        tmp_table.indexes.discard(index)
    tmp_table.create(conn)

    old_columns = _table_columns(conn, table)
    columns = ", ".join(f'"{col.name}"' for col in model.__table__.columns if col.name in old_columns)
    conn.exec_driver_sql(f'INSERT INTO "{tmp}" ({columns}) SELECT {columns} FROM "{table}"')
    old_count = conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{table}"').scalar()
    new_count = conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{tmp}"').scalar()
    if old_count != new_count:
        raise RuntimeError(f"Rebuilding {table} lost rows: {old_count} -> {new_count}")
    conn.exec_driver_sql(f'DROP TABLE "{table}"')
    conn.exec_driver_sql(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    for index in model.__table__.indexes:
        index.create(conn, checkfirst=True)


def _add_column_if_missing(conn: Connection, table: str, column: str, sql_type: str) -> None:
    if column not in _table_columns(conn, table):
        conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {column} {sql_type}')


def _backfill_null(conn: Connection, table: str, column: str, primary_user_id: str) -> int:
    if column not in _table_columns(conn, table):
        return 0
    result = conn.execute(
        text(f'UPDATE "{table}" SET {column} = :uid WHERE {column} IS NULL'),
        {"uid": primary_user_id},
    )
    return result.rowcount or 0


def _delete_orphan_alert_history(conn: Connection) -> None:
    """Delete alert history whose rule (or trigger) no longer exists.

    Triggers / notifications / cooldowns have no owner column; they belong to whoever owns the rule with
    that id. Rule deletion did not cascade before P2a and SQLite reuses rowids, so a new rule could
    inherit another user's old history. Rows with a NULL link (``.env`` legacy rules) are kept.
    """
    existing = {name for (name,) in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, column, parent, parent_column in _ORPHAN_ALERT_HISTORY:
        if not {table, parent} <= existing or column not in _table_columns(conn, table):
            continue
        result = conn.exec_driver_sql(
            f'DELETE FROM "{table}" WHERE "{column}" IS NOT NULL '
            f'AND "{column}" NOT IN (SELECT "{parent_column}" FROM "{parent}")'
        )
        if result.rowcount:
            logger.info("Multi-user: deleted %s orphan rows from %s", result.rowcount, table)


def _snapshot_is_position_aware(context_snapshot: Optional[str]) -> bool:
    """Whether a stored context snapshot shows the report was produced with portfolio / position context."""
    if not context_snapshot or "portfolio" not in context_snapshot:
        return False
    try:
        data = json.loads(context_snapshot)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("portfolio_context"):
        return True
    enhanced = data.get("enhanced_context")
    if isinstance(enhanced, dict) and enhanced.get("portfolio_context"):
        return True
    diagnostics = data.get("diagnostics")
    trigger = diagnostics.get("trigger_source") if isinstance(diagnostics, dict) else None
    return (trigger or data.get("trigger_source")) == "portfolio"


def _mark_ids(conn: Connection, sql_template: str, ids: Set[int], *params: object) -> None:
    """Run ``sql_template`` (with one ``{marks}`` placeholder list) over ``ids`` in chunks."""
    ordered = sorted(ids)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        marks = ",".join("?" for _ in chunk)
        conn.exec_driver_sql(sql_template.format(marks=marks), (*params, *chunk))


def _backfill_report_privacy(conn: Connection, primary_user_id: str) -> None:
    """Mark position-aware reports private to the primary user and give their signals the same owner.

    A report is position-aware when its stored snapshot carries the portfolio context / was triggered from the
    portfolio page, or when a signal extracted from it recorded a real ``holding_state``. Everything else stays
    shared (``is_private`` = 0). Signals of a private report are owned by the report's requester.
    """
    tables = {name for (name,) in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if "analysis_history" not in tables:
        return
    conn.exec_driver_sql('UPDATE "analysis_history" SET is_private = 0 WHERE is_private IS NULL')

    private_ids: Set[int] = set()
    for row_id, snapshot in conn.exec_driver_sql(
        'SELECT id, context_snapshot FROM "analysis_history" WHERE context_snapshot LIKE ?', ("%portfolio%",)
    ).fetchall():
        if _snapshot_is_position_aware(snapshot):
            private_ids.add(int(row_id))

    position_signal_ids: Set[int] = set()
    if "decision_signals" in tables:
        for signal_id, source_report_id, metadata in conn.exec_driver_sql(
            'SELECT id, source_report_id, metadata_json FROM "decision_signals" WHERE metadata_json LIKE ?',
            ("%holding_state%",),
        ).fetchall():
            try:
                state = (json.loads(metadata) or {}).get("holding_state")
            except (TypeError, ValueError, AttributeError):
                continue
            if state in _POSITION_AWARE_HOLDING_STATES:
                position_signal_ids.add(int(signal_id))
                if source_report_id is not None:
                    private_ids.add(int(source_report_id))

    _mark_ids(conn, 'UPDATE "analysis_history" SET is_private = 1 WHERE id IN ({marks})', private_ids)
    if private_ids:
        logger.info("Multi-user: marked %s position-aware reports private to the primary user", len(private_ids))

    if "decision_signals" in tables:
        _own_signals_of_private_reports(conn, primary_user_id)
        _mark_ids(
            conn,
            'UPDATE "decision_signals" SET owner_user_id = ? WHERE owner_user_id IS NULL AND id IN ({marks})',
            position_signal_ids,
            primary_user_id,
        )


def _own_signals_of_private_reports(conn: Connection, primary_user_id: str) -> None:
    """Give the (still shared) decision signals extracted from a private report that report's requester as owner."""
    conn.execute(
        text(
            'UPDATE "decision_signals" SET owner_user_id = COALESCE('
            '(SELECT requester_user_id FROM "analysis_history" h WHERE h.id = decision_signals.source_report_id), '
            ":uid) WHERE owner_user_id IS NULL AND source_type = 'analysis' AND source_report_id IN "
            '(SELECT id FROM "analysis_history" WHERE is_private = 1)'
        ),
        {"uid": primary_user_id},
    )


def _backfill_legacy_agent_privacy(conn: Connection, primary_user_id: str) -> None:
    """Mark Agent-mode reports that predate the privacy marker private (it cannot be shown that no holdings ran).

    An Agent run may call ``get_portfolio_snapshot`` and write the account's holdings into the report. From P2b-2
    on the run's tool calls are checked when the report is saved (and ``report_trade_date`` is written in the same
    place); a report without ``report_trade_date`` was saved before that, so nothing says which tools ran: it is
    private to its requester. Agent mode is recognised by the ``agent:`` data source the pipeline stamps on every
    Agent report. Everything else keeps its (already determined) sharing.
    """
    tables = {name for (name,) in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = _table_columns(conn, "analysis_history") if "analysis_history" in tables else set()
    if not {"is_private", "report_trade_date", "raw_result"} <= columns:
        return

    private_ids: Set[int] = set()
    for row_id, raw_result in conn.exec_driver_sql(
        'SELECT id, raw_result FROM "analysis_history" WHERE (is_private IS NULL OR is_private = 0) '
        "AND report_trade_date IS NULL AND raw_result LIKE ?",
        ("%agent:%",),
    ).fetchall():
        try:
            data = json.loads(raw_result) if raw_result else None
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict):
            private_ids.add(int(row_id))  # unreadable, but it says "agent:": undeterminable -> private
            continue
        if "agent:" in str(data.get("data_sources") or ""):
            private_ids.add(int(row_id))

    _mark_ids(conn, 'UPDATE "analysis_history" SET is_private = 1 WHERE id IN ({marks})', private_ids)
    if private_ids:
        logger.info("Multi-user: marked %s legacy Agent-mode reports private (tool calls were not recorded)", len(private_ids))
    if "decision_signals" in tables and "owner_user_id" in _table_columns(conn, "decision_signals"):
        _own_signals_of_private_reports(conn, primary_user_id)
    _purge_private_derived_data(conn)


def _purge_private_derived_data(conn: Connection) -> None:
    """Delete shared-output rows that were derived from private reports (see the module docstring).

    Backtest results of a private report go, and so do the rollups (``backtest_summaries``) of every
    (eval window, engine version) bucket that lost a result: a rollup cannot be corrected row by row, and the
    next backtest run recomputes the overall rollup and the rollups of the stocks it touches. Skill opinion
    samples of a private report go with their outcomes (explicitly: SQLite does not enforce the cascade).
    """
    tables = {name for (name,) in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    if "analysis_history" not in tables or "is_private" not in _table_columns(conn, "analysis_history"):
        return
    private = '(SELECT id FROM "analysis_history" WHERE is_private = 1)'
    if "backtest_results" in tables:
        buckets = conn.exec_driver_sql(
            f'SELECT DISTINCT eval_window_days, engine_version FROM "backtest_results" '
            f"WHERE analysis_history_id IN {private}"
        ).fetchall()
        if buckets:
            removed = conn.exec_driver_sql(
                f'DELETE FROM "backtest_results" WHERE analysis_history_id IN {private}'
            ).rowcount
            logger.info("Multi-user: deleted %s backtest results derived from private reports", removed)
            if "backtest_summaries" in tables:
                for window, engine in buckets:
                    conn.exec_driver_sql(
                        'DELETE FROM "backtest_summaries" WHERE eval_window_days = ? AND engine_version = ?',
                        (window, engine),
                    )
    if {"skill_opinion_samples", "skill_opinion_outcomes"} <= tables:
        samples = f'(SELECT id FROM "skill_opinion_samples" WHERE analysis_history_id IN {private})'
        conn.exec_driver_sql(f'DELETE FROM "skill_opinion_outcomes" WHERE skill_opinion_sample_id IN {samples}')
        removed = conn.exec_driver_sql(
            f'DELETE FROM "skill_opinion_samples" WHERE analysis_history_id IN {private}'
        ).rowcount
        if removed:
            logger.info("Multi-user: deleted %s skill opinion samples derived from private reports", removed)


def _ensure_p3a_tables(conn: Connection) -> None:
    """Create the P3a tables when ``create_all`` did not (it normally runs first); idempotent, never touches rows."""
    from src import storage

    for model in (storage.UserNotificationChannel, storage.UserNoticeMarker):
        model.__table__.create(conn, checkfirst=True)
        for index in model.__table__.indexes:
            index.create(conn, checkfirst=True)


def _backfill_targets() -> List[tuple]:
    user_id_models, _ = _models()
    targets = [(model.__table__.name, "user_id") for model in user_id_models]
    targets.append((_ANALYSIS_HISTORY_TABLE, "requester_user_id"))
    return targets


def run_multi_user_migration(engine: Engine, primary_user_id: str) -> bool:
    """Run the migration if it is not recorded yet; returns True when it changed the schema.

    When already recorded only rows with a NULL owner (written by older code after a
    rollback) are backfilled. One transaction; any failure rolls back and re-raises.
    """
    if engine.url.get_backend_name() != "sqlite":
        logger.info("Multi-user migration skipped: not a SQLite engine (relies on create_all)")
        return False

    user_id_models, rebuild_models = _models()
    with engine.connect() as raw_conn:
        # Manual BEGIN/COMMIT: pysqlite's implicit transactions do not wrap DDL.
        conn = raw_conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            applied = False
            if _is_recorded(conn):
                for table, column in _backfill_targets():
                    moved = _backfill_null(conn, table, column, primary_user_id)
                    if moved:
                        logger.info("Multi-user: backfilled %s %s.%s with the primary user", moved, table, column)
            else:
                applied = True
                for model in user_id_models:
                    _add_column_if_missing(conn, model.__table__.name, "user_id", _USER_ID_COLUMN_SQL)
                for column, sql_type in _ANALYSIS_HISTORY_COLUMNS.items():
                    _add_column_if_missing(conn, _ANALYSIS_HISTORY_TABLE, column, sql_type)
                for table, column in _backfill_targets():
                    _backfill_null(conn, table, column, primary_user_id)
                for model in rebuild_models:
                    if not _has_user_scoped_unique(conn, model.__table__.name):
                        logger.info("Multi-user: rebuilding %s with per-user unique constraints", model.__table__.name)
                        _rebuild_table(conn, model)
                for model in user_id_models:
                    for index in model.__table__.indexes:
                        index.create(conn, checkfirst=True)
                _record_version(conn, MULTI_USER_SCHEMA_VERSION, MULTI_USER_SCHEMA_DESCRIPTION)
            if not _is_recorded(conn, MULTI_USER_P2A_SCHEMA_VERSION):
                applied = True
                for model in _p2a_models():
                    table = model.__table__.name
                    _add_column_if_missing(conn, table, "user_id", _USER_ID_COLUMN_SQL)
                    _backfill_null(conn, table, "user_id", primary_user_id)
                    for index in model.__table__.indexes:
                        index.create(conn, checkfirst=True)
                _delete_orphan_alert_history(conn)
                _record_version(conn, MULTI_USER_P2A_SCHEMA_VERSION, MULTI_USER_P2A_SCHEMA_DESCRIPTION)
            else:
                for model in _p2a_models():
                    moved = _backfill_null(conn, model.__table__.name, "user_id", primary_user_id)
                    if moved:
                        logger.info(
                            "Multi-user: backfilled %s %s.user_id with the primary user", moved, model.__table__.name
                        )
            if not _is_recorded(conn, MULTI_USER_P2B2_SCHEMA_VERSION):
                applied = True
                existing_tables = {name for (name,) in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                for table, column, sql_type in _P2B2_COLUMNS:
                    if table in existing_tables:
                        _add_column_if_missing(conn, table, column, sql_type)
                _backfill_report_privacy(conn, primary_user_id)
                for model in _p2b2_models():
                    present = _table_columns(conn, model.__table__.name)
                    for index in model.__table__.indexes:
                        # a table older than other, later columns (``_ensure_decision_signal_profile_schema`` adds
                        # those and their indexes itself) cannot carry an index on a column it does not have yet
                        if all(column.name in present for column in index.columns):
                            index.create(conn, checkfirst=True)
                _record_version(conn, MULTI_USER_P2B2_SCHEMA_VERSION, MULTI_USER_P2B2_SCHEMA_DESCRIPTION)
            if not _is_recorded(conn, MULTI_USER_P2B2_PURGE_SCHEMA_VERSION):
                applied = True
                _purge_private_derived_data(conn)
                _record_version(conn, MULTI_USER_P2B2_PURGE_SCHEMA_VERSION, MULTI_USER_P2B2_PURGE_SCHEMA_DESCRIPTION)
            if not _is_recorded(conn, MULTI_USER_P2B4_SCHEMA_VERSION):
                applied = True
                if _ANALYSIS_HISTORY_TABLE in _existing_tables(conn):
                    _add_column_if_missing(conn, _ANALYSIS_HISTORY_TABLE, "skill_fingerprint", "VARCHAR(64)")
                _record_version(conn, MULTI_USER_P2B4_SCHEMA_VERSION, MULTI_USER_P2B4_SCHEMA_DESCRIPTION)
            if not _is_recorded(conn, MULTI_USER_P2B4_AGENT_PRIVACY_VERSION):
                applied = True
                _backfill_legacy_agent_privacy(conn, primary_user_id)
                _record_version(
                    conn, MULTI_USER_P2B4_AGENT_PRIVACY_VERSION, MULTI_USER_P2B4_AGENT_PRIVACY_DESCRIPTION
                )
            if not _is_recorded(conn, MULTI_USER_P3A_SCHEMA_VERSION):
                applied = True
                _ensure_p3a_tables(conn)
                _record_version(conn, MULTI_USER_P3A_SCHEMA_VERSION, MULTI_USER_P3A_SCHEMA_DESCRIPTION)
            conn.exec_driver_sql("COMMIT")
        except BaseException:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception as rollback_exc:  # noqa: BLE001 - keep the original error
                logger.warning("Multi-user migration rollback failed: %s", rollback_exc)
            raise
    if applied:
        logger.info("Multi-user schema migration %s applied", MULTI_USER_SCHEMA_VERSION)
    return applied


__all__ = [
    "MULTI_USER_P2A_SCHEMA_VERSION",
    "MULTI_USER_P2B2_PURGE_SCHEMA_VERSION",
    "MULTI_USER_P2B2_SCHEMA_VERSION",
    "MULTI_USER_P2B4_AGENT_PRIVACY_VERSION",
    "MULTI_USER_P2B4_SCHEMA_VERSION",
    "MULTI_USER_P3A_SCHEMA_VERSION",
    "MULTI_USER_SCHEMA_VERSION",
    "backup_before_multi_user_migration",
    "run_multi_user_migration",
]
