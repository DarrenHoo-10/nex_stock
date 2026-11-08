# -*- coding: utf-8 -*-
"""
===================================
通知目标绑定命令
===================================

``/bind`` 把当前飞书会话（私聊或群聊）设为唯一的通知推送目标，``/unbind`` 取消。
推送目标优先级：绑定会话 > ``FEISHU_CHAT_ID`` > Webhook（兼容）。
"""

from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse, ChatType


def _chat_label(chat_type: str) -> str:
    return "私聊" if chat_type == "p2p" else "群聊"


class BindCommand(BotCommand):
    """把当前会话设为通知目标（替换已有绑定）"""

    @property
    def name(self) -> str:
        return "bind"

    @property
    def aliases(self) -> List[str]:
        return ["绑定"]

    @property
    def description(self) -> str:
        return "把当前会话设为通知推送目标"

    @property
    def usage(self) -> str:
        return "/bind"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        if message.platform != "feishu":
            return BotResponse.error_response("/bind 仅支持飞书")
        if not message.chat_id:
            return BotResponse.error_response("无法识别当前会话，绑定失败")

        from src.config import get_config
        from src.services import feishu_binding_service as binding

        chat_type = binding.CHAT_TYPE_P2P if message.chat_type == ChatType.PRIVATE else binding.CHAT_TYPE_GROUP
        previous = binding.get_binding()
        binding.bind_chat(message.chat_id, chat_type, bound_by_open_id=message.user_id or None)

        lines = [
            f"已将当前{_chat_label(chat_type)}设为通知目标。",
            "之后的个股报告、盘前推送、板块/计划结果、告警和系统错误都会发到这里；"
            "回复推送消息即可就该推送继续提问。",
        ]
        if previous and previous.get("chat_id") != message.chat_id:
            lines.append(f"原绑定的{_chat_label(previous.get('chat_type', ''))}已被替换。")
        if not binding.has_app_credentials(get_config()):
            lines.append("注意：未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，绑定已保存但暂时无法以机器人身份推送。")
        lines.append("发送 /unbind 取消绑定。")
        return BotResponse.text_response("\n".join(lines), at_user=False)


class UnbindCommand(BotCommand):
    """取消通知目标绑定"""

    @property
    def name(self) -> str:
        return "unbind"

    @property
    def aliases(self) -> List[str]:
        return ["解绑"]

    @property
    def description(self) -> str:
        return "取消通知推送目标绑定"

    @property
    def usage(self) -> str:
        return "/unbind"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        if message.platform != "feishu":
            return BotResponse.error_response("/unbind 仅支持飞书")

        from src.services import feishu_binding_service as binding

        if binding.unbind_chat():
            return BotResponse.text_response(
                "已取消绑定。之后的推送回退到 FEISHU_CHAT_ID（如已配置），否则回退到 Webhook（如已配置）。",
                at_user=False,
            )
        return BotResponse.text_response("当前没有绑定的通知目标。", at_user=False)
