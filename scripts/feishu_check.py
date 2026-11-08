#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书应用机器人自检（长连接 + 通知推送）。

依次检查：
1. 应用凭据：用 FEISHU_APP_ID / FEISHU_APP_SECRET 换取 tenant_access_token
2. 机器人信息：GET /open-apis/bot/v3/info（需要在开放平台开启「机器人」能力）
3. 推送目标：/bind 绑定的会话 > FEISHU_CHAT_ID > Webhook（兼容），显示最终生效的目标
4. （可选）--listen N：打开长连接 N 秒，确认能收到 im.message.receive_v1 事件
5. （可选）--send-test：向推送目标发送一张测试卡片

使用方法：
    python scripts/feishu_check.py
    python scripts/feishu_check.py --listen 60       # 打开长连接 60 秒，期间给机器人发一条消息
    python scripts/feishu_check.py --send-test       # 向推送目标发测试卡片

注意：--listen 与正在运行的服务共用同一个应用时，飞书会把事件分发给其中一个连接，
请先停止服务再做长连接检查。输出不包含任何密钥或消息正文。
退出码：全部通过 0，任一项失败 1。
"""

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def mask(value: Optional[str], keep: int = 4) -> str:
    """Show only the first ``keep`` characters of an id (never used for secrets)."""
    text = str(value or "")
    if len(text) <= keep:
        return "*" * len(text)
    return f"{text[:keep]}***"


def _build_client(app_id: str, app_secret: str, domain: str) -> Any:
    import lark_oapi as lark

    return (
        lark.Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .domain(domain)
        .log_level(lark.LogLevel.WARNING)
        .build()
    )


def _fetch_tenant_token(client: Any, app_id: str, app_secret: str) -> tuple:
    """Return ``(ok, detail)`` for a tenant_access_token request (the token itself is never kept)."""
    from lark_oapi.api.auth.v3 import InternalTenantAccessTokenRequest, InternalTenantAccessTokenRequestBody

    request = (
        InternalTenantAccessTokenRequest.builder()
        .request_body(
            InternalTenantAccessTokenRequestBody.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .build()
        )
        .build()
    )
    response = client.auth.v3.tenant_access_token.internal(request)
    if response.success():
        return True, "ok"
    return False, f"code={response.code} msg={response.msg}"


def _listen(app_id: str, app_secret: str, domain: str, seconds: int, out: Callable[[str], None]) -> int:
    """Open the long connection for ``seconds`` and count received message events."""
    import lark_oapi as lark
    from lark_oapi import ws

    received = {"count": 0}

    def on_message(event: Any) -> None:
        received["count"] += 1
        message = getattr(getattr(event, "event", None), "message", None)
        out(f"  收到事件 #{received['count']}: chat_type={getattr(message, 'chat_type', '?')} "
            f"message_type={getattr(message, 'message_type', '?')}")

    handler = (
        lark.EventDispatcherHandler.builder("", "", level=lark.LogLevel.WARNING)
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    client = ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        domain=domain,
        log_level=lark.LogLevel.WARNING,
        auto_reconnect=False,
    )
    thread = threading.Thread(target=client.start, daemon=True, name="feishu-check-ws")
    thread.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not thread.is_alive():
            out("  长连接已提前断开（凭据无效、未开启长连接订阅或网络问题）")
            break
        time.sleep(0.5)
    return received["count"]


def run_checks(
    config: Any,
    *,
    listen_seconds: int = 0,
    send_test: bool = False,
    out: Callable[[str], None] = print,
    client_factory: Callable[[str, str, str], Any] = _build_client,
    listener: Callable[[str, str, str, int, Callable[[str], None]], int] = _listen,
    sender_factory: Optional[Callable[[Any], Any]] = None,
) -> int:
    """Run the checks; returns the process exit code."""
    from bot.platforms.feishu_stream import FeishuReplyClient, _resolve_feishu_domain
    from src.services.feishu_binding_service import get_binding, resolve_notification_target

    failures = 0
    app_id = str(getattr(config, "feishu_app_id", "") or "").strip()
    app_secret = str(getattr(config, "feishu_app_secret", "") or "").strip()
    domain = _resolve_feishu_domain(getattr(config, "feishu_domain", "feishu"))

    out("[1/5] 应用凭据")
    client = None
    if not app_id or not app_secret:
        out("  失败：未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
        failures += 1
    else:
        out(f"  app_id={mask(app_id)} domain={domain}")
        try:
            client = client_factory(app_id, app_secret, domain)
            ok, detail = _fetch_tenant_token(client, app_id, app_secret)
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok:
            out("  通过：已获取 tenant_access_token")
        else:
            out(f"  失败：{detail}")
            failures += 1
            client = None

    out("[2/5] 机器人信息")
    if client is None:
        out("  跳过（凭据未通过）")
    else:
        reply_client = FeishuReplyClient.__new__(FeishuReplyClient)
        reply_client._client = client
        info = reply_client.get_bot_info()
        if info:
            out(f"  通过：app_name={info.get('app_name') or '?'} open_id={mask(info.get('open_id'))}")
        else:
            out("  失败：无法获取机器人信息（请在开放平台为应用开启「机器人」能力并发布版本）")
            failures += 1

    out("[3/5] 推送目标")
    target = resolve_notification_target(config)
    if target is not None:
        binding = get_binding() if target.source == "binding" else None
        extra = f" chat_type={binding['chat_type']} bound_at={binding['bound_at']}" if binding else ""
        out(f"  机器人推送 -> {target.source} {target.receive_id_type}={mask(target.chat_id, 6)}{extra}")
    elif str(getattr(config, "feishu_webhook_url", "") or "").strip():
        out("  Webhook（兼容方式，不推荐）：未绑定会话且未配置 FEISHU_CHAT_ID；在目标会话里发送 /bind 可改用机器人推送")
    else:
        out("  未解析到推送目标：在目标会话里给机器人发送 /bind，或配置 FEISHU_CHAT_ID")
        failures += 1

    out("[4/5] 长连接事件")
    if listen_seconds <= 0:
        out("  未检查（加 --listen N 打开长连接 N 秒，并在此期间给机器人发一条消息）")
    elif client is None:
        out("  跳过（凭据未通过）")
    else:
        out(f"  长连接已打开，请在 {listen_seconds} 秒内给机器人发一条消息（群聊需 @机器人）…")
        try:
            count = listener(app_id, app_secret, domain, listen_seconds, out)
        except Exception as exc:
            out(f"  失败：{type(exc).__name__}: {exc}")
            failures += 1
        else:
            if count > 0:
                out(f"  通过：收到 {count} 条消息事件")
            else:
                out("  失败：未收到事件（检查：事件订阅方式=长连接并添加 im.message.receive_v1、权限已开通、版本已发布、"
                    "没有别的实例占用同一应用的长连接）")
                failures += 1

    out("[5/5] 测试推送")
    if not send_test:
        out("  未发送（加 --send-test 向推送目标发送测试卡片）")
    else:
        if sender_factory is None:
            from src.notification_sender.feishu_sender import FeishuSender

            sender_factory = FeishuSender
        content = "**飞书自检**\n\n这是一条测试推送，收到说明机器人推送链路正常。"
        try:
            sent = bool(sender_factory(config).send_to_feishu(content, record_in_session=False))
        except Exception as exc:
            out(f"  失败：{type(exc).__name__}: {exc}")
            failures += 1
        else:
            if sent:
                out("  通过：测试卡片已发送")
            else:
                out("  失败：测试卡片发送失败（检查机器人是否在目标会话中、应用是否有发送消息权限）")
                failures += 1

    out("结果：全部通过" if failures == 0 else f"结果：{failures} 项未通过")
    return 0 if failures == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="飞书应用机器人自检（凭据 / 机器人 / 推送目标 / 长连接 / 测试推送）")
    parser.add_argument("--listen", type=int, default=0, metavar="N", help="打开长连接 N 秒，确认能收到消息事件")
    parser.add_argument("--send-test", action="store_true", help="向推送目标发送一张测试卡片")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    from src.config import get_config

    return run_checks(get_config(), listen_seconds=max(0, args.listen), send_test=args.send_test)


if __name__ == "__main__":
    sys.exit(main())
