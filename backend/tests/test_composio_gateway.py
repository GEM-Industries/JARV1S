import pytest

from core.integrations.composio_gateway import ComposioCatalogError, ComposioGateway


@pytest.mark.asyncio
async def test_composio_gateway_caches_mcp_clients(monkeypatch):
    created: list["FakeMCPClient"] = []

    class FakeMCPClient:
        def __init__(self, *, url, headers):
            self.url = url
            self.headers = headers
            self.started = False
            self.shutdown_called = False
            created.append(self)

        async def start(self):
            self.started = True

        async def call_tool(self, tool_name, arguments):
            return {"tool": tool_name, "arguments": arguments}

        async def shutdown(self):
            self.shutdown_called = True

    gateway = ComposioGateway(
        api_key="test-composio-key",
        user_id="user-1",
        callback_host="http://localhost:8000",
        frontend_origin="http://localhost:5173",
    )
    async def fake_get_mcp_url(_app_name):
        return "https://mcp.test/spotify"

    monkeypatch.setattr(gateway, "get_mcp_url", fake_get_mcp_url)
    monkeypatch.setattr("core.integrations.composio_gateway.StreamableHTTPClient", FakeMCPClient)

    try:
        first = await gateway.call_mcp_tool("spotify", "SPOTIFY_TEST", {"q": "x"})
        second = await gateway.call_mcp_tool("spotify", "SPOTIFY_TEST", {"q": "y"})
    finally:
        await gateway.shutdown()

    assert first == {"tool": "SPOTIFY_TEST", "arguments": {"q": "x"}}
    assert second == {"tool": "SPOTIFY_TEST", "arguments": {"q": "y"}}
    assert len(created) == 1
    assert created[0].headers == {"x-api-key": "test-composio-key"}
    assert created[0].started is True
    assert created[0].shutdown_called is True


@pytest.mark.asyncio
async def test_on_app_connected_mounts_nothing_without_allowlist(monkeypatch):
    gateway = ComposioGateway(
        api_key="test-composio-key",
        user_id="user-1",
        callback_host="http://localhost:8000",
        frontend_origin="http://localhost:5173",
    )

    async def boom(_app_name):
        raise AssertionError("must not contact Composio MCP without an allowlist")

    monkeypatch.setattr(gateway, "get_mcp_url", boom)

    assert await gateway.on_app_connected("github", tools_allowlist=None) is False
    assert await gateway.on_app_connected("github", tools_allowlist=[]) is False


@pytest.mark.asyncio
async def test_list_trigger_types_raises_on_http_failure(monkeypatch):
    gateway = ComposioGateway(
        api_key="test-composio-key",
        user_id="user-1",
        callback_host="http://localhost:8000",
        frontend_origin="http://localhost:5173",
    )

    class Response:
        status_code = 500
        text = "upstream unavailable"

    async def fail(*_args, **_kwargs):
        return Response()

    monkeypatch.setattr(gateway, "_request", fail)

    with pytest.raises(ComposioCatalogError, match="Failed to list trigger types"):
        await gateway.list_trigger_types("slack")

