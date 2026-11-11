"""Owned paid screening using the existing engine in an isolated worker process.

The legacy engine bridges environment variables and owns module caches. Never run
it in a shared request thread for a personal account. Only the effective user
configuration crosses the private pipe; durable tasks retain a grant reference.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import re
import time
import uuid

from fastapi import HTTPException

from src.services.finance_identity import FinanceIdentityStore, fail, is_user_center_mode


def _process_marker(pid):
    """Return actual process creation identity, or None after confirmed exit."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied cannot prove the old process exited.
            return None if ctypes.get_last_error() == 87 else "unknown"
        try:
            code = wintypes.DWORD()
            if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return "unknown"
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return stat.read_text().rsplit(")", 1)[1].split()[19] if stat.exists() else "unknown"
    except (ProcessLookupError, FileNotFoundError):
        return None
    except OSError:
        return "unknown"


_EPOCH = _process_marker(os.getpid())


class ScreeningWorkerError(RuntimeError):
    """A diagnostic identifier with no provider exception body or credentials."""
    pass


def _user_config(user_id):
    try:
        from src.services.user_config_service import require_user_model, has_usable_model
    except ImportError:
        if is_user_center_mode():
            fail(503, "FINANCE_PERSONAL_SCREENING_REQUIRES_USER_CONFIG")
        from src.config import get_config
        config = get_config()
    else:
        config = require_user_model(user_id)
        if not has_usable_model(config):
            fail(409, "MODEL_KEY_REQUIRED")
    if not getattr(config, "llm_model_list", None):
        fail(409, "MODEL_KEY_REQUIRED")
    return config


def _request(arguments):
    from src.services.screening.scope import ScreeningScope
    if set(arguments) - {"strategy", "market", "max_results", "variant_seed", "scope"}:
        fail(422, "FINANCE_UNKNOWN_ARGUMENT")
    strategy = arguments.get("strategy")
    market = arguments.get("market", "cn")
    maximum = arguments.get("max_results", 20)
    seed = arguments.get("variant_seed", "")
    if not isinstance(strategy, str) or not re.fullmatch(r"[\w-]{1,64}", strategy):
        fail(422, "FINANCE_STRATEGY_INVALID")
    if market not in {"cn", "us"} or type(maximum) is not int or not 1 <= maximum <= 100:
        fail(422, "FINANCE_SCREENING_INPUT_INVALID")
    if not isinstance(seed, str) or len(seed) > 128:
        fail(422, "FINANCE_SCREENING_INPUT_INVALID")
    try:
        scope = ScreeningScope.model_validate(arguments.get("scope") or {})
    except ValueError:
        fail(422, "FINANCE_SCREENING_SCOPE_INVALID")
    if market != "cn" and (scope.boards or scope.topics):
        fail(422, "FINANCE_SCREENING_SCOPE_MARKET")
    return {"strategy": strategy, "market": market, "max_results": maximum,
            "selection_seed": seed, "scope": scope.model_dump()}


def _cache_directory(config, owner, task_id):
    # A server-derived namespace; no user-supplied paths or shared private search cache.
    root = Path(config.database_path).resolve().parent / "screening-users"
    scope = hashlib.sha256(json.dumps([owner["issuer"], owner["subject"]]).encode()).hexdigest()
    target = root / scope / task_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def _credential_fingerprint(config):
    """In-memory approval fence; never persist credentials or this digest."""
    try:
        from src.services.user_config_service import DATA_SOURCE_CREDENTIAL_KEYS, SEARCH_KEYS
        keys = DATA_SOURCE_CREDENTIAL_KEYS | SEARCH_KEYS
    except ImportError:
        keys = frozenset()
    values = {key: getattr(config, key.lower(), None) for key in keys}
    values["models"] = config.llm_model_list
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).digest()


def _worker_environment(config, cache):
    environment = {name: value for name, value in os.environ.items()
                   if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                                       "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}}
    try:
        from src.services.user_config_service import DATA_SOURCE_CREDENTIAL_KEYS, SEARCH_KEYS
        keys = DATA_SOURCE_CREDENTIAL_KEYS | SEARCH_KEYS
    except ImportError:
        keys = frozenset({"TUSHARE_TOKEN", "FINNHUB_API_KEY", "ALPHAVANTAGE_API_KEY"})
    for key in keys:
        value = getattr(config, key.lower(), None)
        if isinstance(value, (tuple, list)):
            value = ",".join(str(item) for item in value)
        environment[key] = str(value or "")
    from src.services.finance_report_events import primary_user_id
    environment.update({"PRIMARY_USER_ID": primary_user_id(), "POST_ANALYZERS": "scorecard", "DATABASE_PATH": str(config.database_path), "SCREENING_DATA_DIR": str(cache),
                        "SCREENING_FALLBACK_SNAPSHOT_PATH": str(cache / "snapshot.last_good.json"),
                        "SCREENING_DAILY_HISTORY_CACHE_DIR": str(cache / "daily_history"),
                        "SCREENING_INDUSTRY_PROVIDER_CACHE_DIR": str(cache / "industry"),
                        "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_LOG": "ERROR"})
    return environment


def _execute_worker(connection, config, request, environment, user_id):
    """Child entry point. All compatibility mutations are confined to this process."""
    import logging
    import sys
    import threading
    pipe_lock = threading.RLock()

    def emit(message):
        with pipe_lock:
            connection.send(message)

    try:
        os.environ.clear()
        os.environ.update(environment)
        os.chdir(environment["SCREENING_DATA_DIR"])
        logging.disable(logging.CRITICAL)
        # Provider diagnostics can contain request credentials. The pipe is the only output.
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
        from src.config import Config
        Config._instance = config
        from src.services.screening import config as runtime_config, ranker
        from src.services.screening.scope import ScreeningScope
        from src.services.screening_service import ScreeningService
        runtime_config._load_env_file = lambda: None  # never fill missing personal keys from server .env

        def authorize():
            with pipe_lock:
                connection.send({"type": "authorize"})
                if connection.recv() is not True:
                    raise PermissionError("FINANCE_SCREENING_AUTHORITY_CHANGED")

        from src.search_service import BaseSearchProvider
        original_search = BaseSearchProvider._execute_search

        def authorized_search(*args, **kwargs):
            authorize()
            return original_search(*args, **kwargs)
        BaseSearchProvider._execute_search = authorized_search

        original = ranker._call_screening_litellm_completion_once

        def billed_call(call, **kwargs):
            authorize()
            try:
                from src.services.user_config_service import require_usable_model
            except ImportError:
                pass
            else:
                require_usable_model(config)
            response = original(call, **kwargs)
            from src.llm.usage import extract_usage_payload, normalize_litellm_usage
            from src.storage import persist_llm_usage
            usage = normalize_litellm_usage(extract_usage_payload(response), model=kwargs["model"])
            extras = {"user_id": user_id} if "user_id" in inspect.signature(persist_llm_usage).parameters else {}
            persist_llm_usage(usage, kwargs["model"], "screening", **extras)
            return response
        ranker._call_screening_litellm_completion_once = billed_call
        authorize()
        actual = {**request, "scope": ScreeningScope.model_validate(request["scope"])}
        result = ScreeningService(config=config, db_manager=None).screen(
            **actual, progress_callback=lambda progress, message: emit(
                {"type": "progress", "progress": progress, "message": message}),
        )
        authorize()
        emit({"type": "result", "result": result})
    except Exception as error:
        # Do not persist provider exception text or credential-bearing reprs.
        import traceback
        cause = error.__cause__ or error
        frames = traceback.extract_tb(cause.__traceback__)
        where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "unknown"
        emit({"type": "error", "error": f"{type(cause).__name__}@{where}"})
    finally:
        connection.close()


class FinanceUserScreening:
    def __init__(self, store=None, *, worker_target=None):
        self.store = store or FinanceIdentityStore()
        self.worker_target = worker_target or _execute_worker
        with self.store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS finance_screening_tasks (
                task_id TEXT PRIMARY KEY, issuer TEXT NOT NULL, subject TEXT NOT NULL, grant_id TEXT NOT NULL,
                user_id TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                result TEXT, error TEXT, epoch TEXT NOT NULL, owner_pid INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, finished REAL)""")
            if "owner_pid" not in {row[1] for row in db.execute("PRAGMA table_info(finance_screening_tasks)")}:
                db.execute("ALTER TABLE finance_screening_tasks ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0")
            db.execute("DROP INDEX IF EXISTS finance_screening_one_active")

    def _authorize(self, grant_id, owner, *, accepted_task=False):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM grants WHERE id=?", (grant_id,)).fetchone()
        if accepted_task:
            # A previously accepted paid job uses the durable binding; bearer
            # expiry does not end a live login, while revoke/rebind still does.
            if not row or row["revoked"]:
                fail(401, "FINANCE_DELEGATION_INVALID")
            try:
                private = json.loads(self.store.cipher().decrypt(row["cookie"].encode()))
                payload, cookie = json.loads(row["identity"]), private["cookie"]
            except Exception:
                fail(401, "FINANCE_DELEGATION_INVALID")
        else:
            payload, cookie = self.store._payload(row)
        if any(payload[key] != owner[key] for key in ("issuer", "subject", "instanceId", "coreId")):
            fail(403, "FINANCE_SCREENING_OWNER_MISMATCH")
        self.store.verify_authority(payload, cookie)

    def _rows(self, owner, task_id=None):
        with self.store.db() as db:
            active = db.execute("""SELECT task_id,owner_pid,epoch FROM finance_screening_tasks
                WHERE issuer=? AND subject=? AND status IN ('pending','processing')""",
                                (owner["issuer"], owner["subject"])).fetchall()
            for row in active:
                marker = _process_marker(row["owner_pid"]) if row["owner_pid"] else None
                if marker is None or (marker != "unknown" and marker != row["epoch"]):
                    db.execute("UPDATE finance_screening_tasks SET status='interrupted',error='process_exited',finished=? WHERE task_id=? AND status IN ('pending','processing')",
                               (time.time(), row["task_id"]))
            query = "SELECT * FROM finance_screening_tasks WHERE issuer=? AND subject=?"
            params = [owner["issuer"], owner["subject"]]
            if task_id:
                query += " AND task_id=?"
                params.append(task_id)
            return [dict(row) for row in db.execute(query + " ORDER BY created DESC LIMIT 100", params)]

    def get(self, arguments, *, owner):
        if set(arguments) - {"task_id"}:
            fail(422, "FINANCE_UNKNOWN_ARGUMENT")
        task_id = arguments.get("task_id")
        if task_id is not None and (not isinstance(task_id, str) or not task_id):
            fail(422, "FINANCE_SCREENING_TASK_INVALID")
        rows = self._rows(owner, task_id)
        if task_id and not rows:
            fail(404, "FINANCE_SCREENING_TASK_NOT_FOUND")

        def view(row):
            return {"task_id": row["task_id"], "status": row["status"], "progress": row["progress"],
                    "created_at": row["created"], "finished_at": row["finished"], "error": row["error"],
                    "audience": "private", "result": json.loads(row["result"]) if row["result"] else None}
        return view(rows[0]) if task_id else {"tasks": [view(row) for row in rows]}

    def start(self, arguments, *, owner, session, operation_id):
        if not operation_id or not isinstance(operation_id, str) or len(operation_id) > 256:
            fail(422, "FINANCE_OPERATION_ID_REQUIRED")
        request = _request(arguments)
        if is_user_center_mode():
            if session is None or session.user_id != owner["subject"]:
                fail(403, "FINANCE_SCREENING_OWNER_MISMATCH")
            user_id = session.user_id
        else:
            from src.services.finance_report_events import primary_user_id
            user_id = primary_user_id()
        # Missing P2 config is an explicit capability gap, never a primary-key fallback.
        _user_config(user_id)
        self._authorize(owner["delegationId"], owner)
        self._rows(owner)
        scope = "screening:" + hashlib.sha256(json.dumps([owner["issuer"], owner["subject"]]).encode()).hexdigest()
        return self.store.operation(scope, operation_id, request,
                                    lambda: self._submit(request, owner, user_id))

    def _submit(self, request, owner, user_id):
        from src.services.task_queue import get_task_queue
        task_id = uuid.uuid4().hex
        with self.store.db() as db:
            db.execute("INSERT INTO finance_screening_tasks(task_id,issuer,subject,grant_id,user_id,request,status,epoch,owner_pid,created) VALUES(?,?,?,?,?,?,'pending',?,?,?)",
                       (task_id, owner["issuer"], owner["subject"], owner["delegationId"], user_id,
                        json.dumps(request), _EPOCH, os.getpid(), time.time()))

        def run():
            try:
                result = self._run(task_id, request, owner, user_id)
                self._update(task_id, status="completed", progress=100, result=json.dumps(result), finished=time.time())
                return result
            except Exception as error:
                code = str(error) if isinstance(error, ScreeningWorkerError) else type(error).__name__
                self._update(task_id, status="failed", error=code, finished=time.time())
                raise HTTPException(424, detail={"error": "FINANCE_SCREENING_FAILED"}) from None
        try:
            submit = get_task_queue().submit_background_task
            extras = {"user_id": user_id} if "user_id" in inspect.signature(submit).parameters else {}
            submit(run, stock_code="personal_screening", stock_name=request["strategy"],
                   report_type="personal_screening", task_id=task_id, trace_id=task_id, **extras)
        except Exception:
            self._update(task_id, status="failed", error="queue_unavailable", finished=time.time())
            raise
        return {"task_id": task_id, "status": "pending", "audience": "private"}

    def _update(self, task_id, **values):
        with self.store.db() as db:
            db.execute("UPDATE finance_screening_tasks SET " + ",".join(key + "=?" for key in values) + " WHERE task_id=?",
                       [*values.values(), task_id])

    def _run(self, task_id, request, owner, user_id):
        self._authorize(owner["delegationId"], owner, accepted_task=True)
        config = _user_config(user_id)  # resolve again when the queue actually starts
        credential_fingerprint = _credential_fingerprint(config)
        cache = _cache_directory(config, owner, task_id)
        environment = _worker_environment(config, cache)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=self.worker_target, args=(child, config, request, environment, user_id), daemon=True)
        self._update(task_id, status="processing")
        process.start()
        child.close()
        deadline = time.monotonic() + max(30, float(getattr(config, "analysis_timeout", 600) or 600))
        try:
            while time.monotonic() < deadline:
                if not parent.poll(.2):
                    if not process.is_alive():
                        raise RuntimeError("worker_interrupted")
                    continue
                message = parent.recv()
                if message["type"] == "authorize":
                    try:
                        self._authorize(owner["delegationId"], owner, accepted_task=True)
                        if _credential_fingerprint(_user_config(user_id)) != credential_fingerprint:
                            raise PermissionError("FINANCE_SCREENING_CONFIGURATION_CHANGED")
                        parent.send(True)
                    except Exception:
                        parent.send(False)
                        raise
                elif message["type"] == "progress":
                    from src.services.task_queue import get_task_queue
                    self._update(task_id, progress=message["progress"])
                    get_task_queue().update_task_progress(task_id, message["progress"], message["message"])
                elif message["type"] == "result":
                    self._authorize(owner["delegationId"], owner, accepted_task=True)
                    return {**message["result"], "audience": "private", "requester_user_id": user_id}
                else:
                    raise ScreeningWorkerError(str(message.get("error", "worker_failed")))
            raise TimeoutError("screening_timeout")
        finally:
            parent.close()
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
