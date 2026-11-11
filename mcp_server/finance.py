"""Delegated finance MCP transport. User credentials live in HTTP headers only."""
import hmac
import json
import urllib.error
import urllib.request

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from mcp.types import ToolAnnotations

from . import config

FINANCE_TOOLS = frozenset({"financial_discover", "financial_call"})


def request_headers():
    from fastmcp.server.dependencies import get_http_headers
    return get_http_headers(include={"authorization"})


def is_delegated(headers):
    value = headers.get("authorization", "")
    return bool(value) and not hmac.compare_digest(value.encode(), ("Bearer " + config.MCP_BEARER_TOKEN).encode())


def finance_request(method, path, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(config.SERVICE_BASE_URL + "/api/v1/finance" + path, data=data, method=method)
    for key in ("authorization", "x-finance-delegation", "x-finance-audience"):
        if headers.get(key):
            request.add_header(key, headers[key])
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # Do not echo remote URLs, headers, bodies or credential-bearing errors.
        code = "FINANCE_UPSTREAM_DENIED"
        try:
            denied = json.loads(exc.read(65536).decode())
            denied = denied.get("data", denied)
            denied = denied.get("detail", denied)
            candidate = denied.get("error")
            if candidate in {"intraday_identity_unsupported", "intraday_unavailable", "AUTH_REQUIRED", "ADMIN_REQUIRED",
                             "FINANCE_LOGIN_EXPIRED", "FINANCE_DELEGATION_INVALID", "FINANCE_OPERATION_CONFLICT",
                             "FINANCE_OPERATION_OUTCOME_UNKNOWN", "USER_CENTER_UNAVAILABLE", "not_found"}:
                code = candidate
        except (ValueError, TypeError, AttributeError):
            pass
        raise ToolError(json.dumps({"error": code, "status_code": exc.code})) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise ToolError('{"error":"FINANCE_UPSTREAM_UNAVAILABLE"}') from None


class FinanceToolBoundary(Middleware):
    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        if is_delegated(request_headers()):
            return [tool for tool in tools if tool.name in FINANCE_TOOLS]
        return tools

    async def on_call_tool(self, context, call_next):
        headers = request_headers()
        delegated = is_delegated(headers)
        name = context.message.name
        if delegated and name not in FINANCE_TOOLS:
            raise ToolError('{"error":"FINANCE_MANAGER_TOOL_FORBIDDEN"}')
        if name in FINANCE_TOOLS and not delegated:
            raise ToolError('{"error":"FINANCE_DELEGATION_REQUIRED"}')
        return await call_next(context)


def install(mcp):
    mcp.add_middleware(FinanceToolBoundary())

    @mcp.tool(name="financial_discover", annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
              description="Discover versioned finance business tools and schemas for the verified delegated user and audience. Credentials come from transport headers only.")
    def financial_discover() -> dict:
        return finance_request("GET", "/tools", request_headers())

    @mcp.tool(name="financial_call", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
              description="Call a discovered finance tool as the verified delegated user. Writes need a stable operation_id; credentials and acting identity are never tool arguments.")
    def financial_call(name: str, arguments: dict, operation_id: str | None = None) -> dict:
        return finance_request("POST", "/tools/call", request_headers(),
                               {"name": name, "arguments": arguments, "operationId": operation_id})
