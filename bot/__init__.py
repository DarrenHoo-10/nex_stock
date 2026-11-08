# -*- coding: utf-8 -*-
"""
===================================
机器人命令触发系统
===================================

通过 @机器人 或发送命令触发股票分析等功能。
支持飞书、钉钉、企业微信、Telegram 等多平台。

模块结构：
- models.py: 统一的消息/响应模型
- dispatcher.py: 命令分发器
- commands/: 命令处理器
- platforms/: 平台适配器
- handler.py: Webhook 处理器

使用方式：
1. 配置环境变量（各平台的 Token 等）
2. 启动服务（--serve / --schedule）
3. 各平台接入方式：
   - 飞书: 应用机器人 + 事件订阅「长连接」（FEISHU_STREAM_ENABLED=true），无需回调地址，
     详见 docs/bot/feishu-bot-config.md
   - 钉钉: Stream 模式（DINGTALK_STREAM_ENABLED=true）
   - 企业微信 / Telegram: 见 docs/bot-command.md

支持的命令：
- /analyze <股票代码>  - 分析指定股票
- /market             - 大盘复盘
- /batch              - 批量分析自选股
- /help               - 显示帮助
- /status             - 系统状态
- /bind, /unbind      - 把当前飞书会话设为 / 取消通知目标
"""

from bot.models import BotMessage, BotResponse, ChatType, WebhookResponse
from bot.dispatcher import CommandDispatcher, get_dispatcher

__all__ = [
    'BotMessage',
    'BotResponse',
    'ChatType',
    'WebhookResponse',
    'CommandDispatcher',
    'get_dispatcher',
]
