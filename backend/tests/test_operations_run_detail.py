from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.operations.service import get_trigger_run_detail


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_args):
        return self

    def __aiter__(self):
        self._iter = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self, docs=None, find_one_doc=None):
        self.docs = docs or []
        self.find_one_doc = find_one_doc

    async def find_one(self, *_args, **_kwargs):
        return self.find_one_doc

    def find(self, *_args, **_kwargs):
        return FakeCursor(self.docs)


@pytest.mark.asyncio
async def test_get_trigger_run_detail_joins_trace_perf_and_protocols():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    instance = {
        "id": "trg-1",
        "owner_id": "owner-1",
        "status": "completed",
        "due_at": now,
        "created_at": now,
        "updated_at": now,
        "completed_at": now,
        "origin_snapshot": {"kind": "external", "source": "calendar"},
        "action_snapshot": {"kind": "run_protocol", "protocol_name": "Morning"},
        "source_event": {"rule_id": "rule-1", "rule_name": "Morning check"},
        "turn_ids": ["turn-1"],
        "result_text": "Done",
    }
    conversations = [
        {
            "timestamp": now,
            "role": "assistant",
            "content": "Checked calendar.",
            "metadata": {
                "turn_id": "turn-1",
                "turn_type": "text_only",
                "instance_id": "trg-1",
            },
        }
    ]
    turn_runs = [
        {
            "turn_id": "turn-1",
            "status": "completed",
            "started_at": now,
            "completed_at": now,
            "response_ms": 120.5,
            "total_ms": 420.0,
            "stages": [{"key": "llm", "label": "LLM", "ms": 300.0}],
        }
    ]
    protocol_runs = [
        {
            "turn_id": "turn-1",
            "protocol_name": "Morning",
            "triggered_by": "trigger",
            "started_at": now,
            "completed_at": now,
            "status": "completed",
        }
    ]
    fake_db = SimpleNamespace(
        trigger_instances=FakeCollection(find_one_doc=instance),
        conversations=FakeCollection(conversations),
        turn_runs=FakeCollection(turn_runs),
        protocol_runs=FakeCollection(protocol_runs),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        detail = await get_trigger_run_detail("owner-1", "trg-1")

    assert detail is not None
    assert detail.kind == "automation"
    assert detail.source == "Morning check"
    assert detail.turn_ids == ["turn-1"]
    assert len(detail.attempts) == 1
    attempt = detail.attempts[0]
    assert attempt.trace[0].content == "Checked calendar."
    assert attempt.perf is not None
    assert attempt.perf.total_ms == 420.0
    assert attempt.protocols[0].protocol_name == "Morning"


@pytest.mark.asyncio
async def test_get_trigger_run_detail_handles_no_turn_attempts():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    instance = {
        "id": "trg-2",
        "owner_id": "owner-1",
        "status": "expired",
        "due_at": now,
        "created_at": now,
        "origin_snapshot": {"kind": "time"},
        "action_snapshot": {"kind": "notify"},
        "source_event": {},
        "turn_ids": [],
        "failure_reason": "no_session",
    }
    fake_db = SimpleNamespace(
        trigger_instances=FakeCollection(find_one_doc=instance),
        conversations=FakeCollection(),
        turn_runs=FakeCollection(),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        detail = await get_trigger_run_detail("owner-1", "trg-2")

    assert detail is not None
    assert detail.kind == "trigger"
    assert detail.attempts == []
    assert detail.failure_reason == "no_session"


@pytest.mark.asyncio
async def test_get_operation_run_route_404s_for_missing_run():
    from api.routes.operations import get_operation_run

    with patch("api.routes.operations.get_trigger_run_detail", AsyncMock(return_value=None)):
        with pytest.raises(Exception) as exc:
            await get_operation_run("missing")

    assert getattr(exc.value, "status_code", None) == 404
