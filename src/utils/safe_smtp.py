# -*- coding: utf-8 -*-
"""SMTP connections to a server a user supplied (personal e-mail channel, design 3.5 / 5).

``smtplib`` has no redirects, but the host still has to be a public one and must stay the one that was checked:
the host is resolved once, every answer must be public (``resolve_public_host``), and the socket connects to the
checked address itself (pinned), so a second DNS answer cannot point the connection somewhere else. TLS still
verifies the certificate against the original host name. Only the submission ports below are allowed; 465 is
implicit TLS, 587 / 25 must upgrade with STARTTLS (credentials are never sent in clear text).

Inside a ``user_outbound_requests(deadline_seconds=...)`` scope the connection shares that scope's hard wall-clock
deadline (DNS, connect, TLS, every command): the socket is registered with the scope's watchdog, which shuts it down
when the deadline passes, and the per-operation timeout never exceeds the time left. The EHLO / HELO name is a fixed
``EHLO_HOSTNAME`` - ``smtplib`` would otherwise send ``socket.getfqdn()``, the server's own host name, to a server the
user chose.
"""

from __future__ import annotations

import smtplib
import socket
import ssl
from typing import Optional

from src.utils.safe_requests import current_scope, scoped_resolver
from src.utils.url_safety import Resolver, UnsafeUrlError, resolve_public_host

ALLOWED_SMTP_PORTS = (465, 587, 25)
IMPLICIT_TLS_PORT = 465
EHLO_HOSTNAME = "localhost"  # fixed: never the server's own ``socket.getfqdn()``


def check_smtp_endpoint(host: str, port: int, *, resolver: Optional[Resolver] = None) -> str:
    """Return the public address to connect to, or raise :class:`UnsafeUrlError` (also for a port that is not allowed)."""
    if port not in ALLOWED_SMTP_PORTS:
        raise UnsafeUrlError("port_not_allowed", f"SMTP port must be one of {', '.join(map(str, ALLOWED_SMTP_PORTS))}")
    return resolve_public_host(host, port, resolver=resolver or scoped_resolver)[0]


class _PinnedMixin:
    _pinned_ip: str = ""

    def _get_socket(self, host, port, timeout):  # noqa: D401 - smtplib hook
        scope = current_scope()
        if scope is not None:
            scope.check()
            remaining = scope.remaining()
            if remaining is not None:
                timeout = max(0.001, min(timeout, remaining)) if timeout else max(0.001, remaining)
        sock = socket.create_connection((self._pinned_ip, port), timeout, self.source_address)
        if scope is not None:
            scope.register(sock)  # the scope's watchdog shuts it down at the deadline
        context = getattr(self, "context", None)
        if context is not None:  # implicit TLS (SMTP_SSL)
            return context.wrap_socket(sock, server_hostname=self._host)
        return sock


class _PinnedSMTP(_PinnedMixin, smtplib.SMTP):
    pass


class _PinnedSMTPSSL(_PinnedMixin, smtplib.SMTP_SSL):
    pass


def connect_pinned_smtp(
    host: str,
    port: int,
    *,
    timeout: float = 30,
    resolver: Optional[Resolver] = None,
) -> smtplib.SMTP:
    """Open an SMTP session to ``host:port`` over the checked public address (TLS on, certificate verified)."""
    address = check_smtp_endpoint(host, port, resolver=resolver)
    context = ssl.create_default_context()
    if port == IMPLICIT_TLS_PORT:
        server: smtplib.SMTP = _PinnedSMTPSSL(timeout=timeout, context=context, local_hostname=EHLO_HOSTNAME)
    else:
        server = _PinnedSMTP(timeout=timeout, local_hostname=EHLO_HOSTNAME)
    server._pinned_ip = address  # type: ignore[attr-defined]
    server._host = host  # type: ignore[attr-defined]  # TLS server name; ``connect`` below dials the pinned address
    try:
        server.connect(host, port)
        if port != IMPLICIT_TLS_PORT:
            server.starttls(context=context)
            server.ehlo()
    except Exception:
        try:
            server.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    return server
