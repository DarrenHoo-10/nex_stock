# -*- coding: utf-8 -*-
"""SSRF guard for URLs a user supplies and the server will call (docs/design/multi-user.md 3.5).

One reusable helper for every user-supplied outbound endpoint: model Base URLs, SearXNG
instances, the Hindsight URL and (P3) notification webhooks. A URL is accepted only when it

* is ``https`` without credentials, control characters or backslashes;
* has a real multi-label hostname (no ``localhost``, ``*.local`` / ``*.internal``, single labels);
* is not a numeric host alias (``2130706433``, ``0x7f.1``, ``127.1``, full-width digits ...);
* resolves, for every A / AAAA answer, to a public address - private, loopback, link-local,
  reserved, multicast, unspecified, carrier-grade NAT and cloud-metadata ranges are refused,
  including IPv4 addresses embedded in IPv6 (mapped, 6to4, NAT64, Teredo).

The check is meant to run twice: when a value is saved (fail early with a clear message) and
again right before the server connects (``assert_public_https_url``), because a hostname can
start resolving to a private address after it was saved (DNS rebinding). The pre-connect check
narrows that window to the gap between the check and the client's own resolution; pinning the
resolved address would need control of the HTTP client, which LiteLLM does not expose.

Redirect policy: the checks above only see the first URL, and a public host may answer with a 3xx to
``http://169.254.169.254/`` or ``https://127.0.0.1/``. Every request the server makes to a user-supplied
URL therefore must not follow redirects (a 3xx is an error for the caller, never a hop):

* ``requests`` callers pass ``**NO_REDIRECT_REQUESTS_KWARGS`` (``allow_redirects=False``);
* ``httpx`` callers that do not own their client (LiteLLM, the OpenAI SDK) run the call inside
  ``forbid_httpx_redirects()``, which turns ``follow_redirects`` off for every ``httpx`` request sent from
  that context (see its docstring for how).

New user-URL consumers (P3 notification webhooks ...) reuse these instead of adding their own policy.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import ipaddress
import socket
import threading
import unicodedata
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence
from urllib.parse import urlparse

Resolver = Callable[[str, int], Iterable[str]]

_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa", ".intranet")
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "100.64.0.0/10",   # carrier-grade NAT
        "192.0.0.0/24",    # IETF protocol assignments
        "198.18.0.0/15",   # benchmarking
        "169.254.0.0/16",  # link-local incl. cloud metadata
        "100.100.100.200/32",  # Alibaba Cloud metadata
        "fd00:ec2::/32",   # AWS IPv6 metadata
    )
)


class UnsafeUrlError(ValueError):
    """The URL is not an acceptable public https endpoint; ``reason`` is a stable machine code."""

    code = "unsafe_url"

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _default_resolver(host: str, port: int) -> List[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _normalize_host(host: str) -> str:
    candidate = (host or "").strip().lower().rstrip(".")
    if not candidate:
        raise UnsafeUrlError("missing_host", "URL has no host")
    if ":" in candidate:  # IPv6 literal
        return candidate
    try:
        normalized = unicodedata.normalize("NFKC", candidate)
        return normalized.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise UnsafeUrlError("invalid_host", "URL host is not a valid hostname") from exc


def _canonical_ipv4(host: str) -> Optional[str]:
    """Canonical dotted quad for libc-style numeric aliases (``inet_aton`` accepts ``127.1``), else None."""
    try:
        return socket.inet_ntoa(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _embedded_ipv4(address: ipaddress.IPv6Address) -> List[ipaddress.IPv4Address]:
    embedded: List[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if address in ipaddress.ip_network("64:ff9b::/96"):  # NAT64
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return embedded


def is_public_ip(address: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """Whether ``address`` is a globally routable unicast address (embedded IPv4 included)."""
    if isinstance(address, ipaddress.IPv6Address):
        for embedded in _embedded_ipv4(address):
            if not is_public_ip(embedded):
                return False
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or not address.is_global
    ):
        return False
    return not any(address in network for network in _BLOCKED_NETWORKS if network.version == address.version)


def _parse_ip(text: str) -> "Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]":
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


def _check_syntax(url: str, *, schemes: Sequence[str]):
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("empty", "URL is empty")
    if any(char == "\\" or char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise UnsafeUrlError("invalid_url", "URL contains whitespace, control characters or backslashes")
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("invalid_url", "URL is not valid") from exc
    if (parsed.scheme or "").lower() not in schemes:
        raise UnsafeUrlError("scheme_not_allowed", "Only https URLs are allowed")
    if not parsed.netloc or not host:
        raise UnsafeUrlError("missing_host", "URL has no host")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("credentials_in_url", "URL must not contain credentials")
    return parsed, host, port


def check_public_https_url(
    url: str,
    *,
    resolver: Optional[Resolver] = None,
    resolve: bool = True,
    schemes: Sequence[str] = ("https",),
) -> str:
    """Return ``url`` (stripped) when it is a public https endpoint, else raise :class:`UnsafeUrlError`.

    ``resolve=False`` skips DNS (syntax and IP-literal checks only); production callers keep the
    default so a hostname that points at a private address is refused. ``resolver`` maps
    ``(host, port)`` to address strings and exists so tests never touch the network.
    """
    url = url.strip() if isinstance(url, str) else url
    _parsed, raw_host, port = _check_syntax(url, schemes=schemes)
    host = _normalize_host(raw_host)

    literal = _parse_ip(host)
    if literal is not None:
        if not is_public_ip(literal):
            raise UnsafeUrlError("private_address", "URL must not target a private, loopback or link-local address")
        return url

    canonical = _canonical_ipv4(host)
    if canonical is not None:
        # inet_aton accepts "127.1", "0x7f.1", "2130706433": numeric aliases that hide the real address.
        raise UnsafeUrlError("numeric_host", "URL host must be a hostname or a canonical IP address")
    if host.replace(".", "").isdigit() or host.startswith("0x"):
        raise UnsafeUrlError("numeric_host", "URL host must be a hostname or a canonical IP address")

    if host == "localhost" or host in _METADATA_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeUrlError("private_host", "URL host is a local or internal name")
    if "." not in host:
        raise UnsafeUrlError("private_host", "URL host must be a fully qualified public hostname")

    if not resolve:
        return url
    lookup = resolver or _default_resolver
    try:
        answers = list(lookup(host, port or 443))
    except OSError as exc:
        raise UnsafeUrlError("dns_failure", f"URL host {host} cannot be resolved") from exc
    if not answers:
        raise UnsafeUrlError("dns_failure", f"URL host {host} cannot be resolved")
    for answer in answers:
        address = _parse_ip(str(answer))
        if address is None or not is_public_ip(address):
            raise UnsafeUrlError("private_address", "URL host resolves to a private, loopback or link-local address")
    return url


def assert_public_https_url(url: str, *, resolver: Optional[Resolver] = None) -> str:
    """Call-time form of :func:`check_public_https_url` (re-resolves DNS right before connecting)."""
    return check_public_https_url(url, resolver=resolver, resolve=True)


def split_url_list(value: str) -> List[str]:
    """Split a comma / semicolon / newline separated URL list (``SEARXNG_BASE_URLS``, ``CUSTOM_WEBHOOK_URLS``)."""
    normalized = (value or "").replace(";", ",").replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def is_public_https_url(url: str, *, resolver: Optional[Resolver] = None) -> bool:
    try:
        check_public_https_url(url, resolver=resolver)
    except UnsafeUrlError:
        return False
    return True


# --------------------------------------------------------------------------- redirect policy

#: ``requests`` keyword arguments for a request to a user-supplied URL: a redirect is returned, never followed.
NO_REDIRECT_REQUESTS_KWARGS: Dict[str, bool] = {"allow_redirects": False}

_redirects_forbidden: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "url_safety_redirects_forbidden", default=False
)
_guard_lock = threading.Lock()
_guard_installed = False


def _install_httpx_redirect_guard() -> None:
    """Wrap ``httpx.Client.send`` / ``AsyncClient.send`` once so a forbidden context never follows redirects.

    ``send`` is httpx's public single entry point (``get`` / ``post`` / ``request`` / ``stream`` and the OpenAI
    SDK all end there) and takes ``follow_redirects`` per call, so no client has to be replaced. Outside a
    ``forbid_httpx_redirects()`` context the wrapper passes every call through untouched.
    """
    global _guard_installed
    with _guard_lock:
        if _guard_installed:
            return
        import httpx

        original_send = httpx.Client.send
        original_async_send = httpx.AsyncClient.send

        @functools.wraps(original_send)
        def send(self, request, *args, **kwargs):
            if _redirects_forbidden.get():
                kwargs["follow_redirects"] = False
            return original_send(self, request, *args, **kwargs)

        @functools.wraps(original_async_send)
        async def async_send(self, request, *args, **kwargs):
            if _redirects_forbidden.get():
                kwargs["follow_redirects"] = False
            return await original_async_send(self, request, *args, **kwargs)

        httpx.Client.send = send  # type: ignore[method-assign]
        httpx.AsyncClient.send = async_send  # type: ignore[method-assign]
        _guard_installed = True


@contextlib.contextmanager
def forbid_httpx_redirects() -> Iterator[None]:
    """Within this context (this thread / task) no ``httpx`` request follows a redirect.

    For HTTP clients the caller does not own: LiteLLM keeps a different client per provider kind (the OpenAI
    SDK, its own handler, a Router-managed cache) and builds them with ``follow_redirects=True``, so a per-call
    client would have to be injected for each kind and each Router deployment. Wrap the call that sends the
    request, not the code that later reads a streamed body: the redirect decision is made when the request is
    sent. A redirect response then surfaces as the provider's HTTP error.
    """
    _install_httpx_redirect_guard()
    token = _redirects_forbidden.set(True)
    try:
        yield
    finally:
        _redirects_forbidden.reset(token)
