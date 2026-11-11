"""Single-owner Feishu handoff, shared by CLI, Web, workers and hot reload.

Ownership is durable once enabled; deleting an environment flag cannot reactivate
an old sender. A live legacy receiver holds an OS lease until its websocket exits.
"""
from __future__ import annotations

import os
import hashlib
import posixpath
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from urllib.parse import unquote, urlsplit

from src.services.finance_identity import FinanceIdentityStore, fail, setting
from src.config import get_config

_in_send = ContextVar("finance_feishu_send", default=False)


def managed_store():
    path = Path(get_config().database_path).with_suffix(".finance.db")
    enabled = setting("FINANCE_FEISHU_MANAGED").lower() in {"1", "true", "yes"}
    if not enabled and not path.exists():
        return None
    store = FinanceIdentityStore(path)
    with store.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='feishu_owner'").fetchone()
        if not enabled and not exists:
            return None
        db.execute("CREATE TABLE IF NOT EXISTS feishu_owner (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, version INTEGER NOT NULL, app_id TEXT NOT NULL, tenant_id TEXT)")
        selected = setting("FINANCE_FEISHU_APP_ID") or str(getattr(get_config(), "feishu_app_id", "") or "").strip()
        if not selected:
            fail(503, "FINANCE_FEISHU_APP_REQUIRED")
        db.execute("INSERT OR IGNORE INTO feishu_owner VALUES(1,'stock',1,?,?)", (selected, setting("FINANCE_FEISHU_TENANT_ID") or None))
        saved = db.execute("SELECT app_id FROM feishu_owner WHERE id=1").fetchone()[0]
        if saved != selected:
            fail(409, "FINANCE_FEISHU_APP_MISMATCH")
    return store


def channel_state():
    store = managed_store()
    if store is None:
        return {"owner": "stock", "version": 0, "managed": False}
    with store.db() as db:
        row = db.execute("SELECT * FROM feishu_owner WHERE id=1").fetchone()
    return {"owner": row["owner"], "version": row["version"], "managed": True,
            "appId": row["app_id"], "tenantId": row["tenant_id"]}


def stock_allowed(actor=None):
    state = channel_state()
    if actor is not None and state["managed"]:
        nested = getattr(actor, "_reply_client", actor)
        actual = (getattr(nested, "_app_id", None) or getattr(nested, "_feishu_app_id", None)
                  or getattr(nested, "feishu_app_id", None))
        if actual and actual != state["appId"]:
            return True
    return state["owner"] == "stock"


@contextmanager
def receiver_lease(store, app_id=None):
    app_id = app_id or setting("FINANCE_FEISHU_APP_ID") or str(getattr(get_config(), "feishu_app_id", "") or "")
    suffix = hashlib.sha256(app_id.encode()).hexdigest()[:20]
    path = store.path.with_suffix(f".feishu-receiver.{suffix}.lock")
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fail(409, "FINANCE_STOCK_RECEIVER_STILL_RUNNING")
        yield
    finally:
        handle.close()  # OS releases the lease even after a process crash.


def transition(expected_version, owner):
    if owner not in {"stock", "paused", "core"}:
        fail(422, "FINANCE_INVALID_CHANNEL_OWNER")
    store = managed_store()
    if store is None:
        fail(409, "FINANCE_CHANNEL_NOT_MANAGED")
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM feishu_owner WHERE id=1").fetchone()
        if row["version"] != expected_version:
            fail(409, "FINANCE_CHANNEL_VERSION_CONFLICT")
        if owner != row["owner"] and owner != "paused" and row["owner"] != "paused":
            fail(409, "FINANCE_CHANNEL_MUST_PAUSE_FIRST")
        # Core cannot claim while the legacy SDK remains connected. stop() in
        # the old SDK only stops dispatch, so the owning process must exit.
        if owner == "core":
            with receiver_lease(store, row["app_id"]):
                db.execute("UPDATE feishu_owner SET owner=?,version=version+1 WHERE id=1", (owner,))
        else:
            db.execute("UPDATE feishu_owner SET owner=?,version=version+1 WHERE id=1", (owner,))
    return channel_state()


def _independent_personal_webhook(actor):
    webhook = getattr(actor, "_feishu_url", None)
    if not webhook:
        return False
    from src.services.user_notification_service import UserNotificationService

    if not isinstance(actor, UserNotificationService) or actor._row_id is None:
        return False
    shared = getattr(get_config(), "feishu_webhook_url", None)
    if not shared:
        return True

    def target(url):
        parts = urlsplit(url)
        path = posixpath.normpath(unquote(parts.path))
        # Signing parameters can change. Feishu identifies a bot webhook by its path token.
        if path.startswith("/open-apis/bot/v2/hook/"):
            return ("feishu", path)
        return (parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), path)

    try:
        return target(webhook) != target(shared)
    except ValueError:
        return False


def stock_send(function):
    """Hold the ownership transaction through leaf I/O, including retries.

    A concurrent handoff cannot succeed while a pre-cutover send is in flight.
    Nested SDK/file helpers share the outer guard rather than reacquiring it.
    """
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _in_send.get():
            return function(*args, **kwargs)
        store = managed_store()
        if store is None:
            return function(*args, **kwargs)
        actor = args[0] if args else None
        actual_app = getattr(actor, "_app_id", None) or getattr(actor, "_feishu_app_id", None)
        if actual_app and actual_app != channel_state()["appId"]:
            return function(*args, **kwargs)
        if not actual_app and _independent_personal_webhook(actor):
            return function(*args, **kwargs)
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT owner FROM feishu_owner WHERE id=1").fetchone()[0] != "stock":
                return False
            token = _in_send.set(True)
            try:
                return function(*args, **kwargs)
            finally:
                _in_send.reset(token)
    return wrapped


def stock_receiver(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        store = managed_store()
        if store is None:
            # Keep a lease even before management is enabled. A later config
            # reload cannot hide an already connected Stock SDK from handoff.
            store = FinanceIdentityStore(Path(get_config().database_path).with_suffix(".finance.db"))
        with receiver_lease(store, getattr(self, "_app_id", None)):
            if not stock_allowed(self):
                self._running = False
                return
            return function(self, *args, **kwargs)
    return wrapped


def require_stock_registration(app_id=None):
    state = channel_state()
    actual = app_id or str(getattr(get_config(), "feishu_app_id", "") or "").strip()
    if state["managed"] and actual and actual != state["appId"]:
        return
    if state["owner"] != "stock":
        fail(409, "FINANCE_FEISHU_MANAGED_BY_CORE")
