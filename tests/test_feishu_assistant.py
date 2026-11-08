# -*- coding: utf-8 -*-
"""Feishu assistant integration (offline): routing, sessions, cards, binding, pushes, memory.

Only the Feishu SDK / HTTP boundary and the LLM backend are faked; message
parsing, routing, persistence, notification dispatch and note handling run for real.
"""

import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bot.commands.bind import BindCommand, UnbindCommand
from bot.models import BotMessage, BotResponse, ChatType
from bot.platforms import feishu_stream
from bot.platforms.feishu_stream import PLACEHOLDER_TEXT, FeishuReplyClient, FeishuStreamHandler
from src.agent import assistant_memory
from src.config import Config
from src.notification import NotificationChannel, NotificationService
from src.notification_sender.feishu_sender import FeishuSender
from src.repositories.feishu_binding_repo import FeishuBindingRepository
from src.services import feishu_binding_service as binding
from src.services.assistant_note_service import AssistantNoteService
from src.services.feishu_assistant_service import AssistantAnswer, FeishuAssistantService
from src.storage import ConversationMessage, DatabaseManager


@pytest.fixture()
def db(tmp_path):
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url=f"sqlite:///{tmp_path / 'feishu.db'}")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def no_recall(monkeypatch):
    from src.agent.recall import RecallService
    from src.agent.recall.none_provider import NoneRecallProvider

    monkeypatch.setattr(assistant_memory, "get_recall_service", lambda config=None: RecallService(NoneRecallProvider()))


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _app_config(**overrides) -> Config:
    values = dict(feishu_app_id="cli_test_app", feishu_app_secret="test-secret")
    values.update(overrides)
    return Config(stock_list=[], **values)


# ---------------------------------------------------------------------------
# Binding table + target resolution order
# ---------------------------------------------------------------------------


def test_binding_is_single_and_replaced_by_rebind(db):
    repo = FeishuBindingRepository(db)
    assert repo.get_binding() is None

    first = binding.bind_chat("oc_a", "group", "ou_1", repo=repo)
    assert first["chat_id"] == "oc_a" and first["session_id"] == "feishu:oc_a"
    binding.bind_chat("oc_b", "p2p", "ou_2", repo=repo)

    current = repo.get_binding()
    assert (current["chat_id"], current["chat_type"], current["bound_by_open_id"]) == ("oc_b", "p2p", "ou_2")
    assert binding.unbind_chat(repo=repo) is True
    assert repo.get_binding() is None
    assert binding.unbind_chat(repo=repo) is False


def test_target_order_binding_then_chat_id_then_none(db):
    repo = FeishuBindingRepository(db)
    config = _app_config(feishu_chat_id="oc_cfg")

    target = binding.resolve_notification_target(config, repo=repo)
    assert (target.source, target.chat_id, target.receive_id_type) == ("config", "oc_cfg", "chat_id")

    binding.bind_chat("oc_bound", "group", repo=repo)
    target = binding.resolve_notification_target(config, repo=repo)
    assert (target.source, target.chat_id, target.session_id) == ("binding", "oc_bound", "feishu:oc_bound")

    binding.unbind_chat(repo=repo)
    assert binding.resolve_notification_target(_app_config(), repo=repo) is None
    # Without app credentials nothing resolves, even with a binding present.
    binding.bind_chat("oc_bound", "group", repo=repo)
    assert binding.resolve_notification_target(Config(stock_list=[], feishu_chat_id="oc_cfg"), repo=repo) is None


def test_open_id_config_target_has_no_known_session_until_send_response(db):
    config = _app_config(feishu_chat_id="ou_me", feishu_receive_id_type="open_id")
    target = binding.resolve_notification_target(config, repo=FeishuBindingRepository(db))
    assert (target.receive_id_type, target.session_id) == ("open_id", None)

    session_id = binding.record_push(
        target, "# 盘前版\n\n内容", route_type="report", sent=[("om_1", "oc_p2p_real")], db_manager=db,
    )
    assert session_id == "feishu:oc_p2p_real"


# ---------------------------------------------------------------------------
# Session mapping
# ---------------------------------------------------------------------------


def test_session_id_mapping(db):
    repo = FeishuBindingRepository(db)
    # p2p: one stable session per chat, even for messages carrying thread ids
    assert binding.resolve_session_id(chat_id="oc_p", chat_type="p2p", root_id="om_x", repo=repo) == "feishu:oc_p"
    # group: per thread when present, per chat otherwise
    assert binding.resolve_session_id(chat_id="oc_g", chat_type="group", repo=repo) == "feishu:oc_g"
    assert binding.resolve_session_id(chat_id="oc_g", chat_type="group", thread_id="omt_1", repo=repo) == "feishu:oc_g:omt_1"
    assert binding.resolve_session_id(chat_id="oc_g", chat_type="group", root_id="om_r", repo=repo) == "feishu:oc_g:om_r"


def test_reply_to_known_message_continues_its_session(db):
    repo = FeishuBindingRepository(db)
    repo.remember_message_session("om_push", "oc_g", "feishu:oc_g", kind="push")

    # a reply in a group thread rooted at the push joins the chat session the push was written into
    assert binding.resolve_session_id(
        chat_id="oc_g", chat_type="group", root_id="om_push", parent_id="om_push", repo=repo,
    ) == "feishu:oc_g"
    # parent wins over root
    repo.remember_message_session("om_answer", "oc_g", "feishu:oc_g:om_r", kind="answer")
    assert binding.resolve_session_id(
        chat_id="oc_g", chat_type="group", root_id="om_push", parent_id="om_answer", repo=repo,
    ) == "feishu:oc_g:om_r"


# ---------------------------------------------------------------------------
# Pushes: delivered by the bot app, recorded into the bound chat's session
# ---------------------------------------------------------------------------


class _FakeSdkClient:
    """Stands in for lark.Client at the HTTP boundary (message.create)."""

    def __init__(self, chat_id="oc_bound"):
        self.created = []
        self._chat_id = chat_id
        self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=self._create)))

    def _create(self, request):
        self.created.append(request)
        n = len(self.created)
        return SimpleNamespace(
            success=lambda: True, code=0, msg="ok", get_log_id=lambda: "log",
            data=SimpleNamespace(message_id=f"om_push{n}", chat_id=self._chat_id),
        )


def _notification_service(config, client):
    with mock.patch("src.notification.get_config", return_value=config):
        service = NotificationService()
    patcher = mock.patch.object(FeishuSender, "_ensure_app_client", return_value=client)
    patcher.start()
    return service, patcher


def test_push_goes_through_bot_app_and_is_recorded_into_session(db):
    binding.bind_chat("oc_bound", "group", "ou_1", repo=FeishuBindingRepository(db))
    config = _app_config()  # no FEISHU_CHAT_ID, no webhook: the binding is the only target
    client = _FakeSdkClient("oc_bound")
    service, patcher = _notification_service(config, client)
    try:
        assert NotificationChannel.FEISHU in service.get_available_channels()
        result = service.send_with_results("# 盘前版 2026-11-07\n\n关注 AI 算力", route_type="report")
    finally:
        patcher.stop()

    assert result.success and [item.channel for item in result.channel_results] == ["feishu"]
    assert len(client.created) == 1
    assert client.created[0].request_body.receive_id == "oc_bound"
    assert client.created[0].receive_id_type == "chat_id"

    with db.get_session() as session:
        rows = session.query(ConversationMessage).filter_by(session_id="feishu:oc_bound").all()
    assert len(rows) == 1 and rows[0].role == "assistant"
    assert "关注 AI 算力" in rows[0].content
    snapshot = json.loads(rows[0].context_snapshot)
    assert snapshot["kind"] == "notification" and snapshot["route_type"] == "report"
    assert snapshot["title"] == "盘前版 2026-11-07" and snapshot["time"]

    # the pushed message is linked to the session, and the push is in the follow-up's context
    assert FeishuBindingRepository(db).lookup_message_session("om_push1") == "feishu:oc_bound"
    from src.agent.chat_context import build_visible_chat_history

    history = build_visible_chat_history("feishu:oc_bound", None, SimpleNamespace(), allow_llm_compression=False, db_manager=db)
    assert history[-1]["role"] == "assistant" and "关注 AI 算力" in history[-1]["content"]


def test_alert_and_system_error_routes_are_recorded_with_their_route_type(db):
    binding.bind_chat("oc_bound", "p2p", repo=FeishuBindingRepository(db))
    client = _FakeSdkClient("oc_bound")
    service, patcher = _notification_service(_app_config(), client)
    try:
        service.send_with_results("告警：600519 跌破止损", route_type="alert")
        service.send_with_results("系统错误：数据源超时", route_type="system_error")
    finally:
        patcher.stop()

    with db.get_session() as session:
        rows = session.query(ConversationMessage).filter_by(session_id="feishu:oc_bound").order_by(ConversationMessage.id).all()
    assert [json.loads(row.context_snapshot)["route_type"] for row in rows] == ["alert", "system_error"]


def test_route_filter_still_applies(db):
    binding.bind_chat("oc_bound", "group", repo=FeishuBindingRepository(db))
    client = _FakeSdkClient()
    config = _app_config(notification_report_channels=["email"])  # feishu not routed for reports
    service, patcher = _notification_service(config, client)
    try:
        result = service.send_with_results("# 报告", route_type="report")
    finally:
        patcher.stop()
    assert client.created == []
    assert not result.success


def test_webhook_fallback_when_no_target_and_nothing_recorded(db):
    config = Config(stock_list=[], feishu_webhook_url="https://feishu.example/hook")
    with mock.patch("src.notification.get_config", return_value=config), \
            mock.patch("src.notification_sender.feishu_sender.requests.post") as post:
        post.return_value = mock.MagicMock(status_code=200, text="ok")
        post.return_value.json.return_value = {"code": 0}
        service = NotificationService()
        result = service.send_with_results("hello webhook", route_type="report")

    assert result.success
    post.assert_called_once()
    with db.get_session() as session:
        assert session.query(ConversationMessage).count() == 0


def test_binding_beats_feishu_chat_id_and_webhook(db):
    binding.bind_chat("oc_bound", "group", repo=FeishuBindingRepository(db))
    config = _app_config(feishu_chat_id="oc_cfg", feishu_webhook_url="https://feishu.example/hook")
    client = _FakeSdkClient("oc_bound")
    service, patcher = _notification_service(config, client)
    try:
        with mock.patch("src.notification_sender.feishu_sender.requests.post") as post:
            assert service.send_with_results("# 报告", route_type="report").success
    finally:
        patcher.stop()
    post.assert_not_called()
    assert client.created[0].request_body.receive_id == "oc_bound"


def test_record_failure_does_not_fail_a_delivered_push(db):
    binding.bind_chat("oc_bound", "group", repo=FeishuBindingRepository(db))
    client = _FakeSdkClient()
    service, patcher = _notification_service(_app_config(), client)
    try:
        with mock.patch.object(DatabaseManager, "save_chat_notification_message", side_effect=RuntimeError("db down")):
            assert service.send_with_results("# 报告", route_type="report").success
    finally:
        patcher.stop()


def test_config_test_send_is_not_recorded(db):
    binding.bind_chat("oc_bound", "group", repo=FeishuBindingRepository(db))
    sender = FeishuSender(_app_config())
    with mock.patch.object(FeishuSender, "_ensure_app_client", return_value=_FakeSdkClient()):
        assert sender.send_to_feishu("test message", record_in_session=False)
    with db.get_session() as session:
        assert session.query(ConversationMessage).count() == 0


def test_long_push_is_chunked_and_every_chunk_is_linked(db):
    binding.bind_chat("oc_bound", "group", repo=FeishuBindingRepository(db))
    client = _FakeSdkClient()
    config = _app_config(feishu_max_bytes=200)
    sender = FeishuSender(config)
    body = "\n".join(f"第{i}行 " + "内容" * 10 for i in range(30))
    with mock.patch.object(FeishuSender, "_ensure_app_client", return_value=client), \
            mock.patch("src.notification_sender.feishu_sender.time.sleep"):
        assert sender.send_to_feishu(body)
    assert len(client.created) > 1
    repo = FeishuBindingRepository(db)
    assert all(repo.lookup_message_session(f"om_push{i + 1}") == "feishu:oc_bound" for i in range(len(client.created)))
    with db.get_session() as session:
        assert session.query(ConversationMessage).filter_by(session_id="feishu:oc_bound").count() == 1


# ---------------------------------------------------------------------------
# Reply client: updatable card, patch in place, long answers
# ---------------------------------------------------------------------------


def _reply_client(max_bytes=20000):
    client = FeishuReplyClient.__new__(FeishuReplyClient)
    client._max_bytes = max_bytes
    client._client = mock.MagicMock()
    return client


def _ok(message_id=None):
    return SimpleNamespace(success=lambda: True, code=0, msg="ok", get_log_id=lambda: "l",
                           data=SimpleNamespace(message_id=message_id))


def _fail():
    return SimpleNamespace(success=lambda: False, code=230099, msg="fail", get_log_id=lambda: "l", data=None)


def test_card_json_is_updatable_only_when_requested():
    updatable = json.loads(FeishuReplyClient.build_card_json("hi", updatable=True))
    plain = json.loads(FeishuReplyClient.build_card_json("hi"))
    assert updatable["config"]["update_multi"] is True
    assert "update_multi" not in plain["config"]
    assert updatable["elements"][0]["text"] == {"tag": "lark_md", "content": "hi"}


def test_reply_card_returns_new_message_id_and_uses_interactive_reply():
    client = _reply_client()
    client._client.im.v1.message.reply.return_value = _ok("om_placeholder")

    assert client.reply_card("om_user", PLACEHOLDER_TEXT) == "om_placeholder"

    request = client._client.im.v1.message.reply.call_args.args[0]
    assert request.message_id == "om_user"
    assert request.request_body.msg_type == "interactive"
    assert json.loads(request.request_body.content)["config"]["update_multi"] is True
    client._client.im.v1.message.reply.return_value = _fail()
    assert client.reply_card("om_user", "x") is None


def test_patch_card_uses_message_patch_with_updatable_card(monkeypatch):
    client = _reply_client()
    client._client.im.v1.message.patch.return_value = _ok()

    assert client.patch_card("om_placeholder", "最终答案") is True

    request = client._client.im.v1.message.patch.call_args.args[0]
    assert request.message_id == "om_placeholder"
    card = json.loads(request.request_body.content)
    assert card["elements"][0]["text"]["content"] == "最终答案"
    assert card["config"]["update_multi"] is True


def test_patch_card_retries_once_then_gives_up(monkeypatch):
    monkeypatch.setattr(feishu_stream.time, "sleep", lambda _s: None)
    client = _reply_client()
    client._client.im.v1.message.patch.side_effect = [_fail(), _ok()]
    assert client.patch_card("om_p", "x") is True
    client._client.im.v1.message.patch.side_effect = [_fail(), _fail()]
    assert client.patch_card("om_p", "x") is False


def test_finish_card_patches_first_chunk_and_replies_the_rest(monkeypatch):
    monkeypatch.setattr(feishu_stream.time, "sleep", lambda _s: None)
    client = _reply_client(max_bytes=300)
    client._client.im.v1.message.patch.return_value = _ok()
    replies = iter(_ok(f"om_more{i}") for i in range(10))
    client._client.im.v1.message.reply.side_effect = lambda request: next(replies)
    text = "\n".join(f"## 小节{i}\n" + "分析内容" * 20 for i in range(8))

    carried = client.finish_card("om_placeholder", "om_user", text)

    assert carried[0] == "om_placeholder" and len(carried) > 1
    assert client._client.im.v1.message.patch.call_count == 1
    assert client._client.im.v1.message.reply.call_count == len(carried) - 1
    patched = json.loads(client._client.im.v1.message.patch.call_args.args[0].request_body.content)
    assert "小节0" in patched["elements"][0]["text"]["content"]
    for call in client._client.im.v1.message.reply.call_args_list:
        assert call.args[0].message_id == "om_user"
        card = json.loads(call.args[0].request_body.content)
        assert len(card["elements"][0]["text"]["content"].encode("utf-8")) <= 300 + 40


def test_finish_card_falls_back_to_reply_cards_when_patch_fails(monkeypatch):
    monkeypatch.setattr(feishu_stream.time, "sleep", lambda _s: None)
    client = _reply_client()
    client._client.im.v1.message.patch.return_value = _fail()
    client._client.im.v1.message.reply.return_value = _ok("om_fallback")

    assert client.finish_card("om_placeholder", "om_user", "答案") == ["om_fallback"]
    assert client._client.im.v1.message.patch.call_count == 2  # retried before falling back


def test_finish_card_without_placeholder_replies_directly():
    client = _reply_client()
    client._client.im.v1.message.reply.return_value = _ok("om_direct")
    assert client.finish_card(None, "om_user", "答案") == ["om_direct"]
    client._client.im.v1.message.patch.assert_not_called()


# ---------------------------------------------------------------------------
# Handler routing: command vs assistant vs bind; placeholder -> patch flow
# ---------------------------------------------------------------------------


class _FakeReplyClient:
    def __init__(self, *, patch_ok=True, bot_open_id="ou_bot"):
        self.calls = []
        self.patch_ok = patch_ok
        self.bot_open_id = bot_open_id
        self._lock = threading.Lock()
        self._n = 0

    def _record(self, kind, **kw):
        with self._lock:
            self.calls.append((kind, kw))

    def get_bot_open_id(self):
        return self.bot_open_id

    def reply_card(self, message_id, content, updatable=True):
        with self._lock:
            self._n += 1
            new_id = f"om_card{self._n}"
        self._record("reply_card", message_id=message_id, content=content, id=new_id)
        return new_id

    def finish_card(self, placeholder_id, message_id, text):
        self._record("finish_card", placeholder_id=placeholder_id, message_id=message_id, text=text)
        return [placeholder_id] if placeholder_id else []

    def reply_text(self, message_id, text, at_user=False, user_id=None):
        self._record("reply_text", message_id=message_id, text=text)
        return True

    def kinds(self):
        with self._lock:
            return [kind for kind, _ in self.calls]


class _FakeAssistant:
    def __init__(self, text="助手答案", success=True, delay=0.0):
        self.asked = []
        self._answer = AssistantAnswer(text, success=success)
        self._delay = delay

    def ask(self, session_id, text):
        self.asked.append((session_id, text))
        time.sleep(self._delay)
        return self._answer


def _event(text, *, message_id="om_1", chat_id="oc_c", chat_type="p2p", mentions=None,
           root_id=None, parent_id=None, thread_id=None, open_id="ou_user"):
    message = SimpleNamespace(
        message_id=message_id, chat_id=chat_id, chat_type=chat_type, message_type="text",
        content=json.dumps({"text": text}, ensure_ascii=False), mentions=mentions or [],
        root_id=root_id, parent_id=parent_id, thread_id=thread_id, create_time="1762500000000",
    )
    return SimpleNamespace(
        header=SimpleNamespace(event_id="e", event_type="im.message.receive_v1", create_time="1", token="t", app_id="a"),
        event=SimpleNamespace(message=message, sender=SimpleNamespace(
            sender_id=SimpleNamespace(open_id=open_id, user_id=None))),
    )


def _mention(open_id, key="@_user_1"):
    return SimpleNamespace(key=key, id=SimpleNamespace(open_id=open_id))


@pytest.fixture()
def handler_env(db):
    created = []

    def make(assistant=None, reply_client=None, on_message=None):
        reply = reply_client or _FakeReplyClient()
        commands = []

        def default_on_message(message):
            commands.append(message.content)
            return BotResponse.text_response(f"cmd:{message.content}")

        handler = FeishuStreamHandler(
            on_message or default_on_message, reply,
            assistant=assistant if assistant is not None else _FakeAssistant(),
        )
        created.append(handler)
        return handler, reply, commands

    yield make
    for handler in created:
        handler.shutdown(wait=True)


def test_p2p_plain_text_goes_to_assistant_with_placeholder_then_patch(handler_env):
    assistant = _FakeAssistant("最终答案")
    handler, reply, commands = handler_env(assistant=assistant)

    handler.handle_message(_event("宁德时代怎么看"))

    assert _wait_for(lambda: "finish_card" in reply.kinds())
    kinds = reply.kinds()
    assert kinds.index("reply_card") < kinds.index("finish_card")
    placeholder = reply.calls[kinds.index("reply_card")][1]
    assert placeholder["content"] == PLACEHOLDER_TEXT and placeholder["message_id"] == "om_1"
    finish = reply.calls[kinds.index("finish_card")][1]
    assert finish["placeholder_id"] == placeholder["id"] and finish["text"] == "最终答案"
    assert assistant.asked == [("feishu:oc_c", "宁德时代怎么看")]
    assert commands == []


def test_slash_command_goes_to_dispatcher_not_assistant(handler_env):
    assistant = _FakeAssistant()
    handler, reply, commands = handler_env(assistant=assistant)

    handler.handle_message(_event("/analyze 600519"))

    assert _wait_for(lambda: "reply_text" in reply.kinds())
    assert commands == ["/analyze 600519"] and assistant.asked == []
    assert "reply_card" not in reply.kinds()


def test_bind_command_reaches_dispatcher_and_binds_the_chat(handler_env, db):
    from bot.dispatcher import CommandDispatcher

    dispatcher = CommandDispatcher()
    dispatcher.register_class(BindCommand)
    dispatcher.register_class(UnbindCommand)
    handler, reply, _ = handler_env(on_message=dispatcher.dispatch)

    handler.handle_message(_event("/bind", message_id="om_b", chat_id="oc_group", chat_type="group"))
    assert _wait_for(lambda: FeishuBindingRepository(db).get_binding() is not None)
    stored = FeishuBindingRepository(db).get_binding()
    assert (stored["chat_id"], stored["chat_type"], stored["bound_by_open_id"]) == ("oc_group", "group", "ou_user")
    assert _wait_for(lambda: "reply_text" in reply.kinds())
    assert "已将当前群聊设为通知目标" in reply.calls[reply.kinds().index("reply_text")][1]["text"]

    handler.handle_message(_event("/unbind", message_id="om_u", chat_id="oc_group", chat_type="group"))
    assert _wait_for(lambda: FeishuBindingRepository(db).get_binding() is None)


def test_group_message_needs_bot_mention_for_assistant(handler_env):
    assistant = _FakeAssistant()
    handler, reply, _ = handler_env(assistant=assistant)

    handler.handle_message(_event("大家好", message_id="om_1", chat_id="oc_g", chat_type="group"))
    handler.handle_message(_event("@_user_1 你觉得呢", message_id="om_2", chat_id="oc_g", chat_type="group",
                                  mentions=[_mention("ou_someone_else")]))
    time.sleep(0.2)
    assert assistant.asked == [] and reply.calls == []

    handler.handle_message(_event("@_user_1 你觉得呢", message_id="om_3", chat_id="oc_g", chat_type="group",
                                  mentions=[_mention("ou_bot")]))
    assert _wait_for(lambda: "finish_card" in reply.kinds())
    assert assistant.asked == [("feishu:oc_g", "你觉得呢")]


def test_group_thread_and_reply_to_push_session_ids(handler_env, db):
    FeishuBindingRepository(db).remember_message_session("om_push", "oc_g", "feishu:oc_g", kind="push")
    assistant = _FakeAssistant()
    handler, reply, _ = handler_env(assistant=assistant)
    mention = [_mention("ou_bot")]

    handler.handle_message(_event("@_user_1 第一问", message_id="om_1", chat_id="oc_g", chat_type="group", mentions=mention))
    handler.handle_message(_event("@_user_1 话题内追问", message_id="om_2", chat_id="oc_g", chat_type="group",
                                  mentions=mention, thread_id="omt_9", root_id="om_t"))
    handler.handle_message(_event("@_user_1 就这条推送追问", message_id="om_3", chat_id="oc_g", chat_type="group",
                                  mentions=mention, root_id="om_push", parent_id="om_push"))
    assert _wait_for(lambda: len(assistant.asked) == 3)
    assert sorted(assistant.asked) == sorted([
        ("feishu:oc_g", "第一问"),
        ("feishu:oc_g:omt_9", "话题内追问"),
        ("feishu:oc_g", "就这条推送追问"),
    ])


def test_answer_messages_are_linked_to_the_session(handler_env, db):
    handler, reply, _ = handler_env(assistant=_FakeAssistant())
    handler.handle_message(_event("问题", message_id="om_user", chat_id="oc_g", chat_type="group",
                                  mentions=[_mention("ou_bot")]))
    assert _wait_for(lambda: "finish_card" in reply.kinds())
    repo = FeishuBindingRepository(db)
    assert _wait_for(lambda: repo.lookup_message_session("om_card1") == "feishu:oc_g")
    assert repo.lookup_message_session("om_user") == "feishu:oc_g"


def test_assistant_failure_text_is_patched_into_the_placeholder(handler_env):
    assistant = _FakeAssistant("分析失败：模型超时", success=False)
    handler, reply, _ = handler_env(assistant=assistant)
    handler.handle_message(_event("问题"))
    assert _wait_for(lambda: "finish_card" in reply.kinds())
    finish = reply.calls[reply.kinds().index("finish_card")][1]
    assert finish["text"] == "分析失败：模型超时" and finish["placeholder_id"] == "om_card1"


def test_assistant_exception_still_resolves_the_placeholder(handler_env):
    class _Boom:
        def ask(self, session_id, text):
            raise RuntimeError("boom")

    handler, reply, _ = handler_env(assistant=_Boom())
    handler.handle_message(_event("问题"))
    assert _wait_for(lambda: "finish_card" in reply.kinds())
    finish = reply.calls[reply.kinds().index("finish_card")][1]
    assert finish["placeholder_id"] == "om_card1" and "分析失败" in finish["text"]


def test_duplicate_event_delivery_is_answered_once(handler_env):
    assistant = _FakeAssistant()
    handler, reply, _ = handler_env(assistant=assistant)
    handler.handle_message(_event("问题", message_id="om_dup"))
    handler.handle_message(_event("问题", message_id="om_dup"))
    assert _wait_for(lambda: "finish_card" in reply.kinds())
    time.sleep(0.2)
    assert len(assistant.asked) == 1 and reply.kinds().count("reply_card") == 1


def test_second_question_gets_its_placeholder_immediately_while_first_runs(handler_env):
    assistant = _FakeAssistant(delay=0.5)
    handler, reply, _ = handler_env(assistant=assistant)
    handler.handle_message(_event("第一问", message_id="om_1"))
    handler.handle_message(_event("第二问", message_id="om_2"))
    assert _wait_for(lambda: reply.kinds().count("reply_card") == 2, timeout=0.4)
    assert reply.kinds().count("finish_card") == 0
    assert _wait_for(lambda: reply.kinds().count("finish_card") == 2, timeout=5)
    # per-conversation FIFO: answers are produced in order
    assert [q for _, q in assistant.asked] == ["第一问", "第二问"]


def test_without_assistant_legacy_routing_is_unchanged():
    reply = _FakeReplyClient()
    seen = []
    handler = FeishuStreamHandler(lambda m: (seen.append(m.content), BotResponse.text_response("ok"))[1], reply)
    try:
        handler.handle_message(_event("随便聊聊"))
        assert _wait_for(lambda: seen)
    finally:
        handler.shutdown(wait=True)
    assert seen == ["随便聊聊"]


# ---------------------------------------------------------------------------
# Assistant service: same path as the Web chat (real session state, fake backend)
# ---------------------------------------------------------------------------


def _fake_chat_executor(answer="OK 答案", success=True):
    from src.agent.agent_backend import AgentRunResult
    from src.agent.chat_executor import AgentChatExecutor

    backend = mock.MagicMock()
    backend.backend_id = "litellm"
    backend.runtime_owns_loop = False
    backend.run.return_value = AgentRunResult(
        success=success, final_answer=answer if success else "", backend="litellm",
        error_message=None if success else "model_failed", error_code=None, messages=[],
    )
    executor = AgentChatExecutor(backend=backend, config=SimpleNamespace(), context_llm_adapter=None)
    return executor, backend


def _service(db, **config):
    values = dict(report_language="zh", assistant_note_daily_proposal_limit=2, assistant_note_inject_max_chars=3000)
    values.update(config)
    return FeishuAssistantService(config=SimpleNamespace(**values), db_manager=db)


def test_assistant_service_runs_the_web_chat_path_and_persists_the_session(db):
    executor, backend = _fake_chat_executor("这是答案")
    service = _service(db)
    with mock.patch("api.v1.endpoints.agent._build_executor", return_value=executor), \
            mock.patch("api.v1.endpoints.agent._select_agent_chat_backend", return_value="litellm"):
        first = service.ask("feishu:oc_p", "帮我看看大盘")
        second = service.ask("feishu:oc_p", "再说说风险")

    assert first == AssistantAnswer("这是答案") and second.success
    messages = db.get_conversation_messages("feishu:oc_p")
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    # second turn saw the first as history, with market phase / notes system prompt built by the shared path
    request = backend.run.call_args_list[1].args[0]
    assert any(m["content"] == "帮我看看大盘" for m in request.history_messages)
    assert "数据时效" in request.system_prompt


def test_assistant_service_reports_failure_and_unavailable(db):
    from fastapi import HTTPException

    executor, _ = _fake_chat_executor(success=False)
    service = _service(db)
    with mock.patch("api.v1.endpoints.agent._build_executor", return_value=executor), \
            mock.patch("api.v1.endpoints.agent._select_agent_chat_backend", return_value="litellm"):
        failed = service.ask("feishu:oc_p", "问题")
    assert not failed.success and "model_failed" in failed.text

    unavailable = HTTPException(status_code=400, detail={"error": "capability_unsupported", "message": "no_agent_primary"})
    with mock.patch("api.v1.endpoints.agent._select_agent_chat_backend", side_effect=unavailable):
        answer = service.ask("feishu:oc_p", "问题")
    assert not answer.success and "no_agent_primary" in answer.text

    with mock.patch("api.v1.endpoints.agent._select_agent_chat_backend", side_effect=RuntimeError("x")):
        assert service.ask("feishu:oc_p", "问题") == AssistantAnswer("分析失败，请稍后重试。", success=False, kind="error")


def test_push_in_session_is_visible_to_the_next_assistant_turn(db):
    binding.bind_chat("oc_bound", "p2p", repo=FeishuBindingRepository(db))
    target = binding.resolve_notification_target(_app_config(), repo=FeishuBindingRepository(db))
    binding.record_push(target, "# 17:00 板块异动\n\n半导体首次启动", route_type="report", db_manager=db)

    executor, backend = _fake_chat_executor()
    service = _service(db)
    with mock.patch("api.v1.endpoints.agent._build_executor", return_value=executor), \
            mock.patch("api.v1.endpoints.agent._select_agent_chat_backend", return_value="litellm"):
        assert service.ask("feishu:oc_bound", "半导体为什么启动？").success

    history = backend.run.call_args.args[0].history_messages
    assert history[0]["role"] == "assistant" and "半导体首次启动" in history[0]["content"]


# ---------------------------------------------------------------------------
# 「记住」 / 「确认」 / 「取消」
# ---------------------------------------------------------------------------


def test_remember_creates_proposal_and_confirm_writes_note(db):
    service = _service(db, assistant_note_daily_proposal_limit=0)  # limit must not apply to user-initiated notes
    answer = service.ask("feishu:oc_p", "记住：不碰 ST 股")

    assert answer.kind == "memory" and "回复「确认」" in answer.text and "不碰 ST 股" in answer.text
    notes = AssistantNoteService(db)
    assert notes.get_page("global") is None  # nothing written before confirmation
    pending = notes.list_proposals("pending")
    assert len(pending) == 1 and pending[0]["origin_session_id"] == "feishu:oc_p" and pending[0]["source"] == "user_said"

    confirmed = service.ask("feishu:oc_p", "确认")
    assert confirmed.kind == "memory" and "已保存" in confirmed.text
    assert "不碰 ST 股" in notes.get_page("global")["body_md"]
    assert notes.list_proposals("pending") == []

    # a second 「记住」 appends to the existing page
    service.ask("feishu:oc_p", "记住 只做 20 日线之上的票")
    service.ask("feishu:oc_p", "确认。")
    body = notes.get_page("global")["body_md"]
    assert body.count("\n- ") == 1 and "不碰 ST 股" in body and "20 日线" in body


def test_cancel_rejects_proposal_without_touching_the_page(db):
    service = _service(db)
    service.ask("feishu:oc_p", "记住：仓位不超过五成")
    answer = service.ask("feishu:oc_p", "取消")
    assert "已取消" in answer.text
    assert AssistantNoteService(db).get_page("global") is None
    assert AssistantNoteService(db).list_proposals("pending") == []


def test_confirm_without_pending_proposal_is_an_ordinary_message(db):
    service = _service(db)
    with mock.patch.object(FeishuAssistantService, "_run_agent", return_value=AssistantAnswer("普通回答")) as run_agent:
        assert service.ask("feishu:oc_p", "确认").text == "普通回答"
        run_agent.assert_called_once()

    service.ask("feishu:oc_a", "记住：偏好 A")
    with mock.patch.object(FeishuAssistantService, "_run_agent", return_value=AssistantAnswer("普通回答")):
        # a proposal of another session cannot be confirmed from this one
        assert service.ask("feishu:oc_b", "确认").text == "普通回答"
    assert len(AssistantNoteService(db).list_proposals("pending")) == 1


def test_stale_proposal_cannot_be_confirmed(db):
    from datetime import datetime, timedelta

    service = _service(db)
    service.ask("feishu:oc_p", "记住：旧提议")
    with mock.patch("src.services.feishu_assistant_service.datetime") as fake_dt:
        fake_dt.now.return_value = datetime.now() + timedelta(hours=2)
        fake_dt.fromisoformat = datetime.fromisoformat
        with mock.patch.object(FeishuAssistantService, "_run_agent", return_value=AssistantAnswer("普通回答")):
            assert service.ask("feishu:oc_p", "确认").text == "普通回答"
    assert AssistantNoteService(db).get_page("global") is None


def test_remember_about_active_stock_targets_its_page(db):
    service = _service(db)
    detail = SimpleNamespace(active_stock_context={"stock_code": "300750", "stock_name": "宁德时代"})
    with mock.patch("src.services.agent_chat_session_service.AgentChatSessionService.get_session_detail", return_value=detail):
        about_stock = service.ask("feishu:oc_p", "记住：宁德时代等年报再动")
        unrelated = service.ask("feishu:oc_p", "记住：以后别推荐 ST")

    pending = {p["subject"]: p for p in AssistantNoteService(db).list_proposals("pending")}
    assert set(pending) == {"300750", None}
    assert pending["300750"]["scope"] == "stock" and pending["300750"]["title"] == "宁德时代 300750"
    assert "宁德时代 300750" in about_stock.text and "全局" in unrelated.text


@pytest.mark.parametrize("text", ["记住了", "记住吗？", "记住"])
def test_remember_lookalikes_are_not_memory_requests(db, text):
    service = _service(db)
    with mock.patch.object(FeishuAssistantService, "_run_agent", return_value=AssistantAnswer("普通回答")):
        assert service.ask("feishu:oc_p", text).text == "普通回答"
    assert AssistantNoteService(db).list_proposals("pending") == []


# ---------------------------------------------------------------------------
# Bind / unbind commands (direct)
# ---------------------------------------------------------------------------


def _bot_message(platform="feishu", chat_type=ChatType.GROUP, chat_id="oc_g"):
    return BotMessage(platform=platform, message_id="om", user_id="ou_u", user_name="ou_u",
                      chat_id=chat_id, chat_type=chat_type, content="/bind")


def test_bind_command_confirms_replacement_and_warns_without_credentials(db):
    with mock.patch("src.config.get_config", return_value=SimpleNamespace(feishu_app_id="", feishu_app_secret="")):
        BindCommand().execute(_bot_message(chat_id="oc_old"), [])
        response = BindCommand().execute(_bot_message(chat_type=ChatType.PRIVATE, chat_id="oc_new"), [])
    assert "当前私聊" in response.text and "原绑定的群聊已被替换" in response.text and "FEISHU_APP_ID" in response.text
    assert FeishuBindingRepository(db).get_binding()["chat_id"] == "oc_new"

    assert "已取消绑定" in UnbindCommand().execute(_bot_message(), []).text
    assert "没有绑定" in UnbindCommand().execute(_bot_message(), []).text
    assert "仅支持飞书" in BindCommand().execute(_bot_message(platform="dingtalk"), []).text


def test_bind_commands_are_registered_in_the_dispatcher():
    from bot.dispatcher import get_dispatcher, reset_dispatcher

    reset_dispatcher()
    try:
        dispatcher = get_dispatcher()
        assert dispatcher.get_command("bind") is not None and dispatcher.get_command("unbind") is not None
    finally:
        reset_dispatcher()


# ---------------------------------------------------------------------------
# Single client per process
# ---------------------------------------------------------------------------


def test_start_background_is_idempotent_for_a_running_client():
    class _Client:
        def __init__(self):
            self.is_running = False
            self.starts = 0

        def start_background(self):
            self.starts += 1
            self.is_running = True

    client = _Client()
    with mock.patch.object(feishu_stream, "get_feishu_stream_client", return_value=client):
        assert feishu_stream.start_feishu_stream_background() is True
        assert feishu_stream.start_feishu_stream_background() is True
    assert client.starts == 1


def test_main_start_feishu_stream_client_respects_flag():
    import main

    with mock.patch("bot.platforms.start_feishu_stream_background", return_value=True) as start:
        main.start_feishu_stream_client(SimpleNamespace(feishu_stream_enabled=False))
        start.assert_not_called()
        main.start_feishu_stream_client(SimpleNamespace(feishu_stream_enabled=True))
        start.assert_called_once_with()


def test_serve_bot_clients_use_the_same_feishu_starter():
    import main

    config = SimpleNamespace(dingtalk_stream_enabled=False, feishu_stream_enabled=True)
    with mock.patch("main.start_feishu_stream_client") as start:
        main.start_bot_stream_clients(config)
    start.assert_called_once_with(config)


# ---------------------------------------------------------------------------
# Self-check script (mocked client)
# ---------------------------------------------------------------------------


def _load_check_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "feishu_check.py"
    spec = importlib.util.spec_from_file_location("feishu_check_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _CheckClient:
    def __init__(self, token_ok=True, bot_ok=True):
        token_response = SimpleNamespace(success=lambda: token_ok, code=0 if token_ok else 10003, msg="invalid param")
        self.auth = SimpleNamespace(v3=SimpleNamespace(
            tenant_access_token=SimpleNamespace(internal=lambda request: token_response)))
        payload = json.dumps({"code": 0, "bot": {"app_name": "股票助手", "open_id": "ou_bot_open_id"}}).encode()
        self._bot = SimpleNamespace(success=lambda: bot_ok, code=0 if bot_ok else 99, msg="no bot",
                                    raw=SimpleNamespace(content=payload))

    def request(self, request):
        assert request.uri == "/open-apis/bot/v3/info"
        return self._bot


def test_check_script_all_pass_prints_no_secrets(db):
    script = _load_check_script()
    binding.bind_chat("oc_bound_chat_id", "group", repo=FeishuBindingRepository(db))
    config = _app_config(feishu_app_secret="SUPER-SECRET-VALUE")
    lines = []
    sender = mock.MagicMock()
    sender.send_to_feishu.return_value = True

    code = script.run_checks(
        config, listen_seconds=3, send_test=True, out=lines.append,
        client_factory=lambda *a: _CheckClient(), listener=lambda *a: 2,
        sender_factory=lambda cfg: sender,
    )

    text = "\n".join(lines)
    assert code == 0 and "结果：全部通过" in text
    assert "股票助手" in text and "binding" in text and "收到 2 条消息事件" in text
    assert "SUPER-SECRET-VALUE" not in text and "cli_test_app" not in text and "oc_bound_chat_id" not in text
    sender.send_to_feishu.assert_called_once()
    assert sender.send_to_feishu.call_args.kwargs == {"record_in_session": False}


def test_check_script_reports_credential_failure_and_missing_target(db):
    script = _load_check_script()
    lines = []
    code = script.run_checks(_app_config(), out=lines.append, client_factory=lambda *a: _CheckClient(token_ok=False))
    text = "\n".join(lines)
    assert code == 1 and "code=10003" in text and "跳过（凭据未通过）" in text and "未解析到推送目标" in text

    lines.clear()
    code = script.run_checks(Config(stock_list=[]), out=lines.append)
    assert code == 1 and "未配置 FEISHU_APP_ID" in "\n".join(lines)


def test_check_script_listen_without_events_fails_and_webhook_target_is_noted(db):
    script = _load_check_script()
    lines = []
    config = _app_config(feishu_webhook_url="https://feishu.example/hook")
    code = script.run_checks(config, listen_seconds=1, out=lines.append,
                             client_factory=lambda *a: _CheckClient(), listener=lambda *a: 0)
    text = "\n".join(lines)
    assert code == 1 and "未收到事件" in text and "Webhook（兼容方式" in text
