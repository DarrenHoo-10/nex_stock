# nex-stock MCP 管家与常驻服务

本仓库除 FastAPI 分析服务外，还包含一个 **MCP 管家**（`mcp_server/`），把分析服务的能力暴露为
MCP（Model Context Protocol）接口，供 Nex MCP 网关及任意 MCP 客户端远程调用；并附带 Windows
常驻化脚本，实现开机自启与崩溃自恢复。

## 架构

```
[本机 Windows · 常驻]
  ├─ 分析服务  FastAPI :8000（python main.py --serve-only，业务/数据库/报告）
  └─ MCP 管家  :8800（python -m mcp_server，Streamable HTTP + Bearer 认证）
        ↑ HTTP（Nex 网关经 ZeroTier 回连；也可反代为公网 https）
Nex 网关（nex-mcp）→ 项目 nex-stock → upstream `api`
        ↑
任意 MCP 客户端：nex-stock__api__<tool> 系列工具
```

- 管家进程持有分析服务的启停权；管家自身由计划任务 `NexStockMCP` 的看门狗守护（退出后 5 秒内重启）。
- 分析服务未运行时，分析类工具会按需自动拉起（`MCP_SERVICE_START_ON_DEMAND=true`）。

## 工具清单

| 工具 | 只读 | 用途 |
|------|:---:|------|
| `mcp_list` | ✅ | 全量接口自描述文档（含参数/示例/输出契约） |
| `service_status` | ✅ | 分析服务健康状态与进程信息 |
| `service_start` | | 启动分析服务（幂等，等待健康检查通过） |
| `service_stop` | | 停止分析服务进程树（幂等） |
| `analysis_run` | | 提交个股 AI 分析并轮询，返回决策摘要 + Markdown 报告 |
| `analysis_status` | ✅ | 按 task_id 查询任务进度与结果 |
| `market_review_run` | | 触发大盘复盘并轮询，返回 Markdown 报告 |
| `reports_list` | ✅ | 历史分析报告列表 |
| `report_get` | ✅ | 单篇报告全文（markdown）或决策摘要（meta） |

接口契约以 `mcp_list` 为准（遵守 Nex mcp-rules v1：R1-R10，含 Bearer 认证与 inputSchema 描述）。

## 本地运行

```bash
# 前置：.venv 已安装依赖；.env 已配置 MCP_SERVER_BEARER_TOKEN（缺失时拒绝启动）
.venv\Scripts\python -m mcp_server          # 前台运行（日志打到 stdout）

# Windows 常驻化（管理员授权一次）：计划任务登录自启 + 看门狗 + 防火墙放行 8800（仅 ZeroTier 网段）
powershell -ExecutionPolicy Bypass -File scripts\nex_stock_service.ps1 install
powershell -ExecutionPolicy Bypass -File scripts\nex_stock_service.ps1 status
```

## 配置项（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MCP_SERVER_ENABLED` | `true` | 是否启用管家（false 时 `python -m mcp_server` 直接退出） |
| `MCP_SERVER_HOST` | `0.0.0.0` | 监听地址；经 ZeroTier/反代对外时用 0.0.0.0 |
| `MCP_SERVER_PORT` | `8800` | 监听端口 |
| `MCP_SERVER_BEARER_TOKEN` | 无 | **必填**，Bearer 令牌（与 Nex 网关 `upstreamSecrets` 绑定的环境变量值一致） |
| `MCP_SERVICE_AUTOSTART` | `true` | 管家启动时自动拉起分析服务 |
| `MCP_SERVICE_START_ON_DEMAND` | `true` | 分析类工具调用时按需拉起分析服务 |
| `MCP_SERVICE_STARTUP_TIMEOUT_SECONDS` | `90` | 启动等待健康检查的秒数 |
| `MCP_ANALYSIS_WAIT_SECONDS` | `100` | `analysis_run` 等待默认值（网关单次调用限 120 秒，须留余量） |
| `MCP_ANALYSIS_POLL_INTERVAL_SECONDS` | `5` | 任务轮询间隔 |
| `MCP_ANALYSIS_MAX_STOCKS` | `10` | 单次 `analysis_run` 股票数上限 |

## Nex 网关接入（已完成的一次性配置）

1. 网关注册项目 `nex-stock`，upstream `api` 指向 `http://10.73.236.189:8800/mcp`（本机 ZeroTier IP）。
2. 网关 `/etc/nex-mcp/config.json`：
   - `upstreamAllowedHosts` 增加 `10.73.236.189:8800`（内网 http 豁免）；
   - `upstreamSecrets` 增加 `"NEX_UPSTREAM_STOCK_TOKEN": {"projectId": "nex-stock", "hosts": ["10.73.236.189"]}`
     （键名必须匹配 `^NEX_UPSTREAM_[A-Z0-9_]+$`）。
3. 网关 `/etc/nex-mcp/app.env` 设置 `NEX_UPSTREAM_STOCK_TOKEN=<与本地一致>`，重启 `nex-mcp.service`。
4. 验证：`nex project nex-stock`（mcp.state=connected）、`nex interfaces nex-stock`、`nex tools nex-stock`。

## 排障

| 现象 | 处置 |
|------|------|
| 网关调用 401 | 网关 `NEX_UPSTREAM_STOCK_TOKEN` 与本机 `MCP_SERVER_BEARER_TOKEN` 不一致；改本机 .env 后重启管家 |
| 管家 upsteam offline | 本机 `curl http://127.0.0.1:8800/health`；再从网关主机 curl ZeroTier IP 测回连 |
| 管家进程消失 | 看门狗 5 秒内自动拉起；持续未恢复检查计划任务 `NexStockMCP` 状态 |
| `analysis_run` 返回 task_id 而非报告 | 正常（超网关 120 秒限制），用 `analysis_status` 续查 |
| 分析服务起不来 | 看 `logs/analysis_service.log`；常见于端口占用或 .env 配置错误 |

## 回滚

- 本机：`scripts/nex_stock_service.ps1 uninstall`（弹 UAC），删除 `mcp_server/` 目录与 `.env` 中 `MCP_*` 项。
- 网关：`nex call mcp_upstream_delete '{"projectId":"nex-stock","upstreamId":"api","confirm":"api"}'`；
  `nex call project_delete '{"projectId":"nex-stock","confirm":"nex-stock"}'`；还原 `/etc/nex-mcp` 下的 `.bak-*` 备份。
