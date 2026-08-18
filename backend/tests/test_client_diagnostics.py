"""Contract tests for client.diagnostics ingestion."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.websockets.models import ClientDiagnosticBatch, ClientDiagnosticEvent
from api.websockets import handlers as ws_handlers


def test_batch_rejects_unknown_event() -> None:
    with pytest.raises(ValidationError):
        ClientDiagnosticEvent(
            seq=1,
            ts="2026-07-23T00:00:00Z",
            category="mic",
            event="not_real",
            severity="info",
            metadata={},
        )


def test_batch_rejects_too_many_events() -> None:
    events = [
        {
            "seq": i,
            "ts": "2026-07-23T00:00:00Z",
            "category": "transport",
            "event": "transport_transition",
            "severity": "info",
            "metadata": {"phase": "open"},
        }
        for i in range(11)
    ]
    with pytest.raises(ValidationError):
        ClientDiagnosticBatch.model_validate({"events": events, "dropped_count": 0})


def test_event_rejects_mismatched_category_and_sanitizes_ids() -> None:
    payload = {
        "seq": 1,
        "ts": "2026-07-23T00:00:00Z",
        "category": "playback",
        "event": "mic_flatline",
        "severity": "warning",
        "metadata": {},
    }
    with pytest.raises(ValidationError):
        ClientDiagnosticEvent.model_validate(payload)

    payload["category"] = "mic"
    payload["turn_id"] = "turn\nforged"
    event = ClientDiagnosticEvent.model_validate(payload)
    assert event.turn_id == "turn forged"


def test_batch_sanitizes_metadata() -> None:
    batch = ClientDiagnosticBatch.model_validate(
        {
            "events": [
                {
                    "seq": 1,
                    "ts": "2026-07-23T00:00:00Z",
                    "category": "playback",
                    "event": "playback_summary",
                    "severity": "info",
                    "turn_id": "turn-abc",
                    "metadata": {
                        "outcome": "render_completed",
                        "chunks": 3,
                        "nested": {"no": True},
                        "label": "x" * 80,
                    },
                }
            ],
            "dropped_count": 2,
        }
    )
    meta = batch.events[0].metadata
    assert meta["outcome"] == "render_completed"
    assert meta["chunks"] == 3
    assert "nested" not in meta
    assert len(meta["label"]) <= 64


@pytest.mark.asyncio
async def test_handler_rate_limits_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePresence:
        node_id = "browser-1"
        device_kind = "desktop"

    class FakeSession:
        connection_id = "conn-1"
        owner_id = "geoff"
        presence = FakePresence()

    class FakeManager:
        def get_session(self, _session_id: str) -> FakeSession:
            return FakeSession()

    logs: list[str] = []

    class FakeLogger:
        def info(self, msg: str, *args) -> None:
            logs.append(msg % args if args else msg)

        def warning(self, msg: str, *args) -> None:
            logs.append(msg % args if args else msg)

    monkeypatch.setattr(ws_handlers, "manager", FakeManager())
    monkeypatch.setattr(ws_handlers, "logger", FakeLogger())
    ws_handlers._client_diag_budget.clear()
    ws_handlers._client_diag_last_warning.clear()

    from api.websockets.models import WSMessage
    from api.websockets.types import WSMessageType

    def make_message(n: int) -> WSMessage:
        return WSMessage(
            type=WSMessageType.CLIENT_DIAGNOSTICS,
            data={
                "events": [
                    {
                        "seq": i,
                        "ts": "2026-07-23T00:00:00Z",
                        "category": "mic",
                        "event": "mic_flatline",
                        "severity": "warning",
                        "metadata": {"reason": "flatline"},
                    }
                    for i in range(n)
                ],
                "dropped_count": 0,
            },
        )

    await ws_handlers.handle_client_diagnostics("conn-1", make_message(10))
    assert any("ClientDiag event=mic_flatline" in line for line in logs)

    # Exhaust budget with 6 batches of 10 = 60, next should rate-limit.
    for _ in range(5):
        await ws_handlers.handle_client_diagnostics("conn-1", make_message(10))
    logs.clear()
    await ws_handlers.handle_client_diagnostics("conn-1", make_message(10))
    assert any("rate-limited" in line for line in logs)

    # Reconnect with a new connection_id must not reset the node-scoped budget.
    class ReconnectedSession(FakeSession):
        connection_id = "conn-2"

    class ReconnectedManager:
        def get_session(self, _session_id: str) -> ReconnectedSession:
            return ReconnectedSession()

    monkeypatch.setattr(ws_handlers, "manager", ReconnectedManager())
    logs.clear()
    await ws_handlers.handle_client_diagnostics("conn-2", make_message(1))
    assert not any("ClientDiag event=" in line for line in logs)
