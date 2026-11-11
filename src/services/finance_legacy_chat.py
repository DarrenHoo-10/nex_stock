"""Compatibility for old Stock chat URLs, using the same authenticated Core topics.

Called inside the real route handlers after Stock authentication/dependencies.
Leaving FINANCE_CORE_URL empty preserves the existing local assistant paths.
"""
from __future__ import annotations

import asyncio
import json
import uuid
import hashlib

from fastapi.responses import JSONResponse, StreamingResponse

from src.services.finance_identity import FinanceIdentityStore, fail, setting, personal_data_available


def empty_state(session_id):
    return {"session_id": session_id, "messages": [], "session_state": {"selected_skill_ids": None},
            "active_stock_context": None, "session_state_version": 0, "session_generation": None}


def empty_context(session_id):
    return {"session_id": session_id, "model": None, "context_window": None, "message_count": 0,
            "history_tokens": 0, "history_message_count": 0, "compression_enabled": False,
            "trigger_tokens": 0, "protected_turns": 0, "compactable_message_count": 0, "summary": None}


def require_local_access(request, session_id, *, write):
    if not personal_data_available():
        return True
    from api.deps import get_current_user
    from src.config import get_primary_user_id
    from src.agent.conversation import is_bot_session_id
    from src.storage import DatabaseManager, ChatSessionNotFound
    user = get_current_user(request)
    if write and user.user_id != get_primary_user_id() and is_bot_session_id(session_id):
        fail(400, "FINANCE_RESERVED_SESSION_PREFIX")
    try:
        DatabaseManager.get_instance().require_chat_session_access(session_id, user.user_id)
    except ChatSessionNotFound:
        if write:
            fail(404, "FINANCE_SESSION_NOT_FOUND")
        return False
    return True


def request_ledger(owner, request_id, *, session_id=None, message_id=None, task_id=None, request_hash=None):
    store = FinanceIdentityStore()
    owner_key = json.dumps(owner, sort_keys=True)
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS legacy_finance_requests (
            owner TEXT NOT NULL, request_id TEXT NOT NULL, session_id TEXT NOT NULL,
            message_id TEXT, task_id TEXT, request_hash TEXT, PRIMARY KEY(owner,request_id))""")
        if "request_hash" not in {row[1] for row in db.execute("PRAGMA table_info(legacy_finance_requests)")}:
            db.execute("ALTER TABLE legacy_finance_requests ADD COLUMN request_hash TEXT")
        if session_id is not None:
            existing = db.execute("SELECT * FROM legacy_finance_requests WHERE owner=? AND request_id=?",
                                  (owner_key, request_id)).fetchone()
            if existing and existing["session_id"] != session_id:
                fail(409, "FINANCE_REQUEST_CONFLICT")
            if existing and request_hash and existing["request_hash"] and request_hash != existing["request_hash"]:
                fail(409, "FINANCE_REQUEST_CONFLICT")
            db.execute("""INSERT INTO legacy_finance_requests(owner,request_id,session_id,message_id,task_id,request_hash) VALUES(?,?,?,?,?,?)
                ON CONFLICT(owner,request_id) DO UPDATE SET
                message_id=coalesce(excluded.message_id,message_id),task_id=coalesce(excluded.task_id,task_id),request_hash=coalesce(excluded.request_hash,request_hash)""",
                       (owner_key, request_id, session_id, message_id, task_id, request_hash))
        row = db.execute("SELECT * FROM legacy_finance_requests WHERE owner=? AND request_id=?",
                         (owner_key, request_id)).fetchone()
    return dict(row) if row else None


async def maybe_managed(http_request, action, *, payload=None, session_id=None, request_id=None, limit=100):
    if not setting("FINANCE_CORE_URL"):
        return None
    if http_request is None:
        fail(401, "AUTH_REQUIRED")
    from api.v1.endpoints.finance import browser_identity
    from api.v1.endpoints.finance_assistant import core_session, core_call
    write = action in {"send", "stream", "cancel", "compact", "delete"}
    owner = browser_identity(http_request, write)
    if session_id and not require_local_access(http_request, session_id, write=write):
        return JSONResponse(empty_state(session_id) if action == "messages" else empty_context(session_id))
    bot_id, token = await asyncio.to_thread(core_session, http_request, write=write)

    async def core(method, path, *, data=None, params=None, missing_ok=False):
        status, result = await asyncio.to_thread(core_call, method, path, token, data=data, params=params)
        if missing_ok and status == 404:
            return None
        if not 200 <= status < 300:
            fail(status, "FINANCE_LEGACY_CORE_REJECTED")
        return result

    async def alias(value):
        return await core("GET", "finance/alias", params={"sessionId": value}, missing_ok=True)

    if action == "sessions":
        result = await core("GET", "finance/aliases", params={"limit": limit})
        prefix = http_request.query_params.get("user_id")
        if prefix and isinstance(result, dict) and isinstance(result.get("sessions"), list):
            result = {**result, "sessions": [item for item in result["sessions"] if str(item.get("session_id", "")).startswith(prefix)]}
        return JSONResponse(result)
    if action == "messages":
        binding = await alias(session_id)
        if not binding:
            return JSONResponse(empty_state(session_id))
        result = await core("GET", "agents/messages", params={"bot": bot_id, "conversationId": binding["conversationId"]})
        raw_messages = result.get("messages", [])
        while len(raw_messages) < limit and result.get("hasMore"):
            before = result.get("nextBefore") or (raw_messages[0].get("seq") if raw_messages else None)
            if before is None:
                break
            result = await core("GET", "agents/messages", params={"bot": bot_id, "conversationId": binding["conversationId"], "before": before})
            older = result.get("messages", [])
            if not older or older[0].get("id") == raw_messages[0].get("id"):
                break
            raw_messages = older + raw_messages
        messages = [{"role": item["role"], "content": item["content"], "id": item["id"],
                     "created_at": item.get("created_at")} for item in raw_messages
                    if item.get("role") in {"user", "assistant"}]
        return JSONResponse({**empty_state(session_id), "messages": messages[-limit:] if limit > 0 else [],
                             "session_generation": binding["conversationId"]})
    if action == "context":
        result = await core("GET", "finance/alias/context", params={"sessionId": session_id}, missing_ok=True)
        return JSONResponse(result if result is not None else empty_context(session_id))
    if action in {"compact", "delete"}:
        result = await core("POST", "finance/alias/" + action, data={"sessionId": session_id}, missing_ok=action == "delete")
        return JSONResponse(result if result is not None else {"deleted": 0})
    if action == "cancel":
        receipt = request_ledger(owner, request_id)
        if not receipt:
            fail(404, "FINANCE_REQUEST_NOT_FOUND")
        binding = await alias(receipt["session_id"])
        if not binding:
            fail(404, "FINANCE_SESSION_NOT_FOUND")
        task_id = receipt["task_id"]
        if not task_id:
            history = await core("GET", "agents/messages", params={"bot": bot_id, "conversationId": binding["conversationId"]})
            message = next((m for m in history.get("messages", []) if m.get("id") == receipt["message_id"]), None)
            task_id = message.get("task_id") if message else None
        if not task_id and not receipt["message_id"]:
            fail(409, "FINANCE_RECEIPT_OUTCOME_UNKNOWN")
        target = {"messageId": receipt["message_id"]} if receipt["message_id"] else {"taskId": task_id}
        await core("POST", "agents/cancel", data={"botId": bot_id, "conversationId": binding["conversationId"], **target})
        return JSONResponse({"accepted": True, "request_id": request_id})
    if action not in {"send", "stream"}:
        fail(404, "FINANCE_LEGACY_ACTION_NOT_FOUND")

    request_id = payload.request_id or str(uuid.uuid4())
    previous = request_ledger(owner, request_id)
    session_id = payload.session_id or (previous["session_id"] if previous else str(uuid.uuid4()))
    require_local_access(http_request, session_id, write=True)
    request_facts = {**payload.model_dump(), "session_id": session_id, "request_id": request_id}
    request_hash = hashlib.sha256(json.dumps(request_facts, sort_keys=True, default=str).encode()).hexdigest()
    request_ledger(owner, request_id, session_id=session_id, request_hash=request_hash)
    model_selection = None
    if not (previous and previous["message_id"]) and ((payload.model and payload.model.strip()) or payload.reasoning_effort):
        catalog = await core("GET", "finance/models")
        selection = catalog.get("selection") or {}
        requested_model = (payload.model or "").strip()
        connections = [connection for connection in catalog.get("connections", [])
                       if connection.get("enabled") and connection.get("configured")]
        if requested_model:
            candidates = [connection for connection in connections if requested_model in {
                connection.get("defaultModel"), *(connection.get("models") or []), *(connection.get("options") or {}).keys()}]
        else:
            candidates = [connection for connection in connections if connection.get("id") == selection.get("selected_connection")]
        selected = [connection for connection in candidates if connection.get("id") == selection.get("selected_connection")]
        if len(selected) == 1:
            candidates = selected
        if len(candidates) != 1:
            fail(422, "FINANCE_MODEL_SELECTION_REQUIRED")
        connection = candidates[0]
        model = requested_model or selection.get("selected_model") or connection.get("defaultModel")
        if not model:
            fail(422, "FINANCE_MODEL_SELECTION_REQUIRED")
        reasoning = "off" if payload.reasoning_effort == "none" else payload.reasoning_effort
        model_selection = {"connectionId": connection["id"], "model": model}
        if reasoning:
            model_selection["reasoning"] = reasoning
    context = payload.context if isinstance(payload.context, dict) else {}
    source = {k: context[k] for k in ("kind", "id", "code", "name", "tradeDate", "asOf") if isinstance(context.get(k), str)}
    for old, new in (("stock_code", "code"), ("stock_name", "name"), ("report_id", "id"), ("report_date", "tradeDate")):
        if new not in source and isinstance(context.get(old), str):
            source[new] = context[old]
    if previous and previous["message_id"] and previous.get("request_hash"):
        binding = await alias(session_id)
        if not binding:
            fail(404, "FINANCE_SESSION_NOT_FOUND")
        receipt = {"conversationId": binding["conversationId"], "messageId": previous["message_id"], "taskId": previous["task_id"]}
    else:
        submission = {"sessionId": session_id, "clientId": request_id, "text": payload.message, "sourceContext": source}
        if model_selection is not None:
            submission["modelSelection"] = model_selection
        receipt = await core("POST", "finance/alias", data=submission)
    conversation_id, task_id = receipt["conversationId"], receipt.get("taskId")
    request_ledger(owner, request_id, session_id=session_id, message_id=receipt.get("messageId"), task_id=task_id)

    async def events():
        nonlocal task_id
        yield {"type": "accepted", "request_id": request_id, "session_id": session_id,
               "conversation_id": conversation_id, "task_id": task_id,
               "session_generation": conversation_id, "session_state_version": 0,
               "active_stock_context": None, "session_state": {"selected_skill_ids": payload.skills}}
        latest_stream = None
        while True:
            if await http_request.is_disconnected():
                return  # Core keeps the accepted task; reconnect/explicit stop owns cancellation.
            history = await core("GET", "agents/messages", params={"bot": bot_id, "conversationId": conversation_id})
            messages = history.get("messages", [])
            if not task_id:
                accepted = next((m for m in messages if m.get("id") == receipt.get("messageId")), None)
                task_id = accepted.get("task_id") if accepted else None
                if task_id:
                    request_ledger(owner, request_id, session_id=session_id, task_id=task_id)
            tasks = await core("GET", "agents/tasks", params={"bot": bot_id, "conversationId": conversation_id})
            task = next((t for t in tasks.get("tasks", []) if t.get("id") == task_id), None)
            stream = next((s for s in history.get("streaming", []) if s.get("taskId") == task_id), None)
            if stream and stream.get("text") != latest_stream:
                latest_stream = stream.get("text")
                yield {"type": "generating", "content": latest_stream}
            if task and task.get("status") in {"succeeded", "failed", "cancelled"}:
                answers = [m for m in messages if m.get("role") == "assistant" and m.get("task_id") == task_id]
                content = "\n\n".join(m["content"] for m in answers) or task.get("result_summary") or ""
                yield {"type": "done", "success": task["status"] == "succeeded", "content": content,
                       "error": None if task["status"] == "succeeded" else task["status"],
                       "session_id": session_id, "request_id": request_id,
                       "session_generation": conversation_id, "session_state_version": 0,
                       "active_stock_context": None, "session_state": {"selected_skill_ids": payload.skills}}
                return
            await asyncio.sleep(0.5)

    if action == "stream":
        async def encoded():
            async for event in events():
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        return StreamingResponse(encoded(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
    async for event in events():
        if event["type"] == "done":
            return JSONResponse({key: value for key, value in event.items() if key != "type"})
    fail(499, "FINANCE_CLIENT_DISCONNECTED")
