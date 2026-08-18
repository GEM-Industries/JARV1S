"""REST tests for owner speaker-profile endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps.device_auth import require_device, require_owner_id
from api.routes import voice as voice_routes
from core.auth.device_models import DeviceAuthResult, DeviceLocation
from core.voice.speaker_profile import SpeakerProfileError, SpeakerProfileStatus


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


def test_get_speaker_profile_not_enrolled(client: TestClient) -> None:
    with patch.object(
        voice_routes,
        "get_profile_status",
        return_value=SpeakerProfileStatus(status="not_enrolled"),
    ):
        response = client.get("/api/v1/voice/speaker-profile")
    assert response.status_code == 200
    assert response.json() == {"status": "not_enrolled", "updated_at": None}


def test_put_speaker_profile_success(client: TestClient) -> None:
    updated = datetime(2026, 7, 14, tzinfo=timezone.utc)
    with (
        patch.object(
            voice_routes,
            "write_profile",
            return_value=SpeakerProfileStatus(status="enrolled", updated_at=updated),
        ) as write_mock,
        patch.object(
            voice_routes,
            "_reload_owner_verifiers",
            new=AsyncMock(return_value=1),
        ) as reload_mock,
    ):
        response = client.put(
            "/api/v1/voice/speaker-profile",
            json={"clips": ["AAAA"] * 5},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "enrolled"
    write_mock.assert_called_once()
    reload_mock.assert_awaited_once_with("owner-a")


def test_put_speaker_profile_validation_error(client: TestClient) -> None:
    with patch.object(
        voice_routes,
        "write_profile",
        side_effect=SpeakerProfileError("too_quiet", "Clip 1 is too quiet"),
    ):
        response = client.put(
            "/api/v1/voice/speaker-profile",
            json={"clips": ["AAAA"] * 5},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "too_quiet"


def test_put_speaker_profile_returns_failed_clip_index(client: TestClient) -> None:
    with patch.object(
        voice_routes,
        "write_profile",
        side_effect=SpeakerProfileError(
            "inconsistent_samples",
            "Clip 3 does not match",
            clip_index=3,
        ),
    ):
        response = client.put(
            "/api/v1/voice/speaker-profile",
            json={"clips": ["AAAA"] * 5},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["clip_index"] == 3


def test_put_speaker_profile_rejects_oversized_clip(client: TestClient) -> None:
    oversized = "A" * (voice_routes.MAX_BASE64_CLIP_LENGTH + 1)
    response = client.put(
        "/api/v1/voice/speaker-profile",
        json={"clips": [oversized, "AAAA", "AAAA", "AAAA", "AAAA"]},
    )
    assert response.status_code == 422


def test_delete_speaker_profile(client: TestClient) -> None:
    with (
        patch.object(
            voice_routes,
            "delete_profile",
            return_value=SpeakerProfileStatus(status="not_enrolled"),
        ),
        patch.object(
            voice_routes,
            "_reload_owner_verifiers",
            new=AsyncMock(return_value=2),
        ) as reload_mock,
    ):
        response = client.delete("/api/v1/voice/speaker-profile")
    assert response.status_code == 200
    assert response.json()["status"] == "not_enrolled"
    reload_mock.assert_awaited_once_with("owner-a")


def test_speaker_profile_requires_auth(monkeypatch) -> None:
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    app = FastAPI()
    app.include_router(voice_routes.router, prefix="/api/v1")
    with TestClient(app) as unauth:
        assert unauth.get("/api/v1/voice/speaker-profile").status_code == 401


@pytest.mark.asyncio
async def test_reload_owner_verifiers_is_best_effort() -> None:
    healthy = MagicMock()
    broken = MagicMock(side_effect=RuntimeError("reload failed"))
    sessions = [
        SimpleNamespace(
            connection_id="browser-1",
            processor=SimpleNamespace(wakeword_service=SimpleNamespace(reload_verifiers=healthy)),
        ),
        SimpleNamespace(
            connection_id="satellite-1",
            processor=SimpleNamespace(wakeword_service=SimpleNamespace(reload_verifiers=broken)),
        ),
    ]

    with patch("api.websockets.connection.manager") as manager:
        manager.list_owner_sessions.return_value = sessions
        reloaded = await voice_routes._reload_owner_verifiers("owner-a")

    assert reloaded == 1
    healthy.assert_called_once_with()
    broken.assert_called_once_with()
