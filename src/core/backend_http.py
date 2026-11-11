"""Pooled server-to-server HTTP with optional IP routing and hostname-verified TLS."""
from functools import lru_cache
from http.cookiejar import DefaultCookiePolicy
from ipaddress import ip_address
from urllib.parse import urlsplit

import requests


class BackendRouteError(ValueError):
    """The configured private route cannot be used."""


class _HostnameTLSAdapter(requests.adapters.HTTPAdapter):
    """Dial the private address but authenticate the configured service hostname."""

    def __init__(self, hostname: str, pool_size: int):
        self.hostname = hostname
        super().__init__(pool_connections=1, pool_maxsize=pool_size)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        super().init_poolmanager(connections, maxsize, block=block, server_hostname=self.hostname,
                                 assert_hostname=self.hostname, **kwargs)


@lru_cache(maxsize=4)
def backend_session(hostname: str, pool_size: int) -> requests.Session:
    session = requests.Session()
    session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    if hostname:
        session.trust_env = False  # The backend must dial the configured address directly.
        session.mount("https://", _HostnameTLSAdapter(hostname, pool_size))
    else:
        adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=pool_size)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
    return session


def backend_request(method, url, *, connect_ip, pool_size, headers, timeout, **kwargs):
    hostname = ""
    if connect_ip:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise BackendRouteError("Private routing requires HTTPS")
        try:
            ip = ip_address(connect_ip)
        except ValueError as exc:
            raise BackendRouteError("Invalid private connection IP") from exc
        target = f"[{ip}]" if ip.version == 6 else str(ip)
        if parts.port is not None:
            target += f":{parts.port}"
        hostname = parts.hostname
        headers = {**headers, "Host": parts.netloc}
        url = parts._replace(netloc=target).geturl()
    return backend_session(hostname, pool_size).request(
        method, url, headers=headers, timeout=timeout, allow_redirects=False, **kwargs,
    )
