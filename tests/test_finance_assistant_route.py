"""Verify the actual finance transport against an isolated TLS endpoint."""
import pytest
from fastapi import HTTPException

from api.v1.endpoints import finance_assistant as finance
from src.core.backend_http import backend_session
from tests.test_multi_user_p3a_hardening import TlsServer, _make_certificate


def test_finance_private_route_preserves_tls_identity_and_bearer(tmp_path, monkeypatch):
    host = "finance.example.test"
    cert, key = _make_certificate(tmp_path, [host])
    endpoint = TlsServer(cert, key, bind="127.0.0.1")
    endpoint.start()
    settings = {"FINANCE_CORE_URL": f"https://{host}:{endpoint.port}", "FINANCE_CORE_CONNECT_IP": "127.0.0.1"}
    monkeypatch.setattr(finance, "setting", settings.get)
    try:
        for name in (host, "wrong.example.test"):
            monkeypatch.setattr(backend_session(name, 8), "verify", cert)
        status, body = finance.core_call("GET", "agents/messages", "synthetic", params={"after": 11})
        assert status == 200 and body["ok"]
        assert endpoint.sni == [host]
        req = endpoint.requests[0]
        assert "GET /api/agents/messages?after=11 " in req
        assert f"Host: {host}:{endpoint.port}" in req and "Authorization: Bearer synthetic" in req
        settings["FINANCE_CORE_URL"] = f"https://wrong.example.test:{endpoint.port}"
        with pytest.raises(HTTPException) as wrong:
            finance.core_call("GET", "agents/topics", "synthetic")
        assert wrong.value.status_code == 503
        settings["FINANCE_CORE_CONNECT_IP"] = "invalid"
        with pytest.raises(HTTPException) as invalid:
            finance.core_call("GET", "agents/topics", "synthetic")
        assert invalid.value.detail["error"] == "FINANCE_CORE_ROUTE_INVALID"
        assert len(endpoint.requests) == 1
    finally:
        endpoint.close()
