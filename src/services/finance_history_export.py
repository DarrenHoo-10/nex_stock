"""Owner-only legacy export with a transactional SQLite edit/delete journal."""
import json
from datetime import datetime, timezone

from sqlalchemy import text

from src.services.finance_identity import fail, is_user_center_mode, personal_data_available
from src.services.finance_report_events import primary_user_id


def install_journal(db):
    from src.storage import ConversationSessionState
    if db._engine.dialect.name != "sqlite":
        fail(503, "FINANCE_HISTORY_JOURNAL_UNSUPPORTED")
    primary = "'" + primary_user_id().replace("'", "''") + "'"
    has_owner = hasattr(ConversationSessionState, "user_id")
    def owner(alias):
        return f"coalesce((SELECT user_id FROM conversation_session_states WHERE session_id={alias}.session_id),{primary})" if has_owner else primary
    columns = "message_id,session_id,owner_id,role,content,created_at,context_snapshot,deleted"
    def snapshot(alias, who=None):
        return f"{alias}.id,{alias}.session_id,{who or owner(alias)},{alias}.role,{alias}.content,{alias}.created_at,{alias}.context_snapshot,0"
    with db._engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            connection.exec_driver_sql("""CREATE TABLE IF NOT EXISTS finance_history_changes (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
                session_id TEXT NOT NULL, owner_id TEXT NOT NULL, role TEXT,
                content TEXT, created_at TEXT, context_snapshot TEXT, deleted INTEGER NOT NULL DEFAULT 0)""")
            connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS finance_history_journal_meta (id INTEGER PRIMARY KEY, definition TEXT)")
            if "definition" not in {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(finance_history_journal_meta)")}:
                connection.exec_driver_sql("ALTER TABLE finance_history_journal_meta ADD COLUMN definition TEXT")
            definition = f"v3:{has_owner}:{primary}"
            previous = connection.exec_driver_sql("SELECT definition FROM finance_history_journal_meta WHERE id=1").fetchone()
            changed = previous is None or previous[0] != definition
            if changed:
                # A local/P1 journal can precede P2's ownership columns. Refresh
                # its trigger definitions atomically rather than retaining a
                # primary-only INSERT trigger after a multi-user upgrade.
                for name in ("insert", "update", "delete", "remove_role", "owner_insert", "owner_update", "owner_delete"):
                    connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS finance_history_{name}")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS finance_history_owner_cursor ON finance_history_changes(owner_id,cursor)")
            connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS finance_history_insert AFTER INSERT ON conversation_messages
                WHEN NEW.role IN ('user','assistant') BEGIN
                INSERT INTO finance_history_changes({columns}) SELECT {snapshot('NEW')}; END""")
            connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS finance_history_update AFTER UPDATE OF content,role,context_snapshot,session_id ON conversation_messages
                WHEN NEW.role IN ('user','assistant') AND (OLD.content IS NOT NEW.content OR OLD.role IS NOT NEW.role OR OLD.context_snapshot IS NOT NEW.context_snapshot OR OLD.session_id IS NOT NEW.session_id) BEGIN
                UPDATE finance_history_changes SET content=NEW.content,context_snapshot=NEW.context_snapshot WHERE message_id=NEW.id AND deleted=0;
                INSERT INTO finance_history_changes({columns}) SELECT {snapshot('NEW')}; END""")
            connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS finance_history_delete AFTER DELETE ON conversation_messages
                WHEN OLD.role IN ('user','assistant') BEGIN
                UPDATE finance_history_changes SET content=NULL,context_snapshot=NULL,deleted=1 WHERE message_id=OLD.id;
                INSERT INTO finance_history_changes(message_id,session_id,owner_id,deleted)
                SELECT OLD.id,OLD.session_id,{owner('OLD')},1; END""")
            connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS finance_history_remove_role AFTER UPDATE OF role,session_id ON conversation_messages
                WHEN OLD.role IN ('user','assistant') AND (NEW.role NOT IN ('user','assistant') OR OLD.session_id IS NOT NEW.session_id) BEGIN
                UPDATE finance_history_changes SET content=NULL,context_snapshot=NULL,deleted=1 WHERE message_id=OLD.id;
                INSERT INTO finance_history_changes(message_id,session_id,owner_id,deleted)
                SELECT OLD.id,OLD.session_id,{owner('OLD')},1; END""")
            if has_owner:
                for operation, old_owner, new_owner in (
                    ("INSERT", primary, f"coalesce(NEW.user_id,{primary})"),
                    ("UPDATE OF user_id", f"coalesce(OLD.user_id,{primary})", f"coalesce(NEW.user_id,{primary})"),
                    ("DELETE", f"coalesce(OLD.user_id,{primary})", primary),
                ):
                    alias = "OLD" if operation == "DELETE" else "NEW"
                    name = operation.split()[0].lower()
                    connection.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS finance_history_owner_{name} AFTER {operation} ON conversation_session_states
                        WHEN {old_owner} IS NOT {new_owner} BEGIN
                        UPDATE finance_history_changes SET content=NULL,context_snapshot=NULL,deleted=1 WHERE session_id={alias}.session_id AND owner_id={old_owner};
                        INSERT INTO finance_history_changes(message_id,session_id,owner_id,deleted)
                        SELECT id,session_id,{old_owner},1 FROM conversation_messages WHERE session_id={alias}.session_id AND role IN ('user','assistant');
                        INSERT INTO finance_history_changes({columns})
                        SELECT {snapshot('m', new_owner)} FROM conversation_messages m WHERE session_id={alias}.session_id AND role IN ('user','assistant'); END""")
            if changed:
                invalid = f"(c.owner_id IS NOT {owner('c')} OR NOT EXISTS (SELECT 1 FROM conversation_messages m WHERE m.id=c.message_id AND m.role IN ('user','assistant')))"
                connection.exec_driver_sql(f"""INSERT INTO finance_history_changes(message_id,session_id,owner_id,deleted)
                    SELECT DISTINCT c.message_id,c.session_id,c.owner_id,1 FROM finance_history_changes c WHERE c.deleted=0 AND {invalid}""")
                connection.exec_driver_sql(f"""UPDATE finance_history_changes AS c SET content=NULL,context_snapshot=NULL,deleted=1
                    WHERE c.deleted=0 AND {invalid}""")
                connection.exec_driver_sql("""UPDATE finance_history_changes AS c SET
                    content=(SELECT m.content FROM conversation_messages m WHERE m.id=c.message_id),
                    context_snapshot=(SELECT m.context_snapshot FROM conversation_messages m WHERE m.id=c.message_id)
                    WHERE c.deleted=0""")
                connection.exec_driver_sql(f"""INSERT INTO finance_history_changes({columns})
                    SELECT {snapshot('m')} FROM conversation_messages m WHERE role IN ('user','assistant') ORDER BY id""")
                connection.execute(text("INSERT OR REPLACE INTO finance_history_journal_meta(id,definition) VALUES(1,:definition)"), {"definition": definition})
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def export_messages(subject, after=0, limit=100):
    from src.storage import DatabaseManager
    if is_user_center_mode() and not personal_data_available():
        fail(503, "FINANCE_USER_SYSTEM_REQUIRED")
    db = DatabaseManager.get_instance()
    install_journal(db)
    with db._engine.connect() as connection:
        rows = list(connection.execute(text("""SELECT * FROM finance_history_changes
            WHERE owner_id=:owner AND cursor>:after ORDER BY cursor LIMIT :limit"""),
                                       {"owner": subject, "after": after, "limit": limit + 1}).mappings())
    messages = []
    for row in rows[:limit]:
        item = {"id": row["message_id"], "cursor": row["cursor"], "sessionId": row["session_id"]}
        if row["deleted"]:
            messages.append({**item, "deleted": True})
            continue
        stamp = datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.fromtimestamp(0, timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        item.update(role=row["role"], content=row["content"], createdAt=int(stamp.timestamp() * 1000))
        try:
            context = json.loads(row["context_snapshot"] or "{}")
        except (TypeError, ValueError):
            context = {}
        if isinstance(context, dict):
            safe = {key: context[key] for key in ("kind", "id", "code", "name", "tradeDate", "asOf") if isinstance(context.get(key), str)}
            if safe:
                item["sourceContext"] = safe
        messages.append(item)
    return {"messages": messages, "nextCursor": messages[-1]["cursor"] if messages else after,
            "hasMore": len(rows) > limit, "cursorVersion": 2,
            "capabilities": {"edits": True, "deletions": True, "transactionalChanges": True}}
