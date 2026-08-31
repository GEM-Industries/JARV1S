from __future__ import annotations

import pytest

from core.integrations.lifecycle.bespoke import refresh_non_composio_integrations
from core.integrations.mcp import bridge
from core.integrations.mcp.client import stdio_child_environment
from core.integrations.mcp.config import (
    MCPConfigError,
    MCPServerConfig,
    load_mcp_config,
    parse_mcp_document,
)


@pytest.mark.parametrize(
    ("raw", "extras", "reserved", "error"),
    [
        (
            {"servers": [{"name": "echo", "command": ["npx", "echo"]}]},
            False,
            None,
            None,
        ),
        (
            {"servers": [{"name": "github", "type": "composio"}]},
            False,
            None,
            None,
        ),
        (
            {"servers": [{"name": "github", "type": "composio"}]},
            True,
            None,
            "Composio",
        ),
        (
            {"servers": [{"name": "echo", "command": ["npx"], "trusted": True}]},
            True,
            None,
            "trusted",
        ),
        (
            {"servers": [{"name": "calendar", "command": ["npx"]}]},
            True,
            {"calendar"},
            "collides",
        ),
        (
            {
                "servers": [
                    {"name": "echo", "command": ["true"]},
                    {"name": "echo", "url": "https://example/mcp"},
                ]
            },
            False,
            None,
            "Duplicate",
        ),
        (
            {
                "servers": [
                    {
                        "name": "echo",
                        "command": ["npx"],
                        "env": {"GITHUB_TOKEN": "ghp_secret"},
                    }
                ]
            },
            False,
            None,
            "inline secret",
        ),
        (
            {
                "servers": [
                    {
                        "name": "echo",
                        "command": ["npx"],
                        "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                    }
                ]
            },
            False,
            None,
            None,
        ),
    ],
)
def test_mcp_json_contract(raw, extras, reserved, error):
    if error:
        with pytest.raises(MCPConfigError, match=error):
            parse_mcp_document(raw, extras=extras, reserved_names=reserved)
        return
    servers = parse_mcp_document(raw, extras=extras, reserved_names=reserved)
    assert [server.name for server in servers] == [item["name"] for item in raw["servers"]]


def test_packaged_mcp_servers_json_parses():
    from core.config import settings

    servers = load_mcp_config(settings.MCP_SERVERS_CONFIG)
    names = {server.name for server in servers}
    assert {"github", "slack", "google_maps", "zoom"} <= names
    assert "spotify" not in names
    assert all(server.type == "composio" for server in servers)


def test_stdio_child_environment_does_not_inherit_ambient_credentials():
    environ = {
        "PATH": "/usr/bin",
        "HOME": "/Users/geoff",
        "OPENAI_API_KEY": "sk-secret",
        "GITHUB_TOKEN": "ghp_secret",
    }
    env = stdio_child_environment(
        {"TOKEN": "${GITHUB_TOKEN}", "FLAG": "1"},
        environ=environ,
    )
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/Users/geoff"
    assert env["TOKEN"] == "ghp_secret"
    assert env["FLAG"] == "1"
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env


@pytest.mark.asyncio
async def test_invalid_refresh_preserves_live_servers(monkeypatch):
    monkeypatch.setattr(bridge, "_live_bridge_names", frozenset({"keepme"}))
    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.load_runtime_mcp_servers",
        lambda: (_ for _ in ()).throw(MCPConfigError("bad json")),
    )
    torn: list[str] = []

    async def _teardown(name: str) -> bool:
        torn.append(name)
        return True

    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.teardown_local_integration",
        _teardown,
    )

    result = await refresh_non_composio_integrations()
    assert result == []
    assert torn == []
    assert bridge.live_bridge_names() == frozenset({"keepme"})


@pytest.mark.asyncio
async def test_refresh_tears_down_removed_servers(monkeypatch):
    monkeypatch.setattr(bridge, "_live_bridge_names", frozenset({"old", "keep"}))

    keep = MCPServerConfig(name="keep", command=["true"])
    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.load_runtime_mcp_servers",
        lambda: [keep],
    )
    torn: list[str] = []

    async def _teardown(name: str) -> bool:
        torn.append(name)
        return True

    async def _load(configs=None):
        names = [config.name for config in (configs or []) if config.type != "composio"]
        bridge._live_bridge_names = frozenset(names)
        return names

    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.teardown_local_integration",
        _teardown,
    )
    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.load_mcp_bridges",
        _load,
    )
    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.invalidate_cache",
        lambda _name: None,
    )

    async def _register(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.tool_router.register_plugin",
        _register,
    )
    monkeypatch.setattr(
        "core.integrations.lifecycle.bespoke.registry.plugins",
        {},
    )

    result = await refresh_non_composio_integrations()
    assert "old" in torn
    assert "old" not in result
    assert result == ["keep"]
    assert bridge.live_bridge_names() == frozenset({"keep"})
