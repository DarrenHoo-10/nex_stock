# -*- coding: utf-8 -*-
"""SSRF guard for user-supplied URLs (src/utils/url_safety.py). DNS is always injected: no network."""

import socket

import pytest

from src.utils.url_safety import (
    UnsafeUrlError,
    assert_public_https_url,
    check_public_https_url,
    is_public_https_url,
    split_url_list,
)

PUBLIC = "93.184.216.34"


def public_dns(host, port):
    return [PUBLIC]


@pytest.mark.parametrize("url", [
    "https://api.example.com/v1",
    "https://api.example.com:8443/v1/",
    "https://xn--fsq.example.com",
    "https://93.184.216.34/v1",
    "https://[2606:2800:220:1:248:1893:25c8:1946]/v1",
])
def test_public_https_urls_are_accepted(url):
    assert check_public_https_url(url, resolver=public_dns) == url


@pytest.mark.parametrize(("url", "reason"), [
    ("http://api.example.com/v1", "scheme_not_allowed"),
    ("ftp://api.example.com/", "scheme_not_allowed"),
    ("//api.example.com/v1", "scheme_not_allowed"),
    ("https://127.0.0.1/v1", "private_address"),
    ("https://127.1/v1", "numeric_host"),
    ("https://0x7f.0.0.1/v1", "numeric_host"),
    ("https://2130706433/v1", "numeric_host"),
    ("https://0177.0.0.1/v1", "numeric_host"),
    ("https://10.1.2.3/v1", "private_address"),
    ("https://172.16.0.9/v1", "private_address"),
    ("https://192.168.1.1/v1", "private_address"),
    ("https://169.254.169.254/latest/meta-data", "private_address"),
    ("https://100.100.100.200/", "private_address"),
    ("https://100.64.0.1/", "private_address"),
    ("https://0.0.0.0/", "private_address"),
    ("https://[::1]/v1", "private_address"),
    ("https://[::ffff:127.0.0.1]/v1", "private_address"),
    ("https://[::ffff:169.254.169.254]/v1", "private_address"),
    ("https://[64:ff9b::a00:1]/v1", "private_address"),          # NAT64 of 10.0.0.1
    ("https://[2002:7f00:1::]/v1", "private_address"),            # 6to4 of 127.0.0.1
    ("https://[fe80::1]/v1", "private_address"),
    ("https://[fd00:ec2::254]/", "private_address"),
    ("https://localhost/v1", "private_host"),
    ("https://LOCALHOST./v1", "private_host"),
    ("https://app.localhost/v1", "private_host"),
    ("https://printer.local/v1", "private_host"),
    ("https://db.internal/v1", "private_host"),
    ("https://metadata.google.internal/computeMetadata/v1", "private_host"),
    ("https://intranet/v1", "private_host"),
    ("https://user:pass@api.example.com/v1", "credentials_in_url"),
    ("https://api.example.com@evil.example.com/", "credentials_in_url"),
    ("https://api.example.com\\@evil.example.com/", "invalid_url"),
    ("https://api.example.com/a b", "invalid_url"),
    ("https://api.example.com/a\nb", "invalid_url"),
    ("https://①⑥⑨.254.169.254/v1", "private_address"),  # full-width digits normalise to 169.254.169.254
    ("", "empty"),
    ("https://", "missing_host"),
])
def test_unsafe_urls_are_rejected(url, reason):
    with pytest.raises(UnsafeUrlError) as excinfo:
        check_public_https_url(url, resolver=public_dns)
    assert excinfo.value.reason == reason or reason == "invalid_url"
    assert is_public_https_url(url, resolver=public_dns) is False


@pytest.mark.parametrize("answer", [
    "127.0.0.1", "10.0.0.8", "192.168.0.10", "169.254.169.254", "::1", "fe80::1", "fd12::1", "::ffff:10.0.0.1",
])
def test_hostname_resolving_to_a_private_address_is_rejected(answer):
    with pytest.raises(UnsafeUrlError) as excinfo:
        check_public_https_url("https://api.example.com/v1", resolver=lambda host, port: [answer])
    assert excinfo.value.reason == "private_address"


def test_one_private_answer_among_public_ones_rejects_the_host():
    with pytest.raises(UnsafeUrlError):
        check_public_https_url("https://api.example.com", resolver=lambda host, port: [PUBLIC, "10.0.0.1"])


def test_dns_failure_and_empty_answer_fail_closed():
    def broken(host, port):
        raise socket.gaierror("no such host")

    with pytest.raises(UnsafeUrlError) as excinfo:
        check_public_https_url("https://api.example.com", resolver=broken)
    assert excinfo.value.reason == "dns_failure"
    with pytest.raises(UnsafeUrlError):
        check_public_https_url("https://api.example.com", resolver=lambda host, port: [])


def test_call_time_check_resolves_again_so_dns_rebinding_is_caught():
    answers = {"api.example.com": [PUBLIC]}
    lookups = []

    def resolver(host, port):
        lookups.append(host)
        return answers[host]

    assert check_public_https_url("https://api.example.com/v1", resolver=resolver)  # save time: public
    answers["api.example.com"] = ["10.0.0.5"]                                          # later: rebound to the LAN
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url("https://api.example.com/v1", resolver=resolver)
    assert lookups == ["api.example.com", "api.example.com"]


def test_default_resolver_is_socket_getaddrinfo(monkeypatch):
    seen = []

    def fake_getaddrinfo(host, port, **kwargs):
        seen.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", port))]

    monkeypatch.setattr("src.utils.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        check_public_https_url("https://api.example.com:8443/v1")
    assert seen == [("api.example.com", 8443)]


def test_resolve_false_skips_dns_but_keeps_syntax_and_literal_checks():
    assert check_public_https_url("https://api.example.com", resolve=False)
    with pytest.raises(UnsafeUrlError):
        check_public_https_url("https://10.0.0.1", resolve=False)


def test_split_url_list():
    assert split_url_list(" https://a.example.com , https://b.example.com;https://c.example.com\n") == [
        "https://a.example.com", "https://b.example.com", "https://c.example.com",
    ]
    assert split_url_list("") == []
