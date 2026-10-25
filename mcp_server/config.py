"""MCP 管家配置。

从仓库根目录 .env 读取 MCP_SERVER_* 配置项；所有配置不设置也可运行（有安全默认值）。
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# MCP 管家自身
MCP_ENABLED = _env_bool("MCP_SERVER_ENABLED", True)
MCP_HOST = os.getenv("MCP_SERVER_HOST", "0.0.0.0")
MCP_PORT = _env_int("MCP_SERVER_PORT", 8800)
# R10：HTTP upstream 必须 Bearer 认证；未配置时拒绝启动对外服务
MCP_BEARER_TOKEN = os.getenv("MCP_SERVER_BEARER_TOKEN", "").strip()

# 分析服务（被托管的目标进程）
SERVICE_BASE_URL = os.getenv("MCP_SERVICE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SERVICE_AUTOSTART = _env_bool("MCP_SERVICE_AUTOSTART", True)
SERVICE_START_ON_DEMAND = _env_bool("MCP_SERVICE_START_ON_DEMAND", True)
SERVICE_STARTUP_TIMEOUT_SECONDS = _env_int("MCP_SERVICE_STARTUP_TIMEOUT_SECONDS", 90)

# 分析任务
# 注意：Nex 网关对单次 MCP 调用的超时上限为 120 秒，等待默认值必须留出余量；
# 未在等待窗口内完成的任务会返回 task_id，由 agent 用 analysis_status 轮询续查。
ANALYSIS_WAIT_DEFAULT_SECONDS = _env_int("MCP_ANALYSIS_WAIT_SECONDS", 100)
ANALYSIS_POLL_INTERVAL_SECONDS = _env_int("MCP_ANALYSIS_POLL_INTERVAL_SECONDS", 5)
ANALYSIS_MAX_STOCKS = _env_int("MCP_ANALYSIS_MAX_STOCKS", 10)

# 进程与文件
if sys.platform == "win32":
    VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
LOG_DIR = REPO_ROOT / "logs"
SERVICE_LOG_FILE = LOG_DIR / "analysis_service.log"
PID_FILE = REPO_ROOT / "data" / "mcp_service_manager.json"
