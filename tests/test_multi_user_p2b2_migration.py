# -*- coding: utf-8 -*-
"""Multi-user P2b-2 migration: report privacy / reuse columns on ``analysis_history`` and ``decision_signals``.

Old rows are backfilled sensibly: reports produced with portfolio / position context (and the signals extracted
from them) become private to the primary user, everything else stays shared. The database is backed up first,
the migration is idempotent and recorded in ``schema_migrations``.
"""

import json
import sqlite3

import pytest

from src import storage_multi_user_migration as migration
from src.config import Config
from src.storage import DatabaseManager

PRIMARY = "uid-admin"
NEW_REPORT_COLUMNS = ("is_private", "report_trade_date", "report_language")
NOW = "2026-11-01 10:00:00.000000"


@pytest.fixture
def env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stock.db"))
    monkeypatch.setenv("PRIMARY_USER_ID", PRIMARY)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    yield tmp_path
    DatabaseManager.reset_instance()
    Config.reset_instance()


def _columns(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _drop_indexes_on(conn, table, columns):
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        index_columns = [info[2] for info in conn.execute(f'PRAGMA index_info("{row[1]}")').fetchall()]
        if set(index_columns) & set(columns) and not row[1].startswith("sqlite_autoindex"):
            conn.execute(f'DROP INDEX "{row[1]}"')


def _downgrade_to_p2b1(db_path):
    """Remove the P2b-2 columns and the migration record: the schema a P2b-1 database has."""
    with sqlite3.connect(db_path) as conn:
        _drop_indexes_on(conn, "analysis_history", NEW_REPORT_COLUMNS)
        _drop_indexes_on(conn, "decision_signals", ("owner_user_id",))
        for column in NEW_REPORT_COLUMNS:
            conn.execute(f"ALTER TABLE analysis_history DROP COLUMN {column}")
        conn.execute("ALTER TABLE decision_signals DROP COLUMN owner_user_id")
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2B2_SCHEMA_VERSION,))


def _insert_report(conn, report_id, snapshot=None, requester=PRIMARY):
    conn.execute(
        "INSERT INTO analysis_history (id, query_id, code, name, report_type, requester_user_id, context_snapshot, created_at) "
        "VALUES (?, ?, '600519', 'n', 'detailed', ?, ?, ?)",
        (report_id, f"q{report_id}", requester, json.dumps(snapshot) if snapshot is not None else None, NOW),
    )


def _insert_signal(conn, signal_id, report_id, holding_state):
    conn.execute(
        "INSERT INTO decision_signals (id, stock_code, market, source_type, source_report_id, trigger_source, action, "
        "plan_quality, status, metadata_json) VALUES (?, '600519', 'cn', 'analysis', ?, 'api', 'buy', 'unknown', 'active', ?)",
        (signal_id, report_id, json.dumps({"holding_state": holding_state})),
    )


def _legacy_database(env):
    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    _downgrade_to_p2b1(db_path)
    with sqlite3.connect(db_path) as conn:
        _insert_report(conn, 1, {"enhanced_context": {"code": "600519"}})                                   # plain
        _insert_report(conn, 2, {"enhanced_context": {"portfolio_context": {"quantity": 100}}})             # position context
        _insert_report(conn, 3, {"diagnostics": {"trigger_source": "portfolio"}})                           # triggered from holdings
        _insert_report(conn, 4, None)                                                                      # no snapshot ...
        _insert_report(conn, 5, {"note": "a portfolio is mentioned here but this is not a position analysis"})
        _insert_report(conn, 6, {"enhanced_context": {}}, requester="uid-user")                             # someone else's, plain
        _insert_signal(conn, 1, 1, "unknown")           # shared report, shared signal
        _insert_signal(conn, 2, 2, "unknown")           # signal of a position-context report
        _insert_signal(conn, 4, 4, "holding")           # ... and a signal that proves report 4 was position-aware
        _insert_signal(conn, 6, 6, "unknown")
    return db_path


def _private_ids(db_path):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute("SELECT id FROM analysis_history WHERE is_private = 1")}


def _owners(db_path):
    with sqlite3.connect(db_path) as conn:
        return dict(conn.execute("SELECT id, owner_user_id FROM decision_signals"))


def test_columns_are_added_and_old_rows_backfilled_sensibly(env):
    db_path = _legacy_database(env)
    assert "is_private" not in _columns(db_path, "analysis_history")

    DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert set(NEW_REPORT_COLUMNS) <= set(_columns(db_path, "analysis_history"))
    assert "owner_user_id" in _columns(db_path, "decision_signals")
    assert _private_ids(db_path) == {2, 3, 4}  # position-aware reports are private; the rest stays shared
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM analysis_history WHERE is_private IS NULL").fetchone() == (0,)
        requesters = dict(conn.execute("SELECT id, requester_user_id FROM analysis_history"))
    assert requesters[2] == PRIMARY and requesters[6] == "uid-user"  # private reports belong to the primary user
    assert _owners(db_path) == {1: None, 2: PRIMARY, 4: PRIMARY, 6: None}


def test_the_database_is_backed_up_before_the_migration_touches_it(env):
    db_path = _legacy_database(env)

    DatabaseManager(db_url=f"sqlite:///{db_path}")

    backups = sorted(db_path.parent.glob("stock.db.pre-multi-user-*.bak"))
    assert backups, "no backup written"
    with sqlite3.connect(backups[-1]) as conn:
        columns = [row[1] for row in conn.execute('PRAGMA table_info("analysis_history")')]
        assert "is_private" not in columns  # the restore point is the pre-migration schema
        assert conn.execute("SELECT COUNT(*) FROM analysis_history").fetchone() == (6,)


def test_the_migration_is_idempotent_and_recorded_once(env):
    db_path = _legacy_database(env)
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    backups_after_first = sorted(db_path.parent.glob("stock.db.pre-multi-user-*.bak"))
    first_state = (_private_ids(db_path), _owners(db_path))

    DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert (_private_ids(db_path), _owners(db_path)) == first_state
    assert sorted(db_path.parent.glob("stock.db.pre-multi-user-*.bak")) == backups_after_first  # nothing new to protect
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2B2_SCHEMA_VERSION,)
        ).fetchone() == (1,)


def test_new_indexes_exist_after_migration(env):
    db_path = _legacy_database(env)
    DatabaseManager(db_url=f"sqlite:///{db_path}")

    with sqlite3.connect(db_path) as conn:
        history = {row[1] for row in conn.execute('PRAGMA index_list("analysis_history")')}
        signals = {row[1] for row in conn.execute('PRAGMA index_list("decision_signals")')}
    assert "ix_analysis_reuse_key" in history and "ix_analysis_history_is_private" in history
    assert "ix_decision_signals_owner_user_id" in signals


def test_a_fresh_database_gets_the_columns_and_the_record_without_a_backup(env):
    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert set(NEW_REPORT_COLUMNS) <= set(_columns(db_path, "analysis_history"))
    assert not list(db_path.parent.glob("stock.db.pre-multi-user-*.bak"))
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2B2_SCHEMA_VERSION,)
        ).fetchone() == (1,)


def test_a_failing_backfill_rolls_everything_back_and_refuses_startup(env):
    db_path = _legacy_database(env)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migration, "_backfill_report_privacy", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert "is_private" not in _columns(db_path, "analysis_history")  # rolled back with the failing step
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2B2_SCHEMA_VERSION,)
        ).fetchone() == (0,)
