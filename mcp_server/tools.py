"""MCP 管家工具集与 ASGI 应用。

遵循 Nex 网关 mcp-rules v1（R1-R10）：
- R1/R4：mcp_list 文档化全部接口（参数、示例、输出说明、输出示例）
- R2/R3：工具 snake_case、description 与 readOnlyHint/destructiveHint 完整
- R5：返回类型注解驱动 outputSchema 生成
- R6/R7：错误以 {"error": "..."} 返回，不携带密钥与堆栈
- R10：所有非 /health 请求强制 Bearer 认证
"""

import json
from typing import Annotated, Any, Dict, List, Optional

from pydantic import Field

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import api_client, config, service

MCP_LIST_VERSION = "1.2.0"

mcp = FastMCP(name="nex-stock")
from .finance import install as install_finance_tools
install_finance_tools(mcp)


def _tool_error(message: str) -> ToolError:
    """R6：错误以 JSON {"error": ...} 形式返回，isError=true。"""
    return ToolError(json.dumps({"error": message}, ensure_ascii=False))


def _summary_of(result: Dict[str, Any]) -> Dict[str, Any]:
    """从 AnalysisResultResponse 提取决策摘要字段（防御式，字段缺失返回 None）。"""
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    keys = (
        "stock_name",
        "trend_prediction",
        "sentiment_score",
        "operation_advice",
        "decision_type",
        "action",
        "confidence_level",
    )
    return {k: summary.get(k) for k in keys}


_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


# --------------------------------------------------------------------------
# mcp_list（R1：网关强制要求的只读自描述接口）
# --------------------------------------------------------------------------

def _mcp_list_document() -> Dict[str, Any]:
    return {
        "version": MCP_LIST_VERSION,
        "rulesVersion": "v1",
        "interfaces": [
            {
                "name": "financial_discover", "summary": "Discover finance.v1 delegated business tools",
                "description": "Requires paired finance service and delegated-user HTTP headers; never uses manager identity.",
                "readOnly": True, "params": [], "inputExample": {},
                "output": {"fields": [{"name": "version", "type": "string", "description": "Finance contract version"},
                                      {"name": "tools", "type": "array", "description": "Permitted finance tool schemas"}],
                           "example": {"version": "finance.v1", "tools": []},
                           "description": "Business schemas and OpenAPI filtered by audience; credentials are transport-only."},
            },
            {
                "name": "financial_call", "summary": "Call a discovered finance business tool",
                "description": "Executes with the verified user's current authority; legacy manager tools are unavailable to this identity.",
                "readOnly": False,
                "params": [{"name": "name", "type": "string", "required": True, "description": "Discovered tool name"},
                           {"name": "arguments", "type": "object", "required": True, "description": "Tool arguments without identity/credentials"},
                           {"name": "operation_id", "type": "string", "required": False, "description": "Stable idempotency key required for writes"}],
                "inputExample": {"name": "read_api_doc", "arguments": {}},
                "output": {"fields": [{"name": "version", "type": "string", "description": "Finance contract version"}],
                           "example": {"version": "finance.v1", "tools": []},
                           "description": "Exact selected tool result; HTTP identity and authorization failures remain errors."},
            },
            {
                "name": "mcp_list",
                "summary": "列出本服务全部 MCP 接口的自描述文档",
                "description": "返回本服务所有工具的参数、示例与输出契约；新增或修改接口时版本号递增。",
                "readOnly": True,
                "params": [
                    {"name": "tool", "type": "string", "required": False, "description": "只返回指定名称的接口文档；留空返回全部"}
                ],
                "inputExample": {"tool": "analysis_run"},
                "output": {
                    "fields": [
                        {"name": "version", "type": "string", "description": "接口文档版本号"},
                        {"name": "rulesVersion", "type": "string", "description": "遵循的 mcp-rules 版本"},
                        {"name": "interfaces", "type": "array", "description": "接口描述数组"},
                    ],
                    "example": {"version": MCP_LIST_VERSION, "rulesVersion": "v1", "interfaces": []},
                    "description": "interfaces 数组每项含 name/summary/description/readOnly/params/inputExample/output",
                },
            },
            {
                "name": "service_status",
                "summary": "查看分析服务健康状态与进程信息",
                "description": "检查股票分析服务（FastAPI）是否健康，附管家托管的 PID、自启配置等；只读。",
                "readOnly": True,
                "params": [],
                "inputExample": {},
                "output": {
                    "fields": [
                        {"name": "healthy", "type": "boolean", "description": "服务健康检查是否通过"},
                        {"name": "base_url", "type": "string", "description": "分析服务地址"},
                        {"name": "managed_pid", "type": "number", "description": "管家托管的进程 PID，非托管为 null"},
                        {"name": "autostart_enabled", "type": "boolean", "description": "管家启动时是否自动拉起服务"},
                        {"name": "start_on_demand_enabled", "type": "boolean", "description": "分析类工具是否允许按需拉起服务"},
                    ],
                    "example": {"healthy": True, "base_url": "http://127.0.0.1:8000", "managed_pid": 12345, "autostart_enabled": True, "start_on_demand_enabled": True},
                    "description": "healthy=false 表示服务未运行，可用 service_start 拉起",
                },
            },
            {
                "name": "service_start",
                "summary": "启动股票分析服务",
                "description": "以 `python main.py --serve-only` 拉起分析服务并等待健康检查通过（默认最长 120 秒）；已健康时幂等返回。",
                "readOnly": False,
                "params": [],
                "inputExample": {},
                "output": {
                    "fields": [
                        {"name": "started", "type": "boolean", "description": "本次是否执行了启动动作"},
                        {"name": "already_healthy", "type": "boolean", "description": "调用前服务是否已在运行"},
                        {"name": "healthy", "type": "boolean", "description": "调用后服务是否健康"},
                        {"name": "pid", "type": "number", "description": "新启动的进程 PID"},
                    ],
                    "example": {"started": True, "already_healthy": False, "healthy": True, "pid": 12345},
                    "description": "启动失败时返回 isError 与 error 字段",
                },
            },
            {
                "name": "service_stop",
                "summary": "停止股票分析服务",
                "description": "终止分析服务进程树（含其子进程）；管家自身不受影响，之后可用 service_start 再次拉起。幂等。",
                "readOnly": False,
                "destructive": True,
                "params": [],
                "inputExample": {},
                "output": {
                    "fields": [
                        {"name": "stopped", "type": "boolean", "description": "服务是否已停止"},
                        {"name": "already_stopped", "type": "boolean", "description": "调用前服务是否已停止"},
                        {"name": "pid", "type": "number", "description": "被停止的进程 PID"},
                    ],
                    "example": {"stopped": True, "already_stopped": False, "pid": 12345},
                    "description": "停止失败（still_healthy=true）时通过 error 字段说明",
                },
            },
            {
                "name": "analysis_run",
                "summary": "触发个股 AI 分析并返回分析报告",
                "description": "提交异步分析任务并轮询至完成（默认最长等待 100 秒，受网关单次调用超时限制），返回决策摘要与 Markdown 报告；未完成的任务返回 task_id，用 analysis_status 续查。每次调用消耗 LLM 额度（约 1-2 分钟/只）。分析服务未运行时会自动拉起。",
                "readOnly": False,
                "params": [
                    {"name": "stock_codes", "type": "array", "required": True, "description": "股票代码列表（如 [\"600519\",\"hk00700\",\"AAPL\"]），最多 10 只"},
                    {"name": "report_type", "type": "string", "required": False, "description": "报告类型：detailed（默认）/ simple / full / brief"},
                    {"name": "wait_seconds", "type": "number", "required": False, "description": "等待分析完成的最长秒数，默认 100（受网关 120 秒调用超时限制）；超时后返回 task_id 供 analysis_status 续查"},
                    {"name": "analysis_phase", "type": "string", "required": False, "description": "分析阶段：auto（默认）/ premarket / intraday / postmarket"},
                    {"name": "notify", "type": "boolean", "required": False, "description": "是否同时触发推送通知（需已配置通知渠道），默认 false"},
                ],
                "inputExample": {"stock_codes": ["600519"], "report_type": "detailed", "wait_seconds": 600},
                "output": {
                    "fields": [
                        {"name": "results", "type": "array", "description": "每只股票的结果：status/summary/report_markdown 或 task_id+status"},
                        {"name": "duplicates", "type": "array", "description": "因重复任务被拒绝的股票及原因"},
                        {"name": "task_ids", "type": "array", "description": "全部任务 ID（含去重复用的）"},
                    ],
                    "example": {"results": [{"stock_code": "600519", "status": "completed", "summary": {"trend_prediction": "看多", "sentiment_score": 70}, "report_markdown": "# 分析报告..."}], "duplicates": [], "task_ids": ["task_x"]},
                    "description": "status 可能值：completed / failed / pending / processing / timeout / unknown",
                },
            },
            {
                "name": "analysis_status",
                "summary": "按任务 ID 查询分析进度与结果",
                "description": "查询异步分析任务的当前状态；completed 时附带决策摘要，可配合 analysis_run 超时后续查。只读。",
                "readOnly": True,
                "params": [
                    {"name": "task_id", "type": "string", "required": True, "description": "analysis_run 返回的任务 ID"}
                ],
                "inputExample": {"task_id": "task_x"},
                "output": {
                    "fields": [
                        {"name": "task_id", "type": "string", "description": "任务 ID"},
                        {"name": "status", "type": "string", "description": "pending / processing / completed / failed / cancelled / timeout / unknown"},
                        {"name": "progress", "type": "number", "description": "进度百分比 0-100"},
                        {"name": "summary", "type": "object", "description": "completed 时的决策摘要"},
                        {"name": "error", "type": "string", "description": "failed 时的原因"},
                    ],
                    "example": {"task_id": "task_x", "status": "processing", "progress": 40, "summary": None, "error": None},
                    "description": "status=completed 时 summary 非空",
                },
            },
            {
                "name": "market_review_run",
                "summary": "触发大盘复盘分析并返回报告",
                "description": "提交大盘复盘任务并轮询至完成（默认最长等待 100 秒），返回 Markdown 复盘报告；未完成时返回 task_id 供 analysis_status 续查。消耗 LLM 额度（约 1 分钟）。分析服务未运行时会自动拉起。",
                "readOnly": False,
                "params": [
                    {"name": "region", "type": "string", "required": False, "description": "市场：cn（默认）/ hk / us / jp / kr，可逗号组合"},
                    {"name": "wait_seconds", "type": "number", "required": False, "description": "等待完成的最长秒数，默认 100（受网关 120 秒调用超时限制）"},
                    {"name": "notify", "type": "boolean", "required": False, "description": "是否同时触发推送通知，默认 false"},
                ],
                "inputExample": {"region": "cn", "wait_seconds": 600},
                "output": {
                    "fields": [
                        {"name": "task_id", "type": "string", "description": "任务 ID"},
                        {"name": "status", "type": "string", "description": "completed / failed / timeout / 其他中间态"},
                        {"name": "region", "type": "string", "description": "实际执行的地区"},
                        {"name": "report_markdown", "type": "string", "description": "completed 时的复盘报告 Markdown"},
                    ],
                    "example": {"task_id": "task_y", "status": "completed", "region": "cn", "report_markdown": "# 大盘复盘..."},
                    "description": "同市场重复提交会被服务端拒绝（409），error 字段说明",
                },
            },
            {
                "name": "reports_list",
                "summary": "列出历史分析报告",
                "description": "分页查询历史分析记录（含大盘复盘），返回决策结论与元信息。只读。",
                "readOnly": True,
                "params": [
                    {"name": "stock_code", "type": "string", "required": False, "description": "按股票代码过滤（如 600519；大盘复盘固定为 MARKET）"},
                    {"name": "limit", "type": "number", "required": False, "description": "返回条数，默认 20，最大 100"},
                ],
                "inputExample": {"stock_code": "600519", "limit": 10},
                "output": {
                    "fields": [
                        {"name": "reports", "type": "array", "description": "记录数组：query_id/stock_code/stock_name/report_type/trend_prediction/sentiment_score/operation_advice/created_at 等"},
                    ],
                    "example": {"reports": [{"query_id": "q1", "stock_code": "600519", "stock_name": "贵州茅台", "report_type": "detailed", "trend_prediction": "看多", "sentiment_score": 70, "operation_advice": "买入", "created_at": "2026-10-25T17:20:23"}]},
                    "description": "query_id 可用于 report_get 读取全文",
                },
            },
            {
                "name": "report_get",
                "summary": "读取单份历史报告全文",
                "description": "按 query_id 或记录 ID 读取报告；format=markdown 返回推送同款 Markdown 全文，format=meta 返回决策摘要。只读。",
                "readOnly": True,
                "params": [
                    {"name": "record_id", "type": "string", "required": True, "description": "reports_list 返回的 query_id 或数字记录 ID"},
                    {"name": "format", "type": "string", "required": False, "description": "markdown（默认）或 meta"},
                ],
                "inputExample": {"record_id": "q1", "format": "markdown"},
                "output": {
                    "fields": [
                        {"name": "record_id", "type": "string", "description": "记录 ID"},
                        {"name": "format", "type": "string", "description": "实际返回格式"},
                        {"name": "content_markdown", "type": "string", "description": "format=markdown 时的报告全文"},
                        {"name": "summary", "type": "object", "description": "format=meta 时的决策摘要"},
                    ],
                    "example": {"record_id": "q1", "format": "markdown", "content_markdown": "# 分析报告...", "summary": None},
                    "description": "记录不存在时返回 isError",
                },
            },
        ],
        "resources": [],
        "prompts": [],
    }


@mcp.tool(annotations=_READ)
def mcp_list(
    tool: Annotated[str, Field(description="只返回指定名称的接口文档；留空返回全部")] = "",
) -> Dict[str, Any]:
    """列出本服务全部 MCP 接口的自描述文档；传 tool 只看单个接口。

    Args:
        tool: 只返回指定名称的接口文档；留空返回全部
    """
    doc = _mcp_list_document()
    if tool:
        matched = [i for i in doc["interfaces"] if i["name"] == tool.strip()]
        if not matched:
            raise _tool_error(f"unknown tool: {tool}")
        doc["interfaces"] = matched
    return doc


# --------------------------------------------------------------------------
# 服务管理
# --------------------------------------------------------------------------

@mcp.tool(annotations=_READ)
def service_status() -> Dict[str, Any]:
    """查看分析服务健康状态与进程信息（只读）。"""
    return service.get_service_status()


@mcp.tool(annotations=_WRITE)
def service_start() -> Dict[str, Any]:
    """启动分析服务并等待健康检查通过；已健康时幂等返回。"""
    result = service.start_service()
    if result.get("error"):
        raise _tool_error(result["error"])
    return result


@mcp.tool(annotations=_DESTRUCTIVE)
def service_stop() -> Dict[str, Any]:
    """停止分析服务进程树（幂等）；管家自身不受影响。"""
    return service.stop_service()


# --------------------------------------------------------------------------
# 分析与报告
# --------------------------------------------------------------------------

@mcp.tool(annotations=_WRITE)
def analysis_run(
    stock_codes: Annotated[List[str], Field(description="股票代码列表（如 [\"600519\",\"hk00700\",\"AAPL\"]），最多 10 只")],
    report_type: Annotated[str, Field(description="报告类型：detailed（默认）/ simple / full / brief")] = "detailed",
    wait_seconds: Annotated[int, Field(description="等待分析完成的最长秒数，默认 100（受网关 120 秒调用超时限制）；超时后返回 task_id 供 analysis_status 续查")] = config.ANALYSIS_WAIT_DEFAULT_SECONDS,
    analysis_phase: Annotated[str, Field(description="分析阶段：auto（默认）/ premarket / intraday / postmarket")] = "auto",
    notify: Annotated[bool, Field(description="是否同时触发推送通知（需已配置通知渠道），默认 false")] = False,
) -> Dict[str, Any]:
    """触发个股 AI 分析并轮询至完成（默认最长等待 100 秒，受网关单次调用超时限制），返回决策摘要与 Markdown 报告；未完成的任务返回 task_id，用 analysis_status 续查。

    Args:
        stock_codes: 股票代码列表（如 ["600519","hk00700","AAPL"]），最多 10 只
        report_type: 报告类型：detailed（默认）/ simple / full / brief
        wait_seconds: 等待分析完成的最长秒数，默认 100（受网关 120 秒调用超时限制）；超时后返回 task_id 供 analysis_status 续查
        analysis_phase: 分析阶段：auto（默认）/ premarket / intraday / postmarket
        notify: 是否同时触发推送通知（需已配置通知渠道），默认 false
    """
    codes = [str(c).strip() for c in (stock_codes or []) if str(c).strip()]
    if not codes:
        raise _tool_error("stock_codes is required")
    if len(codes) > config.ANALYSIS_MAX_STOCKS:
        raise _tool_error(f"too many stock_codes (max {config.ANALYSIS_MAX_STOCKS})")
    ensured = service.ensure_service()
    if not ensured.get("ready"):
        raise _tool_error(ensured.get("error") or "analysis service unavailable")
    try:
        submit = api_client.submit_analysis(
            codes, report_type=report_type, analysis_phase=analysis_phase, notify=notify,
        )
        statuses = api_client.wait_for_tasks(
            submit["task_ids"], wait_seconds=wait_seconds,
        )
    except api_client.ServiceApiError as exc:
        raise _tool_error(str(exc)) from exc

    results: List[Dict[str, Any]] = []
    code_by_task = dict.fromkeys(submit["task_ids"])
    for task_id, status in statuses.items():
        state = str(status.get("status") or "unknown")
        item: Dict[str, Any] = {"task_id": task_id, "status": state}
        if state == "completed":
            result = status.get("result") or {}
            item["stock_code"] = result.get("stock_code")
            summary = _summary_of(result)
            if not summary.get("stock_name"):
                summary["stock_name"] = result.get("stock_name")
            item["summary"] = summary
            item["report_markdown"] = api_client.get_report_markdown(str(result.get("query_id"))) or ""
        elif status.get("error"):
            item["error"] = str(status["error"])
        results.append(item)
        code_by_task[task_id] = item.get("stock_code")
    return {"results": results, "duplicates": submit["duplicates"], "task_ids": submit["task_ids"]}


@mcp.tool(annotations=_READ)
def analysis_status(
    task_id: Annotated[str, Field(description="analysis_run 返回的任务 ID")],
) -> Dict[str, Any]:
    """按任务 ID 查询分析进度；completed 时附决策摘要。

    Args:
        task_id: analysis_run 返回的任务 ID
    """
    try:
        status = api_client.get_task_status(task_id.strip())
    except api_client.ServiceApiError as exc:
        raise _tool_error(str(exc)) from exc
    result = status.get("result") or {}
    return {
        "task_id": status.get("task_id") or task_id,
        "status": status.get("status"),
        "progress": status.get("progress"),
        "stock_name": status.get("stock_name"),
        "summary": _summary_of(result) if result else None,
        "error": status.get("error"),
    }


@mcp.tool(annotations=_WRITE)
def market_review_run(
    region: Annotated[str, Field(description="市场：cn（默认）/ hk / us / jp / kr，可逗号组合")] = "cn",
    wait_seconds: Annotated[int, Field(description="等待完成的最长秒数，默认 100（受网关 120 秒调用超时限制）；未完成时返回 task_id 供 analysis_status 续查")] = config.ANALYSIS_WAIT_DEFAULT_SECONDS,
    notify: Annotated[bool, Field(description="是否同时触发推送通知，默认 false")] = False,
) -> Dict[str, Any]:
    """触发大盘复盘分析并轮询至完成，返回 Markdown 复盘报告。

    Args:
        region: 市场：cn（默认）/ hk / us / jp / kr，可逗号组合
        wait_seconds: 等待完成的最长秒数，默认 100（受网关 120 秒调用超时限制）；未完成时返回 task_id 供 analysis_status 续查
        notify: 是否同时触发推送通知，默认 false
    """
    ensured = service.ensure_service()
    if not ensured.get("ready"):
        raise _tool_error(ensured.get("error") or "analysis service unavailable")
    try:
        submit = api_client.submit_market_review(region=region, notify=notify)
        statuses = api_client.wait_for_tasks([submit["task_id"]], wait_seconds=wait_seconds)
    except api_client.ServiceApiError as exc:
        raise _tool_error(str(exc)) from exc
    status = statuses[submit["task_id"]]
    state = str(status.get("status") or "unknown")
    return {
        "task_id": submit["task_id"],
        "status": state,
        "region": submit.get("region"),
        "report_markdown": (status.get("market_review_report") or "") if state == "completed" else "",
        "error": status.get("error"),
    }


@mcp.tool(annotations=_READ)
def reports_list(
    stock_code: Annotated[str, Field(description="按股票代码过滤（如 600519；大盘复盘固定为 MARKET），留空返回全部")] = "",
    limit: Annotated[int, Field(description="返回条数，默认 20，最大 100")] = 20,
) -> Dict[str, Any]:
    """列出历史分析报告（决策结论与元信息）。

    Args:
        stock_code: 按股票代码过滤（如 600519；大盘复盘固定为 MARKET），留空返回全部
        limit: 返回条数，默认 20，最大 100
    """
    ensured = service.ensure_service()
    if not ensured.get("ready"):
        raise _tool_error(ensured.get("error") or "analysis service unavailable")
    try:
        items = api_client.list_reports(stock_code=stock_code.strip() or None, limit=limit)
    except api_client.ServiceApiError as exc:
        raise _tool_error(str(exc)) from exc
    keys = (
        "query_id", "stock_code", "stock_name", "report_type", "region",
        "trend_prediction", "sentiment_score", "operation_advice", "action",
        "current_price", "change_pct", "model_used", "created_at",
    )
    return {"reports": [{k: item.get(k) for k in keys} for item in items]}


@mcp.tool(annotations=_READ)
def report_get(
    record_id: Annotated[str, Field(description="reports_list 返回的 query_id 或数字记录 ID")],
    format: Annotated[str, Field(description="markdown（默认）或 meta")] = "markdown",
) -> Dict[str, Any]:
    """按 query_id 或记录 ID 读取报告：format=markdown 全文 / meta 决策摘要。

    Args:
        record_id: reports_list 返回的 query_id 或数字记录 ID
        format: markdown（默认）或 meta
    """
    ensured = service.ensure_service()
    if not ensured.get("ready"):
        raise _tool_error(ensured.get("error") or "analysis service unavailable")
    fmt = (format or "markdown").strip().lower()
    rid = record_id.strip()
    if fmt not in ("markdown", "meta"):
        raise _tool_error("format must be 'markdown' or 'meta'")
    try:
        if fmt == "markdown":
            content = api_client.get_report_markdown(rid)
            if not content:
                raise _tool_error(f"report not found or empty: {rid}")
            return {"record_id": rid, "format": "markdown", "content_markdown": content, "summary": None}
        detail = api_client._request("GET", f"/api/v1/history/{rid}", timeout=30)
        report = detail.get("report") if isinstance(detail.get("report"), dict) else {}
        return {
            "record_id": rid,
            "format": "meta",
            "content_markdown": None,
            "summary": _summary_of({"report": report}) or {"raw_summary": report.get("summary")},
        }
    except api_client.ServiceApiError as exc:
        raise _tool_error(str(exc)) from exc


# --------------------------------------------------------------------------
# ASGI 应用（Streamable HTTP + Bearer 认证）
# --------------------------------------------------------------------------

def create_asgi_app():
    """返回带 Bearer 认证的 Streamable HTTP ASGI 应用（/mcp）与 /health 探针。"""
    if not config.MCP_BEARER_TOKEN:
        raise RuntimeError(
            "MCP_SERVER_BEARER_TOKEN 未配置：对外暴露的 HTTP MCP 服务必须启用 Bearer 认证（mcp-rules R10）"
        )
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True)
    token_header = f"Bearer {config.MCP_BEARER_TOKEN}".encode()
    from src.services.finance_identity import setting
    finance_token = setting("FINANCE_SERVICE_TOKEN")
    if finance_token and finance_token == config.MCP_BEARER_TOKEN:
        raise RuntimeError("Finance and legacy manager service credentials must be distinct")

    async def asgi_app(scope, receive, send):
        if scope["type"] != "http":
            await mcp_app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health":
            body = b'{"status":"ok","service":"nex-stock-mcp"}'
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if setting("FINANCE_SERVICE_TOKEN") == config.MCP_BEARER_TOKEN:
            body = b'{"error":"credential_scope_collision"}'
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        if headers.get("authorization") != token_header.decode("latin-1"):
            from fastapi import HTTPException
            from src.services.finance_identity import service_auth
            from .finance import finance_request
            rejection_status = 401
            try:
                service_auth(headers)
                finance_request("POST", "/delegations/verify", headers, {})
            except HTTPException as exc:
                rejection_status = exc.status_code
            except ToolError as exc:
                try:
                    rejection_status = json.loads(str(exc)).get("status_code", 503)
                except (TypeError, ValueError):
                    rejection_status = 503
            else:
                await mcp_app(scope, receive, send)
                return
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": rejection_status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await mcp_app(scope, receive, send)

    return asgi_app
