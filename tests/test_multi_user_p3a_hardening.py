# -*- coding: utf-8 -*-
"""Multi-user P3a hardening: a user-supplied push target / schedule cannot hurt the server or other users.

One regression test per audit finding (see docs/design/multi-user.md section 21):

* response body cap (gzip bomb), hard wall-clock deadline (drip server, DNS, SMTP), one test send in flight per user;
* DNS-rebinding pin (the connection goes to the validated address, SNI / certificate verification kept), no environment
  proxy, no ``latency_ms``, generic ``unsafe_target``, no secret in log records, ``fec0::/10``, fixed EHLO name;
* alert pushes in a bounded pool (a hung target of user A delays neither B nor the primary), push rate limits;
* pre-open earliest time, degraded shared context never cached, fair order, batch cut-off, one run per user;
* user-view runs never write ``reports/``, the news pool does not reveal other users' announcements, legacy channel
  keys are no longer personal settings.

The HTTP tests talk to a real local TLS server through the real ``requests`` / ``urllib3`` stack: DNS for the guard is
stubbed (every name is "public") and ``socket.getaddrinfo`` is rerouted so the public test address lands on loopback.
Nothing leaves the machine.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import socket
import ssl
import sys
import threading
import time
import tracemalloc
import zlib
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import requests

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.config import get_config
from src.core.pipeline import StockAnalysisPipeline
from src.notification import ChannelAttemptResult, NotificationDispatchResult, NotificationService
from src.repositories.user_repo import UserRepository
from src.services import user_notification_service as nm
from src.services import user_scheduled_runs as runs
from src.services.alert_service import AlertService
from src.services.alert_worker import AlertWorker
from src.services.user_notification_service import UserNotificationChannelService
from src.services.user_schedule_service import earliest_preopen_time, effective_preopen_time
from src.utils import safe_requests, safe_smtp, url_safety
from src.utils.safe_requests import BlockedRequestError, RESPONSE_BODY_CAP_BYTES, user_outbound_requests
from src.utils.url_safety import is_public_ip
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    stub_dns,
    teardown_multi_user_env,
)
from tests.p3a_helpers import CUSTOM, FakeHTTP, create_channel

BASE = "/api/v1/me/notification-channels"
PUBLIC_IP = "93.184.216.34"      # what the SSRF guard sees for every test host
VALIDATED_ROUTE = "127.0.0.2"    # where the public test address "lives" (the server the guard approved)
REBIND_ROUTE = "127.0.0.1"       # what the HTTP client's own second lookup would return (the server the user aims at)
PREOPEN_NOW = datetime(2026, 11, 9, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
CRLF2 = b"\r\n\r\n"


# ------------------------------------------------------------------------------------------------ local TLS server

def _make_certificate(directory, names):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{names[0]}.pem", directory / f"{names[0]}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


class TlsServer(threading.Thread):
    """Minimal HTTPS server with a misbehaviour per ``mode``; records connections and the SNI name it was asked for."""

    def __init__(self, cert, key, *, mode="ok", bind="127.0.0.2", port=0, payload=b""):
        super().__init__(daemon=True)
        self.mode, self.payload = mode, payload
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind, port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self.requests = []
        self.sni = []
        self.stop_flag = threading.Event()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.context.sni_callback = lambda sslobj, name, ctx: self.sni.append(name)

    def run(self):
        while not self.stop_flag.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self):
        self.stop_flag.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self, conn):
        try:
            conn.settimeout(10)
            tls = self.context.wrap_socket(conn, server_side=True)
            data = b""
            while CRLF2 not in data:
                chunk = tls.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(CRLF2)
            length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")), 0)
            while len(body) < length:
                body += tls.recv(65536)
            self.requests.append(head.decode("latin-1") + "\r\n\r\n")
            if self.mode == "ok":
                answer = b'{"ok":true,"code":0,"errcode":0}'
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(answer) + answer)
            elif self.mode == "status500":
                tls.sendall(b"HTTP/1.1 500 Server Error\r\nContent-Length: 0\r\n\r\n")
            elif self.mode == "gzip":
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(self.payload))
                tls.sendall(self.payload)
                time.sleep(1)
            elif self.mode == "drip_headers":  # one header byte every 0.2 s, never finishing the response head
                for ch in b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 10_000:
                    tls.sendall(bytes([ch]))
                    time.sleep(0.2)
            elif self.mode == "drip_body":  # complete head, then one body byte every 0.2 s
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
                for _ in range(100_000):
                    tls.sendall(b"x")
                    time.sleep(0.2)
            elif self.mode == "silent":
                time.sleep(30)
        except (OSError, ssl.SSLError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    directory = tmp_path_factory.mktemp("p3a-certs")
    return {
        "hooks": _make_certificate(directory, ["rebind.example.com", "drip.example.com", "hooks.example.org", "api.telegram.org"]),
        "other": _make_certificate(directory, ["other.example.net"]),
    }


@pytest.fixture
def net(monkeypatch, certs):
    """Reroute the test addresses to loopback and trust the test certificate; yields a server factory."""
    real = socket.getaddrinfo
    routes = {PUBLIC_IP: VALIDATED_ROUTE}
    unpinned = {"rebind.example.com": REBIND_ROUTE}  # the client's own lookup of the name (what a rebinding DNS answers)

    def fake(host, port, *args, **kwargs):
        target = routes.get(host) or unpinned.get(host)
        if target:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (target, port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", certs["hooks"][0])
    started = []

    def server(**kwargs):
        cert, key = kwargs.pop("cert", certs["hooks"])
        srv = TlsServer(cert, key, **kwargs)
        srv.start()
        started.append(srv)
        return srv

    yield SimpleNamespace(server=server, routes=routes, unpinned=unpinned)
    for srv in started:
        srv.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    built.dns = stub_dns(monkeypatch)  # the guard sees PUBLIC_IP for every host
    monkeypatch.setattr(nm, "TEST_SEND_MIN_INTERVAL_SECONDS", 0.0)
    nm._last_test_at.clear()
    nm._tests_in_flight.clear()
    nm.reset_rate_limits()
    repo = UserRepository()
    for user in (USER_A, USER_B):
        repo.touch_app_user(user, user, user, "user")
    yield built
    teardown_multi_user_env()


def _channel(app, user_id, url, channel_type="custom", **extra):
    response = create_channel(app.as_user(user_id), channel_type, {**CUSTOM, "webhook_url": url, **extra})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _post(url, **kwargs):
    return requests.post(url, json={"msg": "hi"}, timeout=10, **kwargs)


# ------------------------------------------------------------------------------------------------ 1. response cap

def test_a_gzip_bomb_response_is_cut_at_the_cap_and_does_not_balloon_memory(app, net):
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
    block = b"\0" * (1 << 20)
    payload = b"".join(compressor.compress(block) for _ in range(200)) + compressor.flush()  # 200 MB inflated
    server = net.server(mode="gzip", payload=payload)

    tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        started = time.monotonic()
        with user_outbound_requests(deadline_seconds=20):
            response = _post(f"https://rebind.example.com:{server.port}/hook")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert response.status_code == 200
    assert len(response.content) == RESPONSE_BODY_CAP_BYTES == 64 * 1024  # decoded bytes read: exactly the cap
    assert peak < 8 * 1024 * 1024, peak  # a 200 MB body was never inflated
    assert time.monotonic() - started < 10
    assert "Accept-Encoding: identity" in server.requests[0]


def test_json_and_text_of_a_capped_response_still_work_for_the_senders(app, net):
    server = net.server(mode="ok")
    with user_outbound_requests(deadline_seconds=20):
        response = _post(f"https://rebind.example.com:{server.port}/hook")
    assert response.json() == {"ok": True, "code": 0, "errcode": 0}
    assert '"ok":true' in response.text


def test_the_system_requests_path_is_not_capped_nor_pinned(app, net):
    """Outside the user scope ``requests`` is untouched (primary user's .env channels)."""
    big = b"x" * (200 * 1024)

    def fake_send(adapter, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = big
        response.request = request
        return response

    with patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
        assert len(requests.post("https://sys.example.net/x", json={}).content) == len(big)


# ------------------------------------------------------------------------------------------------ 2. deadlines

@pytest.mark.parametrize("mode", ["drip_headers", "drip_body"])
def test_a_drip_feeding_server_is_cut_at_the_wall_clock_deadline(app, net, mode):
    server = net.server(mode=mode)
    started = time.monotonic()
    with pytest.raises(requests.exceptions.RequestException) as excinfo:
        with user_outbound_requests(deadline_seconds=1.0):
            _post(f"https://rebind.example.com:{server.port}/hook/SECRET")
    elapsed = time.monotonic() - started
    assert elapsed < 4.0, elapsed  # per-socket timeouts would have waited for the next byte, again and again
    assert "SECRET" not in str(excinfo.value) and "rebind.example.com" not in str(excinfo.value)


def test_a_silent_server_and_a_hanging_dns_lookup_are_bounded_too(app, net, monkeypatch):
    server = net.server(mode="silent")
    started = time.monotonic()
    with pytest.raises(requests.exceptions.RequestException):
        with user_outbound_requests(deadline_seconds=1.0):
            _post(f"https://rebind.example.com:{server.port}/hook")
    assert time.monotonic() - started < 4.0

    release = threading.Event()
    monkeypatch.setattr(url_safety, "_default_resolver", lambda host, port: (release.wait(30), [PUBLIC_IP])[1])
    monkeypatch.setattr(safe_requests, "DNS_TIMEOUT_SECONDS", 0.3)
    started = time.monotonic()
    with pytest.raises(BlockedRequestError) as excinfo:
        with user_outbound_requests(deadline_seconds=5.0):
            _post("https://slow-dns.example.com/hook")
    assert time.monotonic() - started < 2.0 and excinfo.value.reason == "dns_failure"
    release.set()


def test_the_test_endpoint_returns_at_the_deadline_for_a_drip_server(app, net, monkeypatch):
    monkeypatch.setattr(get_config(), "user_push_send_deadline_seconds", 1)
    monkeypatch.setattr(get_config(), "user_push_total_deadline_seconds", 2)
    server = net.server(mode="drip_headers")
    channel_id = _channel(app, USER_A, f"https://drip.example.com:{server.port}/hook")

    started = time.monotonic()
    result = app.as_user(USER_A).post(f"{BASE}/{channel_id}/test").json()

    assert time.monotonic() - started < 6.0
    assert result == {"success": False, "status": "failed", "error_category": "send_failed"}  # and no latency_ms
    assert not nm._tests_in_flight  # the worker really ended, the slot is free again


def test_smtp_connection_shares_the_wall_clock_deadline(monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]
    real_create = socket.create_connection
    monkeypatch.setattr(socket, "create_connection", lambda address, timeout=None, source_address=None: real_create(("127.0.0.1", port), timeout))
    started = time.monotonic()
    try:
        with pytest.raises((OSError, BlockedRequestError)):
            with user_outbound_requests(deadline_seconds=1.0):
                safe_smtp.connect_pinned_smtp("smtp.example.com", 587, timeout=30, resolver=lambda h, p: [PUBLIC_IP])
    finally:
        server.close()
    assert time.monotonic() - started < 4.0  # the 30 s socket timeout did not apply: the scope deadline did


def test_one_test_send_per_user_at_a_time_and_the_allowance_is_recorded(app, monkeypatch):
    monkeypatch.setattr(nm, "TEST_SEND_MIN_INTERVAL_SECONDS", 60.0)
    first = _channel(app, USER_A, "https://hooks.example.org/in/1")
    second = _channel(app, USER_A, "https://hooks.example.org/in/2")
    gate, entered = threading.Event(), threading.Event()

    def hung_send(self, user_id, row):
        entered.set()
        gate.wait(10)
        return {"success": False, "status": "failed", "error_category": "send_failed"}

    monkeypatch.setattr(UserNotificationChannelService, "_send_test", hung_send)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(UserNotificationChannelService().test(USER_A, first)), daemon=True)
    worker.start()
    assert entered.wait(5)

    client = app.as_user(USER_A)
    concurrent = client.post(f"{BASE}/{second}/test")  # another channel of the same user, while one test hangs
    assert concurrent.status_code == 429 and concurrent.json()["error"] == "rate_limited"
    assert app.as_user(USER_B).post(f"{BASE}/{first}/test").status_code == 404  # B has no such channel (and is not blocked by A)

    gate.set()
    worker.join(5)
    assert outcome["status"] == "failed" and "latency_ms" not in outcome
    assert (USER_A, first) in nm._last_test_at and not nm._tests_in_flight
    again = app.as_user(USER_A).post(f"{BASE}/{first}/test")  # the time was recorded when the test ended, not only when it began
    assert again.status_code == 429 and again.json()["error"] == "rate_limited"


def test_the_composite_notifier_stops_trying_channels_when_the_push_budget_is_spent(app, monkeypatch):
    monkeypatch.setattr(get_config(), "user_push_total_deadline_seconds", 1)
    http = FakeHTTP().install(monkeypatch)
    calls = []

    def slow(request):
        calls.append(request.url)
        time.sleep(1.2)  # the first channel uses up the whole push budget
        return 200, {}, b'{"code":0}'

    http.routes["hooks.example.org"] = slow
    _channel(app, USER_A, "https://hooks.example.org/in/1")
    _channel(app, USER_A, "https://hooks.example.org/in/2")

    notifier = nm.build_user_notifier(USER_A)
    nm.push_to_user(USER_A, "盘前推送", notifier=notifier)

    assert len(calls) == 1  # the second channel was not tried: the budget was gone
    assert [item["success"] for item in notifier.row_outcomes] == [True, False]


# ------------------------------------------------------------------------------------------------ 3. rebinding, proxy

def test_the_connection_goes_to_the_validated_address_not_to_a_second_lookup(app, net):
    validated = net.server(mode="ok", bind=VALIDATED_ROUTE)
    evil = net.server(mode="ok", bind=REBIND_ROUTE, port=validated.port)  # what getaddrinfo("rebind.example.com") would return

    with user_outbound_requests(deadline_seconds=20):
        response = _post(f"https://rebind.example.com:{validated.port}/hook")

    assert response.json()["ok"] is True
    assert validated.accepted == 1 and evil.accepted == 0
    assert validated.sni == ["rebind.example.com"]  # SNI is still the host name, not the pinned address


def test_certificate_hostname_verification_still_applies_to_the_pinned_address(app, net, certs):
    wrong = net.server(mode="ok", cert=certs["other"])  # certificate for another name
    with pytest.raises(requests.exceptions.SSLError):
        with user_outbound_requests(deadline_seconds=20):
            _post(f"https://rebind.example.com:{wrong.port}/hook")


def test_a_connection_to_an_unvalidated_host_is_refused_inside_the_scope(app, net):
    server = net.server(mode="ok")
    net.unpinned["other.example.com"] = VALIDATED_ROUTE
    with user_outbound_requests(deadline_seconds=20):
        # an adapter-level connection the guard did not validate (e.g. a redirect hop / proxy tunnel) has no pinned address
        import urllib3.util.connection as connection

        with pytest.raises(OSError):
            connection.create_connection(("other.example.com", server.port), 5)


def test_environment_proxies_are_never_used_for_user_targets(app, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    http = FakeHTTP().install(monkeypatch)
    with user_outbound_requests():
        _post("https://hooks.example.org/in/xyz")
    assert http.calls[0].kwargs["proxies"] == {}
    http.reset()
    _post("https://sys.example.net/x")  # outside the scope the environment is still honoured (primary user unchanged)
    assert http.calls[0].kwargs["proxies"].get("https") == "http://127.0.0.1:9"


def test_fec0_site_local_ipv6_is_not_public():
    assert not is_public_ip(ipaddress.ip_address("fec0::1"))
    assert not is_public_ip(ipaddress.ip_address("fec0:0:0:1::5"))
    assert is_public_ip(ipaddress.ip_address("2606:4700:4700::1111"))
    with pytest.raises(url_safety.UnsafeUrlError):
        url_safety.check_public_https_url("https://[fec0::1]/hook")


def test_smtp_ehlo_name_is_fixed_and_the_hosts_fqdn_is_never_read(monkeypatch):
    class Sock:
        def __init__(self):
            self.sent = b""
            self._answers = [b"220 hello\r\n", b"250-smtp\r\n250 STARTTLS\r\n", b"220 go\r\n", b"250 ok\r\n"]

        def makefile(self, *a, **k):
            import io
            return io.BytesIO(b"220 hello\r\n250-smtp\r\n250 STARTTLS\r\n220 go\r\n250 ok\r\n")

        def sendall(self, data):
            self.sent += data

        def close(self):
            pass

        def settimeout(self, value):
            pass

    sock = Sock()
    monkeypatch.setattr(socket, "getfqdn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("getfqdn must not be called")))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sock)

    class Context:
        def wrap_socket(self, s, server_hostname=None):
            return s

    monkeypatch.setattr(safe_smtp.ssl, "create_default_context", lambda: Context())
    server = safe_smtp.connect_pinned_smtp("smtp.example.com", 465, resolver=lambda h, p: [PUBLIC_IP])
    assert server.local_hostname == safe_smtp.EHLO_HOSTNAME == "localhost"


# ------------------------------------------------------------------------------------------------ 8. secrets in logs

def test_no_token_or_path_secret_reaches_a_log_record_for_user_channels(app, net, caplog):
    caplog.set_level(logging.DEBUG)
    telegram_token = "123456789:AAFakeTokenFakeTokenFakeToken_123"
    server = net.server(mode="status500")
    client = app.as_user(USER_A)

    refused_port = socket.socket()
    refused_port.bind(("127.0.0.2", 0))
    closed = refused_port.getsockname()[1]
    refused_port.close()  # nothing listens: connection refused
    tg = create_channel(client, "telegram", {"bot_token": telegram_token, "chat_id": "-1001234567890"}).json()["id"]
    path_closed = _channel(app, USER_A, f"https://hooks.example.org:{closed}/hook/SECRETPATHTOKEN123")
    path_500 = _channel(app, USER_A, f"https://hooks.example.org:{server.port}/hook/SECRETPATH500")
    with patch("src.notification_sender.telegram_sender.time.sleep", lambda s: None):
        for channel_id in (tg, path_closed, path_500):
            client.post(f"{BASE}/{channel_id}/test")
    assert server.requests, "the 500 server was reached, so urllib3 had a request line to log"

    text = "\n".join(record.getMessage() + "\n" + str(record.exc_text or "") for record in caplog.records)
    assert caplog.records and any("Webhook" in record.getMessage() or "Telegram" in record.getMessage() for record in caplog.records)
    for secret in (telegram_token, "AAFakeTokenFake", "SECRETPATHTOKEN123", "SECRETPATH500"):
        assert secret not in text, secret


def test_request_errors_leave_the_safe_layer_without_url_host_or_port_detail(app, net):
    closed = socket.socket()
    closed.bind(("127.0.0.2", 0))
    port = closed.getsockname()[1]
    closed.close()
    with pytest.raises(requests.exceptions.ConnectionError) as refused:
        with user_outbound_requests(deadline_seconds=5):
            _post(f"https://rebind.example.com:{port}/bot123:SECRET/sendMessage")
    server = net.server(mode="silent")
    with pytest.raises(requests.exceptions.RequestException) as filtered:
        with user_outbound_requests(deadline_seconds=1):
            _post(f"https://rebind.example.com:{server.port}/bot123:SECRET/sendMessage")
    for exc in (refused.value, filtered.value):
        assert "SECRET" not in str(exc) and "rebind.example.com" not in str(exc) and str(port) not in str(exc)
        assert exc.__cause__ is None and exc.request is None and exc.response is None


# ------------------------------------------------------------------------------------------------ 9. generic unsafe_target

def test_resolution_dependent_rejections_are_one_generic_unsafe_target(app, monkeypatch):
    client = app.as_user(USER_A)

    def issue_code(url):
        response = create_channel(client, "custom", {**CUSTOM, "webhook_url": url})
        assert response.status_code == 400 and response.json()["error"] == "unsafe_target", response.text
        return response.json()["detail"]["issues"][0]["code"]

    stub_dns(monkeypatch, {"private.example.com": "10.1.2.3"})
    private = issue_code("https://private.example.com/hook")
    literal = issue_code("https://10.0.0.5/hook")

    def broken(host, port):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(url_safety, "_default_resolver", broken)
    unresolved = issue_code("https://nxdomain.example.com/hook")
    assert private == literal == unresolved == "unsafe_target"

    # pure syntax problems keep their specific codes
    assert issue_code("http://hooks.example.org/hook") == "scheme_not_allowed"
    assert issue_code("https://user:pw@hooks.example.org/hook") == "credentials_in_url"
    smtp = create_channel(client, "email", {"smtp_host": "nxdomain.example.com", "smtp_port": 587, "password": "p",
                                            "sender": "me@mail.example.com"})
    assert smtp.json()["detail"]["issues"][0]["code"] == "unsafe_target"
    port = create_channel(client, "email", {"smtp_host": "smtp.mail.example.com", "smtp_port": 2525, "password": "p",
                                            "sender": "me@mail.example.com"})
    assert port.json()["detail"]["issues"][0]["code"] == "port_not_allowed"


# ------------------------------------------------------------------------------------------------ 2/10. alerts

RULE = {
    "name": "Moutai breakout", "target_scope": "single_symbol", "target": "600519", "alert_type": "price_cross",
    "parameters": {"direction": "above", "price": 1800}, "severity": "warning", "enabled": True,
}


async def _quote(_monitor, _stock_code):
    return SimpleNamespace(price=1810.0)


def _worker(system):
    config = SimpleNamespace(agent_event_monitor_enabled=True, agent_event_alert_rules_json="", trading_day_check_enabled=False)
    return AlertWorker(config_provider=lambda: config, service=AlertService(), notifier=system)


def _system_notifier():
    notifier = MagicMock()
    notifier.send_with_results.return_value = NotificationDispatchResult(
        dispatched=True, success=True, status="sent", channel_results=[ChannelAttemptResult(channel="feishu", success=True)],
    )
    return notifier


def test_a_hung_personal_push_of_user_a_delays_neither_user_b_nor_the_primary(app, monkeypatch):
    monkeypatch.setattr(get_config(), "user_push_total_deadline_seconds", 1)
    monkeypatch.setattr(nm, "_DEADLINE_GRACE_SECONDS", 0.3)
    http = FakeHTTP().install(monkeypatch)
    hung, release = threading.Event(), threading.Event()

    def a_target(request):
        hung.set()
        release.wait(20)  # user A's target never answers
        return 200, {}, b'{"code":0}'

    http.routes["a-hook.example.org"] = a_target
    _channel(app, USER_A, "https://a-hook.example.org/in")
    _channel(app, USER_B, "https://b-hook.example.org/in")
    for user in (USER_A, USER_B, PRIMARY_ID):
        assert app.as_user(user).post("/api/v1/alerts/rules", json=RULE).status_code == 200
    system = _system_notifier()

    started = time.monotonic()
    with patch("src.agent.events.EventMonitor._get_realtime_quote", new=_quote):
        stats = _worker(system).run_once()
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 4.0, elapsed  # bounded by the push deadline, not by A's target
        assert hung.is_set() and release.is_set() is False  # A's push was in flight (and is still stuck) at this point
        assert stats["triggered"] == 3
        system.send_with_results.assert_called_once()  # the primary user's alert went through its own notifier
        assert [call.host for call in http.calls if call.host == "b-hook.example.org"] == ["b-hook.example.org"]
        b_items = app.as_user(USER_B).get("/api/v1/alerts/notifications").json()["items"]
        assert [(n["channel"], n["success"]) for n in b_items] == [("custom", True)]
        a_items = app.as_user(USER_A).get("/api/v1/alerts/notifications").json()["items"]
        assert [n["success"] for n in a_items] == [False]  # recorded as failed; A's cooldown is not started
        assert all(n["latency_ms"] is None for n in a_items + b_items)  # no timing of a user's own target is kept
    finally:
        release.set()


def test_personal_alert_pushes_are_rate_limited_per_user(app, monkeypatch):
    monkeypatch.setattr(get_config(), "user_alert_push_max_per_hour", 1)
    http = FakeHTTP().install(monkeypatch)
    _channel(app, USER_A, "https://a-hook.example.org/in")
    client = app.as_user(USER_A)
    assert client.post("/api/v1/alerts/rules", json=RULE).status_code == 200
    assert client.post("/api/v1/alerts/rules", json={**RULE, "name": "second", "target": "000001"}).status_code == 200

    with patch("src.agent.events.EventMonitor._get_realtime_quote", new=_quote):
        stats = _worker(_system_notifier()).run_once()

    assert stats["triggered"] == 2 and stats["notified"] == 1
    assert len(http.calls_to("a-hook.example.org")) == 1
    channels = sorted(n["channel"] for n in client.get("/api/v1/alerts/notifications").json()["items"])
    assert channels == ["__rate_limited__", "custom"]


def test_chat_send_is_rate_limited_per_non_primary_user_and_the_primary_is_not(app, monkeypatch):
    monkeypatch.setattr(get_config(), "user_chat_send_max_per_hour", 2)
    http = FakeHTTP().install(monkeypatch)
    _channel(app, USER_A, "https://a-hook.example.org/in")
    _channel(app, USER_B, "https://b-hook.example.org/in")
    client = app.as_user(USER_A)

    assert client.post("/api/v1/agent/chat/send", json={"content": "1"}).json() == {"success": True}
    assert client.post("/api/v1/agent/chat/send", json={"content": "2"}).json() == {"success": True}
    limited = client.post("/api/v1/agent/chat/send", json={"content": "3"}).json()
    assert limited["success"] is False and limited["error"] == "rate_limited"
    assert len(http.calls_to("a-hook.example.org")) == 2  # the third was not relayed
    assert app.as_user(USER_B).post("/api/v1/agent/chat/send", json={"content": "b"}).json() == {"success": True}

    with patch("src.notification.NotificationService") as notification:
        notification.return_value.send.return_value = True
        for _ in range(5):
            assert app.as_user(PRIMARY_ID).post("/api/v1/agent/chat/send", json={"content": "p"}).json() == {"success": True}


# ------------------------------------------------------------------------------------------------ 4/5. schedules

def _put(app, user_id, **items):
    return app.as_user(user_id).put(
        "/api/v1/me/settings", json={"items": [{"key": key, "value": value} for key, value in items.items()]},
    )


def test_preopen_time_has_a_lower_bound_after_the_us_close(app):
    assert earliest_preopen_time() == "05:30"  # 16:00 New York is 04:00 / 05:00 in China, plus the data-ready buffer
    for early in ("00:00", "03:59", "05:29"):
        refused = _put(app, USER_A, PREOPEN_PUSH_TIME=early)
        assert refused.status_code == 400 and refused.json()["error"] == "preopen_time_too_early", early
        assert refused.json()["detail"]["issues"][0]["earliest_time"] == "05:30"
    assert _put(app, USER_A, PREOPEN_PUSH_TIME="05:30").status_code == 200
    schedule = app.as_user(USER_A).get("/api/v1/me/schedule").json()["preopen_push"]
    assert schedule["earliest_time"] == "05:30" and schedule["latest_time"] == "09:00"

    # runtime clamp: a stored value from before the bound existed is not obeyed
    assert effective_preopen_time(SimpleNamespace(preopen_push_time="03:00")) == "05:30"
    assert effective_preopen_time(SimpleNamespace(preopen_push_time="09:40")) == "09:00"
    assert effective_preopen_time(SimpleNamespace(preopen_push_time="08:15")) == "08:15"


def test_a_firing_before_the_final_us_session_does_nothing(app):
    _put(app, USER_A, PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:15")
    built = []
    early = datetime(2026, 11, 9, 4, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = runs.run_scheduled_user_preopens(
        "08:15", now=early, shared_builder=lambda: built.append(1), runner=lambda *a, **k: {"status": "succeeded"},
    )
    assert result["status"] == "skipped" and result["reason"] == "too_early" and built == []


def test_only_a_complete_shared_preopen_object_is_cached_for_the_day(app):
    for user in (USER_A, USER_B):
        _put(app, user, PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:15")
    runs.reset_shared_state()
    built = []
    degraded = {"value": ["us_partial"]}

    def builder():
        built.append(1)
        return SimpleNamespace(degraded=list(degraded["value"]))

    def runner(user_id, shared, *, day, now):
        shared()
        return {"status": "succeeded"}

    runs.run_scheduled_user_preopens("08:15", now=PREOPEN_NOW, runner=runner, shared_builder=builder)
    assert built == [1]  # both users of the batch shared one object ...
    runs.run_scheduled_user_preopens("08:15", now=PREOPEN_NOW, runner=runner, shared_builder=builder)
    assert built == [1, 1]  # ... but a degraded one is recomputed at the next firing

    degraded["value"] = []
    runs.run_scheduled_user_preopens("08:15", now=PREOPEN_NOW, runner=runner, shared_builder=builder)
    runs.run_scheduled_user_preopens("08:15", now=PREOPEN_NOW, runner=runner, shared_builder=builder)
    assert built == [1, 1, 1]  # the complete one is kept for the day
    runs.reset_shared_state()


def test_users_are_started_in_a_fair_rotating_order_not_a_fixed_one():
    users = [f"u{i}" for i in range(8)]
    orders = {day: runs.fair_order(users, f"preopen|2026-11-{day:02d}|08:15") for day in range(2, 12)}
    assert all(sorted(order) == sorted(users) for order in orders.values())
    assert runs.fair_order(users, "seed") == runs.fair_order(users, "seed")  # reproducible within a firing
    assert len({tuple(order) for order in orders.values()}) > 5  # ... and different on other days
    assert len({order[0] for order in orders.values()}) > 2  # nobody is always first

    started = []
    results = runs.run_for_users(
        runs.fair_order(users, "x"), lambda uid: started.append(uid) or {"status": "succeeded"}, max_workers=1, timeout_seconds=5,
    )
    assert started == runs.fair_order(users, "x") and set(results) == set(users)


def test_a_batch_is_late_after_its_cutoff_users_not_started_are_skipped_and_running_ones_abandoned():
    gate = threading.Event()
    started = []

    def runner(user_id):
        started.append(user_id)
        gate.wait(10)
        return {"status": "succeeded"}

    began = time.monotonic()
    results = runs.run_for_users(
        ["slow", "queued-1", "queued-2"], runner, max_workers=1, timeout_seconds=30, batch_timeout_seconds=0.3,
    )
    gate.set()
    assert time.monotonic() - began < 3.0
    assert results["slow"] == {"status": "timeout", "reason": "late"}
    assert results["queued-1"] == results["queued-2"] == {"status": "skipped", "reason": "late"}
    assert started == ["slow"]


def test_preopen_batch_after_the_cutoff_skips_everyone(app):
    _put(app, USER_A, PREOPEN_PUSH_ENABLED="true", PREOPEN_PUSH_TIME="08:15")
    called = []
    late = datetime(2026, 11, 9, 9, 25, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = runs.run_scheduled_user_preopens(
        "08:15", now=late, runner=lambda *a, **k: called.append(1) or {"status": "succeeded"}, shared_builder=lambda: None,
    )
    assert result["users"][USER_A] == {"status": "skipped", "reason": "late"} and called == []


def test_a_user_never_has_two_scheduled_runs_at_once():
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def hung(user_id):
        calls.append(user_id)
        entered.set()
        gate.wait(10)
        return {"status": "succeeded"}

    first = runs.run_for_users(["u1"], hung, max_workers=1, timeout_seconds=0.2)
    assert first["u1"] == {"status": "timeout"} and entered.is_set()  # abandoned, but its thread is still alive
    second = runs.run_for_users(["u1", "u2"], lambda uid: calls.append(uid) or {"status": "succeeded"}, max_workers=2, timeout_seconds=5)
    assert second["u1"] == {"status": "skipped", "reason": "already_running"}
    assert second["u2"]["status"] == "succeeded" and calls == ["u1", "u2"]
    gate.set()
    deadline = time.monotonic() + 3
    while "u1" in runs._RUNNING_USERS and time.monotonic() < deadline:
        time.sleep(0.02)
    third = runs.run_for_users(["u1"], lambda uid: {"status": "succeeded"}, max_workers=1, timeout_seconds=5)
    assert third["u1"]["status"] == "succeeded"  # the slot is free again once the thread really ended


# ------------------------------------------------------------------------------------------------ 6. reports/

def test_a_user_view_run_writes_nothing_to_the_shared_reports_directory(app, monkeypatch, tmp_path):
    from src.enums import ReportType
    from src.services.user_config_service import get_user_config

    saved = []
    monkeypatch.setattr(NotificationService, "save_report_to_file", lambda self, content, filename=None: saved.append(filename) or str(tmp_path / "r.md"))
    fallback_dir = tmp_path / "fallback-reports"
    monkeypatch.setattr(StockAnalysisPipeline, "_report_output_dir", staticmethod(lambda: fallback_dir))
    http = FakeHTTP().install(monkeypatch)
    give_user_model(USER_A)
    _channel(app, USER_A, "https://a-hook.example.org/in")
    from tests.test_multi_user_p3a_pushes import _analysis_result

    pipeline = StockAnalysisPipeline(config=get_user_config(USER_A), user_id=USER_A)
    assert pipeline._save_local_report([_analysis_result()], ReportType.SIMPLE) is None
    pipeline._send_single_stock_notification(_analysis_result(), report_type=ReportType.SIMPLE)
    assert pipeline.notifier.save_report_to_file("x", filename="report_20261110.md") is None  # the backstop on the notifier itself
    assert pipeline.notifier._feishu_send_as_file is False
    assert saved == [] and not fallback_dir.exists()
    assert http.calls_to("a-hook.example.org")  # the push itself still happens

    primary = StockAnalysisPipeline(config=get_config())  # the primary user's behaviour is unchanged
    assert primary._save_local_report([_analysis_result()], ReportType.SIMPLE) == str(tmp_path / "r.md")
    assert saved  # it saved through the notifier, as before


# ------------------------------------------------------------------------------------------------ 7. news pool

def test_news_search_does_not_show_announcements_of_codes_the_user_does_not_follow(app):
    from src.agent.tools.search_tools import _handle_search_market_news, _pool_news_hits
    from src.repositories.intelligence_repo import IntelligenceRepository
    from src.services.news_akshare_source import KIND_ANNOUNCEMENT, NewsRecord
    from src.services.news_digest_service import build_news_digest
    from src.services.news_pool_service import china_now
    from src.services.watchlist_service import WatchlistService

    published = china_now().replace(tzinfo=None) - dt.timedelta(hours=1)
    records = [
        NewsRecord(title="年报预告 贵州茅台公告", summary="", url="no-url:a1", published_at=published, source_key="notice",
                   kind=KIND_ANNOUNCEMENT, scope_type="symbol", scope_value="600519", stock_codes=["600519"]),
        NewsRecord(title="年报预告 平安银行公告", summary="", url="no-url:a2", published_at=published, source_key="notice",
                   kind=KIND_ANNOUNCEMENT, scope_type="symbol", scope_value="000001", stock_codes=["000001"]),
        NewsRecord(title="年报预告 行业快讯", summary="", url="https://news.example.com/flash", published_at=published,
                   source_key="cls", stock_codes=["000002"]),
    ]
    IntelligenceRepository().upsert_news_records(records, fetched_at=published, dedup_window_minutes=0)
    WatchlistService().add(USER_A, "600519")

    def titles(user_id):
        return sorted(hit["title"] for hit in _pool_news_hits(["年报预告"], 3, user_id))

    assert titles(USER_A) == ["年报预告 行业快讯", "年报预告 贵州茅台公告"]  # own announcement + general news only
    assert titles(USER_B) == ["年报预告 行业快讯"]  # follows nothing: no announcement, whoever it concerns
    assert len(titles(PRIMARY_ID)) == 3 and len(titles(None)) == 3  # the primary user sees the whole pool, as before

    tool_result = _handle_search_market_news("年报预告", 3, USER_B)
    assert [hit["title"] for hit in tool_result["pool_results"]] == ["年报预告 行业快讯"]
    digest = lambda viewer: sorted(i["title"] for i in build_news_digest(  # noqa: E731
        published - dt.timedelta(hours=1), published + dt.timedelta(hours=3), viewer_user_id=viewer))
    assert digest(USER_A) == ["年报预告 行业快讯", "年报预告 贵州茅台公告"] and digest(USER_B) == ["年报预告 行业快讯"]
    assert len(digest(None)) == 3


# ------------------------------------------------------------------------------------------------ 12. legacy keys

LEGACY = {
    "FEISHU_WEBHOOK_URL": "https://legacy-feishu.example.org/hook/zzz",
    "TELEGRAM_BOT_TOKEN": "123456789:AAFakeTokenFakeTokenFakeToken_123",
    "CUSTOM_WEBHOOK_URLS": "https://legacy-custom.example.org/hook",
    "EMAIL_PASSWORD": "pw",
    "PUSHPLUS_TOKEN": "legacypushplustoken",
}


def test_legacy_channel_keys_are_rejected_hidden_and_never_stored(app):
    client = app.as_user(USER_A)
    for key, value in LEGACY.items():
        refused = client.put("/api/v1/me/settings", json={"items": [{"key": key, "value": value}]})
        assert refused.status_code == 400 and refused.json()["error"] == "managed_elsewhere", key
        assert "notification-channels" in refused.json()["message"] and value not in refused.text
    mixed = client.put("/api/v1/me/settings", json={"items": [
        {"key": "REPORT_LANGUAGE", "value": "en"}, {"key": "FEISHU_WEBHOOK_URL", "value": LEGACY["FEISHU_WEBHOOK_URL"]},
    ]})
    assert mixed.status_code == 400  # all or nothing
    from src.services.user_settings_service import UserSettingsService
    assert UserSettingsService().get_plain_values(USER_A) == {}

    for user in (USER_A, PRIMARY_ID):
        groups = app.as_user(user).get("/api/v1/me/settings").json()["groups"]
        shown = {field["key"] for group in groups for field in group["fields"]}
        assert not shown & set(LEGACY), user
        assert {"NOTIFICATION_QUIET_HOURS", "DAILY_PLAN_ENABLED", "PREOPEN_PUSH_TIME"} <= shown  # the preferences stay
