# -*- coding: utf-8 -*-
"""Multi-user P3a: personal push channels (``/api/v1/me/notification-channels``).

Real app, router, services, senders and SQLite over a temp directory. Only the lowest HTTP layer (``FakeHTTP``) and DNS
(``stub_dns``) are doubled, so the SSRF / no-redirect hook, the encryption and the existing senders all run for real.
"""

from __future__ import annotations

import json
import smtplib
import socket
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from src.notification_sender.email_sender import EmailSender
from src.services import user_notification_service as notification_module
from src.services.user_notification_service import (
    CHANNEL_TYPES,
    UserNotificationService,
    build_user_notifier,
    notifier_for_user,
    push_to_user,
)
from src.storage import UserNotificationChannel
from src.utils.safe_requests import BlockedRequestError, user_outbound_requests
from src.utils.url_safety import UnsafeUrlError
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    stub_dns,
    teardown_multi_user_env,
)
from tests.p3a_helpers import (
    CHANNEL_HOSTS,
    CONFIGS,
    CUSTOM,
    EMAIL,
    FEISHU,
    SYSTEM_CHANNEL_ENV,
    FakeHTTP,
    create_channel,
    redirect_to,
)

BASE = "/api/v1/me/notification-channels"
MASK = "******"


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    built.dns = stub_dns(monkeypatch)
    built.http = FakeHTTP().install(monkeypatch)
    monkeypatch.setattr(notification_module, "TEST_SEND_MIN_INTERVAL_SECONDS", 0.0)
    notification_module._last_test_at.clear()
    yield built
    teardown_multi_user_env()


def _rows(app, user_id):
    with app.db.get_session() as session:
        return session.execute(
            select(UserNotificationChannel).where(UserNotificationChannel.user_id == user_id)
        ).scalars().all()


# ------------------------------------------------------------------ CRUD, masking, ownership


def test_create_list_update_delete_roundtrip_with_masked_secrets(app):
    client = app.as_user(USER_A)

    created = create_channel(client, "feishu", name="工作群")
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["channel_type"] == "feishu" and item["name"] == "工作群" and item["enabled"] is True
    assert item["source"] == "user" and item["editable"] is True and item["readable"] is True
    # secrets (the webhook URL carries the token) are masked; plain fields are shown
    assert item["config"] == {"webhook_url": MASK, "secret": MASK, "keyword": "股票"}
    assert FEISHU["webhook_url"] not in created.text and FEISHU["secret"] not in created.text

    listing = client.get(BASE).json()
    assert listing["is_primary"] is False and listing["managed_in_system_settings"] is False
    assert [c["id"] for c in listing["channels"]] == [item["id"]]
    assert listing["max_channels"] == 10
    assert {t["channel_type"] for t in listing["channel_types"]} == set(CHANNEL_TYPES)
    assert FEISHU["webhook_url"] not in json.dumps(listing)

    updated = client.put(f"{BASE}/{item['id']}", json={"name": "新名字", "enabled": False, "config": {"keyword": "行情"}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "新名字" and updated.json()["enabled"] is False
    assert updated.json()["config"]["keyword"] == "行情" and updated.json()["config"]["webhook_url"] == MASK

    after = client.delete(f"{BASE}/{item['id']}")
    assert after.status_code == 200 and after.json()["channels"] == []
    assert _rows(app, USER_A) == []


def test_the_stored_config_is_encrypted_with_the_owners_data_key_only(app):
    item = create_channel(app.as_user(USER_A), "custom").json()
    create_channel(app.as_user(USER_B), "custom")
    [row] = _rows(app, USER_A)
    assert row.id == item["id"]
    # no plaintext of the webhook / token in the database row
    assert "hooks.example.org" not in row.config_encrypted and "bearer-secret" not in row.config_encrypted

    from src.user_secrets import UserSecretError, UserSecretStore

    store = UserSecretStore()
    assert json.loads(store.decrypt(USER_A, row.config_encrypted))["webhook_url"] == CUSTOM["webhook_url"]
    with pytest.raises(UserSecretError):  # B's data key cannot open A's channel
        store.decrypt(USER_B, row.config_encrypted)


def test_empty_or_masked_secret_on_update_keeps_the_current_value(app, monkeypatch):
    client = app.as_user(USER_A)
    channel_id = create_channel(client, "custom").json()["id"]

    for blank in ({"webhook_url": "", "bearer_token": ""}, {"webhook_url": MASK, "bearer_token": MASK}, {}):
        response = client.put(f"{BASE}/{channel_id}", json={"config": blank})
        assert response.status_code == 200, response.text

    # a real send still goes to the original URL with the original token
    assert client.post(f"{BASE}/{channel_id}/test").json()["success"] is True
    [call] = app.http.calls_to("hooks.example.org")
    assert call.url == CUSTOM["webhook_url"] and call.headers["Authorization"] == "Bearer bearer-secret"

    # a new secret value replaces it; a plain optional field can be cleared with ""
    client.put(f"{BASE}/{channel_id}", json={"config": {"bearer_token": "rotated", "body_template": ""}})
    app.http.reset()
    client.post(f"{BASE}/{channel_id}/test")
    assert app.http.calls[0].headers["Authorization"] == "Bearer rotated"
    assert json.loads(app.http.calls[0].body).keys() >= {"text", "content"}  # default payload: the template was cleared


def test_a_foreign_channel_id_answers_exactly_like_a_missing_one(app):
    owned = create_channel(app.as_user(USER_A), "feishu").json()["id"]
    client = app.as_user(USER_B)
    calls = (
        ("put", {"json": {"name": "x"}}), ("delete", {}), ("post", {"suffix": "/test"}),
    )
    for method, options in calls:
        suffix = options.get("suffix", "")
        kwargs = {"json": options["json"]} if "json" in options else {}
        foreign = getattr(client, method)(f"{BASE}/{owned}{suffix}", **kwargs)
        missing = getattr(client, method)(f"{BASE}/999999{suffix}", **kwargs)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json() and foreign.json()["error"] == "channel_not_found"
    # untouched, and invisible in B's list
    assert client.get(BASE).json()["channels"] == []
    assert app.as_user(USER_A).get(BASE).json()["channels"][0]["name"] != "x"
    assert app.http.calls == []  # B's attempted test sent nothing


def test_channel_count_limit_is_per_user_and_configurable(app, monkeypatch):
    from src.config import get_config

    monkeypatch.setattr(get_config(), "notification_channels_max_per_user", 2)
    client = app.as_user(USER_A)
    assert create_channel(client, "wecom").status_code == 201
    assert create_channel(client, "wecom").status_code == 201
    third = create_channel(client, "wecom")
    assert third.status_code == 400 and third.json()["error"] == "channel_limit_reached"
    # another user has their own allowance; deleting frees a slot
    assert create_channel(app.as_user(USER_B), "wecom").status_code == 201
    first = client.get(BASE).json()["channels"][0]["id"]
    client.delete(f"{BASE}/{first}")
    assert create_channel(client, "wecom").status_code == 201


def test_validation_errors_name_fields_but_never_echo_values(app):
    client = app.as_user(USER_A)
    unknown_type = client.post(BASE, json={"channel_type": "discord", "config": {}})
    assert unknown_type.status_code == 400 and unknown_type.json()["error"] == "invalid_channel_type"

    missing = create_channel(client, "telegram", {"chat_id": "-1001"})
    assert missing.status_code == 400 and missing.json()["error"] == "invalid_config"
    assert {i["key"] for i in missing.json()["detail"]["issues"]} == {"bot_token"}

    bad_token = create_channel(client, "telegram", {"bot_token": "not a token SECRETVALUE", "chat_id": "-1001"})
    assert bad_token.status_code == 400 and "SECRETVALUE" not in bad_token.text

    extra = create_channel(client, "wecom", {**CONFIGS["wecom"], "unexpected": "x"})
    assert extra.status_code == 400 and extra.json()["detail"]["issues"][0]["code"] == "unknown_field"

    assert create_channel(client, "email", {**EMAIL, "receivers": [f"u{i}@x.example.com" for i in range(6)]}).status_code == 400
    assert create_channel(client, "email", {**EMAIL, "sender": "not-an-address"}).status_code == 400
    assert client.get(BASE).json()["channels"] == []


def test_primary_user_lists_env_channels_read_only_and_cannot_write(app, monkeypatch):
    from src.config import Config

    for key, value in SYSTEM_CHANNEL_ENV.items():
        monkeypatch.setenv(key, value)
    Config.reset_instance()
    client = app.as_user(PRIMARY_ID)

    listing = client.get(BASE)
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["is_primary"] is True and payload["managed_in_system_settings"] is True
    assert "系统设置" in payload["note"]
    types = {c["channel_type"] for c in payload["channels"]}
    assert {"wechat", "feishu", "telegram", "email"} <= types
    assert all(c["source"] == "env" and c["editable"] is False and c["id"] is None for c in payload["channels"])
    # types only: not a single secret or address of the system channels
    for secret in list(SYSTEM_CHANNEL_ENV.values()) + ["sys-wecom.example.net"]:
        assert secret not in listing.text

    for response in (
        create_channel(client, "feishu"),
        client.put(f"{BASE}/1", json={"name": "x"}),
        client.delete(f"{BASE}/1"),
        client.post(f"{BASE}/1/test"),
    ):
        assert response.status_code == 403 and response.json()["error"] == "primary_user_uses_env"
    assert _rows(app, PRIMARY_ID) == []
    assert app.http.calls == []


# ------------------------------------------------------------------ SSRF at save time


@pytest.mark.parametrize("url", [
    "http://hooks.example.org/in/xyz",              # not https
    "https://127.0.0.1/hook",                       # loopback
    "https://169.254.169.254/latest/meta-data/",    # cloud metadata
    "https://10.0.0.5/hook",                        # private
    "https://localhost/hook",
    "https://intranet/hook",                        # not a fully qualified public name
    "https://box.internal/hook",                    # internal suffix
    "https://user:pw@hooks.example.org/in",         # credentials
    "https://2130706433/hook",                      # numeric alias of 127.0.0.1
])
def test_webhook_urls_must_be_public_https(app, url):
    for channel_type in ("feishu", "dingtalk", "wecom", "custom"):
        response = create_channel(app.as_user(USER_A), channel_type, {**CONFIGS[channel_type], "webhook_url": url})
        assert response.status_code == 400, (channel_type, url, response.text)
        assert response.json()["error"] == "unsafe_target"
        assert url not in response.text  # the rejected value is not echoed
    assert _rows(app, USER_A) == []


def test_a_hostname_that_resolves_to_a_private_address_is_refused_and_rechecked_on_update(app, monkeypatch):
    client = app.as_user(USER_A)
    stub_dns(monkeypatch, {"rebind.example.org": "10.1.2.3"})
    refused = create_channel(client, "custom", {"webhook_url": "https://rebind.example.org/in"})
    assert refused.status_code == 400 and refused.json()["error"] == "unsafe_target"

    stub_dns(monkeypatch, {"rebind.example.org": "93.184.216.34"})
    channel_id = create_channel(client, "custom", {"webhook_url": "https://rebind.example.org/in"}).json()["id"]
    stub_dns(monkeypatch, {"rebind.example.org": "10.1.2.3"})  # rebinding after the save
    assert client.put(f"{BASE}/{channel_id}", json={"name": "renamed", "config": {}}).status_code == 400


@pytest.mark.parametrize("host", ["10.0.0.7", "192.168.1.1", "localhost", "smtp.internal", "mail", "127.0.0.1", "0x7f.1"])
def test_smtp_host_must_be_a_public_host(app, host):
    response = create_channel(app.as_user(USER_A), "email", {**EMAIL, "smtp_host": host})
    assert response.status_code == 400 and response.json()["error"] in ("unsafe_target", "invalid_config")
    assert _rows(app, USER_A) == []


def test_smtp_hostname_resolving_privately_and_disallowed_ports_are_refused(app, monkeypatch):
    stub_dns(monkeypatch, {"smtp.rebind.example.org": "10.9.9.9"})
    client = app.as_user(USER_A)
    private = create_channel(client, "email", {**EMAIL, "smtp_host": "smtp.rebind.example.org"})
    assert private.status_code == 400 and private.json()["error"] == "unsafe_target"
    for port in (22, 80, 3306, 2525, 0, "abc"):
        response = create_channel(client, "email", {**EMAIL, "smtp_port": port})
        assert response.status_code == 400, port
    for port in (465, 587, 25):
        assert create_channel(client, "email", {**EMAIL, "smtp_port": port}).status_code == 201


# ------------------------------------------------------------------ test send and send-time protection


@pytest.mark.parametrize("channel_type", [t for t in CHANNEL_HOSTS])
def test_each_web_channel_type_sends_a_test_message_through_the_existing_sender(app, channel_type):
    client = app.as_user(USER_A)
    channel_id = create_channel(client, channel_type).json()["id"]

    result = client.post(f"{BASE}/{channel_id}/test")

    assert result.status_code == 200, result.text
    assert result.json()["success"] is True and result.json()["status"] == "sent"
    assert result.json()["error_category"] is None
    assert app.http.hosts() == [CHANNEL_HOSTS[channel_type]]
    [call] = app.http.calls
    assert "NexStock" in call.body  # the fixed test message (JSON-escaped CJK, the ASCII part is readable)
    # the channel's own credentials (and only they) were used
    if channel_type == "telegram":
        assert "/bot123456789:AAFakeTokenFakeTokenFakeToken_123/" in call.url
    if channel_type == "dingtalk":
        assert "access_token=tok" in call.url and "sign=" in call.url  # signed with the channel's own secret
    if channel_type == "feishu":
        assert json.loads(call.body)["sign"]  # webhook secret signature present


def test_email_channel_sends_through_the_users_own_smtp_account(app, monkeypatch):
    server = MagicMock()
    connections = []

    def connect(host, port, **kwargs):
        connections.append((host, port))
        return server

    monkeypatch.setattr("src.utils.safe_smtp.connect_pinned_smtp", connect)
    smtp_ssl = MagicMock(side_effect=AssertionError("the system SMTP path must not be used"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", smtp_ssl)
    monkeypatch.setattr(smtplib, "SMTP", smtp_ssl)
    monkeypatch.setenv("EMAIL_SENDER", "system@sys.example.net")
    monkeypatch.setenv("EMAIL_PASSWORD", "system-mail-password")

    client = app.as_user(USER_A)
    channel_id = create_channel(client, "email").json()["id"]
    result = client.post(f"{BASE}/{channel_id}/test")

    assert result.json()["success"] is True
    assert connections == [("smtp.mail.example.com", 587)]
    server.login.assert_called_once_with("login@mail.example.com", "smtp-password")
    message = server.send_message.call_args.args[0]
    assert message["To"] == "a@x.example.com, b@x.example.com"
    assert "me@mail.example.com" in message["From"]
    assert app.http.calls == []


def test_test_send_failures_report_a_generic_category_never_the_upstream_response(app):
    app.http.routes["hooks.example.org"] = (500, {}, "UPSTREAM-SECRET-BODY token=abc")
    client = app.as_user(USER_A)
    channel_id = create_channel(client, "custom").json()["id"]

    result = client.post(f"{BASE}/{channel_id}/test")

    assert result.status_code == 200
    assert result.json() == {"success": False, "status": "failed", "error_category": "send_failed"}
    assert "UPSTREAM-SECRET-BODY" not in result.text and "abc" not in result.text


def test_a_redirect_to_the_metadata_service_is_never_followed(app):
    app.http.routes["hooks.example.org"] = redirect_to("http://169.254.169.254/latest/meta-data/")
    client = app.as_user(USER_A)
    channel_id = create_channel(client, "custom").json()["id"]

    result = client.post(f"{BASE}/{channel_id}/test")

    assert result.json()["success"] is False and result.json()["error_category"] == "send_failed"
    assert app.http.hosts() == ["hooks.example.org"]  # exactly one request: the redirect target was never contacted
    assert "169.254.169.254" not in result.text


def test_a_redirect_is_followed_for_the_system_channels_as_before(app, monkeypatch):
    """The hook is scoped to personal channels: outside it ``requests`` keeps its default behaviour."""
    import requests

    app.http.routes["sys-custom.example.net"] = redirect_to("https://other.example.net/final")
    app.http.routes["other.example.net"] = (200, {}, b"ok")
    requests.post("https://sys-custom.example.net/hook", json={})
    assert app.http.hosts() == ["sys-custom.example.net", "other.example.net"]


def test_the_target_is_checked_again_when_the_message_is_sent(app, monkeypatch):
    client = app.as_user(USER_A)
    channel_id = create_channel(client, "custom").json()["id"]
    stub_dns(monkeypatch, {"hooks.example.org": "10.0.0.9"})  # the host now resolves to a private address

    test = client.post(f"{BASE}/{channel_id}/test").json()
    assert test == {"success": False, "status": "blocked", "error_category": "target_not_allowed"}

    # the real push path (not only the test endpoint) is guarded as well: the request is blocked before it is sent
    notifier = build_user_notifier(USER_A)
    result = push_to_user(USER_A, "盘前推送", notifier=notifier)
    assert result.success is False
    assert app.http.calls == []


def test_user_outbound_scope_blocks_plain_http_and_private_targets_for_any_requests_call(app):
    import requests

    with user_outbound_requests():
        for url in ("http://hooks.example.org/x", "https://10.0.0.1/x", "https://169.254.169.254/x"):
            with pytest.raises(BlockedRequestError):
                requests.post(url, json={})
    assert app.http.calls == []
    requests.post("http://hooks.example.org/x", json={})  # outside the scope: unchanged behaviour
    assert app.http.hosts() == ["hooks.example.org"]


def test_test_send_is_rate_limited_per_channel(app, monkeypatch):
    monkeypatch.setattr(notification_module, "TEST_SEND_MIN_INTERVAL_SECONDS", 60.0)
    client = app.as_user(USER_A)
    first_id = create_channel(client, "custom").json()["id"]
    other_id = create_channel(client, "wecom").json()["id"]
    assert client.post(f"{BASE}/{first_id}/test").status_code == 200
    again = client.post(f"{BASE}/{first_id}/test")
    assert again.status_code == 429 and again.json()["error"] == "rate_limited"
    assert client.post(f"{BASE}/{other_id}/test").status_code == 200  # another channel has its own allowance
    assert len(app.http.calls) == 2


# ------------------------------------------------------------------ the personal notifier


def test_the_personal_notifier_has_only_the_users_enabled_channels_and_none_of_the_systems(app, monkeypatch):
    from src.config import Config

    for key, value in SYSTEM_CHANNEL_ENV.items():
        monkeypatch.setenv(key, value)
    Config.reset_instance()
    client = app.as_user(USER_A)
    assert build_user_notifier(USER_A).is_available() is False  # no channel = nothing, not the .env channels
    create_channel(client, "wecom")
    off = create_channel(client, "custom", enabled=False).json()

    notifier = build_user_notifier(USER_A)
    assert isinstance(notifier, UserNotificationService)
    assert [c.value for c in notifier.get_available_channels()] == ["wechat"]
    assert off["enabled"] is False

    result = push_to_user(USER_A, "hello", notifier=notifier)
    assert result.success is True and [r.channel for r in result.channel_results] == ["wechat"]
    assert app.http.hosts() == [CHANNEL_HOSTS["wecom"]]
    # the system's channels were never contacted, whatever the .env says
    assert not set(app.http.hosts()) & {"sys-wecom.example.net", "sys-feishu.example.net", "sys-custom.example.net"}

    assert isinstance(notifier_for_user(PRIMARY_ID), type(notifier_for_user(None)))
    assert not isinstance(notifier_for_user(PRIMARY_ID), UserNotificationService)
    with pytest.raises(ValueError):
        build_user_notifier(PRIMARY_ID)


def test_two_channels_of_one_type_both_receive_and_one_failing_channel_does_not_stop_the_other(app):
    client = app.as_user(USER_A)
    create_channel(client, "custom", {**CUSTOM, "webhook_url": "https://bad.example.org/in"}, name="broken")
    create_channel(client, "custom", {**CUSTOM, "webhook_url": "https://good.example.org/in"}, name="fine")
    app.http.routes["bad.example.org"] = (500, {}, b"boom")

    notifier = build_user_notifier(USER_A)
    result = notifier.send_with_results("两个自定义渠道")

    assert app.http.hosts() == ["bad.example.org", "good.example.org"]
    assert result.success is True  # at least one delivered
    assert [r.success for r in result.channel_results] == [True]  # per channel type; the row detail is below
    assert [(o["channel_type"], o["success"]) for o in notifier.row_outcomes] == [("custom", False), ("custom", True)]


def test_all_channels_failing_is_a_failed_result_not_an_exception(app):
    client = app.as_user(USER_A)
    create_channel(client, "custom")
    app.http.routes["hooks.example.org"] = (500, {}, b"x")
    result = push_to_user(USER_A, "x")
    assert result.success is False and result.status == "all_failed"


def test_noise_control_state_is_namespaced_per_user(app, monkeypatch):
    from src.config import Config
    from src.notification_noise import reset_notification_noise_state

    monkeypatch.setenv("NOTIFICATION_DEDUP_TTL_SECONDS", "3600")
    monkeypatch.setenv("NOTIFICATION_COOLDOWN_SECONDS", "3600")
    Config.reset_instance()
    reset_notification_noise_state()
    create_channel(app.as_user(USER_A), "wecom")
    create_channel(app.as_user(USER_B), "wecom")

    assert push_to_user(USER_A, "same text").success is True
    assert push_to_user(USER_A, "same text").status == "noise_suppressed"  # A's own duplicate is suppressed
    assert push_to_user(USER_B, "same text").success is True  # but B is not affected by A's
    assert len(app.http.calls) == 2
    reset_notification_noise_state()


def test_report_history_comparison_is_off_for_personal_notifiers(app, monkeypatch):
    monkeypatch.setenv("REPORT_HISTORY_COMPARE_N", "5")
    from src.config import Config

    Config.reset_instance()
    create_channel(app.as_user(USER_A), "wecom")
    assert build_user_notifier(USER_A)._config.report_history_compare_n == 0


# ------------------------------------------------------------------ pinned SMTP


class _FakeSocket:
    def __init__(self, script):
        self.script = script
        self.sent = []

    def makefile(self, *args, **kwargs):
        import io

        return io.BytesIO(self.script)

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        pass


def test_smtp_connection_is_pinned_to_the_checked_address_and_verifies_the_original_name(monkeypatch):
    from src.utils import safe_smtp

    connections = []
    wrapped = []
    sockets = [_FakeSocket(b"220 hello\r\n")]

    def create_connection(address, timeout=None, source_address=None):
        connections.append(address)
        return sockets.pop(0)

    class FakeContext:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.append(server_hostname)
            return sock

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(safe_smtp.ssl, "create_default_context", lambda: FakeContext())

    server = safe_smtp.connect_pinned_smtp(
        "smtp.mail.example.com", 465, resolver=lambda host, port: ["93.184.216.34"],
    )

    assert connections == [("93.184.216.34", 465)]  # the checked address, not the host name (no second resolution)
    assert wrapped == ["smtp.mail.example.com"]       # TLS still verifies the certificate for the original name
    assert isinstance(server, smtplib.SMTP_SSL)


def test_smtp_pinning_refuses_private_answers_other_ports_and_mixed_answers():
    from src.utils import safe_smtp

    with pytest.raises(UnsafeUrlError):
        safe_smtp.connect_pinned_smtp("smtp.example.org", 587, resolver=lambda h, p: ["10.0.0.1"])
    with pytest.raises(UnsafeUrlError):  # one private answer among public ones is enough to refuse
        safe_smtp.connect_pinned_smtp("smtp.example.org", 587, resolver=lambda h, p: ["93.184.216.34", "192.168.0.1"])
    with pytest.raises(UnsafeUrlError) as excinfo:
        safe_smtp.connect_pinned_smtp("smtp.example.org", 2525, resolver=lambda h, p: ["93.184.216.34"])
    assert excinfo.value.reason == "port_not_allowed"


def test_system_email_path_is_unchanged_without_an_explicit_smtp_host():
    config = MagicMock()
    config.email_sender = "me@qq.com"
    config.email_password = "x"
    config.email_receivers = []
    config.stock_email_groups = []
    sender = EmailSender(config)
    assert sender._email_config["smtp_host"] == "" and sender._email_config["username"] == ""
