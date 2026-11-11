"""Revocable, service-bound finance delegations; opaque credentials never identify users by input."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from fastapi import HTTPException

from src.auth import is_user_center_mode, read_auth_setting
from src.config import get_config

acting_session: ContextVar = ContextVar("finance_acting_session", default=None)


def fail(status, code):
    raise HTTPException(status, detail={"error": code})


def setting(key):
    return read_auth_setting(key).strip()


def configuration():
    result = {key: setting("FINANCE_" + key) for key in ("SERVICE_TOKEN", "INSTANCE_ID", "CORE_ID")}
    if not all(result.values()) or len(result["SERVICE_TOKEN"]) < 32:
        fail(503, "FINANCE_NOT_CONFIGURED")
    return result


def service_auth(headers):
    cfg = configuration()
    value = headers.get("authorization", "")
    if not hmac.compare_digest(value.encode(), ("Bearer " + cfg["SERVICE_TOKEN"]).encode()):
        fail(401, "FINANCE_SERVICE_REQUIRED")
    return cfg


def user_system_available():
    """P2b-2 owns private results and paid jobs. P1 alone already defines CurrentUser."""
    if not personal_data_available():
        return False
    from src import storage_multi_user_migration as migration
    from src.storage import AnalysisHistory
    return hasattr(migration, "MULTI_USER_P2B2_PURGE_SCHEMA_VERSION") and hasattr(AnalysisHistory, "is_private")


def personal_data_available():
    from api import deps
    if not hasattr(deps, "CurrentUser"):
        return False
    try:
        from src import storage_multi_user_migration as migration
    except ImportError:
        return False
    from src.storage import ConversationSessionState, DatabaseManager
    return (hasattr(migration, "MULTI_USER_P2A_SCHEMA_VERSION")
            and hasattr(ConversationSessionState, "user_id")
            and hasattr(DatabaseManager, "require_chat_session_access"))


def identity(session):
    cfg = configuration()
    if is_user_center_mode():
        from src.user_center_auth import load_settings
        if session is None or not session.user_id:
            fail(401, "AUTH_REQUIRED")
        issuer = load_settings().base_url.rstrip("/")
        subject = session.user_id
    else:
        issuer, subject = setting("FINANCE_LOCAL_ISSUER"), setting("FINANCE_LOCAL_SUBJECT")
        if not issuer or not subject:
            fail(503, "FINANCE_LOCAL_BINDING_REQUIRED")
    return {"issuer": issuer, "subject": subject, "instanceId": cfg["INSTANCE_ID"], "coreId": cfg["CORE_ID"]}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class FinanceIdentityStore:
    """Separate durable ledger next to the application database; no plaintext session cookies."""

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(get_config().database_path).with_suffix(".finance.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, identity TEXT NOT NULL,
                cookie TEXT NOT NULL, expires INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                refresh_hash TEXT);
            CREATE TABLE IF NOT EXISTS exchanges (
                code_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, expires INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS operations (
                grant_id TEXT NOT NULL, operation_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                result TEXT, PRIMARY KEY(grant_id, operation_id));
            """)
            db.execute("BEGIN IMMEDIATE")
            if "refresh_hash" not in {row[1] for row in db.execute("PRAGMA table_info(grants)")}:
                db.execute("ALTER TABLE grants ADD COLUMN refresh_hash TEXT")

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            yield db

    def cipher(self):
        from cryptography.fernet import Fernet
        key = hashlib.sha256(configuration()["SERVICE_TOKEN"].encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    def bound(self, owner):
        with self.db() as db:
            row = db.execute("SELECT * FROM grants WHERE identity=? AND revoked=0 ORDER BY expires DESC LIMIT 1",
                             (json.dumps(owner, sort_keys=True),)).fetchone()
        if row is None:
            return False
        try:
            private = json.loads(self.cipher().decrypt(row["cookie"].encode()))
            return bool(private.get("token") and private.get("refreshToken"))
        except Exception:
            return False

    def create(self, owner, cookie):
        return self.ensure_bound(owner, cookie, replace=True)

    def ensure_bound(self, owner, cookie, *, replace=False):
        """Transactional explicit connect; concurrent browsers reuse one binding.

        Returns server-only grant material. Browser endpoints must strip both
        credential fields. Rotation-inaccessible rows are retired on explicit bind.
        """
        token, grant_id = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        expires = int(time.time() * 1000) + 900_000
        refresh = secrets.token_urlsafe(40)
        encrypted = self.cipher().encrypt(json.dumps({"cookie": cookie, "token": token, "refreshToken": refresh}).encode()).decode()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not replace:
                existing = db.execute("SELECT * FROM grants WHERE identity=? AND revoked=0 ORDER BY expires DESC LIMIT 1",
                                      (json.dumps(owner, sort_keys=True),)).fetchone()
                if existing:
                    try:
                        private = json.loads(self.cipher().decrypt(existing["cookie"].encode()))
                    except Exception:
                        private = None
                    if private and private.get("refreshToken"):
                        private["cookie"] = cookie
                        db.execute("UPDATE grants SET cookie=? WHERE id=?",
                                   (self.cipher().encrypt(json.dumps(private).encode()).decode(), existing["id"]))
                        return {**owner, "delegationId": existing["id"], "delegationToken": private["token"],
                                "refreshToken": private["refreshToken"], "expiresAt": existing["expires"],
                                "capabilities": renewal_capabilities()}
            # A pairing has one current durable binding. Explicit rebind retires
            # older grants so revoking the current grant cannot fall back to one.
            db.execute("UPDATE grants SET revoked=1 WHERE identity=?", (json.dumps(owner, sort_keys=True),))
            db.execute("INSERT INTO grants(id,token_hash,identity,cookie,expires,refresh_hash) VALUES(?,?,?,?,?,?)",
                       (grant_id, digest(token), json.dumps(owner, sort_keys=True), encrypted, expires, digest(refresh)))
        return {**owner, "delegationId": grant_id, "delegationToken": token, "refreshToken": refresh,
                "expiresAt": expires, "capabilities": renewal_capabilities()}

    def _payload(self, row):
        if not row or row["revoked"] or row["expires"] <= int(time.time() * 1000):
            fail(401, "FINANCE_DELEGATION_INVALID")
        try:
            private = json.loads(self.cipher().decrypt(row["cookie"].encode()))
        except Exception:
            fail(401, "FINANCE_DELEGATION_INVALID")
        return {**json.loads(row["identity"]), "delegationId": row["id"],
                "delegationToken": private["token"], "refreshToken": private.get("refreshToken"),
                "expiresAt": row["expires"], "capabilities": renewal_capabilities()}, private["cookie"]

    def verify(self, token):
        with self.db() as db:
            row = db.execute("SELECT * FROM grants WHERE token_hash=?", (digest(token),)).fetchone()
        payload, cookie = self._payload(row)
        return payload, self.verify_authority(payload, cookie)

    @staticmethod
    def verify_authority(payload, cookie):
        session = None
        if is_user_center_mode():
            from src.user_center_auth import resolve_session, role_satisfies, load_settings, UserCenterAuthError
            try:
                session = resolve_session([cookie])
            except UserCenterAuthError as exc:
                fail(exc.status_code, exc.code)
            if session is None or not role_satisfies(session.role, load_settings().required_role):
                fail(401, "FINANCE_LOGIN_EXPIRED")
        if identity(session) != {k: payload[k] for k in ("issuer", "subject", "instanceId", "coreId")}:
            fail(403, "FINANCE_AUDIENCE_MISMATCH")
        return session

    def subscription(self, refresh_token):
        """Public-only proof of an explicit, unrevoked binding; never a user identity."""
        with self.db() as db:
            row = db.execute("SELECT * FROM grants WHERE refresh_hash=? AND revoked=0", (digest(refresh_token),)).fetchone()
        if not row:
            fail(401, "FINANCE_SUBSCRIPTION_INVALID")
        try:
            private = json.loads(self.cipher().decrypt(row["cookie"].encode()))
        except Exception:
            fail(401, "FINANCE_SUBSCRIPTION_INVALID")
        cfg = configuration()
        owner = json.loads(row["identity"])
        if (owner["instanceId"], owner["coreId"]) != (cfg["INSTANCE_ID"], cfg["CORE_ID"]):
            fail(403, "FINANCE_AUDIENCE_MISMATCH")
        if private.get("refreshToken") != refresh_token:
            fail(401, "FINANCE_SUBSCRIPTION_INVALID")
        return row["id"]

    def renew(self, grant_id, refresh_token):
        self.subscription(refresh_token)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE id=? AND refresh_hash=? AND revoked=0",
                             (grant_id, digest(refresh_token))).fetchone()
            if not row:
                fail(401, "FINANCE_DELEGATION_INVALID")
            private = json.loads(self.cipher().decrypt(row["cookie"].encode()))
            owner = json.loads(row["identity"])
            self.verify_authority(owner, private["cookie"])
            if row["expires"] > int(time.time() * 1000) + 30_000:
                return {**owner, "delegationId": grant_id, "delegationToken": private["token"],
                        "refreshToken": private["refreshToken"], "expiresAt": row["expires"],
                        "capabilities": renewal_capabilities()}
            private["token"] = secrets.token_urlsafe(32)
            expires = int(time.time() * 1000) + 900_000
            encrypted = self.cipher().encrypt(json.dumps(private).encode()).decode()
            cursor = db.execute("UPDATE grants SET token_hash=?,cookie=?,expires=? WHERE id=? AND revoked=0 AND refresh_hash=?",
                                (digest(private["token"]), encrypted, expires, grant_id, digest(refresh_token)))
            if not cursor.rowcount:
                fail(401, "FINANCE_DELEGATION_INVALID")
        return {**owner, "delegationId": grant_id, "delegationToken": private["token"],
                "refreshToken": private["refreshToken"], "expiresAt": expires, "capabilities": renewal_capabilities()}

    def exchange(self, owner, cookie=""):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM grants WHERE identity=? AND revoked=0 ORDER BY expires DESC LIMIT 1",
                             (json.dumps(owner, sort_keys=True),)).fetchone()
            if not row:
                fail(409, "FINANCE_BINDING_REQUIRED")
            # The durable binding survives its short bearer lease. Only a fresh
            # browser login can renew it, and renewal never revives revoked rows.
            try:
                private = json.loads(self.cipher().decrypt(row["cookie"].encode()))
            except Exception:
                fail(409, "FINANCE_BINDING_REQUIRED")
            if row["expires"] <= int(time.time() * 1000):
                private["token"] = secrets.token_urlsafe(32)
            private["cookie"] = cookie
            encrypted = self.cipher().encrypt(json.dumps(private).encode()).decode()
            db.execute("UPDATE grants SET cookie=?,token_hash=?,expires=? WHERE id=?",
                       (encrypted, digest(private["token"]), int(time.time() * 1000) + 900_000, row["id"]))
            code, expires = secrets.token_urlsafe(32), int(time.time() * 1000) + 60_000
            db.execute("INSERT INTO exchanges VALUES(?,?,?,0)", (digest(code), row["id"], expires))
        return {"code": code, "expiresAt": expires}

    def redeem(self, code, core_id):
        if core_id != configuration()["CORE_ID"]:
            fail(403, "FINANCE_AUDIENCE_MISMATCH")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM exchanges WHERE code_hash=?", (digest(code),)).fetchone()
            if not row or row["used"] or row["expires"] <= int(time.time() * 1000):
                fail(401, "FINANCE_EXCHANGE_INVALID")
            grant = db.execute("SELECT * FROM grants WHERE id=?", (row["grant_id"],)).fetchone()
            payload, _ = self._payload(grant)
            db.execute("UPDATE exchanges SET used=1 WHERE code_hash=?", (digest(code),))
        return self.verify(payload["delegationToken"])[0]

    def revoke(self, owner, grant_id):
        with self.db() as db:
            cursor = db.execute("UPDATE grants SET revoked=1 WHERE id=? AND identity=?",
                                (grant_id, json.dumps(owner, sort_keys=True)))
            if not cursor.rowcount:
                fail(404, "FINANCE_DELEGATION_NOT_FOUND")

    def list(self, owner):
        with self.db() as db:
            rows = db.execute("SELECT id,expires,revoked FROM grants WHERE identity=?",
                              (json.dumps(owner, sort_keys=True),)).fetchall()
        return [{"delegationId": row["id"], "expiresAt": row["expires"], "revoked": bool(row["revoked"])} for row in rows]

    def operation(self, grant_id, operation_id, arguments, execute):
        fingerprint = digest(json.dumps(arguments, sort_keys=True, separators=(",", ":")))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE grant_id=? AND operation_id=?",
                             (grant_id, operation_id)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    fail(409, "FINANCE_OPERATION_CONFLICT")
                if row["result"] is None:
                    fail(409, "FINANCE_OPERATION_OUTCOME_UNKNOWN")
                return json.loads(row["result"])
            db.execute("INSERT INTO operations VALUES(?,?,?,NULL)", (grant_id, operation_id, fingerprint))
        # Commit the intent before execution: a crash must never replay a possible write.
        result = execute()
        with self.db() as db:
            db.execute("UPDATE operations SET result=? WHERE grant_id=? AND operation_id=?",
                       (json.dumps(result), grant_id, operation_id))
        return result


@contextmanager
def session_scope(session):
    token = acting_session.set(session)
    try:
        yield
    finally:
        acting_session.reset(token)


def renewal_capabilities():
    return {"offlinePrivateRenewal": not is_user_center_mode(),
            "publicEventsWithoutUpstreamSession": True}


class InternalIdentityMiddleware:
    """Per-request ASGI state, avoiding shared dependency overrides across concurrent users."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        session = acting_session.get()
        if session is not None:
            scope.setdefault("state", {})["user_center_session"] = session
        await self.app(scope, receive, send)
