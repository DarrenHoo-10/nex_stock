"""分析服务 HTTP 客户端：封装 /api/v1 下的分析触发、状态轮询与报告读取。

只依赖标准库，避免管家进程引入重型依赖。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from . import config


class ServiceApiError(Exception):
    """分析服务调用失败（非 2xx 或网络错误）。"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _request(
    method: str,
    path: str,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    url = f"{config.SERVICE_BASE_URL}{path}"
    data = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ServiceApiError(f"HTTP {exc.code} on {path}: {detail}", status_code=exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ServiceApiError(f"request failed on {path}: {exc}") from exc
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise ServiceApiError(f"non-JSON response on {path}") from exc
    if not isinstance(parsed, dict):
        return {"data": parsed}
    return parsed


def submit_analysis(
    stock_codes: List[str],
    report_type: str = "detailed",
    analysis_phase: str = "auto",
    notify: bool = False,
) -> Dict[str, Any]:
    """提交异步分析任务，归一化返回 task_ids / duplicates。

    兼容单只（TaskAccepted / 409 DuplicateTaskErrorResponse）与批量
    （BatchTaskAcceptedResponse）两种响应。
    """
    body: Dict[str, Any] = {
        "stock_codes": stock_codes,
        "report_type": report_type,
        "async_mode": True,
        "analysis_phase": analysis_phase,
        "notify": notify,
        "selection_source": "manual",
    }
    resp = _request("POST", "/api/v1/analysis/analyze", json_body=body, timeout=30)
    task_ids: List[str] = []
    duplicates: List[Dict[str, Any]] = []
    if resp.get("task_id"):
        task_ids.append(str(resp["task_id"]))
    for accepted in resp.get("accepted") or []:
        if accepted.get("task_id"):
            task_ids.append(str(accepted["task_id"]))
    duplicate = resp.get("existing_task_id")
    if duplicate:
        duplicates.append({"stock_code": resp.get("stock_code"), "task_id": str(duplicate)})
        task_ids.append(str(duplicate))
    for dup in resp.get("duplicates") or []:
        duplicates.append({"stock_code": dup.get("stock_code"), "task_id": str(dup.get("existing_task_id") or "")})
        if dup.get("existing_task_id"):
            task_ids.append(str(dup["existing_task_id"]))
    for rejected in resp.get("rejected") or []:
        duplicates.append({"stock_code": rejected.get("stock_code"), "reason": rejected.get("reason")})
    if not task_ids and not duplicates:
        raise ServiceApiError(f"unexpected analyze response: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return {"task_ids": task_ids, "duplicates": duplicates, "raw_message": resp.get("message")}


def submit_market_review(region: str = "cn", notify: bool = False) -> Dict[str, Any]:
    """提交大盘复盘后台任务，返回 task_id（409 冲突时抛错说明已有任务在跑）。"""
    resp = _request(
        "POST",
        "/api/v1/analysis/market-review",
        json_body={"send_notification": notify, "region": region},
        timeout=30,
    )
    task_id = resp.get("task_id")
    if not task_id:
        raise ServiceApiError(f"market review not submitted: {json.dumps(resp, ensure_ascii=False)[:200]}")
    return {"task_id": str(task_id), "region": resp.get("region") or region}


def get_task_status(task_id: str) -> Dict[str, Any]:
    """查询任务状态；队列查不到时服务端会回退查历史库。"""
    return _request("GET", f"/api/v1/analysis/status/{task_id}", timeout=30)


def wait_for_tasks(
    task_ids: List[str],
    wait_seconds: int,
    poll_interval: Optional[float] = None,
) -> Dict[str, Dict[str, Any]]:
    """轮询任务直至 completed / failed / 超时。

    返回 {task_id: 状态响应}；超时的任务保留最后一次状态。
    """
    interval = poll_interval or max(2.0, float(config.ANALYSIS_POLL_INTERVAL_SECONDS))
    pending = {tid: None for tid in task_ids}
    results: Dict[str, Dict[str, Any]] = {}
    deadline = time.time() + max(30, int(wait_seconds))
    while True:
        for tid in list(pending.keys()):
            try:
                status = get_task_status(tid)
            except ServiceApiError as exc:
                status = {"task_id": tid, "status": "unknown", "error": str(exc)}
            state = str(status.get("status") or "").lower()
            if state in ("completed", "failed", "cancelled", "cancel_requested"):
                results[tid] = status
                pending.pop(tid)
        if not pending or time.time() >= deadline:
            for tid in pending:
                results[tid] = results.get(tid) or pending[tid] or {"task_id": tid, "status": "timeout"}
            break
        time.sleep(interval)
    return results


def get_report_markdown(query_id: str) -> Optional[str]:
    """读取报告 Markdown（/history/{id}/markdown）；不存在返回 None。"""
    try:
        resp = _request("GET", f"/api/v1/history/{query_id}/markdown", timeout=30)
    except ServiceApiError:
        return None
    content = resp.get("content")
    return str(content) if content else None


def list_reports(stock_code: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """历史报告列表（GET /api/v1/history）。"""
    query = f"?limit={max(1, min(int(limit), 100))}"
    if stock_code:
        query += f"&stock_code={urllib.parse.quote(stock_code)}"
    resp = _request("GET", f"/api/v1/history{query}", timeout=30)
    items = resp.get("items") or []
    return items if isinstance(items, list) else []
