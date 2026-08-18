"""Tests for the direct Home Assistant client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from plugins.smart_home.ha_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantError,
    normalize_ha_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ha"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8123", "http://localhost:8123"),
        ("homeassistant.local:8123", "http://homeassistant.local:8123"),
        ("http://localhost:8123/", "http://localhost:8123"),
    ],
)
def test_normalize_ha_url(raw: str, expected: str) -> None:
    assert normalize_ha_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://homeassistant.local:8123",
        "http://user:password@homeassistant.local:8123",
    ],
)
def test_normalize_ha_url_rejects_unsupported_or_credentialed_urls(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_ha_url(raw)


@pytest.mark.asyncio
async def test_ping_success() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json=_load("api_ping.json"))

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        token="test-token",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    data = await client.ping()
    assert data["message"] == "API running."
    await client.aclose()


@pytest.mark.asyncio
async def test_ping_rejects_invalid_token() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid token"})

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        token="bad",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(HomeAssistantAuthError):
        await client.ping()
    await client.aclose()


@pytest.mark.asyncio
async def test_get_states() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states"
        return httpx.Response(200, json=_load("states_sample.json"))

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        token="test-token",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    states = await client.get_states()
    assert len(states) == 3
    assert states[0]["entity_id"] == "light.living_room"
    await client.aclose()


@pytest.mark.asyncio
async def test_call_service() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        token="test-token",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await client.call_service("light", "turn_on", entity_id=["light.one", "light.two"], data={"brightness": 128})
    assert requests[0].url.path == "/api/services/light/turn_on"
    payload = json.loads(requests[0].content)
    assert payload["entity_id"] == ["light.one", "light.two"]
    assert payload["brightness"] == 128
    await client.aclose()


@pytest.mark.asyncio
async def test_call_service_surfaces_home_assistant_error_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="invalid color temperature")

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        token="test-token",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(HomeAssistantError, match="invalid color temperature"):
        await client.call_service("light", "turn_on", entity_id="light.living_room")

    await client.aclose()


class _FakeWebSocket:
    def __init__(self, responses: list[dict]):
        self._responses = [json.dumps(r) for r in responses]
        self.sent: list[dict] = []

    async def recv(self) -> str:
        return self._responses.pop(0)

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class _FakeWebSocketContext:
    def __init__(self, websocket: _FakeWebSocket):
        self.websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_ws_command_authenticates_and_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    client = HomeAssistantClient(base_url="http://localhost:8123", token="token")
    websocket = _FakeWebSocket(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": {"name": "Owner"}},
        ]
    )

    monkeypatch.setattr(
        "plugins.smart_home.ha_client.ws_connect",
        lambda *args, **kwargs: _FakeWebSocketContext(websocket),
    )

    user = await client.current_user()
    assert user == {"name": "Owner"}
    assert websocket.sent == [
        {"type": "auth", "access_token": "token"},
        {"id": 1, "type": "auth/current_user"},
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_ws_command_rejects_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = HomeAssistantClient(base_url="http://localhost:8123", token="bad")
    websocket = _FakeWebSocket(
        [
            {"type": "auth_required"},
            {"type": "auth_invalid", "message": "bad token"},
        ]
    )

    monkeypatch.setattr(
        "plugins.smart_home.ha_client.ws_connect",
        lambda *args, **kwargs: _FakeWebSocketContext(websocket),
    )

    with pytest.raises(HomeAssistantAuthError):
        await client.current_user()
    await client.aclose()


@pytest.mark.asyncio
async def test_create_long_lived_access_token_replaces_existing_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HomeAssistantClient(base_url="http://localhost:8123", token="token")
    calls: list[tuple[str, dict]] = []

    async def fake_ws_command(command_type: str, **kwargs):
        calls.append((command_type, kwargs))
        if command_type == "auth/refresh_tokens":
            return [
                {
                    "id": "old-jarvis",
                    "client_name": "JARV1S",
                    "type": "long_lived_access_token",
                },
                {
                    "id": "other",
                    "client_name": "Other",
                    "type": "long_lived_access_token",
                },
                {
                    "id": "oauth",
                    "client_name": None,
                    "type": "normal",
                },
            ]
        if command_type == "auth/delete_refresh_token":
            return None
        if command_type == "auth/long_lived_access_token":
            return "new-ll-token"
        raise AssertionError(f"unexpected command: {command_type}")

    monkeypatch.setattr(client, "ws_command", fake_ws_command)

    token = await client.create_long_lived_access_token("JARV1S")
    assert token == "new-ll-token"
    assert calls == [
        ("auth/refresh_tokens", {}),
        ("auth/delete_refresh_token", {"refresh_token_id": "old-jarvis"}),
        ("auth/long_lived_access_token", {"client_name": "JARV1S", "lifespan": 3650}),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_delete_long_lived_access_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    client = HomeAssistantClient(base_url="http://localhost:8123", token="token")
    calls: list[tuple[str, dict]] = []

    async def fake_ws_command(command_type: str, **kwargs):
        calls.append((command_type, kwargs))
        if command_type == "auth/refresh_tokens":
            return [
                {
                    "id": "old-jarvis",
                    "client_name": "JARV1S",
                    "type": "long_lived_access_token",
                },
                {
                    "id": "other",
                    "client_name": "Other",
                    "type": "long_lived_access_token",
                },
            ]
        if command_type == "auth/delete_refresh_token":
            return None
        raise AssertionError(f"unexpected command: {command_type}")

    monkeypatch.setattr(client, "ws_command", fake_ws_command)

    deleted = await client.delete_long_lived_access_tokens("JARV1S")
    assert deleted == 1
    assert calls == [
        ("auth/refresh_tokens", {}),
        ("auth/delete_refresh_token", {"refresh_token_id": "old-jarvis"}),
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_onboarding_and_auth_code_exchange() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/onboarding":
            return httpx.Response(200, json=[{"step": "user", "done": False}])
        if request.url.path == "/api/onboarding/users":
            return httpx.Response(200, json=_load("onboarding_users_response.json"))
        if request.url.path == "/auth/token":
            return httpx.Response(200, json=_load("auth_token_response.json"))
        return httpx.Response(404)

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    assert await client.onboarding_pending() is True
    onboarding = await client.create_onboarding_user(
        name="Owner",
        username="owner",
        password="secret",
    )
    assert onboarding.auth_code

    auth = await client.exchange_auth_code(onboarding.auth_code)
    assert auth.access_token
    assert requests[-1].headers["Content-Type"] == "application/x-www-form-urlencoded"
    await client.aclose()


@pytest.mark.asyncio
async def test_reload_config_entry_uses_homeassistant_service() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        token="test-token",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await client.reload_config_entry("tuya-entry-1")
    assert requests[0].url.path == "/api/services/homeassistant/reload_config_entry"
    payload = json.loads(requests[0].content)
    assert payload["entry_id"] == "tuya-entry-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_list_config_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = HomeAssistantClient(base_url="http://localhost:8123", token="token")
    entries = _load("config_entries_tuya.json")
    websocket = _FakeWebSocket(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": entries},
        ]
    )
    monkeypatch.setattr(
        "plugins.smart_home.ha_client.ws_connect",
        lambda *args, **kwargs: _FakeWebSocketContext(websocket),
    )
    result = await client.list_config_entries(domain="tuya")
    assert result[0]["entry_id"] == "tuya-entry-1"
    assert websocket.sent[-1] == {"id": 1, "type": "config_entries/get", "domain": "tuya"}
    await client.aclose()


@pytest.mark.asyncio
async def test_registry_write_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    client = HomeAssistantClient(base_url="http://localhost:8123", token="token")
    responses = [
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": True, "result": {"area_id": "bedroom", "name": "Bedroom"}},
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": True, "result": {"area_id": "bedroom", "name": "Main Bedroom"}},
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": True, "result": None},
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": True, "result": {"entity_id": "light.grid_bulb_1", "name": "Bedroom Lamp"}},
    ]
    call_index = {"value": 0}

    class _MultiWebSocket(_FakeWebSocket):
        async def recv(self) -> str:
            idx = call_index["value"]
            call_index["value"] += 1
            return self._responses[idx]

    websocket = _MultiWebSocket(responses)
    monkeypatch.setattr(
        "plugins.smart_home.ha_client.ws_connect",
        lambda *args, **kwargs: _FakeWebSocketContext(websocket),
    )

    area = await client.create_area("Bedroom")
    assert area["area_id"] == "bedroom"

    renamed = await client.update_area("bedroom", name="Main Bedroom")
    assert renamed["name"] == "Main Bedroom"

    await client.delete_area("bedroom")

    entity = await client.update_entity("light.grid_bulb_1", name="Bedroom Lamp")
    assert entity["name"] == "Bedroom Lamp"
    commands = [msg for msg in websocket.sent if msg.get("type") != "auth"]
    assert commands[0] == {"id": 1, "type": "config/area_registry/create", "name": "Bedroom"}
    assert commands[1] == {
        "id": 1,
        "type": "config/area_registry/update",
        "area_id": "bedroom",
        "name": "Main Bedroom",
    }
    assert commands[2] == {"id": 1, "type": "config/area_registry/delete", "area_id": "bedroom"}
    await client.aclose()


@pytest.mark.asyncio
async def test_config_flow_rest_methods() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/api/config/config_entries/flow":
            return httpx.Response(200, json={"flow_id": "flow-1", "type": "form"})
        if request.method == "GET" and request.url.path == "/api/config/config_entries/flow/flow-1":
            return httpx.Response(200, json={"flow_id": "flow-1", "type": "form", "step_id": "user"})
        if request.method == "POST" and request.url.path == "/api/config/config_entries/flow/flow-1":
            return httpx.Response(200, json={"type": "create_entry", "title": "Tapo"})
        return httpx.Response(404)

    client = HomeAssistantClient(
        base_url="http://localhost:8123",
        token="test-token",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    created = await client.create_config_flow("tplink")
    assert created["flow_id"] == "flow-1"
    fetched = await client.fetch_config_flow("flow-1")
    assert fetched["step_id"] == "user"
    handled = await client.handle_config_flow_step("flow-1", {"host": "192.168.1.50"})
    assert handled["type"] == "create_entry"
    await client.aclose()


@pytest.mark.asyncio
async def test_create_ha_client_requires_url() -> None:
    from plugins.smart_home.ha_client import create_ha_client

    with pytest.raises(ValueError, match="HA_URL"):
        await create_ha_client({})


@pytest.mark.asyncio
async def test_complete_bootstrap_onboarding_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    onboarding_state = [{"step": s, "done": False} for s in ("user", "core_config", "analytics", "integration")]
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/api/onboarding" and request.method == "GET":
            return httpx.Response(200, json=list(onboarding_state))
        if path == "/api/onboarding/users":
            onboarding_state[0]["done"] = True
            return httpx.Response(200, json=_load("onboarding_users_response.json"))
        if path == "/auth/token":
            return httpx.Response(200, json=_load("auth_token_response.json"))
        if path.startswith("/api/onboarding/") and request.method == "POST":
            step = path.rsplit("/", 1)[-1]
            for item in onboarding_state:
                if item["step"] == step:
                    item["done"] = True
            return httpx.Response(200, json={})
        return httpx.Response(404)

    client = HomeAssistantClient(
        base_url="http://127.0.0.1:8123",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    websocket = _FakeWebSocket(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True, "result": "long-lived-token"},
        ]
    )
    monkeypatch.setattr(
        "plugins.smart_home.ha_client.ws_connect",
        lambda *args, **kwargs: _FakeWebSocketContext(websocket),
    )

    token = await client.complete_bootstrap_onboarding(
        owner_name="Owner",
        username="owner",
        password="secret",
    )
    assert token == "long-lived-token"
    assert all(step["done"] for step in onboarding_state)

    posted = {
        request.url.path: json.loads(request.content or b"{}")
        for request in requests
        if request.method == "POST" and request.url.path.startswith("/api/onboarding/")
    }
    assert posted["/api/onboarding/core_config"] == {}
    assert posted["/api/onboarding/analytics"] == {}
    assert posted["/api/onboarding/integration"] == {
        "client_id": "http://127.0.0.1:8123/",
        "redirect_uri": "http://127.0.0.1:8123/?auth_callback=1",
    }
    await client.aclose()
