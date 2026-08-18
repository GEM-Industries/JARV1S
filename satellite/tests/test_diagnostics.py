"""Satellite client diagnostics emit only failures/recovery."""

from __future__ import annotations

from jarvis_satellite.diagnostics import SatelliteDiagnostics
from jarvis_satellite.protocol import MessageType


def test_records_only_allowlisted_events() -> None:
    diag = SatelliteDiagnostics()
    diag.record("transport_transition", metadata={"phase": "open", "recovery": "reconnect"})
    diag.record("not_an_event", metadata={"x": 1})
    messages = diag.drain_messages()
    assert len(messages) == 1
    assert messages[0]["type"] == MessageType.CLIENT_DIAGNOSTICS
    events = messages[0]["data"]["events"]
    assert len(events) == 1
    assert events[0]["event"] == "transport_transition"


def test_sanitizes_metadata_and_batches() -> None:
    diag = SatelliteDiagnostics()
    for i in range(12):
        diag.record(
            "mic_flatline",
            severity="warning",
            metadata={"reason": "overflow", "i": i, "nested": {"no": True}, "label": "x" * 80},
        )
    messages = diag.drain_messages()
    assert len(messages) == 2
    assert len(messages[0]["data"]["events"]) == 10
    assert len(messages[1]["data"]["events"]) == 2
    meta = messages[0]["data"]["events"][0]["metadata"]
    assert "nested" not in meta
    assert len(meta["label"]) <= 64


def test_batch_is_retained_until_marked_sent() -> None:
    diag = SatelliteDiagnostics()
    diag.record("playback_failed", severity="warning", metadata={"reason": "timeout"})

    first = diag.next_message()
    retry = diag.next_message()
    assert first is not None
    assert retry is not None
    assert first["data"] == retry["data"]

    diag.mark_sent(len(first["data"]["events"]))
    assert diag.next_message() is None
