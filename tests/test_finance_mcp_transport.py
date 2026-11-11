"""Real Streamable HTTP MCP client, with only Stock's outbound HTTP boundary mocked."""
import asyncio

import pytest


def test_delegated_mcp_headers_discovery_call_and_manager_boundary(monkeypatch):
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from mcp_server import tools, finance
    from src.services import finance_identity
    monkeypatch.setattr(tools.config, "MCP_BEARER_TOKEN", "manager-secret-for-test")
    values = {"FINANCE_SERVICE_TOKEN": "s" * 40, "FINANCE_CORE_ID": "test-core", "FINANCE_INSTANCE_ID": "test-stock"}
    monkeypatch.setattr(finance_identity, "setting", lambda key: values.get(key, ""))
    sent = []
    def upstream(method, path, headers, body=None):
        assert headers["authorization"] == "Bearer " + "s" * 40
        assert headers["x-finance-delegation"] == "delegated-user-test"
        sent.append((method, path, dict(headers), body))
        if path == "/delegations/verify":
            return {"subject": "alice"}
        if path == "/tools":
            return {"version": "finance.v1", "tools": [{"name": "strategy_inputs"}]}
        return {"version": "finance.v1", "data": {"owner": "alice"}}
    monkeypatch.setattr(finance, "finance_request", upstream)
    actual_app = tools.mcp.http_app(path="/mcp", stateless_http=True)
    monkeypatch.setattr(tools.mcp, "http_app", lambda **kwargs: actual_app)
    app = tools.create_asgi_app()
    def factory(**kwargs):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), **kwargs)
    async def run():
        async with actual_app.router.lifespan_context(actual_app):
            transport = StreamableHttpTransport("http://testserver/mcp", headers={
                "Authorization": "Bearer " + "s" * 40, "X-Finance-Delegation": "delegated-user-test",
                "X-Finance-Audience": "private"}, httpx_client_factory=factory)
            async with Client(transport) as client:
                listed = await client.list_tools()
                assert {tool.name for tool in listed} == {"financial_discover", "financial_call"}
                descriptor = next(tool for tool in listed if tool.name == "financial_call")
                assert not {"user_id", "authorization", "delegationToken"} & set(descriptor.input_schema["properties"])
                discovered = await client.call_tool("financial_discover")
                assert discovered.data["version"] == "finance.v1"
                result = await client.call_tool("financial_call", {"name": "call_api", "arguments": {
                    "method": "GET", "path": "/api/v1/stocks/watchlist"}})
                assert result.data["data"]["owner"] == "alice"
                with pytest.raises(Exception, match="FINANCE_MANAGER_TOOL_FORBIDDEN"):
                    await client.call_tool("service_stop")
            admin = StreamableHttpTransport("http://testserver/mcp", headers={"Authorization": "Bearer manager-secret-for-test"}, httpx_client_factory=factory)
            async with Client(admin) as client:
                with pytest.raises(Exception, match="FINANCE_DELEGATION_REQUIRED"):
                    await client.call_tool("financial_discover")
            # A hot configuration change must not turn the finance credential
            # into the legacy process-manager privilege after startup.
            values["FINANCE_SERVICE_TOKEN"] = tools.config.MCP_BEARER_TOKEN
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                response = await http.get("http://testserver/mcp", headers={"Authorization": "Bearer " + tools.config.MCP_BEARER_TOKEN})
                assert response.status_code == 503
    asyncio.run(run())
    assert any(path == "/tools/call" for _, path, _, _ in sent)


def test_mcp_preserves_stable_domain_error_without_remote_sensitive_message(monkeypatch):
    import io
    import urllib.error
    from fastmcp.exceptions import ToolError
    from mcp_server.finance import finance_request
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError("http://stock.test", 422, "failed", {}, io.BytesIO(
            b'{"data":{"detail":{"error":"intraday_identity_unsupported","message":"sensitive credential"}}}'))
    monkeypatch.setattr("urllib.request.urlopen", denied)
    with pytest.raises(ToolError) as exc:
        finance_request("POST", "/tools/call", {"authorization": "Bearer private-test"}, {})
    assert "intraday_identity_unsupported" in str(exc.value) and "422" in str(exc.value)
    assert "sensitive credential" not in str(exc.value) and "private-test" not in str(exc.value)
