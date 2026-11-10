# -*- coding: utf-8 -*-
"""Outbound ``requests`` policy for targets a user supplied (personal notification channels, design 3.5 / 5).

The notification senders call ``requests.post`` directly and are shared with the system's own ``.env`` channels,
so the policy is applied around them rather than inside each sender: within :func:`user_outbound_requests`
(this thread / task only) every request sent through ``requests``

* must target a public https URL - checked again right before the request is sent, so a host that started to
  resolve to a private address after the channel was saved is refused (``src/utils/url_safety.py``);
* **connects to the address that was checked** (DNS-rebinding pin): the guard resolves the host once, every answer
  must be public, and ``urllib3``'s ``create_connection`` is told to dial those addresses instead of resolving the
  name a second time. TLS still uses the original host name (SNI + certificate verification), so nothing about the
  HTTPS handshake changes; a connection to any host that was not validated is refused;
* never follows a redirect: a 3xx is returned to the sender, which treats it as a failed send, instead of
  bouncing the server to ``http://169.254.169.254/`` or an internal host (``NO_REDIRECT_REQUESTS_KWARGS``);
* never uses an environment proxy (``HTTPS_PROXY`` / ``HTTP_PROXY``): a proxy would resolve and dial the name itself,
  which defeats the pin, and the proxy's own network position is not the one the checks assume;
* buffers at most ``RESPONSE_BODY_CAP_BYTES`` (64 KB, *decoded*) of the response: the request is made with
  ``stream=True`` and ``Accept-Encoding: identity``, a server that compresses anyway is decoded incrementally and
  cut at the cap (a gzip bomb costs 64 KB, not its inflated size), then the connection is closed. ``.json()`` /
  ``.text`` / ``.content`` of the returned response read that capped body, so the senders need no change;
* has a hard wall-clock deadline when the scope is opened with ``deadline_seconds`` - DNS resolution, connect,
  TLS, request and response included. ``requests`` / ``urllib3`` / ``smtplib`` timeouts are per socket operation, so
  a server that sends one byte every few seconds would otherwise hold a request for as long as it likes; a
  watchdog timer shuts down every socket the scope opened when the deadline passes (the blocked read fails at
  once) and later requests in the scope are refused (``BlockedRequestError('deadline')``);
* never leaks the URL into an exception message: ``requests`` / ``urllib3`` put the full URL (a Telegram bot
  token, a webhook path secret) into their error text and the senders log ``{e}``. Failures leave this layer as
  generic ``RequestException`` subclasses (the same text for refused / filtered / timed out) and the
  ``urllib3`` loggers are silenced inside the scope.

Outside the context ``requests`` behaves exactly as before, so the primary user's ``.env`` channels are unchanged.
The hook wraps ``requests.Session.send`` once (the single place every ``requests`` call ends up in), the same
technique ``forbid_httpx_redirects`` uses for httpx.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import socket
import ssl
import threading
import time
import zlib
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TypeVar

import requests

from src.utils.url_safety import (
    NO_REDIRECT_REQUESTS_KWARGS,
    UnsafeUrlError,
    assert_public_https_url,
    resolve_public_https_url,
)

RESPONSE_BODY_CAP_BYTES = 64 * 1024
_RAW_READ_CAP_BYTES = 1024 * 1024  # undecoded bytes read at most (compressed input is cut long before this)
_READ_CHUNK_BYTES = 4096
DNS_TIMEOUT_SECONDS = 5.0
_MAX_PIN_ATTEMPTS = 4
_URLLIB3_LOGGERS = ("urllib3", "urllib3.connectionpool", "urllib3.connection", "urllib3.response", "urllib3.util.retry")

T = TypeVar("T")


class BlockedRequestError(requests.exceptions.RequestException):
    """The request target is not an acceptable public https endpoint; ``reason`` is a stable machine code."""

    def __init__(self, reason: str):
        super().__init__(f"outbound request blocked ({reason})")
        self.reason = reason


class _Scope:
    """State of one ``user_outbound_requests`` context: deadline, validated addresses, open sockets."""

    def __init__(self, deadline_seconds: Optional[float]):
        self.deadline: Optional[float] = (time.monotonic() + deadline_seconds) if deadline_seconds else None
        self.pins: Dict[Tuple[str, int], List[str]] = {}
        self.sockets: List[socket.socket] = []
        self.expired = False
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        if deadline_seconds:
            self._timer = threading.Timer(max(0.0, deadline_seconds), self._expire)
            self._timer.daemon = True
            self._timer.start()

    def remaining(self) -> Optional[float]:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def check(self) -> None:
        remaining = self.remaining()
        if self.expired or (remaining is not None and remaining <= 0):
            raise BlockedRequestError("deadline")

    def register(self, sock: socket.socket) -> None:
        with self._lock:
            if not self.expired:
                self.sockets.append(sock)
                return
        _shutdown(sock)
        raise BlockedRequestError("deadline")

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            sockets, self.sockets = self.sockets, []
        for sock in sockets:  # a thread blocked in recv / send on one of them fails immediately
            _shutdown(sock)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            self.sockets = []


def _shutdown(sock: socket.socket) -> None:
    """Abort ``sock`` from another thread: ``shutdown`` wakes a blocked read on Linux, ``close`` does on Windows."""
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)  # the plain socket call: an SSLSocket would first drop its SSL object
    except (OSError, ValueError):
        pass
    try:
        sock.close()
    except OSError:
        pass


_scope: "contextvars.ContextVar[Optional[_Scope]]" = contextvars.ContextVar("user_outbound_requests_scope", default=None)
_guard_lock = threading.Lock()
_guard_installed = False


# --------------------------------------------------------------------------- DNS within the deadline

def call_with_deadline(fn: Callable[[], T], seconds: float, *, name: str = "deadline-call") -> T:
    """Run ``fn`` in a daemon thread and return its result, or raise :class:`TimeoutError` after ``seconds``.

    For calls that cannot be interrupted (``getaddrinfo``) or whose caller must not wait for them: the thread is
    abandoned on timeout (it ends on its own), an exception of ``fn`` is re-raised in the caller.
    """
    box: Dict[str, Any] = {}
    finished = threading.Event()

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller
            box["error"] = exc
        finally:
            finished.set()

    threading.Thread(target=_target, daemon=True, name=name).start()
    if not finished.wait(max(0.0, seconds)):
        raise TimeoutError(f"{name} exceeded {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def scoped_resolver(host: str, port: int) -> List[str]:
    """``url_safety`` resolver bounded by the scope deadline (at most ``DNS_TIMEOUT_SECONDS``); a timeout is a DNS failure."""
    from src.utils import url_safety

    scope = _scope.get()
    limit = DNS_TIMEOUT_SECONDS
    if scope is not None:
        remaining = scope.remaining()
        if remaining is not None:
            limit = min(limit, max(0.0, remaining))
    try:
        return call_with_deadline(lambda: url_safety._default_resolver(host, port), limit, name="dns-lookup")
    except TimeoutError as exc:
        raise OSError("DNS lookup timed out") from exc


# --------------------------------------------------------------------------- response body cap

def _make_decoder(encoding: str):
    if encoding in ("gzip", "x-gzip"):
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj(zlib.MAX_WBITS)
    return None


def _read_capped(response: requests.Response, cap: int) -> None:
    """Replace the (unread) streamed body of ``response`` by at most ``cap`` decoded bytes and close the connection."""
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    if response._content is not False:  # already materialised (a test double / adapter that does not stream)
        content = response._content if isinstance(response._content, bytes) else b""
        response._content = content[:cap]
        response._content_consumed = True
        return
    raw = response.raw
    body = bytearray()
    try:
        if encoding in ("", "identity", "gzip", "x-gzip", "deflate"):
            decoder = _make_decoder(encoding)
            read_total = 0
            while len(body) <= cap and read_total < _RAW_READ_CAP_BYTES:
                chunk = raw.read(_READ_CHUNK_BYTES, decode_content=False)
                if not chunk:
                    break
                read_total += len(chunk)
                if decoder is None:
                    body += chunk
                    continue
                data = chunk
                try:
                    while data and len(body) <= cap:
                        body += decoder.decompress(data, cap + 1 - len(body))
                        data = decoder.unconsumed_tail
                except zlib.error:
                    if encoding == "deflate" and not body:  # raw deflate without the zlib wrapper
                        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                        body += decoder.decompress(chunk, cap + 1 - len(body))
                    else:
                        break
        # any other content-encoding (br, zstd ...) cannot be decoded safely here: the body is dropped
    finally:
        try:
            raw.close()
        except Exception:  # noqa: BLE001
            pass
        response.close()
    response._content = bytes(body[:cap])
    response._content_consumed = True


# --------------------------------------------------------------------------- exception sanitising

def _sanitized(exc: BaseException, scope: _Scope) -> requests.exceptions.RequestException:
    """A generic exception with no URL, host, path or token: network failures all read the same."""
    if isinstance(exc, BlockedRequestError):
        return exc
    if scope.expired or (scope.remaining() is not None and scope.remaining() <= 0):
        return BlockedRequestError("deadline")
    if isinstance(exc, requests.exceptions.SSLError):
        return requests.exceptions.SSLError("TLS handshake or certificate check failed")
    if isinstance(exc, requests.exceptions.Timeout):
        return requests.exceptions.Timeout("request failed (network)")
    if isinstance(exc, requests.exceptions.ConnectionError):
        return requests.exceptions.ConnectionError("request failed (network)")
    return requests.exceptions.RequestException("request failed")


class _ScopeLogFilter(logging.Filter):
    """Drops ``urllib3`` log records emitted inside a user scope: they carry the request path (``/bot<token>/sendMessage``)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        return _scope.get() is None


def _send_in_scope(original_send: Callable, scope: _Scope, session: requests.Session, request, kwargs: Dict[str, Any]):
    scope.check()
    try:
        host, port, addresses = resolve_public_https_url(request.url, resolver=scoped_resolver)
    except UnsafeUrlError as exc:
        raise BlockedRequestError(exc.reason) from None
    scope.pins[(host.lower().rstrip("."), int(port))] = addresses

    kwargs.update(NO_REDIRECT_REQUESTS_KWARGS)
    kwargs["stream"] = True
    kwargs["proxies"] = {}  # never an environment proxy: it would resolve / dial the name itself
    request.headers["Accept-Encoding"] = "identity"
    remaining = scope.remaining()
    if remaining is not None:
        timeout = kwargs.get("timeout")
        if isinstance(timeout, tuple):
            kwargs["timeout"] = tuple(min(item, remaining) if item else remaining for item in timeout)
        else:
            kwargs["timeout"] = min(timeout, remaining) if timeout else remaining
    try:
        response = original_send(session, request, **kwargs)
        _read_capped(response, RESPONSE_BODY_CAP_BYTES)
    except BaseException as exc:  # noqa: BLE001 - re-raised without the URL
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise _sanitized(exc, scope) from None
    return response


def _install_guard() -> None:
    global _guard_installed
    with _guard_lock:
        if _guard_installed:
            return
        original_send = requests.Session.send

        @functools.wraps(original_send)
        def send(self, request, **kwargs):
            scope = _scope.get()
            if scope is None:
                return original_send(self, request, **kwargs)
            return _send_in_scope(original_send, scope, self, request, kwargs)

        requests.Session.send = send  # type: ignore[method-assign]

        import urllib3.util.connection as urllib3_connection

        original_create_connection = urllib3_connection.create_connection

        @functools.wraps(original_create_connection)
        def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
            scope = _scope.get()
            if scope is None:
                return original_create_connection(address, timeout, source_address, socket_options)
            host, port = address[0], int(address[1])
            addresses = scope.pins.get((str(host).lower().rstrip("."), port))
            if not addresses:
                raise OSError("connection target was not validated")
            remaining = scope.remaining()
            if remaining is not None:
                if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
                    timeout = max(0.001, remaining)
                else:
                    timeout = max(0.001, min(timeout, remaining))
            last_error: Optional[OSError] = None
            for ip in addresses[:_MAX_PIN_ATTEMPTS]:  # the checked answers, in resolver order; the name is never resolved again
                try:
                    sock = original_create_connection((ip, port), timeout, source_address, socket_options)
                except OSError as exc:
                    last_error = exc
                    continue
                scope.register(sock)
                return sock
            raise last_error or OSError("no address to connect to")

        urllib3_connection.create_connection = create_connection  # type: ignore[assignment]

        # ``SSLContext.wrap_socket`` detaches the TCP socket it is given and performs the TLS handshake before it returns
        # the new ``SSLSocket``, so neither socket could be aborted while a server drips handshake bytes. Inside a scope the
        # handshake is therefore run by us, after the ``SSLSocket`` has been registered with the scope's watchdog.
        original_ssl_create = ssl.SSLSocket.__dict__["_create"].__func__

        def ssl_create(cls, sock, *args, **kwargs):
            scope = _scope.get()
            if scope is None or args or kwargs.get("server_side"):
                return original_ssl_create(cls, sock, *args, **kwargs)
            handshake = kwargs.pop("do_handshake_on_connect", True)
            wrapped = original_ssl_create(cls, sock, *args, do_handshake_on_connect=False, **kwargs)
            scope.register(wrapped)
            if handshake:
                try:
                    wrapped.do_handshake()
                except BaseException:
                    wrapped.close()
                    raise
            return wrapped

        ssl.SSLSocket._create = classmethod(ssl_create)  # type: ignore[method-assign]

        log_filter = _ScopeLogFilter()
        for name in _URLLIB3_LOGGERS:
            logging.getLogger(name).addFilter(log_filter)
        _guard_installed = True


@contextlib.contextmanager
def user_outbound_requests(deadline_seconds: Optional[float] = None) -> Iterator[None]:
    """Apply the user-target policy to every ``requests`` call made inside (re-entrant).

    ``deadline_seconds`` is the wall-clock budget of everything done inside (all requests together); inside a scope
    that already has a deadline the shorter one applies.
    """
    _install_guard()
    parent = _scope.get()
    if parent is not None and parent.remaining() is not None:
        deadline_seconds = parent.remaining() if not deadline_seconds else min(deadline_seconds, parent.remaining())
    scope = _Scope(max(0.001, deadline_seconds) if deadline_seconds else None)
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)
        scope.close()


def in_user_outbound_scope() -> bool:
    return _scope.get() is not None


def current_scope() -> Optional[_Scope]:
    """The active scope (``safe_smtp`` registers its socket and reads the remaining time from it)."""
    return _scope.get()


def check_user_target(url: Optional[str]) -> None:
    """Save-time / test-time form of the same check (raises :class:`UnsafeUrlError`)."""
    assert_public_https_url(url or "", resolver=scoped_resolver)
