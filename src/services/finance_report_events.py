"""Durable report.ready snapshots with owner/audience filtering and archive catch-up.

The authoritative committed reports are the outbox source: polling reconciles them
before reading the append-only ledger, closing the crash gap without network I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from src.services.finance_identity import FinanceIdentityStore, configuration


def _deliverable_plan(run):
    if not run.push_preview or run.status not in {"completed", "partial", "success", "succeeded"}:
        return False
    try:
        outcome = json.loads(run.push_json or "{}")
    except (TypeError, ValueError):
        return False
    # P3 leaves a scheduled plan on the Web when its owner has no model.
    return isinstance(outcome, dict) and outcome.get("reason") != "no_model"


def primary_user_id():
    from src import config
    if hasattr(config, "get_primary_user_id"):
        return config.get_primary_user_id()
    return "local"


class FinanceReportEvents:
    def __init__(self, store=None):
        self.store = store or FinanceIdentityStore()
        with self.store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS report_events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, trading_day TEXT, report_date TEXT,
                owner TEXT, audience TEXT NOT NULL, content TEXT NOT NULL,
                UNIQUE(report_id,version,audience))""")
            db.execute("""CREATE TABLE IF NOT EXISTS public_report_events (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL,
                version TEXT NOT NULL, kind TEXT NOT NULL, trading_day TEXT, report_date TEXT,
                content TEXT NOT NULL, UNIQUE(report_id,version))""")
            db.execute("BEGIN IMMEDIATE")
            for table in ("report_events", "public_report_events"):
                if "report_date" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN report_date TEXT")
                    for row in db.execute(f"SELECT cursor,report_id FROM {table} WHERE report_id LIKE 'market-review:%' OR report_id LIKE 'stock-summary:%'").fetchall():
                        try:
                            artifact_day = date.fromisoformat(row["report_id"].split(":", 1)[1]).isoformat()
                        except (IndexError, ValueError):
                            artifact_day = None
                        db.execute(f"UPDATE {table} SET trading_day=NULL,report_date=? WHERE cursor=?", (artifact_day, row["cursor"]))

    def record(self, report_id, kind, trading_day, content, *, owner, public=False, report_date=None):
        if not content or (not public and not owner):
            raise ValueError("A report needs content and explicit ownership or verified public provenance")
        version = hashlib.sha256(content.encode()).hexdigest()
        try:
            trading_day = date.fromisoformat(str(trading_day)).isoformat()
        except (TypeError, ValueError):
            trading_day = None
        try:
            report_date = date.fromisoformat(str(report_date)).isoformat()
        except (TypeError, ValueError):
            report_date = None
        with self.store.db() as db:
            db.execute("""INSERT OR IGNORE INTO report_events
                (report_id,version,kind,trading_day,owner,audience,content,report_date) VALUES(?,?,?,?,?,?,?,?)""",
                       (report_id, version, kind, str(trading_day) if trading_day else None,
                        owner, "public" if public else "private", content, report_date))
            db.execute("UPDATE report_events SET trading_day=?,report_date=? WHERE report_id=? AND version=? AND audience=?",
                       (trading_day, report_date, report_id, version, "public" if public else "private"))
            if public:
                db.execute("""INSERT OR IGNORE INTO public_report_events
                    (report_id,version,kind,trading_day,content,report_date) VALUES(?,?,?,?,?,?)""",
                           (report_id, version, kind, str(trading_day) if trading_day else None, content, report_date))
                db.execute("UPDATE public_report_events SET trading_day=?,report_date=? WHERE report_id=? AND version=?",
                           (trading_day, report_date, report_id, version))

    def reconcile(self, db_manager=None, reports_dir=None, *, public_only=False):
        from sqlalchemy import select
        from src.storage import AnalysisHistory, DailyPlanRun, DatabaseManager
        db_manager = db_manager or DatabaseManager.get_instance()
        primary = primary_user_id()
        with db_manager.get_session() as session:
            # These sources are already durable. A crash anywhere during this
            # reconciliation is safe: version uniqueness deduplicates the replay.
            history_query = select(AnalysisHistory).order_by(AnalysisHistory.id)
            if public_only:
                history_query = history_query.where(AnalysisHistory.is_private == False) if hasattr(AnalysisHistory, "is_private") else None  # noqa: E712
            for row in session.scalars(history_query) if history_query is not None else []:
                owner = getattr(row, "requester_user_id", None) or primary
                # Old main rows cannot prove absence of personal context.
                public = hasattr(row, "is_private") and not row.is_private
                content = json.dumps({"code": row.code, "name": row.name,
                                      "summary": row.analysis_summary, "result": row.raw_result},
                                     ensure_ascii=False, default=str)
                self.record(f"analysis:{row.id}", "stock_analysis", getattr(row, "report_trade_date", None),
                            content, owner=owner, public=public, report_date=row.created_at.date() if row.created_at else None)
            for row in session.scalars(select(DailyPlanRun).order_by(DailyPlanRun.id)) if not public_only else []:
                if _deliverable_plan(row):
                    self.record(f"plan:{row.run_id}", "daily_plan", row.trade_date, row.push_preview,
                                owner=getattr(row, "user_id", None) or primary)
        if reports_dir is None:
            from api.v1.endpoints.closing_analysis import _REPORTS_DIR
            reports_dir = _REPORTS_DIR
        if not reports_dir.exists():
            return
        for path in sorted(reports_dir.iterdir()):
            match = re.fullmatch(r"(market_review|report)_([0-9]{8})\.md", path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                day = date.fromisoformat(f"{match[2][:4]}-{match[2][4:6]}-{match[2][6:]}")
                if match[1] == "market_review":
                    try:
                        from src.services.closing_report_visibility import read_public_market_review
                    except ImportError:
                        continue  # Never certify a legacy mixed/unverified file.
                    content = read_public_market_review(reports_dir, day)
                    if content:
                        self.record(f"market-review:{day}", "market_review", None, content, owner=None, public=True, report_date=day)
                else:
                    if public_only:
                        continue
                    # Daily stock digests can mix personalized analysis. Only
                    # the migration's explicit primary owner may receive them.
                    self.record(f"stock-summary:{day}", "stock_summary", None,
                                path.read_text(encoding="utf-8"), owner=primary, report_date=day)
            except (ValueError, FileNotFoundError):
                continue

    def read(self, subject, after=0, audience="private", limit=100):
        with self.store.db() as db:
            # Scan all rows, not only visible rows: sparse per-user cursors can
            # advance past other owners without exposing their payloads or IDs.
            rows = db.execute("SELECT * FROM report_events WHERE cursor>? ORDER BY cursor LIMIT ?",
                              (after, limit)).fetchall()
        events = []
        for row in rows:
            if row["audience"] != "public" and (audience == "public" or row["owner"] != subject):
                continue
            if not self._source_still_visible(row, subject):
                continue
            events.append({"cursor": row["cursor"], "instanceId": configuration()["INSTANCE_ID"],
                           "reportId": row["report_id"], "version": row["version"], "kind": row["kind"],
                           "tradingDay": row["trading_day"], **({"reportDate": row["report_date"]} if row["report_date"] else {}), "views": [{"audience": row["audience"],
                               "content": row["content"], "verified": True}]})
        return {"events": events, "nextCursor": rows[-1]["cursor"] if rows else after}

    def read_public(self, after=0, limit=100):
        with self.store.db() as db:
            rows = db.execute("SELECT *, 'public' AS audience, NULL AS owner FROM public_report_events WHERE cursor>? ORDER BY cursor LIMIT ?",
                              (after, limit)).fetchall()
        events = [{"cursor": row["cursor"], "instanceId": configuration()["INSTANCE_ID"],
                   "reportId": row["report_id"], "version": row["version"], "kind": row["kind"],
                   "tradingDay": row["trading_day"], **({"reportDate": row["report_date"]} if row["report_date"] else {}), "views": [{"audience": "public", "content": row["content"], "verified": True}]}
                  for row in rows if self._source_still_visible(row, None)]
        return {"events": events, "nextCursor": rows[-1]["cursor"] if rows else after}

    def head(self, *, subject=None, audience="private", public=False):
        with self.store.db() as db:
            if public:
                cursor = db.execute("SELECT coalesce(max(cursor),0) FROM public_report_events").fetchone()[0]
            else:
                cursor = db.execute("SELECT coalesce(max(cursor),0) FROM report_events WHERE audience='public' OR (owner=? AND ?='private')",
                                    (subject, audience)).fetchone()[0]
        return {"events": [], "nextCursor": cursor}

    @staticmethod
    def _source_still_visible(row, subject):
        """Recheck mutable ownership/deletion before returning a durable old event."""
        from sqlalchemy import select
        from src.storage import AnalysisHistory, DailyPlanRun, DatabaseManager
        report_id = row["report_id"]
        if report_id.startswith("analysis:"):
            with DatabaseManager.get_instance().get_session() as session:
                record = session.get(AnalysisHistory, int(report_id.split(":", 1)[1]))
                if record is None:
                    return False
                public = hasattr(record, "is_private") and not record.is_private
                owner = getattr(record, "requester_user_id", None) or primary_user_id()
                return (row["audience"] == "public") == public and (public or owner == subject == row["owner"])
        if report_id.startswith("plan:"):
            with DatabaseManager.get_instance().get_session() as session:
                record = session.scalar(select(DailyPlanRun).where(DailyPlanRun.run_id == report_id.split(":", 1)[1]))
                return (record is not None and _deliverable_plan(record)
                        and (getattr(record, "user_id", None) or primary_user_id()) == subject == row["owner"])
        if report_id.startswith("market-review:"):
            try:
                from src.services.closing_report_visibility import read_public_market_review
                from api.v1.endpoints.closing_analysis import _REPORTS_DIR
                content = read_public_market_review(_REPORTS_DIR, date.fromisoformat(report_id.split(":", 1)[1]))
                return bool(content) and hashlib.sha256(content.encode()).hexdigest() == row["version"]
            except ImportError:
                return False
        return True
