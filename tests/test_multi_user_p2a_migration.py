# -*- coding: utf-8 -*-
"""Multi-user P2a follow-up migration: ``assistant_note_proposal.user_id`` (own schema_migrations record)."""

import sqlite3

import pytest

from src import storage_multi_user_migration as migration
from src.config import Config
from src.storage import DatabaseManager

PRIMARY = "uid-admin"
NOW = "2026-11-01 10:00:00.000000"

OLD_PROPOSAL_DDL = """
CREATE TABLE assistant_note_proposal (
    id VARCHAR(36) NOT NULL, scope VARCHAR(16) NOT NULL, subject VARCHAR(32), title VARCHAR(64) NOT NULL,
    base_version INTEGER NOT NULL, before_md TEXT NOT NULL, after_md TEXT NOT NULL, source VARCHAR(16) NOT NULL,
    origin_session_id VARCHAR(100), origin_message_id INTEGER, status VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL, resolved_at DATETIME, PRIMARY KEY (id)
)
"""


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


def _downgrade_proposal_table(db_path):
    """Recreate the P1-era proposal table (no user_id) holding one pending proposal."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE assistant_note_proposal")
        conn.execute(OLD_PROPOSAL_DDL)
        conn.execute(
            "INSERT INTO assistant_note_proposal VALUES ('p-1','global',NULL,'t',0,'','x','user_said',NULL,NULL,'pending',?,NULL)",
            (NOW,),
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2A_SCHEMA_VERSION,))


def test_proposal_user_id_added_and_backfilled_to_primary(env):
    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    _downgrade_proposal_table(db_path)
    assert "user_id" not in _columns(db_path, "assistant_note_proposal")

    DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert "user_id" in _columns(db_path, "assistant_note_proposal")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT user_id FROM assistant_note_proposal WHERE id='p-1'").fetchone() == (PRIMARY,)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2A_SCHEMA_VERSION,)
        ).fetchone() == (1,)
        indexes = [row[1] for row in conn.execute('PRAGMA index_list("assistant_note_proposal")')]
    assert any("user_id" in name for name in indexes)


def test_p2a_migration_is_idempotent_and_backfills_rollback_rows(env):
    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO assistant_note_proposal (id, scope, title, base_version, before_md, after_md, source, status, created_at) "
            "VALUES ('p-2','global','t',0,'','x','user_said','pending',?)",
            (NOW,),
        )
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT user_id FROM assistant_note_proposal WHERE id='p-2'").fetchone() == (PRIMARY,)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2A_SCHEMA_VERSION,)
        ).fetchone() == (1,)


def _seed_alert_history(db_path):
    """One live rule (id 1) with history, plus rows orphaned by a rule / trigger that was deleted before P2a."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO alert_rules (id, user_id, name, target_scope, target, alert_type, parameters, severity, enabled, source) "
            "VALUES (1, ?, 'live', 'single_symbol', '600519', 'price_cross', '{}', 'warning', 1, 'api')",
            (PRIMARY,),
        )
        conn.executemany(
            "INSERT INTO alert_triggers (id, rule_id, target, status) VALUES (?, ?, '600519', 'triggered')",
            [(1, 1), (2, 2), (3, None)],  # live, orphan (rule 2 was deleted), legacy .env rule (NULL)
        )
        conn.executemany(
            "INSERT INTO alert_notifications (id, trigger_id, channel, attempt, success, retryable) "
            "VALUES (?, ?, 'custom', 1, 1, 0)",
            [(1, 1), (2, 2), (3, 99), (4, None)],  # live, of the orphan trigger, of a missing trigger, trigger-less
        )
        conn.executemany(
            "INSERT INTO alert_cooldowns (id, rule_id, target, severity, state) VALUES (?, ?, '600519', 'warning', 'active')",
            [(1, 1), (2, 2)],
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (migration.MULTI_USER_P2A_SCHEMA_VERSION,))


def _ids(db_path, table):
    with sqlite3.connect(db_path) as conn:
        return [row[0] for row in conn.execute(f"SELECT id FROM {table} ORDER BY id")]


def test_p2a_migration_deletes_orphan_alert_history_so_a_reused_rule_id_inherits_nothing(env):
    from src.repositories.alert_repo import AlertRepository

    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    _seed_alert_history(db_path)

    db = DatabaseManager(db_url=f"sqlite:///{db_path}")

    # live rows and the legacy rule-less / trigger-less rows stay; everything orphaned is gone
    assert _ids(db_path, "alert_triggers") == [1, 3]
    assert _ids(db_path, "alert_notifications") == [1, 4]
    assert _ids(db_path, "alert_cooldowns") == [1]
    assert _ids(db_path, "alert_rules") == [1]

    # the next rule reuses id 2 (no AUTOINCREMENT) and starts with an empty history
    repo = AlertRepository(db)
    rule = repo.create_rule("uid-user", {
        "name": "new", "target_scope": "single_symbol", "target": "000001", "alert_type": "price_cross",
        "parameters": "{}", "severity": "warning", "enabled": True, "source": "api",
    })
    assert rule.id == 2
    assert repo.list_triggers("uid-user")[1] == 0
    assert repo.list_notifications("uid-user")[1] == 0
    assert repo.get_rule_cooldown_summary("uid-user", rule_id=2, target="600519", severity="warning") is None


def test_orphan_alert_history_cleanup_runs_once_with_the_p2a_step(env):
    db_path = env / "stock.db"
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    _seed_alert_history(db_path)
    DatabaseManager(db_url=f"sqlite:///{db_path}")
    DatabaseManager.reset_instance()
    with sqlite3.connect(db_path) as conn:  # an orphan written after the migration is not touched by later startups
        conn.execute("INSERT INTO alert_triggers (id, rule_id, target, status) VALUES (9, 77, '600519', 'triggered')")

    DatabaseManager(db_url=f"sqlite:///{db_path}")

    assert _ids(db_path, "alert_triggers") == [1, 3, 9]
