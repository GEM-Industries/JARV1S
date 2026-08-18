"""Tests for fixed-candidate Home Assistant discovery."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from plugins.smart_home import discovery


@pytest.mark.asyncio
async def test_discover_prefers_homeassistant_local_over_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery,
        "resolve_ha_connection",
        AsyncMock(return_value=(None, None)),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in {"homeassistant.local", "127.0.0.1", "localhost"}:
            return httpx.Response(401, json={"message": "Auth required."})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(discovery.httpx, "AsyncClient", client_factory)

    result = await discovery.discover_home_assistant()
    assert result == "http://homeassistant.local:8123"


@pytest.mark.asyncio
async def test_discover_prefers_explicit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery,
        "resolve_ha_connection",
        AsyncMock(return_value=(None, None)),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("http://192.168.1.50:8123/"):
            return httpx.Response(200, json={"message": "API running."})
        return httpx.Response(401, json={"message": "Auth required."})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(discovery.httpx, "AsyncClient", client_factory)

    result = await discovery.discover_home_assistant(preferred_url="http://192.168.1.50:8123")
    assert result == "http://192.168.1.50:8123"


@pytest.mark.asyncio
async def test_discover_returns_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery,
        "resolve_ha_connection",
        AsyncMock(return_value=(None, None)),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(discovery.httpx, "AsyncClient", client_factory)

    result = await discovery.discover_home_assistant()
    assert result is None
