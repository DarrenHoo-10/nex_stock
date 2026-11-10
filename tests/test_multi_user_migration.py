# -*- coding: utf-8 -*-
"""Multi-user P1 schema migration (docs/design/multi-user.md section 4).

Builds a database with the pre-multi-user schema and sample rows, runs the startup
migration through ``DatabaseManager`` and checks columns, backfill, preserved data,
per-user unique constraints, idempotency, the pre-migration backup and rollback.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multi_user_legacy_schema import LEGACY_DDL  # noqa: E402
from src import storage_multi_user_migration as migration  # noqa: E402
from src.config import Config  # noqa: E402
from src.storage import (  # noqa: E402
    AnalysisHistory,
    BoardFollow,
    DailyPlanHolding,
    DatabaseManager,
    DatabaseSchemaMigration,
)

PRIMARY = "uid-admin"
USER_ID_TABLES = (
    "plan_account", "plan_holding", "plan_run", "plan_stock", "portfolio_accounts",
    "board_follow", "alert_rules", "conversation_session_states", "assistant_note", "llm_usage",
)
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


def _build_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_DDL)
        conn.execute(
            "CREATE TABLE schema_migrations (version VARCHAR(64) NOT NULL PRIMARY KEY, "
            "description VARCHAR(255) NOT NULL, applied_at DATETIME NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations VALUES ('2026-06-05-create-all-baseline', 'baseline', ?)", (NOW,)
        )
        conn.execute("INSERT INTO plan_account (id, cash, updated_at) VALUES (1, 1234.5, ?)", (NOW,))
        conn.executemany(
            "INSERT INTO plan_holding (id, code, shares, cost, buy_date, horizon, position) VALUES (?,?,?,?,?,?,?)",
            [(3, "600519", 100, 1500.0, "2026-10-01", "swing", 0), (7, "000001", 200, 11.2, None, None, 1)],
        )
        conn.execute(
            "INSERT INTO plan_run (id, run_id, trade_date, status, \"trigger\", started_at, stock_total, "
            "stock_succeeded, stock_failed) VALUES (1, 'run-1', '2026-11-02', 'succeeded', 'manual', ?, 2, 2, 0)",
            (NOW,),
        )
        conn.executemany(
            "INSERT INTO plan_stock (id, run_id, trade_date, code, status, payload_json, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(1, "run-1", "2026-11-02", "600519", "ready", "{}", NOW), (2, "run-1", "2026-11-02", "000001", "ready", "{}", NOW)],
        )
        conn.execute(
            "INSERT INTO portfolio_accounts (id, owner_id, name, market, base_currency, is_active, created_at, updated_at) "
            "VALUES (5, 'free-text-owner', 'main', 'cn', 'CNY', 1, ?, ?)",
            (NOW, NOW),
        )
        conn.executemany(
            "INSERT INTO board_follow (id, board_ref, board_name, board_type, created_at) VALUES (?,?,?,?,?)",
            [(2, "concept:BK1036", "AI", "concept", NOW), (9, "industry:BK0475", "Bank", "industry", NOW)],
        )
        conn.execute(
            "INSERT INTO alert_rules (id, name, target_scope, target, alert_type, parameters, severity, enabled, source) "
            "VALUES (1, 'r', 'single_symbol', '600519', 'price_cross', '{}', 'warning', 1, 'api')"
        )
        conn.execute(
            "INSERT INTO conversation_session_states (session_id, selected_skill_ids_json, created_at, updated_at, state_version) "
            "VALUES ('s-1', 'null', ?, ?, 0)",
            (NOW, NOW),
        )
        conn.executemany(
            "INSERT INTO assistant_note (id, scope, subject, title, body_md, version, updated_at) VALUES (?,?,?,?,?,?,?)",
            [(4, "global", None, "Global", "# g", 1, NOW), (6, "stock", "600519", "Moutai", "# m", 3, NOW)],
        )
        conn.executemany(
            "INSERT INTO llm_usage (id, call_type, model, prompt_tokens, completion_tokens, total_tokens) VALUES (?,?,?,?,?,?)",
            [(1, "analysis", "m", 1, 2, 3), (2, "agent", "m", 4, 5, 9)],
        )
        conn.executemany(
            "INSERT INTO analysis_history (id, query_id, code, name, report_type) VALUES (?,?,?,?,?)",
            [(1, "q1", "600519", "Moutai", "simple"), (2, "q2", "000001", "PAB", "full")],
        )
        conn.commit()
    finally:
        conn.close()


def _open(env: Path) -> DatabaseManager:
    return DatabaseManager(db_url=f"sqlite:///{env / 'stock.db'}")


def _columns(db_path: Path, table: str):
    with sqlite3.connect(db_path) as conn:
        return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _rows(db_path: Path, sql: str):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def _unique_index_columns(db_path: Path, table: str):
    result = {}
    with sqlite3.connect(db_path) as conn:
        for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
            if int(row[2]) == 1:
                cols = tuple(i[2] for i in conn.execute(f'PRAGMA index_info("{row[1]}")'))
                result[row[1]] = (cols, bool(row[4]))
    return result


@pytest.fixture
def legacy(env):
    db_path = env / "stock.db"
    _build_legacy_db(db_path)
    return db_path


def _backups(env: Path):
    return sorted(env.glob("stock.db.pre-multi-user-*.bak"))


def test_migration_adds_columns_and_backfills_every_row(env, legacy):
    _open(env)

    for table in USER_ID_TABLES:
        assert "user_id" in _columns(legacy, table), table
        owners = {row[0] for row in _rows(legacy, f"SELECT user_id FROM {table}")}
        assert owners == {PRIMARY}, (table, owners)

    history_columns = _columns(legacy, "analysis_history")
    assert "requester_user_id" in history_columns
    assert "model_channel_fingerprint" in history_columns
    assert {row[0] for row in _rows(legacy, "SELECT requester_user_id FROM analysis_history")} == {PRIMARY}
    assert {row[0] for row in _rows(legacy, "SELECT model_channel_fingerprint FROM analysis_history")} == {None}


def test_portfolio_owner_id_free_text_is_untouched(env, legacy):
    _open(env)

    assert _rows(legacy, "SELECT owner_id, user_id FROM portfolio_accounts") == [("free-text-owner", PRIMARY)]


def test_rebuilt_tables_preserve_rows_ids_and_values(env, legacy):
    _open(env)

    assert _rows(legacy, "SELECT id, code, shares, cost, buy_date, horizon, position FROM plan_holding ORDER BY id") == [
        (3, "600519", 100, 1500.0, "2026-10-01", "swing", 0),
        (7, "000001", 200, 11.2, None, None, 1),
    ]
    assert _rows(legacy, "SELECT id, board_ref, board_name, board_type FROM board_follow ORDER BY id") == [
        (2, "concept:BK1036", "AI", "concept"),
        (9, "industry:BK0475", "Bank", "industry"),
    ]
    assert _rows(legacy, "SELECT id, scope, subject, title, body_md, version FROM assistant_note ORDER BY id") == [
        (4, "global", None, "Global", "# g", 1),
        (6, "stock", "600519", "Moutai", "# m", 3),
    ]
    # Untouched tables keep their rows as well.
    assert _rows(legacy, "SELECT id, cash FROM plan_account") == [(1, 1234.5)]
    assert _rows(legacy, "SELECT COUNT(*) FROM llm_usage") == [(2,)]
    assert _rows(legacy, "SELECT COUNT(*) FROM plan_stock") == [(2,)]


def test_unique_constraints_are_per_user(env, legacy):
    db = _open(env)

    with db.get_session() as session:
        # Same code / board / note scope for another user is now allowed ...
        session.add(DailyPlanHolding(user_id="u2", code="600519", shares=1, cost=1.0))
        session.add(BoardFollow(user_id="u2", board_ref="concept:BK1036", board_name="AI", board_type="concept"))
        session.commit()
    with sqlite3.connect(legacy) as conn:
        conn.execute(
            "INSERT INTO assistant_note (user_id, scope, subject, title, body_md, version, updated_at) "
            "VALUES ('u2', 'global', NULL, 'G2', '', 1, ?)", (NOW,),
        )
        conn.execute(
            "INSERT INTO assistant_note (user_id, scope, subject, title, body_md, version, updated_at) "
            "VALUES ('u2', 'stock', '600519', 'S2', '', 1, ?)", (NOW,),
        )
        conn.commit()

    # ... but duplicates within one user are still rejected.
    with db.get_session() as session:
        session.add(DailyPlanHolding(user_id=PRIMARY, code="600519", shares=1, cost=1.0))
        with pytest.raises(IntegrityError):
            session.commit()
    with db.get_session() as session:
        session.add(BoardFollow(user_id=PRIMARY, board_ref="concept:BK1036", board_name="AI", board_type="concept"))
        with pytest.raises(IntegrityError):
            session.commit()
    with sqlite3.connect(legacy) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assistant_note (user_id, scope, subject, title, body_md, version, updated_at) "
                f"VALUES ('{PRIMARY}', 'global', NULL, 'dup', '', 1, ?)", (NOW,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assistant_note (user_id, scope, subject, title, body_md, version, updated_at) "
                f"VALUES ('{PRIMARY}', 'stock', '600519', 'dup', '', 1, ?)", (NOW,),
            )
        with pytest.raises(sqlite3.IntegrityError):  # plan_account.user_id is unique
            conn.execute("INSERT INTO plan_account (id, user_id, cash, updated_at) VALUES (2, ?, 0, ?)", (PRIMARY, NOW))


def test_indexes_are_created(env, legacy):
    _open(env)

    with sqlite3.connect(legacy) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for table in ("plan_run", "plan_stock", "portfolio_accounts", "alert_rules", "conversation_session_states", "llm_usage"):
        assert f"ix_{table}_user_id" in names, table
    assert "ix_plan_account_user_id" in names
    assert _unique_index_columns(legacy, "assistant_note")["uix_assistant_note_global"] == (("user_id", "scope"), True)


def test_rebuilt_tables_match_a_fresh_database(env, legacy, tmp_path):
    _open(env)
    DatabaseManager.reset_instance()
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()
    DatabaseManager(db_url=f"sqlite:///{fresh_dir / 'fresh.db'}")
    fresh = fresh_dir / "fresh.db"

    for table in USER_ID_TABLES + ("analysis_history",):
        assert sorted(_columns(legacy, table)) == sorted(_columns(fresh, table)), table
        migrated_unique = {tuple(cols) for cols, _partial in _unique_index_columns(legacy, table).values()}
        fresh_unique = {tuple(cols) for cols, _partial in _unique_index_columns(fresh, table).values()}
        assert migrated_unique == fresh_unique, table


def test_migration_is_recorded_and_idempotent(env, legacy):
    db = _open(env)
    with db.get_session() as session:
        versions = set(session.execute(select(DatabaseSchemaMigration.version)).scalars().all())
    assert migration.MULTI_USER_SCHEMA_VERSION in versions

    before = {t: _rows(legacy, f"SELECT * FROM {t} ORDER BY 1") for t in USER_ID_TABLES}
    DatabaseManager.reset_instance()
    db = _open(env)

    assert migration.run_multi_user_migration(db._engine, PRIMARY) is False
    assert {t: _rows(legacy, f"SELECT * FROM {t} ORDER BY 1") for t in USER_ID_TABLES} == before
    with db.get_session() as session:
        count = len(session.execute(select(DatabaseSchemaMigration.version)).scalars().all())
    assert count == len(versions)


def test_backup_is_created_once_before_the_first_migration(env, legacy):
    legacy_snapshot = _rows(legacy, "SELECT id, code FROM plan_holding ORDER BY id")
    _open(env)

    backups = _backups(env)
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        # The backup is the pre-migration database: old schema, same rows, readable.
        assert "user_id" not in [row[1] for row in conn.execute("PRAGMA table_info(plan_holding)")]
        assert conn.execute("SELECT id, code FROM plan_holding ORDER BY id").fetchall() == legacy_snapshot
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    DatabaseManager.reset_instance()
    _open(env)
    assert _backups(env) == backups


def test_fresh_database_needs_no_backup_and_is_recorded(env):
    db = _open(env)

    assert _backups(env) == []
    with db.get_session() as session:
        versions = set(session.execute(select(DatabaseSchemaMigration.version)).scalars().all())
    assert migration.MULTI_USER_SCHEMA_VERSION in versions
    assert "user_id" in _columns(env / "stock.db", "plan_holding")


def test_failed_migration_rolls_back_and_refuses_to_start(env, legacy, monkeypatch):
    def boom(conn, model):
        raise RuntimeError("simulated rebuild failure")

    monkeypatch.setattr(migration, "_rebuild_table", boom)
    with pytest.raises(RuntimeError, match="simulated rebuild failure"):
        _open(env)

    assert len(_backups(env)) == 1
    # Everything done before the failure (added columns, backfill) was rolled back.
    for table in USER_ID_TABLES:
        assert "user_id" not in _columns(legacy, table), table
    assert "requester_user_id" not in _columns(legacy, "analysis_history")
    assert _rows(
        legacy, f"SELECT version FROM schema_migrations WHERE version = '{migration.MULTI_USER_SCHEMA_VERSION}'"
    ) == []
    # The original data is intact and a retry (after the cause is fixed) succeeds.
    assert _rows(legacy, "SELECT COUNT(*) FROM plan_holding") == [(2,)]
    monkeypatch.undo()
    DatabaseManager.reset_instance()
    _open(env)
    assert "user_id" in _columns(legacy, "plan_holding")


def test_rows_written_without_an_owner_after_a_rollback_are_backfilled(env, legacy):
    _open(env)
    DatabaseManager.reset_instance()
    with sqlite3.connect(legacy) as conn:  # what rolled-back (pre-multi-user) code would write
        conn.execute("INSERT INTO llm_usage (call_type, model, prompt_tokens, completion_tokens, total_tokens) VALUES ('agent', 'm', 1, 1, 2)")
        conn.commit()
    assert _rows(legacy, "SELECT COUNT(*) FROM llm_usage WHERE user_id IS NULL") == [(1,)]

    _open(env)

    assert _rows(legacy, "SELECT COUNT(*) FROM llm_usage WHERE user_id IS NULL") == [(0,)]


def test_new_rows_default_to_the_primary_user(env, legacy):
    db = _open(env)

    with db.get_session() as session:
        holding = DailyPlanHolding(code="300750", shares=10, cost=2.0)
        history = AnalysisHistory(code="300750", name="CATL", created_at=datetime(2026, 11, 3))
        session.add_all([holding, history])
        session.commit()
        assert holding.user_id == PRIMARY
        assert history.requester_user_id == PRIMARY
        assert history.model_channel_fingerprint is None
