"""REST device-auth dependency and owner scoping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from api.deps.device_auth import extract_device_token, require_device, require_owner_id
from api.routes import preferences as preferences_route
from api.routes import setup as setup_route
from core.auth.device_service import device_auth_service
from core.config import settings
from core.preferences.models import UserPreferences
from tests.test_device_auth import InMemoryCollection


@pytest.fixture
def rest_device_db(monkeypatch):
    collections = SimpleNamespace(
        devices=InMemoryCollection(),
        pairing=InMemoryCollection(),
        tickets=InMemoryCollection(),
        attempts=InMemoryCollection(),
    )
    monkeypatch.setattr(device_auth_service, "_devices", lambda: collections.devices)
    monkeypatch.setattr(device_auth_service, "_pairing", lambda: collections.pairing)
    monkeypatch.setattr(device_auth_service, "_tickets", lambda: collections.tickets)
    monkeypatch.setattr(
        device_auth_service, "_pairing_attempts", lambda: collections.attempts
    )
    return collections


def _request(
    *,
    host: str = "203.0.113.10",
    authorization: str | None = None,
    device_token_header: str | None = None,
    cookie: str | None = None,
    forwarded_for: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization:
        headers.append((b"authorization", authorization.encode("utf-8")))
    if device_token_header:
        headers.append((b"x-device-token", device_token_header.encode("utf-8")))
    if cookie:
        headers.append((b"cookie", cookie.encode("utf-8")))
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("utf-8")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/preferences/",
        "raw_path": b"/api/v1/preferences/",
        "query_string": b"",
        "headers": headers,
        "client": (host, 12345),
        "server": ("testserver", 80),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_authenticate_device_token_is_reusable(rest_device_db):
    _, token = await device_auth_service.create_device_credential(
        owner_id="owner-rest",
        node_id="browser-rest",
        kind="browser",
    )
    first = await device_auth_service.authenticate_device_token(token)
    second = await device_auth_service.authenticate_device_token(token)
    assert first.owner_id == "owner-rest"
    assert second.device_id == first.device_id


@pytest.mark.asyncio
async def test_require_device_rejects_missing_token_when_auth_required(monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    request = _request()
    with pytest.raises(HTTPException) as exc:
        await require_device(request, creds=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_require_device_accepts_bearer_token(rest_device_db, monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    _, token = await device_auth_service.create_device_credential(
        owner_id="owner-rest",
        node_id="browser-rest",
        kind="browser",
    )
    request = _request(authorization=f"Bearer {token}")
    auth = await require_device(
        request, creds=SimpleNamespace(scheme="Bearer", credentials=token)
    )
    assert auth.owner_id == "owner-rest"
    assert await require_owner_id(auth) == "owner-rest"


@pytest.mark.asyncio
async def test_require_device_dev_bypass_on_localhost(monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", True)
    monkeypatch.setattr(type(settings), "is_development", property(lambda self: True))
    request = _request(host="127.0.0.1")
    auth = await require_device(request, creds=None)
    assert auth.owner_id == settings.DEFAULT_USER_ID
    assert auth.device_id == "dev-bypass"
    assert auth.kind == "browser"


@pytest.mark.asyncio
async def test_require_device_packaged_host_bypass_is_desktop(monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    monkeypatch.setenv("JARVIS_APP_MODE", "1")
    request = _request(host="127.0.0.1")

    auth = await require_device(request, creds=None)

    assert auth.device_id == "local-host"
    assert auth.node_id == "host"
    assert auth.node_label == "Jarvis Host"
    assert auth.kind == "desktop"


@pytest.mark.asyncio
async def test_require_device_does_not_bypass_forwarded_remote_client(monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", True)
    monkeypatch.setattr(type(settings), "is_development", property(lambda self: True))
    request = _request(host="127.0.0.1", forwarded_for="100.64.0.20")
    with pytest.raises(HTTPException) as exc:
        await require_device(request, creds=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_preferences_route_uses_authenticated_owner(monkeypatch):
    seen: list[str] = []

    async def fake_get(owner_id: str):
        seen.append(owner_id)
        return UserPreferences(owner_id=owner_id)

    monkeypatch.setattr(preferences_route, "get_user_preferences", fake_get)
    result = await preferences_route.get_preferences(owner_id="owner-a")
    assert result.owner_id == "owner-a"
    assert seen == ["owner-a"]


def test_extract_device_token_prefers_bearer():
    request = _request(authorization="Bearer abc", device_token_header="xyz")
    assert (
        extract_device_token(
            request, SimpleNamespace(scheme="Bearer", credentials="abc")
        )
        == "abc"
    )


def test_extract_device_token_accepts_http_only_cookie():
    request = _request(cookie="jarvis_device_token=cookie-token")
    assert extract_device_token(request, None) == "cookie-token"


def test_setup_mutations_require_device_auth(monkeypatch):
    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    app = FastAPI()
    app.include_router(setup_route.router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.post("/api/v1/setup/runtime/initialize")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_browser_pairing_sets_http_only_cookie_without_exposing_token(
    rest_device_db,
):
    from api.routes import device_auth as device_auth_route

    old, _ = await device_auth_service.create_device_credential(
        owner_id="owner-browser",
        node_id="browser-1",
        kind="browser",
    )
    issued = await device_auth_service.issue_pairing_code(owner_id="owner-browser")
    app = FastAPI()
    app.include_router(device_auth_route.router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/device-auth/pair",
            json={"code": issued.code, "node_id": "browser-1"},
        )

    assert response.status_code == 200
    assert "device_token" not in response.json()
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    devices = {
        device.device_id: device
        for device in await device_auth_service.list_devices(owner_id="owner-browser")
    }
    assert devices[old.device_id].revoked_at is not None


@pytest.mark.asyncio
async def test_pairing_succeeds_when_stale_credential_cleanup_fails(
    rest_device_db,
    monkeypatch,
):
    from api.routes import device_auth as device_auth_route

    async def fail_cleanup(**_kwargs):
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(
        device_auth_service,
        "retire_superseded_credentials",
        fail_cleanup,
    )
    issued = await device_auth_service.issue_pairing_code(owner_id="owner-browser")
    app = FastAPI()
    app.include_router(device_auth_route.router, prefix="/api/v1")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/device-auth/pair",
            json={"code": issued.code, "node_id": "browser-1"},
        )

    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_issue_pairing_code_route_uses_auth_owner(rest_device_db):
    from api.routes import device_auth as device_auth_route
    from core.auth.device_models import (
        DeviceAuthResult,
        DeviceLocation,
        PairingCodeIssueRequest,
    )

    auth = DeviceAuthResult(
        device_id="dev-1",
        owner_id="owner-ui",
        node_id="desktop-1",
        capabilities=["mic", "speaker", "display"],
        location=DeviceLocation(),
        kind="browser",
    )
    result = await device_auth_route.issue_pairing_code(
        PairingCodeIssueRequest(node_label="Phone"),
        auth=auth,
    )
    assert result.owner_id == "owner-ui"
    assert result.code
    assert result.pairing_url == f"?pair={result.code}"


def test_backend_ws_url_from_request_prefers_forwarded_https():
    from api.routes.device_auth import backend_ws_url_from_request

    request = _request(host="127.0.0.1")
    request.scope["headers"] = [
        (b"host", b"127.0.0.1:8000"),
        (b"x-forwarded-proto", b"https"),
        (b"x-forwarded-host", b"macbook-pro.tail131191.ts.net"),
    ]
    assert (
        backend_ws_url_from_request(request)
        == "wss://macbook-pro.tail131191.ts.net/api/v1/ws"
    )


@pytest.mark.asyncio
async def test_create_satellite_credential_route_returns_token_once(rest_device_db):
    from api.routes import device_auth as device_auth_route
    from core.auth.device_models import (
        DeviceAuthResult,
        DeviceLocation,
        SatelliteCredentialCreateRequest,
    )

    auth = DeviceAuthResult(
        device_id="dev-host",
        owner_id="owner-ui",
        node_id="desktop-1",
        capabilities=["mic", "speaker", "display"],
        location=DeviceLocation(),
        kind="browser",
    )
    request = _request(host="127.0.0.1")
    request.scope["headers"] = [
        (b"host", b"macbook-pro.tail131191.ts.net"),
        (b"x-forwarded-proto", b"https"),
    ]
    result = await device_auth_route.create_satellite_credential(
        request,
        SatelliteCredentialCreateRequest(
            node_label="Bedroom Satellite",
            ha_area_id="bedroom",
            room_name="Bedroom",
        ),
        auth=auth,
    )
    assert result.device_token.startswith("jarvis_")
    assert result.node_label == "Bedroom Satellite"
    assert result.backend_ws_url == "wss://macbook-pro.tail131191.ts.net/api/v1/ws"
    devices = await device_auth_service.list_devices(owner_id="owner-ui")
    assert any(device.node_id == result.node_id and device.kind == "satellite" for device in devices)
