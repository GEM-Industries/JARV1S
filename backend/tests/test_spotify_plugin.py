import pytest

from plugins.spotify import SpotifyPlugin


@pytest.mark.asyncio
async def test_spotify_delegates_mcp_calls_to_composio_gateway(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    class FakeGateway:
        async def call_mcp_tool(self, app_name: str, tool_name: str, arguments: dict):
            calls.append((app_name, tool_name, arguments))
            return {"successfull": True, "data": {"data": {"ok": True}}}

    monkeypatch.setattr(
        "core.integrations.composio_gateway.get_composio_gateway",
        lambda: FakeGateway(),
    )

    result = await SpotifyPlugin()._mcp("SPOTIFY_TEST", q="hello", empty=None)

    assert calls == [("spotify", "SPOTIFY_TEST", {"q": "hello"})]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_spotify_reports_missing_composio_gateway(monkeypatch):
    monkeypatch.setattr(
        "core.integrations.composio_gateway.get_composio_gateway",
        lambda: None,
    )

    result = await SpotifyPlugin()._mcp("SPOTIFY_TEST")

    assert result == {"error": "Composio gateway not configured"}


@pytest.mark.asyncio
async def test_transfer_playback_resolves_device_by_name(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append((tool_name, kwargs))
        if tool_name == "SPOTIFY_GET_AVAILABLE_DEVICES":
            return {
                "devices": [
                    {"id": "mac", "name": "Geoff's MacBook", "is_active": True},
                    {"id": "kitchen", "name": "Kitchen", "is_active": False},
                ]
            }
        return {}

    monkeypatch.setattr("plugins.spotify.SpotifyPlugin._mcp", fake_mcp)
    result = await SpotifyPlugin().transfer_playback("kitchen")
    assert result.success is True
    assert ("SPOTIFY_TRANSFER_PLAYBACK", {"device_ids": ["kitchen"], "play": True}) in calls


@pytest.mark.asyncio
async def test_transfer_playback_lists_devices_when_name_is_ambiguous(monkeypatch):
    async def fake_mcp(self, tool_name: str, **kwargs):
        return {
            "devices": [
                {"id": "a", "name": "Kitchen speaker", "is_active": False},
                {"id": "b", "name": "Kitchen TV", "is_active": False},
            ]
        }

    monkeypatch.setattr("plugins.spotify.SpotifyPlugin._mcp", fake_mcp)
    result = await SpotifyPlugin().transfer_playback("kitchen")
    assert result.success is False
    assert "Kitchen speaker" in (result.error or "")
    assert "Kitchen TV" in (result.error or "")


@pytest.mark.asyncio
async def test_skip_previous_uses_previous_endpoint(monkeypatch):
    calls: list[str] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append(tool_name)
        return {}

    monkeypatch.setattr("plugins.spotify.SpotifyPlugin._mcp", fake_mcp)
    result = await SpotifyPlugin().skip(direction="previous")
    assert result.success is True
    assert calls == ["SPOTIFY_SKIP_TO_PREVIOUS"]

