# -*- coding: utf-8 -*-
"""The assistant's access to the whole backend API, with progressive disclosure.

Tools:
- read_api_doc: the live OpenAPI document of ``/api/v1`` in three levels — the group directory,
  the endpoints of one group, the full contract of one endpoint.
- call_api: call a ``/api/v1`` endpoint in-process (``api/internal_client.py``), acting as the owner of the
  chat session and nobody else (both tools are ``user_scoped``: the registry hands them the session owner).
  Routes that cannot work in-process, credentials / process settings and (for non-administrators) every
  ``require_admin`` route are out of reach; ``read_api_doc`` does not show what the user cannot call.

The document is generated from the routes on every process start, so it cannot drift from the code.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.agent.tools.registry import ToolDefinition, ToolParameter, ToolPolicy

logger = logging.getLogger(__name__)

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_GROUP_SAMPLE = 5
_DOC_DESCRIPTION_CHARS = 1500
_DOC_MAX_CHARS = 16000
_SCHEMA_MAX_DEPTH = 4
_RESULT_MAX_CHARS = 30000

API_DOC_GUIDE = (
    "后端接口按当前用户的权限对你开放，和这个用户在网页上能做的事一样（系统设置、个人密钥与推送渠道、管理员接口除外）。接口文档按层查看：\n"
    "1. read_api_doc() 看分组目录；\n"
    "2. read_api_doc(group=\"DailyPlan\") 看一个分组里的接口；\n"
    "3. read_api_doc(endpoint=\"GET /api/v1/plan/account\") 看一个接口的参数、请求体和返回结构；\n"
    "然后用 call_api(method, path, query, body) 调用。"
)


# ------------------------------------------------------------------
# document
# ------------------------------------------------------------------

def _spec() -> Dict[str, Any]:
    from api.internal_client import get_openapi_spec

    return get_openapi_spec()


def _operations(spec: Dict[str, Any], user: Any) -> List[Tuple[str, str, Dict[str, Any]]]:
    """Operations ``user`` may see: credentials / process settings and (for non-administrators) admin routes are left out."""
    from api.internal_client import blocked_prefix_reason, hidden_from

    ops = []
    for path, items in spec.get("paths", {}).items():
        if blocked_prefix_reason(path):
            continue
        for method, op in items.items():
            if method.upper() in _METHODS and isinstance(op, dict) and not hidden_from(method, path, user):
                ops.append((method.upper(), path, op))
    return ops


def _group_of(op: Dict[str, Any]) -> str:
    tags = op.get("tags") or []
    return str(tags[0]) if tags else "Other"


def _excluded(method: str, path: str, user: Any) -> Optional[str]:
    from api.internal_client import template_reason

    return template_reason(method, path, user)


def _endpoint_line(method: str, path: str, op: Dict[str, Any], user: Any) -> str:
    line = f"{method} {path} — {op.get('summary') or op.get('operationId') or ''}".rstrip(" —")
    reason = _excluded(method, path, user)
    return f"{line}（不可调用：{reason}）" if reason else line


def _directory(spec: Dict[str, Any], user: Any) -> Dict[str, Any]:
    groups: Dict[str, List[str]] = {}
    for method, path, op in _operations(spec, user):
        groups.setdefault(_group_of(op), []).append(op.get("summary") or f"{method} {path}")
    return {
        "guide": API_DOC_GUIDE,
        "groups": [
            {"group": name, "endpoints": len(items), "examples": items[:_GROUP_SAMPLE]}
            for name, items in sorted(groups.items())
        ],
    }


def _group(spec: Dict[str, Any], group: str, user: Any) -> Dict[str, Any]:
    wanted = group.strip().casefold()
    lines = [
        _endpoint_line(method, path, op, user) for method, path, op in _operations(spec, user)
        if _group_of(op).casefold() == wanted or path.casefold().startswith(wanted)
        or path.casefold().startswith("/api/v1" + wanted)
    ]
    if not lines:
        names = sorted({_group_of(op) for _m, _p, op in _operations(spec, user)})
        return {"error": f"没有分组 {group}", "groups": names}
    return {"group": group, "endpoints": lines,
            "next": "用 read_api_doc(endpoint=\"METHOD /api/v1/...\") 看某个接口的参数和返回结构"}


def _resolve_ref(spec: Dict[str, Any], ref: str) -> Dict[str, Any]:
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return node if isinstance(node, dict) else {}


def _compact_schema(spec: Dict[str, Any], schema: Any, depth: int = 0, seen: Tuple[str, ...] = ()) -> Any:
    """Readable shape of a JSON schema: objects become {field: shape}, scalars a short type string."""
    if not isinstance(schema, dict):
        return "any"
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if ref in seen or depth >= _SCHEMA_MAX_DEPTH:
            return f"<{name}>"
        return _compact_schema(spec, _resolve_ref(spec, ref), depth, seen + (ref,))
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            parts = [_compact_schema(spec, item, depth, seen) for item in schema[key]
                     if not (isinstance(item, dict) and item.get("type") == "null")]
            if len(parts) == 1:
                return parts[0]
            return {key: parts}
    kind = schema.get("type")
    if kind == "array":
        return [_compact_schema(spec, schema.get("items"), depth + 1, seen)]
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if not props:
            extra = schema.get("additionalProperties")
            return {"<key>": _compact_schema(spec, extra, depth + 1, seen)} if isinstance(extra, dict) else "object"
        if depth >= _SCHEMA_MAX_DEPTH:
            return "object{" + ", ".join(list(props)[:12]) + "}"
        required = set(schema.get("required") or [])
        return {
            (name if name in required else f"{name}?"): _compact_schema(spec, prop, depth + 1, seen)
            for name, prop in props.items()
        }
    text = str(kind or "any")
    if schema.get("enum"):
        text += " (" + "|".join(str(item) for item in schema["enum"][:12]) + ")"
    if schema.get("format"):
        text += f" {schema['format']}"
    description = schema.get("description") or schema.get("title")
    if description and depth > 0:
        text += f" — {str(description)[:80]}"
    return text


def _parse_endpoint(endpoint: str) -> Tuple[Optional[str], str]:
    text = endpoint.strip()
    head, _, rest = text.partition(" ")
    if head.upper() in _METHODS and rest.strip():
        return head.upper(), rest.strip()
    return None, text


def _endpoint(spec: Dict[str, Any], endpoint: str, user: Any) -> Dict[str, Any]:
    from api.internal_client import CallRefused, match_any_method, safe_route_path

    method, path = _parse_endpoint(endpoint)
    path = _normalize_path(path).split("?", 1)[0]
    try:
        path = safe_route_path(path)
    except CallRefused as exc:
        return {"error": exc.message, "next": "先用 read_api_doc() 看分组目录"}
    wanted = {(m, t) for m, t in match_any_method(path) if method is None or m == method}
    candidates = [
        (m, p, op) for m, p, op in _operations(spec, user)
        if (p == path and (method is None or m == method)) or (m, p) in wanted
    ]
    # A literal route (e.g. /plan/runs/latest) wins over a parameterised one for the same request.
    if len({m for m, _p, _op in candidates}) == 1 and len(candidates) > 1:
        candidates = sorted(candidates, key=lambda item: item[1].count("{"))[:1]
    if not candidates:
        return {"error": f"没有接口 {endpoint}", "next": "先用 read_api_doc() 看分组目录"}
    if len(candidates) > 1:
        return {"error": "这个路径有多个方法，请写成 \"METHOD /api/v1/...\"",
                "endpoints": [_endpoint_line(m, p, op, user) for m, p, op in candidates]}
    method, path, op = candidates[0]
    detail: Dict[str, Any] = {"endpoint": f"{method} {path}", "summary": op.get("summary")}
    if op.get("description"):
        detail["description"] = str(op["description"]).strip()[:_DOC_DESCRIPTION_CHARS]
    reason = _excluded(method, path, user)
    if reason:
        detail["callable"] = False
        detail["reason"] = reason
    params = []
    for param in op.get("parameters") or []:
        if "$ref" in param:
            param = _resolve_ref(spec, param["$ref"])
        params.append({
            "name": param.get("name"), "in": param.get("in"), "required": bool(param.get("required")),
            "type": _compact_schema(spec, param.get("schema"), 1),
            **({"description": param["description"]} if param.get("description") else {}),
        })
    if params:
        detail["parameters"] = params
    body = op.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content") or {}
        content_type = next(iter(content), "application/json")
        detail["request_body"] = {
            "content_type": content_type, "required": bool(body.get("required")),
            "schema": _compact_schema(spec, (content.get(content_type) or {}).get("schema")),
        }
        if content_type != "application/json":
            detail["request_body"]["note"] = "需要上传文件，call_api 只能发送 JSON 请求体"
    responses = op.get("responses") or {}
    for code in ("200", "201", "202"):
        if code in responses:
            content = (responses[code] or {}).get("content") or {}
            schema = (content.get("application/json") or {}).get("schema")
            detail["response"] = {"status": int(code), "schema": _compact_schema(spec, schema)}
            break
    return detail


def _limit(payload: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return payload
    return {"truncated": True, "text": text[:max_chars]}


def _handle_read_api_doc(group: str = "", endpoint: str = "", user_id: Optional[str] = None) -> dict:
    """``user_id``: the session owner (registry-supplied); the document only shows what that user may call."""
    from api.internal_client import ActingUserRequired, acting_user

    try:
        user = acting_user(user_id)
    except ActingUserRequired as exc:
        return {"error": str(exc)}
    try:
        spec = _spec()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[read_api_doc] OpenAPI unavailable: %s", exc, exc_info=True)
        return {"error": f"接口文档生成失败：{exc}"}
    if endpoint and endpoint.strip():
        return _limit(_endpoint(spec, endpoint, user), _DOC_MAX_CHARS)
    if group and group.strip():
        return _limit(_group(spec, group, user), _DOC_MAX_CHARS)
    return _limit(_directory(spec, user), _DOC_MAX_CHARS)


# ------------------------------------------------------------------
# call
# ------------------------------------------------------------------

def _normalize_path(path: str) -> str:
    text = str(path or "").strip()
    if not text.startswith("/"):
        text = "/" + text
    if not text.startswith("/api/"):
        text = "/api/v1" + text
    return text


def _handle_call_api(
    method: str, path: str, query: Optional[dict] = None, body: Optional[dict] = None, user_id: Optional[str] = None,
) -> dict:
    """``user_id``: the session owner (registry-supplied, never model input). The call acts as exactly that user."""
    from urllib.parse import parse_qsl

    from api.internal_client import ActingUserRequired, CallRefused, acting_user, authorize, call

    verb = str(method or "").strip().upper()
    if verb not in _METHODS:
        return {"error": f"method 只能是 {'/'.join(_METHODS)}"}
    full = _normalize_path(path)
    route_path, _, raw_query = full.partition("?")
    if not route_path.startswith("/api/v1/"):
        return {"error": "只能调用 /api/v1 下的接口"}
    params: Dict[str, Any] = dict(parse_qsl(raw_query)) if raw_query else {}
    if query is not None and not isinstance(query, dict):
        return {"error": "query 必须是对象，例如 {\"limit\": 20}"}
    params.update({k: v for k, v in (query or {}).items() if v is not None})
    if body is not None and not isinstance(body, (dict, list)):
        return {"error": "body 必须是 JSON 对象"}

    try:
        route_path, template = authorize(verb, route_path, acting_user(user_id))
    except ActingUserRequired as exc:
        return {"error": str(exc)}
    except CallRefused as exc:
        if exc.code == "no_route":
            return {"error": f"没有接口 {verb} {route_path}", "next": "先用 read_api_doc() 查接口目录"}
        return {"error": exc.message}

    try:
        status, content_type, text = call(verb, route_path, user_id=user_id, params=params, body=body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[call_api] %s %s failed: %s", verb, route_path, exc, exc_info=True)
        return {"error": f"调用失败：{exc}"}
    logger.info("[call_api] %s %s -> %s", verb, route_path, status)
    result: Dict[str, Any] = {"endpoint": f"{verb} {template}", "status_code": status, "ok": 200 <= status < 300}
    truncated = len(text) > _RESULT_MAX_CHARS
    if "json" in content_type and not truncated:
        try:
            result["data"] = json.loads(text) if text else None
            return result
        except ValueError:
            pass
    result["text"] = text[:_RESULT_MAX_CHARS]
    if truncated:
        result["truncated"] = True
        result["note"] = f"返回内容超过 {_RESULT_MAX_CHARS} 字，已截断；可加分页或筛选参数后重试"
    return result


# ------------------------------------------------------------------
# definitions
# ------------------------------------------------------------------

read_api_doc_tool = ToolDefinition(
    name="read_api_doc",
    description=(
        "Read the backend API document (all /api/v1 endpoints: daily plan, holdings & account, watchlist, "
        "boards, screening, analysis & reports, history, alerts, portfolio, backtest, intelligence, notes, "
        "...) that the current user may call. Progressive: no arguments -> group directory; "
        "group='DailyPlan' -> that group's endpoints; endpoint='GET /api/v1/plan/account' -> parameters, "
        "request body and response shape. Read before call_api."
    ),
    parameters=[
        ToolParameter(name="group", type="string", description="Group name from the directory, or a path prefix like '/plan'",
                      required=False, default=""),
        ToolParameter(name="endpoint", type="string", description="'METHOD /api/v1/path', e.g. 'GET /api/v1/stocks/watchlist'",
                      required=False, default=""),
    ],
    handler=_handle_read_api_doc,
    category="data",
    policy=ToolPolicy.declared(read_only=True, side_effects=[], permissions=["api_doc:read"]),
    user_scoped=True,  # the document shows what the session owner may call
)

call_api_tool = ToolDefinition(
    name="call_api",
    description=(
        "Call a backend /api/v1 endpoint as the current user, with exactly that user's permissions (reads and "
        "writes, e.g. update holdings, add to watchlist, start a screening or analysis). System settings, "
        "personal secrets and push channels are not available, and administrator-only endpoints only "
        "to administrators. Check the endpoint with read_api_doc first. Path parameters go in the path "
        "(plain segments only: no '..', no %-encoding); query is an object of query parameters; "
        "body is the JSON request body. Before a write the user did not ask for, say what you will change."
    ),
    parameters=[
        ToolParameter(name="method", type="string", description="HTTP method", enum=list(_METHODS)),
        ToolParameter(name="path", type="string", description="Path such as '/api/v1/plan/account' or '/api/v1/plan/stocks/000938'"),
        ToolParameter(name="query", type="object", description="Query parameters", required=False),
        ToolParameter(name="body", type="object", description="JSON request body", required=False),
    ],
    handler=_handle_call_api,
    category="action",
    policy=ToolPolicy.declared(read_only=False, side_effects=["api_call"], permissions=["api:all"]),
    user_scoped=True,  # acts as the session owner; the registry supplies user_id, model-supplied values are discarded
)

ALL_API_TOOLS = [read_api_doc_tool, call_api_tool]
