import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from api.websockets.connection import ConnectionManager
from api.websockets.presence import build_presence_identity
from core.plugins.types import UIEnvelope, WidgetLayout, WidgetSize
from core.preferences.models import UserPreferences

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return self.docs[:length] if length else self.docs


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.find_calls = []

    def find(self, filt, projection=None):
        self.find_calls.append((filt, projection))
        return FakeCursor(self.docs)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_pending_input_snapshot_provider_returns_live_widgets(monkeypatch):
    from core import pending_inputs

    now = datetime.now(timezone.utc)
    collection = FakeCollection(
        [
            {
                "input_id": "inp-1",
                "owner_id": "owner-1",
                "kind": "approval",
                "status": "pending",
                "prompt": "Approve this?",
                "detail": "Details",
                "source": {"id": "turn-1"},
                "created_at": now,
                "expires_at": now + timedelta(minutes=2),
            }
        ]
    )
    monkeypatch.setattr(pending_inputs, "_collection", lambda: collection)

    widgets = await pending_inputs.pending_input_snapshot_widgets("owner-1")

    assert widgets[0].widget_id == "pending-inp-1"
    assert widgets[0].component == "PendingInputWidget"
    assert collection.find_calls[0][0]["owner_id"] == "owner-1"
    assert collection.find_calls[0][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_background_task_snapshot_provider_returns_running_widgets(monkeypatch):
    from plugins.agents import client

    collection = FakeCollection(
        [
            {
                "task_id": "task-1",
                "owner_id": "owner-1",
                "status": "running",
                "progress_summary": "Running tests",
                "live_status": "pytest",
                "created_at": 123,
            }
        ]
    )
    monkeypatch.setattr(client.mongodb, "get_collection", lambda _name: collection)

    widgets = await client.background_task_snapshot_widgets("owner-1")

    assert widgets[0].widget_id == "task-receipt-task-1"
    assert widgets[0].component == "ContentWidget"
    assert widgets[0].data["receipt_kind"] == "task_progress"
    assert widgets[0].data["line"] == "pytest"
    assert collection.find_calls[0][0] == {"owner_id": "owner-1", "status": "running"}


@pytest.mark.asyncio
async def test_pinned_widget_snapshot_provider_returns_saved_widgets(monkeypatch):
    from core.plugins import pinned_widgets

    envelope = UIEnvelope(
        widget_id="weather-current",
        component="WeatherWidget",
        title="Weather",
        data={"temperature": 20},
        layout=WidgetLayout(size=WidgetSize.WIDE),
        pinned=True,
    )
    collection = FakeCollection([{"envelope": envelope.model_dump(mode="json")}])
    monkeypatch.setattr(pinned_widgets, "_collection", lambda: collection)

    widgets = await pinned_widgets.pinned_widget_snapshot_widgets("owner-1")

    assert widgets[0].widget_id == "weather-current"
    assert widgets[0].pinned is True
    assert collection.find_calls[0][0] == {"owner_id": "owner-1"}


@pytest.mark.asyncio
async def test_connection_sends_widget_snapshot_after_connect():
    manager = ConnectionManager()
    socket = FakeWebSocket()
    presence = build_presence_identity(
        {"owner_id": "owner-1", "node_id": "browser-1"},
        connection_id="conn-1",
        allow_owner_override=True,
    )
    envelope = UIEnvelope(
        widget_id="task-task-1",
        component="BackgroundTaskWidget",
        title="Background Task",
        data={"task_id": "task-1", "status": "running"},
        layout=WidgetLayout(size=WidgetSize.WIDE),
    )

    with (
        patch("api.websockets.connection.TenVADService"),
        patch("api.websockets.connection.WakeWordService"),
        patch("api.websockets.connection.SpeechProcessor"),
        patch(
            "api.websockets.connection.get_user_preferences",
            new=AsyncMock(return_value=UserPreferences(owner_id="owner-1")),
        ),
        patch(
            "api.websockets.connection.attention_service.get_state",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.websockets.connection.collect_widget_snapshots",
            new=AsyncMock(return_value=[envelope]),
        ),
    ):
        await manager.connect(socket, presence)

    sent = [json.loads(message) for message in socket.sent]
    assert [message["type"] for message in sent[:2]] == [
        "system.connect",
        "ui.snapshot",
    ]
    assert sent[1]["data"]["widgets"][0]["widget_id"] == "task-task-1"


def test_widget_refresh_polling_contract_removed():
    backend_types = (REPO_ROOT / "backend/core/plugins/types.py").read_text()
    frontend_types = (REPO_ROOT / "frontend/src/types/index.ts").read_text()
    wrapper = (
        REPO_ROOT / "frontend/src/components/features/widgets/WidgetWrapper.tsx"
    ).read_text()
    docs = (REPO_ROOT / "docs/UI/WIDGET_SYSTEM.md").read_text()

    assert "refresh_interval_sec" not in backend_types
    assert "refresh_interval_sec" not in frontend_types
    assert "refresh_interval_sec" not in wrapper
    assert "setInterval(checkExpiry, 100)" not in wrapper
    assert "Backend-Pushed" in docs


def test_frontend_handles_widget_snapshots():
    frontend_types = (REPO_ROOT / "frontend/src/types/index.ts").read_text()
    client = (REPO_ROOT / "frontend/src/client/JarvisClient.ts").read_text()
    store = (REPO_ROOT / "frontend/src/store/useJarvisStore.ts").read_text()

    assert "| 'ui.snapshot'" in frontend_types
    assert "case 'ui.snapshot':" in client
    assert "store.setWidgets(widgets)" in client
    assert "localReceipts" not in store


def test_frontend_sends_pin_state_to_backend_without_local_persistence():
    frontend_types = (REPO_ROOT / "frontend/src/types/index.ts").read_text()
    wrapper = (
        REPO_ROOT / "frontend/src/components/features/widgets/WidgetWrapper.tsx"
    ).read_text()
    store = (REPO_ROOT / "frontend/src/store/useJarvisStore.ts").read_text()

    assert "| 'ui.pin'" in frontend_types
    assert "jarvisClient.sendMessage('ui.pin'" in wrapper
    assert "PINNED_WIDGETS_STORAGE_KEY" not in store
    assert "localStorage" not in store
