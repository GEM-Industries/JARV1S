"""REST tests for bounded wake-check endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps.device_auth import require_device, require_owner_id
from api.routes import voice as voice_routes
from core.auth.device_models import DeviceAuthResult, DeviceLocation
from core.voice.wakeword.check import WakeCheckError, WakeCheckResult


def _auth() -> DeviceAuthResult:
    return DeviceAuthResult(
        device_id="dev-1",
        owner_id="owner-a",
        node_id="browser-1",
        node_label="Browser",
        capabilities=["mic", "speaker", "display"],
        location=DeviceLocation(),
        kind="browser",
    )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(voice_routes.router, prefix="/api/v1")
    app.dependency_overrides[require_device] = _auth
    app.dependency_overrides[require_owner_id] = lambda: "owner-a"
    return TestClient(app)


def test_wake_check_recognized(client: TestClient) -> None:
    with patch.object(
        voice_routes,
        "check_wake_phrase",
        return_value=WakeCheckResult(status="recognized"),
    ) as check_mock:
        response = client.post("/api/v1/voice/wake-check", json={"clip": "AAAA"})
    assert response.status_code == 200
    assert response.json() == {"status": "recognized"}
    pcm = check_mock.call_args.args[0]
    assert pcm == b"\x00\x00\x00"
    assert check_mock.call_args.kwargs["owner_id"] == "owner-a"


def test_wake_check_not_detected(client: TestClient) -> None:
    with patch.object(
        voice_routes,
        "check_wake_phrase",
        return_value=WakeCheckResult(status="not_detected"),
    ):
        response = client.post("/api/v1/voice/wake-check", json={"clip": "AAAA"})
    assert response.status_code == 200
    assert response.json() == {"status": "not_detected"}


def test_wake_check_speaker_mismatch(client: TestClient) -> None:
    with patch.object(
        voice_routes,
        "check_wake_phrase",
        return_value=WakeCheckResult(status="speaker_mismatch"),
    ):
        response = client.post("/api/v1/voice/wake-check", json={"clip": "AAAA"})
    assert response.status_code == 200
    assert response.json() == {"status": "speaker_mismatch"}


def test_wake_check_validation_error(client: TestClient) -> None:
    with patch.object(
        voice_routes,
        "check_wake_phrase",
        side_effect=WakeCheckError("too_short", "Wake check clip is empty"),
    ):
        response = client.post("/api/v1/voice/wake-check", json={"clip": "AAAA"})
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "too_short"


def test_wake_check_rejects_invalid_base64(client: TestClient) -> None:
    response = client.post("/api/v1/voice/wake-check", json={"clip": "%%%%"})
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "processing_failed"


def test_wake_check_rejects_oversized_clip(client: TestClient) -> None:
    response = client.post(
        "/api/v1/voice/wake-check",
        json={"clip": "A" * (voice_routes.MAX_BASE64_WAKE_CHECK_LENGTH + 1)},
    )
    assert response.status_code == 422


def test_wake_check_requires_auth(monkeypatch) -> None:
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    app = FastAPI()
    app.include_router(voice_routes.router, prefix="/api/v1")
    with TestClient(app) as unauth:
        assert unauth.post("/api/v1/voice/wake-check", json={"clip": "AAAA"}).status_code == 401


def test_wake_check_does_not_touch_session_manager(client: TestClient) -> None:
    manager = MagicMock()
    with (
        patch.object(
            voice_routes,
            "check_wake_phrase",
            return_value=WakeCheckResult(status="recognized"),
        ),
        patch("api.websockets.connection.manager", manager),
    ):
        response = client.post("/api/v1/voice/wake-check", json={"clip": "AAAA"})
    assert response.status_code == 200
    manager.get_session.assert_not_called()
    manager.list_owner_sessions.assert_not_called()
