# -*- coding: utf-8 -*-
"""
Multi-user P1 schema migration (docs/design/multi-user.md section 4).

* adds ``user_id`` (and ``analysis_history.requester_user_id`` /
  ``model_channel_fingerprint``) to the personal-data tables and backfills every
  existing row with ``PRIMARY_USER_ID``;
* rebuilds ``plan_holding`` / ``board_follow`` / ``assistant_note`` so their unique
  constraints include ``user_id`` (SQLite cannot alter a constraint in place);
* is idempotent and recorded in ``schema_migrations``; everything runs in one
  transaction, a failure rolls back and is re-raised so startup is refused;
* the database file is backed up with the SQLite backup API (WAL-safe) before the
  first run against an old-schema database.

Called from ``DatabaseManager.__init__`` (SQLite only; other engines rely on
``metadata.create_all``).
"""

from __future__ import annotations

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


def _table_columns(conn: Connection, table: str) -> Set[str]:
    return {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()}


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


def _is_recorded(conn: Connection) -> bool:
    if not conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone():
        return False
    return conn.execute(
        text("SELECT 1 FROM schema_migrations WHERE version = :version"),
        {"version": MULTI_USER_SCHEMA_VERSION},
    ).fetchone() is not None


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
    with engine.connect() as conn:
        existing = {name for (name,) in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if not existing & affected or _is_recorded(conn):
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
                from src.storage import DatabaseSchemaMigration

                conn.execute(
                    sqlite_insert(DatabaseSchemaMigration)
                    .values(
                        version=MULTI_USER_SCHEMA_VERSION,
                        description=MULTI_USER_SCHEMA_DESCRIPTION,
                        applied_at=datetime.now(),
                    )
                    .on_conflict_do_nothing(index_elements=["version"])
                )
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
    "MULTI_USER_SCHEMA_VERSION",
    "backup_before_multi_user_migration",
    "run_multi_user_migration",
]
