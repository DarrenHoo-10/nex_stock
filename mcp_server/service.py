"""分析服务进程管理：启动 / 停止 / 状态 / 按需拉起。

被托管的目标进程是仓库自带的 FastAPI 服务（`python main.py --serve-only`）。
管家通过 PID 文件 + 健康检查（/health）双重确认状态；stop 只停目标进程，管家自身不受影响。
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from . import config

_START_FLAG = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0


def _http_get(url: str, timeout: float = 3.0) -> Optional[int]:
    """GET 请求，返回 HTTP 状态码；任何网络错误返回 None。"""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def is_service_healthy(timeout: float = 3.0) -> bool:
    """分析服务健康检查（/health 由 api.app 提供）。"""
    return _http_get(f"{config.SERVICE_BASE_URL}/health", timeout=timeout) == 200


def _read_pid_record() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(config.PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_pid_record(pid: int) -> None:
    config.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.PID_FILE.write_text(
        json.dumps({"pid": pid, "started_at": int(time.time())}, ensure_ascii=False),
        encoding="utf-8",
    )


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        # Windows：tasklist 精确匹配 PID
        probe = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return str(pid) in (probe.stdout or "")
    # POSIX：kill -0
    try:
        os_signal_probe = subprocess.run(["kill", "-0", str(pid)], capture_output=True, timeout=5)
        return os_signal_probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def get_service_status() -> Dict[str, Any]:
    """服务状态：健康检查为主，PID 记录为辅。"""
    record = _read_pid_record()
    pid = int(record.get("pid") or 0) if record else 0
    pid_alive = _pid_alive(pid) if pid else False
    healthy = is_service_healthy()
    return {
        "healthy": healthy,
        "base_url": config.SERVICE_BASE_URL,
        "managed_pid": pid if pid_alive else None,
        "pid_record_stale": bool(pid and not pid_alive),
        "started_at": record.get("started_at") if record else None,
        "autostart_enabled": config.SERVICE_AUTOSTART,
        "start_on_demand_enabled": config.SERVICE_START_ON_DEMAND,
    }


def start_service(wait_for_health: bool = True) -> Dict[str, Any]:
    """启动分析服务；已健康时幂等返回。"""
    if is_service_healthy():
        return {"started": False, "already_healthy": True, "status": get_service_status()}

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = open(config.SERVICE_LOG_FILE, "ab")
    creationflags = _START_FLAG if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0
    proc = subprocess.Popen(
        [
            str(config.VENV_PYTHON),
            str(config.REPO_ROOT / "main.py"),
            "--serve-only",
        ],
        cwd=str(config.REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    _write_pid_record(proc.pid)

    result: Dict[str, Any] = {"started": True, "pid": proc.pid, "already_healthy": False}
    if wait_for_health:
        deadline = time.time() + config.SERVICE_STARTUP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if proc.poll() is not None:
                result["error"] = f"service process exited during startup (code={proc.returncode})"
                return result
            if is_service_healthy():
                result["healthy"] = True
                break
            time.sleep(2)
        else:
            result["healthy"] = False
            result["error"] = f"service did not become healthy within {config.SERVICE_STARTUP_TIMEOUT_SECONDS}s"
    return result


def stop_service(timeout: float = 15.0) -> Dict[str, Any]:
    """停止分析服务进程树；幂等。"""
    record = _read_pid_record()
    pid = int(record.get("pid") or 0) if record else 0
    if is_service_healthy() and not (pid and _pid_alive(pid)):
        # 健康但不是管家拉起的进程（例如手动启动）：按端口找到 PID 再停
        pid = _find_pid_by_port()
    if not pid:
        if config.PID_FILE.exists():
            config.PID_FILE.unlink(missing_ok=True)
        return {"stopped": False, "already_stopped": True}
    if not _pid_alive(pid):
        if config.PID_FILE.exists():
            config.PID_FILE.unlink(missing_ok=True)
        return {"stopped": False, "already_stopped": True}

    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20)
    else:
        subprocess.run(["pkill", "-TERM", "-P", str(pid)], capture_output=True, timeout=10)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_service_healthy():
            break
        time.sleep(1)
    if config.PID_FILE.exists():
        config.PID_FILE.unlink(missing_ok=True)
    stopped_fully = not is_service_healthy()
    return {
        "stopped": stopped_fully,
        "pid": pid,
        "already_stopped": False,
        "still_healthy": not stopped_fully,
    }


def _find_pid_by_port() -> int:
    """从分析服务端口反查 PID（Windows：netstat）。找不到返回 0。"""
    if not hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        return 0
    from urllib.parse import urlparse

    port = urlparse(config.SERVICE_BASE_URL).port or 8000
    try:
        probe = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in (probe.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and "LISTENING" in line:
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return 0


def ensure_service() -> Dict[str, Any]:
    """确保分析服务可用；按需拉起（受 MCP_SERVICE_START_ON_DEMAND 控制）。"""
    if is_service_healthy():
        return {"ready": True, "action": "already_healthy"}
    if not config.SERVICE_START_ON_DEMAND:
        return {
            "ready": False,
            "action": "refused",
            "error": "analysis service is not running and start-on-demand is disabled; call service_start first",
        }
    started = start_service(wait_for_health=True)
    if not started.get("healthy"):
        return {
            "ready": False,
            "action": "start_attempted",
            "error": started.get("error") or "service failed to become healthy",
        }
    return {"ready": True, "action": "started"}
