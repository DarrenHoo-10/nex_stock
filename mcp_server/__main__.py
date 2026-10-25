"""MCP 管家进程入口：`python -m mcp_server`。

启动 Streamable HTTP 服务（默认 0.0.0.0:8800）；
可选随管家自启分析服务（MCP_SERVICE_AUTOSTART=true）。
"""

import logging

from . import config, service


def main() -> None:
    if not config.MCP_ENABLED:
        print("[mcp_server] MCP_SERVER_ENABLED=false，未启动")
        return
    if not config.MCP_BEARER_TOKEN:
        print("[mcp_server] 缺少 MCP_SERVER_BEARER_TOKEN，拒绝启动（对外服务必须 Bearer 认证）")
        raise SystemExit(2)

    if config.SERVICE_AUTOSTART and not service.is_service_healthy():
        print("[mcp_server] 自动拉起分析服务...")
        started = service.start_service(wait_for_health=True)
        if started.get("healthy"):
            print(f"[mcp_server] 分析服务已就绪 (pid={started.get('pid')})")
        else:
            # 自启失败不阻塞管家：分析类工具会按需重试
            print(f"[mcp_server] 分析服务自启失败：{started.get('error')}（可稍后用 service_start 重试）")

    import uvicorn

    from .tools import create_asgi_app

    app = create_asgi_app()
    print(f"[mcp_server] MCP 管家运行中: http://{config.MCP_HOST}:{config.MCP_PORT}/mcp")
    uvicorn.run(app, host=config.MCP_HOST, port=config.MCP_PORT, log_level="warning")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
