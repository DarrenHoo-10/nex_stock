# -*- coding: utf-8 -*-
"""
===================================
飞书 Stream 模式适配器
===================================

使用飞书官方 lark-oapi SDK 的 WebSocket 长连接模式接入机器人，
无需公网 IP 和 Webhook 配置。

优势：
- 不需要公网 IP 或域名
- 不需要配置 Webhook URL
- 通过 WebSocket 长连接接收消息
- 更简单的接入方式
- 内置自动重连和心跳保活

依赖：
pip install lark-oapi

飞书长连接文档：
https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/handle-events
"""

import json
import logging
import threading
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Callable
import time

logger = logging.getLogger(__name__)

FEISHU_DOMAIN = "https://open.feishu.cn"
LARK_DOMAIN = "https://open.larksuite.com"

# Shown in the card that is replied immediately and patched with the final answer.
PLACEHOLDER_TEXT = "正在分析…"
_PATCH_RETRIES = 2
_PATCH_RETRY_DELAY_SECONDS = 1.0
_SEEN_MESSAGE_LIMIT = 512

# 尝试导入飞书 SDK
try:
    import lark_oapi as lark
    from lark_oapi import ws
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        PatchMessageRequest,
        PatchMessageRequestBody,
    )
    from lark_oapi.core.const import (
        FEISHU_DOMAIN as SDK_FEISHU_DOMAIN,
        LARK_DOMAIN as SDK_LARK_DOMAIN,
    )

    FEISHU_DOMAIN = SDK_FEISHU_DOMAIN
    LARK_DOMAIN = SDK_LARK_DOMAIN
    FEISHU_SDK_AVAILABLE = True
except ImportError:
    FEISHU_SDK_AVAILABLE = False
    logger.warning("[Feishu Stream] lark-oapi SDK 未安装，Stream 模式不可用")
    logger.warning("[Feishu Stream] 请运行: pip install lark-oapi")

from bot.models import BotMessage, BotResponse, ChatType
from src.formatters import format_feishu_markdown, chunk_content_by_max_bytes
from src.config import get_config


def _resolve_feishu_domain(domain: Optional[str]) -> str:
    """Resolve the configured Feishu/Lark region to an SDK API domain."""
    raw_domain = str(domain or "feishu").strip().lower()
    if raw_domain in ("feishu", FEISHU_DOMAIN.lower()):
        return FEISHU_DOMAIN
    if raw_domain in ("lark", LARK_DOMAIN.lower()):
        return LARK_DOMAIN

    logger.warning(
        "[Feishu Stream] 无效的 FEISHU_DOMAIN=%s，回退为 feishu",
        raw_domain,
    )
    return FEISHU_DOMAIN


class FeishuReplyClient:
    """
    飞书消息回复客户端

    使用飞书 API 发送回复消息。
    """

    def __init__(
            self,
            app_id: str,
            app_secret: str,
            domain: Optional[str] = None
    ):
        """
        Args:
            app_id: 飞书应用 ID
            app_secret: 飞书应用密钥
            domain: 飞书 API 区域（feishu/lark）或 SDK API 域名
        """
        if not FEISHU_SDK_AVAILABLE:
            raise ImportError("lark-oapi SDK 未安装")

        config = get_config()
        configured_domain = (
            domain
            if domain is not None
            else getattr(config, 'feishu_domain', 'feishu')
        )
        self._domain = _resolve_feishu_domain(configured_domain)

        self._client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .domain(self._domain) \
            .log_level(lark.LogLevel.WARNING) \
            .build()

        # 获取配置的最大字节数
        self._max_bytes = getattr(config, 'feishu_max_bytes', 20000)

    def _send_interactive_card(self, content: str, message_id: Optional[str] = None,
                               chat_id: Optional[str] = None,
                               receive_id_type: str = "chat_id",
                               at_user: bool = False, user_id: Optional[str] = None) -> bool:
        """
        发送交互卡片消息（支持 Markdown 渲染）

        Args:
            content: Markdown 格式的内容
            message_id: 原消息 ID（回复时使用）
            chat_id: 会话 ID（主动发送时使用）
            receive_id_type: 接收者 ID 类型
            at_user: 是否 @用户
            user_id: 用户 open_id（at_user=True 时需要）

        Returns:
            是否发送成功
        """
        try:
            # 如果需要 @用户，在内容前添加 @ 标记
            final_content = content
            if at_user and user_id:
                final_content = f"<at user_id=\"{user_id}\"></at> {content}"

            # 构建交互卡片 payload
            card_data = {
                "config": {"wide_screen_mode": True},
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": final_content
                        }
                    }
                ]
            }

            content_json = json.dumps(card_data)

            if message_id:
                # 回复消息
                request = ReplyMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content_json)
                    .msg_type("interactive")
                    .build()
                ) \
                    .build()
                response = self._client.im.v1.message.reply(request)
            else:
                # 主动发送消息
                request = CreateMessageRequest.builder() \
                    .receive_id_type(receive_id_type) \
                    .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(content_json)
                    .msg_type("interactive")
                    .build()
                ) \
                    .build()
                response = self._client.im.v1.message.create(request)

            if not response.success():
                logger.error(
                    f"[Feishu Stream] 发送交互卡片失败: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                return False

            logger.debug("[Feishu Stream] 发送交互卡片成功")
            return True

        except Exception as e:
            logger.error(f"[Feishu Stream] 发送交互卡片异常: {e}")
            return False

    # ------------------------------------------------------------------
    # Updatable answer cards: placeholder -> patch in place
    # ------------------------------------------------------------------

    @staticmethod
    def build_card_json(content: str, updatable: bool = False) -> str:
        """Interactive card with one ``lark_md`` div.

        ``update_multi`` makes the card editable afterwards through
        ``im.v1.message.patch`` (PATCH /open-apis/im/v1/messages/:message_id).
        """
        config = {"wide_screen_mode": True}
        if updatable:
            config["update_multi"] = True
        card_data = {
            "config": config,
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}}
            ],
        }
        return json.dumps(card_data, ensure_ascii=False)

    def reply_card(self, message_id: str, content: str, updatable: bool = True) -> Optional[str]:
        """Reply with a card and return the new message id (``None`` on failure)."""
        try:
            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                ReplyMessageRequestBody.builder()
                .content(self.build_card_json(content, updatable=updatable))
                .msg_type("interactive")
                .build()
            ) \
                .build()
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                logger.error(
                    f"[Feishu Stream] 回复卡片失败: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                return None
            data = getattr(response, "data", None)
            new_id = getattr(data, "message_id", None)
            return new_id if isinstance(new_id, str) and new_id else None
        except Exception as e:
            logger.error(f"[Feishu Stream] 回复卡片异常: {e}")
            return None

    def patch_card(self, message_id: str, content: str) -> bool:
        """Update an earlier updatable card in place (``im.v1.message.patch``)."""
        card_json = self.build_card_json(content, updatable=True)
        for attempt in range(_PATCH_RETRIES):
            try:
                request = PatchMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(PatchMessageRequestBody.builder().content(card_json).build()) \
                    .build()
                response = self._client.im.v1.message.patch(request)
                if response.success():
                    return True
                logger.error(
                    f"[Feishu Stream] 更新卡片失败(attempt={attempt + 1}): code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
            except Exception as e:
                logger.error(f"[Feishu Stream] 更新卡片异常(attempt={attempt + 1}): {e}")
            if attempt < _PATCH_RETRIES - 1:
                time.sleep(_PATCH_RETRY_DELAY_SECONDS)
        return False

    def _split_for_cards(self, text: str) -> list:
        formatted = format_feishu_markdown(text)
        if len(formatted.encode('utf-8')) <= self._max_bytes:
            return [formatted]
        try:
            return chunk_content_by_max_bytes(formatted, self._max_bytes, add_page_marker=True)
        except ValueError as exc:
            logger.error(f"[Feishu Stream] 回复分段失败，按单条发送: {exc}")
            return [formatted]

    def finish_card(self, placeholder_id: Optional[str], message_id: str, text: str) -> list:
        """Put ``text`` into the placeholder card and send any overflow as follow-up cards.

        Returns the ids of the messages that now carry the answer (placeholder
        first). When there is no placeholder or the patch fails, the whole answer
        is sent as ordinary reply cards instead.
        """
        chunks = self._split_for_cards(text)
        carried: list = []
        rest = chunks
        if placeholder_id:
            if self.patch_card(placeholder_id, chunks[0]):
                carried.append(placeholder_id)
                rest = chunks[1:]
            else:
                logger.warning("[Feishu Stream] 占位卡片更新失败，改为直接回复答案")
        for index, chunk in enumerate(rest):
            new_id = self.reply_card(message_id, chunk, updatable=False)
            if new_id:
                carried.append(new_id)
            if index < len(rest) - 1:
                time.sleep(1)
        return carried

    def get_bot_info(self) -> Optional[dict]:
        """The bot's own info (GET /open-apis/bot/v3/info): ``app_name``, ``open_id``, ..."""
        try:
            from lark_oapi.core import BaseRequest
            from lark_oapi.core.enum import AccessTokenType, HttpMethod

            request = BaseRequest.builder() \
                .http_method(HttpMethod.GET) \
                .uri("/open-apis/bot/v3/info") \
                .token_types({AccessTokenType.TENANT}) \
                .build()
            response = self._client.request(request)
            if not response.success():
                logger.warning(f"[Feishu Stream] 获取机器人信息失败: code={response.code}, msg={response.msg}")
                return None
            payload = json.loads(response.raw.content.decode("utf-8"))
            bot = payload.get("bot")
            return bot if isinstance(bot, dict) else None
        except Exception as e:
            logger.warning(f"[Feishu Stream] 获取机器人信息异常: {e}")
            return None

    def get_bot_open_id(self) -> Optional[str]:
        """The bot's own open_id, used to tell @bot from other mentions."""
        open_id = (self.get_bot_info() or {}).get("open_id")
        return open_id if isinstance(open_id, str) and open_id else None

    def reply_text(self, message_id: str, text: str, at_user: bool = False,
                   user_id: Optional[str] = None) -> bool:
        """
        回复文本消息（支持交互卡片和分段发送）

        Args:
            message_id: 原消息 ID
            text: 回复文本
            at_user: 是否 @用户
            user_id: 用户 open_id（at_user=True 时需要）

        Returns:
            是否发送成功
        """
        # 将文本转换为飞书 Markdown 格式
        formatted_text = format_feishu_markdown(text)

        # 检查是否需要分段发送
        content_bytes = len(formatted_text.encode('utf-8'))
        if content_bytes > self._max_bytes:
            logger.info(
                f"[Feishu Stream] 回复消息内容超长({content_bytes}字节)，将分批发送"
            )
            return self._send_to_chat_chunked(
                formatted_text,
                lambda chunk: self._send_interactive_card(
                    chunk,
                    message_id=message_id,
                    at_user=at_user,
                    user_id=user_id,
                ),
            )

        # 单条消息，使用交互卡片
        return self._send_interactive_card(
            formatted_text, message_id=message_id, at_user=at_user, user_id=user_id
        )

    def send_to_chat(self, chat_id: str, text: str,
                     receive_id_type: str = "chat_id") -> bool:
        """
        发送消息到指定会话（支持交互卡片和分段发送）

        Args:
            chat_id: 会话 ID
            text: 消息文本
            receive_id_type: 接收者 ID 类型，默认 chat_id

        Returns:
            是否发送成功
        """
        # 将文本转换为飞书 Markdown 格式
        formatted_text = format_feishu_markdown(text)

        # 检查是否需要分段发送
        content_bytes = len(formatted_text.encode('utf-8'))
        if content_bytes > self._max_bytes:
            logger.info(
                f"[Feishu Stream] 发送消息内容超长({content_bytes}字节)，将分批发送"
            )
            return self._send_to_chat_chunked(
                formatted_text,
                lambda chunk: self._send_interactive_card(
                    chunk,
                    chat_id=chat_id,
                    receive_id_type=receive_id_type,
                ),
            )

        # 单条消息，使用交互卡片
        return self._send_interactive_card(formatted_text, chat_id=chat_id, receive_id_type=receive_id_type)

    def _send_to_chat_chunked(self, content: str, send_func: Callable[[str], bool]) -> bool:
        """
        分批发送消息（支持交互卡片和分段发送）

        Args:
            content: 消息文本
            send_func: 发送单个分片的函数，返回是否发送成功

        Returns:
            是否全部发送成功
        """
        chunks = chunk_content_by_max_bytes(content, self._max_bytes, add_page_marker=True)
        success_count = 0
        for i, chunk in enumerate(chunks):
            if send_func(chunk):
                success_count += 1
            else:
                logger.error(f"[Feishu Stream] 发送消息失败: {chunk}")
            if i < len(chunks) - 1:
                time.sleep(1)
        return success_count == len(chunks)


class FeishuStreamHandler:
    """
    飞书 Stream 模式消息处理器

    将 SDK 的事件转换为统一的 BotMessage 格式，
    并调用命令分发器处理。
    """

    def __init__(
            self,
            on_message: Callable[[BotMessage], BotResponse],
            reply_client: FeishuReplyClient,
            assistant: Optional[object] = None,
            command_prefix: str = "/",
    ):
        """
        Args:
            on_message: 命令处理回调函数，接收 BotMessage 返回 BotResponse
            reply_client: 飞书回复客户端
            assistant: 助手服务（``ask(session_id, text) -> AssistantAnswer``）。
                传入后，不以命令前缀开头的消息走助手（先回占位卡片再原地更新）；
                不传则保持旧行为：所有消息交给 ``on_message``。
            command_prefix: 命令前缀，以此开头的消息交给 ``on_message``
        """
        self._on_message = on_message
        self._reply_client = reply_client
        self._assistant = assistant
        self._command_prefix = command_prefix or "/"
        self._logger = logger
        # Different conversations can run in parallel, but one conversation
        # must stay FIFO so multi-turn chat and replies do not get reordered.
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="feishu-msg")
        # Placeholders are posted right when a question arrives (not when its
        # turn in the conversation queue starts), on their own small pool.
        self._placeholder_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="feishu-ph")
        self._placeholders: dict[str, Future] = {}
        self._pending_messages: dict[str, deque[BotMessage]] = {}
        self._active_conversations: set[str] = set()
        self._queue_lock = threading.Lock()
        self._seen_message_ids: "OrderedDict[str, None]" = OrderedDict()
        self._bot_open_id: Optional[str] = None
        self._bot_open_id_loaded = False
        self._shutdown = False

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _is_command(self, bot_message: BotMessage) -> bool:
        return (bot_message.content or "").lstrip().startswith(self._command_prefix)

    def _goes_to_assistant(self, bot_message: BotMessage) -> bool:
        """Non-command text: always in p2p, only when the bot is @mentioned in groups."""
        if self._assistant is None or self._is_command(bot_message):
            return False
        if not (bot_message.content or "").strip():
            return False
        return bot_message.chat_type == ChatType.PRIVATE or bot_message.mentioned

    def _already_seen(self, message_id: str) -> bool:
        """Feishu redelivers events that were not acknowledged in time; answer each message once."""
        if not message_id:
            return False
        with self._queue_lock:
            if message_id in self._seen_message_ids:
                return True
            self._seen_message_ids[message_id] = None
            while len(self._seen_message_ids) > _SEEN_MESSAGE_LIMIT:
                self._seen_message_ids.popitem(last=False)
        return False

    def _ensure_bot_open_id(self) -> Optional[str]:
        if not self._bot_open_id_loaded:
            getter = getattr(self._reply_client, "get_bot_open_id", None)
            self._bot_open_id = getter() if callable(getter) else None
            self._bot_open_id_loaded = True
        return self._bot_open_id

    def _conversation_key(self, bot_message: BotMessage) -> str:
        """Return the ordering key used for per-conversation FIFO processing."""
        if bot_message.chat_type == ChatType.PRIVATE:
            return bot_message.chat_id or bot_message.user_id or bot_message.message_id

        chat_id = bot_message.chat_id or "unknown-chat"
        user_id = bot_message.user_id or "unknown-user"
        return f"{chat_id}:{user_id}"

    def _enqueue_message(self, bot_message: BotMessage) -> None:
        """Queue a message and start a worker when its conversation is idle."""
        if self._shutdown:
            self._logger.debug("[Feishu Stream] Handler already stopped, dropping message")
            return

        conversation_key = self._conversation_key(bot_message)
        should_start_worker = False

        with self._queue_lock:
            self._pending_messages.setdefault(conversation_key, deque()).append(bot_message)
            if conversation_key not in self._active_conversations:
                self._active_conversations.add(conversation_key)
                should_start_worker = True

        if should_start_worker:
            try:
                self._executor.submit(self._drain_conversation, conversation_key)
            except RuntimeError as exc:
                with self._queue_lock:
                    self._active_conversations.discard(conversation_key)
                    self._pending_messages.pop(conversation_key, None)
                self._logger.error("[Feishu Stream] 无法启动消息处理线程: %s", exc)

    def _drain_conversation(self, conversation_key: str) -> None:
        """Drain one conversation queue in FIFO order."""
        while True:
            with self._queue_lock:
                queue = self._pending_messages.get(conversation_key)
                if not queue:
                    self._pending_messages.pop(conversation_key, None)
                    self._active_conversations.discard(conversation_key)
                    return
                bot_message = queue.popleft()

            self._process_message(bot_message)

    def _post_placeholder(self, bot_message: BotMessage) -> Optional[str]:
        return self._reply_client.reply_card(bot_message.message_id, PLACEHOLDER_TEXT, updatable=True)

    def _event_ids(self, bot_message: BotMessage) -> dict:
        event = (bot_message.raw_data or {}).get("event") or {}
        return {
            "thread_id": event.get("thread_id") or None,
            "root_id": event.get("root_id") or None,
            "parent_id": event.get("parent_id") or None,
        }

    def _process_assistant_message(self, bot_message: BotMessage) -> None:
        """Placeholder card -> assistant turn -> patch the card with the answer."""
        from src.services import feishu_binding_service as binding

        chat_type = binding.CHAT_TYPE_P2P if bot_message.chat_type == ChatType.PRIVATE else binding.CHAT_TYPE_GROUP
        session_id = binding.resolve_session_id(
            chat_id=bot_message.chat_id, chat_type=chat_type, **self._event_ids(bot_message),
        )
        placeholder_id: Optional[str] = None
        future = self._placeholders.pop(bot_message.message_id, None)
        if future is not None:
            try:
                placeholder_id = future.result(timeout=30)
            except Exception as exc:
                self._logger.warning("[Feishu Stream] 占位卡片发送失败: %s", exc)
        # Replies to the user's message or to the placeholder continue this session.
        self._link_messages(bot_message.chat_id, session_id, [bot_message.message_id, placeholder_id])

        try:
            answer_text = self._assistant.ask(session_id, bot_message.content).text
        except Exception as exc:  # the service never raises; guard so the placeholder always resolves
            self._logger.exception("[Feishu Stream] 助手调用异常: %s", exc)
            answer_text = "分析失败，请稍后重试。"
        try:
            carried = self._reply_client.finish_card(placeholder_id, bot_message.message_id, answer_text)
        except Exception as exc:
            self._logger.error("[Feishu Stream] 发送答案失败: %s", exc)
            carried = []
        self._link_messages(bot_message.chat_id, session_id, carried)

    def _link_messages(self, chat_id: str, session_id: str, message_ids: list) -> None:
        try:
            from src.repositories.feishu_binding_repo import FeishuBindingRepository

            repo = FeishuBindingRepository()
            for message_id in message_ids:
                if message_id:
                    repo.remember_message_session(message_id, chat_id, session_id, kind="answer")
        except Exception as exc:  # linking only improves reply continuity; never block the answer
            self._logger.warning("[Feishu Stream] 记录消息与会话关联失败: %s", exc)

    def _process_message(self, bot_message: BotMessage) -> None:
        """Execute command / assistant handling off the SDK callback thread."""
        if self._goes_to_assistant(bot_message):
            try:
                self._process_assistant_message(bot_message)
            except Exception as e:
                self._logger.error(f"[Feishu Stream] 助手消息处理失败: {e}")
                self._logger.exception(e)
            return
        if self._assistant is not None and not self._is_command(bot_message):
            return  # group chatter without @bot: not for us
        try:
            response = self._on_message(bot_message)

            if response and response.text:
                self._reply_client.reply_text(
                    message_id=bot_message.message_id,
                    text=response.text,
                    at_user=response.at_user,
                    user_id=bot_message.user_id if response.at_user else None,
                )
        except Exception as e:
            self._logger.error(f"[Feishu Stream] 异步处理消息失败: {e}")
            self._logger.exception(e)

    @staticmethod
    def _truncate_log_content(text: str, max_len: int = 200) -> str:
        """截断日志内容"""
        cleaned = text.replace("\n", " ").strip()
        if len(cleaned) > max_len:
            return f"{cleaned[:max_len]}..."
        return cleaned

    def _log_incoming_message(self, message: BotMessage) -> None:
        """记录收到的消息日志"""
        content = message.raw_content or message.content or ""
        summary = self._truncate_log_content(content)
        self._logger.info(
            "[Feishu Stream] Incoming message: msg_id=%s user_id=%s "
            "chat_id=%s chat_type=%s content=%s",
            message.message_id,
            message.user_id,
            message.chat_id,
            getattr(message.chat_type, "value", message.chat_type),
            summary,
        )

    def handle_message(self, event: 'P2ImMessageReceiveV1') -> None:
        """
        处理接收到的消息事件

        Args:
            event: 飞书消息接收事件
        """
        try:
            # 解析消息
            bot_message = self._parse_event_message(event)

            if bot_message is None:
                return

            if self._already_seen(bot_message.message_id):
                self._logger.debug("[Feishu Stream] 忽略重复投递的消息: %s", bot_message.message_id)
                return

            self._log_incoming_message(bot_message)

            if self._goes_to_assistant(bot_message):
                try:
                    self._placeholders[bot_message.message_id] = self._placeholder_executor.submit(
                        self._post_placeholder, bot_message,
                    )
                except RuntimeError:
                    self._logger.debug("[Feishu Stream] Handler already stopped, dropping message")
                    return

            self._enqueue_message(bot_message)

        except Exception as e:
            self._logger.error(f"[Feishu Stream] 处理消息失败: {e}")
            self._logger.exception(e)

    def _parse_event_message(self, event: 'P2ImMessageReceiveV1') -> Optional[BotMessage]:
        """
        解析飞书事件消息为统一格式

        Args:
            event: P2ImMessageReceiveV1 事件对象
        """
        try:
            event_data = event.event
            if event_data is None:
                return None

            message_data = event_data.message
            sender_data = event_data.sender

            if message_data is None:
                return None

            # 只处理文本消息
            message_type = message_data.message_type or ""
            if message_type != "text":
                self._logger.debug(f"[Feishu Stream] 忽略非文本消息: {message_type}")
                return None

            # 解析消息内容
            content_str = message_data.content or "{}"
            try:
                content_json = json.loads(content_str)
                raw_content = content_json.get("text", "")
            except json.JSONDecodeError:
                raw_content = content_str

            # 提取命令（去除 @机器人）
            content = self._extract_command(raw_content, message_data.mentions)
            mentioned = "@" in raw_content or bool(message_data.mentions)
            if self._assistant is not None and message_data.mentions:
                # Only an @ of the bot itself counts; fall back to "any @" when its id is unknown.
                bot_open_id = self._ensure_bot_open_id()
                if bot_open_id:
                    mentioned = any(
                        getattr(getattr(m, "id", None), "open_id", None) == bot_open_id
                        for m in message_data.mentions
                    )

            # 获取发送者信息
            user_id = ""
            if sender_data and sender_data.sender_id:
                user_id = sender_data.sender_id.open_id or sender_data.sender_id.user_id or ""

            # 获取会话类型
            chat_type_str = message_data.chat_type or ""
            if chat_type_str == "group":
                chat_type = ChatType.GROUP
            elif chat_type_str == "p2p":
                chat_type = ChatType.PRIVATE
            else:
                chat_type = ChatType.UNKNOWN

            # 创建时间
            create_time = message_data.create_time
            try:
                if create_time:
                    timestamp = datetime.fromtimestamp(int(create_time) / 1000)
                else:
                    timestamp = datetime.now()
            except (ValueError, TypeError):
                timestamp = datetime.now()

            # 构建原始数据
            raw_data = {
                "header": {
                    "event_id": event.header.event_id if event.header else "",
                    "event_type": event.header.event_type if event.header else "",
                    "create_time": event.header.create_time if event.header else "",
                    "token": event.header.token if event.header else "",
                    "app_id": event.header.app_id if event.header else "",
                },
                "event": {
                    "message_id": message_data.message_id,
                    "chat_id": message_data.chat_id,
                    "chat_type": message_data.chat_type,
                    "content": message_data.content,
                    "root_id": getattr(message_data, "root_id", None),
                    "parent_id": getattr(message_data, "parent_id", None),
                    "thread_id": getattr(message_data, "thread_id", None),
                }
            }

            return BotMessage(
                platform="feishu",
                message_id=message_data.message_id or "",
                user_id=user_id,
                user_name=user_id,  # 飞书不直接返回用户名
                chat_id=message_data.chat_id or "",
                chat_type=chat_type,
                content=content,
                raw_content=raw_content,
                mentioned=mentioned,
                mentions=[m.key or "" for m in (message_data.mentions or [])],
                timestamp=timestamp,
                raw_data=raw_data,
            )

        except Exception as e:
            self._logger.error(f"[Feishu Stream] 解析消息失败: {e}")
            return None

    def _extract_command(self, text: str, mentions: list) -> str:
        """
        提取命令内容（去除 @机器人）

        飞书的 @用户 格式是：@_user_1, @_user_2 等

        Args:
            text: 原始消息文本
            mentions: @提及列表
        """
        import re

        # 方式1: 通过 mentions 列表移除（精确匹配）
        for mention in (mentions or []):
            key = getattr(mention, 'key', '') or ''
            if key:
                text = text.replace(key, '')

        # 方式2: 正则兜底，移除飞书 @用户 格式（@_user_N）
        # 当 mentions 为空或未正确传递时生效
        text = re.sub(r'@_user_\d+\s*', '', text)

        # 清理多余空格
        return ' '.join(text.split())

    def shutdown(self, wait: bool = False) -> None:
        """Stop accepting new messages and tear down worker threads."""
        self._shutdown = True
        with self._queue_lock:
            self._pending_messages.clear()
            self._active_conversations.clear()
        self._placeholders.clear()
        self._placeholder_executor.shutdown(wait=False)
        self._executor.shutdown(wait=wait)


class FeishuStreamClient:
    """
    飞书 Stream 模式客户端

    封装 lark-oapi SDK 的 WebSocket 客户端，提供简单的启动接口。

    使用方式：
        client = FeishuStreamClient()
        client.start()  # 阻塞运行

        # 或者在后台运行
        client.start_background()
    """

    def __init__(
            self,
            app_id: Optional[str] = None,
            app_secret: Optional[str] = None,
            domain: Optional[str] = None
    ):
        """
        Args:
            app_id: 应用 ID（不传则从配置读取）
            app_secret: 应用密钥（不传则从配置读取）
            domain: 飞书 API 区域（feishu/lark）或 SDK API 域名
        """
        if not FEISHU_SDK_AVAILABLE:
            raise ImportError(
                "lark-oapi SDK 未安装。\n"
                "请运行: pip install lark-oapi"
            )

        from src.config import get_config
        config = get_config()

        self._app_id = app_id or getattr(config, 'feishu_app_id', None)
        self._app_secret = app_secret or getattr(config, 'feishu_app_secret', None)
        configured_domain = (
            domain
            if domain is not None
            else getattr(config, 'feishu_domain', 'feishu')
        )
        self._domain = _resolve_feishu_domain(configured_domain)

        if not self._app_id or not self._app_secret:
            raise ValueError(
                "飞书 Stream 模式需要配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET"
            )

        self._ws_client: Optional[ws.Client] = None
        self._reply_client: Optional[FeishuReplyClient] = None
        self._message_handler: Optional[FeishuStreamHandler] = None
        self._background_thread: Optional[threading.Thread] = None
        self._running = False

    def _create_message_handler(self) -> Callable[[BotMessage], BotResponse]:
        """创建消息处理函数"""

        def handle_message(message: BotMessage) -> BotResponse:
            from bot.dispatcher import get_dispatcher
            dispatcher = get_dispatcher()
            return dispatcher.dispatch(message)

        return handle_message

    def _create_event_handler(self) -> 'lark.EventDispatcherHandler':
        """创建事件分发处理器"""
        # 创建回复客户端
        self._reply_client = FeishuReplyClient(
            self._app_id,
            self._app_secret,
            domain=self._domain,
        )

        # 创建消息处理器：命令走分发器，其余文本走助手
        from src.config import get_config
        config = get_config()
        from src.services.feishu_assistant_service import FeishuAssistantService

        handler = FeishuStreamHandler(
            self._create_message_handler(),
            self._reply_client,
            assistant=FeishuAssistantService(),
            command_prefix=getattr(config, 'bot_command_prefix', '/') or '/',
        )
        self._message_handler = handler

        # 创建并注册事件处理器
        # 注意：encrypt_key 和 verification_token 在长连接模式下不是必需的
        # 但 SDK 要求传入（可以为空字符串）

        encrypt_key = getattr(config, 'feishu_encrypt_key', '') or ''
        verification_token = getattr(config, 'feishu_verification_token', '') or ''

        event_handler = lark.EventDispatcherHandler.builder(
            encrypt_key=encrypt_key,
            verification_token=verification_token,
            level=lark.LogLevel.WARNING
        ).register_p2_im_message_receive_v1(
            handler.handle_message
        ).build()

        return event_handler

    def start(self) -> None:
        """
        启动 Stream 客户端（阻塞）

        此方法会阻塞当前线程，直到客户端停止。
        """
        logger.info("[Feishu Stream] 正在启动...")

        # 创建事件处理器
        event_handler = self._create_event_handler()

        # 创建 WebSocket 客户端
        self._ws_client = ws.Client(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
            domain=self._domain,
            log_level=lark.LogLevel.WARNING,
            auto_reconnect=True
        )

        self._running = True
        logger.info("[Feishu Stream] 客户端已启动，等待消息...")

        # 启动（阻塞）
        self._ws_client.start()

    def start_background(self) -> None:
        """
        在后台线程启动 Stream 客户端（非阻塞）

        适用于与其他服务（如 WebUI）同时运行的场景。
        """
        if self._background_thread and self._background_thread.is_alive():
            logger.warning("[Feishu Stream] 客户端已在运行")
            return

        self._running = True
        self._background_thread = threading.Thread(
            target=self._run_in_background,
            daemon=True,
            name="FeishuStreamClient"
        )
        self._background_thread.start()
        logger.info("[Feishu Stream] 后台客户端已启动")

    def _run_in_background(self) -> None:
        """后台运行（处理异常和重连）"""
        import time

        while self._running:
            try:
                self.start()
            except Exception as e:
                logger.error(f"[Feishu Stream] 运行异常: {e}")
                if self._running:
                    logger.info("[Feishu Stream] 5 秒后重连...")
                    time.sleep(5)

    def stop(self) -> None:
        """停止客户端"""
        self._running = False
        if self._message_handler is not None:
            self._message_handler.shutdown(wait=False)
        logger.info("[Feishu Stream] 客户端已停止")

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running


# 全局客户端实例
_stream_client: Optional[FeishuStreamClient] = None
_start_lock = threading.Lock()


def get_feishu_stream_client() -> Optional[FeishuStreamClient]:
    """获取全局 Stream 客户端实例"""
    global _stream_client

    if _stream_client is None and FEISHU_SDK_AVAILABLE:
        try:
            _stream_client = FeishuStreamClient()
        except (ImportError, ValueError) as e:
            logger.warning(f"[Feishu Stream] 无法创建客户端: {e}")
            return None

    return _stream_client


def start_feishu_stream_background() -> bool:
    """
    在后台启动飞书 Stream 客户端

    同一进程内只会运行一个客户端：已在运行时直接返回 True，不会重复建立长连接。

    Returns:
        是否成功启动（或已在运行）
    """
    with _start_lock:
        client = get_feishu_stream_client()
        if client:
            if client.is_running:
                logger.info("[Feishu Stream] 客户端已在运行，跳过重复启动")
                return True
            client.start_background()
            return True
        return False
