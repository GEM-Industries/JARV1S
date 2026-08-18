from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.operations.service import get_user_turn_detail, list_user_turns


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def __aiter__(self):
        self._iter = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        return self.docs[: length or len(self.docs)]


class FakeCollection:
    def __init__(self, docs=None, find_one_doc=None):
        self.docs = docs or []
        self.find_one_doc = find_one_doc

    async def find_one(self, *_args, **_kwargs):
        return self.find_one_doc

    def find(self, *_args, **_kwargs):
        return FakeCursor(self.docs)


def _project_doc(doc, projection):
    if not projection:
        return doc
    projected = {}
    for key, include in projection.items():
        if not include or key == "_id":
            continue
        source = doc
        target = projected
        parts = key.split(".")
        for part in parts[:-1]:
            source = source.get(part, {}) if isinstance(source, dict) else {}
            target = target.setdefault(part, {})
        if isinstance(source, dict) and parts[-1] in source:
            target[parts[-1]] = source[parts[-1]]
    return projected


class ProjectingCollection(FakeCollection):
    def find(self, *_args, **kwargs):
        projection = _args[1] if len(_args) > 1 else kwargs.get("projection")
        return FakeCursor([_project_doc(doc, projection) for doc in self.docs])


@pytest.mark.asyncio
async def test_get_user_turn_detail_shape_collapse():
    """Bare user turn: no instance envelope, one attempt, tool rows + telemetry."""
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    turn_id = "turn-user-1"
    perf_doc = {
        "turn_id": turn_id,
        "owner_id": "owner-1",
        "source": "user",
        "modality": "voice",
        "status": "completed",
        "node_id": "jarvis-satellite-1",
        "node_label": "Bedroom Satellite",
        "started_at": now,
        "completed_at": now,
        "response_ms": 850.0,
        "total_ms": 2100.0,
        "stt": {"finish_status": "ok", "transcript_chars": 18},
        "turn_detection": {"reason": "endpoint", "confidence": 0.92},
        "voice": {"audio_ms": 1400, "reason": "endpoint"},
        "stages": [{"key": "llm", "label": "LLM", "ms": 900.0}],
    }
    conversations = [
        {
            "timestamp": now,
            "role": "user",
            "source": "user",
            "content": "turn on the kitchen light",
            "metadata": {"turn_id": turn_id, "node_id": "jarvis-satellite-1"},
        },
        {
            "timestamp": now,
            "role": "assistant",
            "source": "user",
            "content": "",
            "metadata": {
                "turn_id": turn_id,
                "turn_type": "tool_call",
                "tool_call_id": "tc-1",
                "code": "jarvis.smart_home.turn_on(entity_id='light.kitchen')",
            },
        },
        {
            "timestamp": now,
            "role": "user",
            "source": "user",
            "content": "<tool_result>\nEXCEPTION during execution: unavailable\n</tool_result>",
            "metadata": {
                "turn_id": turn_id,
                "turn_type": "tool_result",
                "tool_call_id": "tc-1",
                "output": "EXCEPTION during execution: unavailable",
            },
        },
    ]
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(docs=[perf_doc], find_one_doc=perf_doc),
        conversations=FakeCollection(conversations),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        detail = await get_user_turn_detail("owner-1", turn_id)

    assert detail is not None
    assert detail.id == turn_id
    assert detail.kind == "user"
    assert detail.node_id == "jarvis-satellite-1"
    assert detail.turn_ids == [turn_id]
    assert len(detail.attempts) == 1
    attempt = detail.attempts[0]
    assert attempt.turn_id == turn_id
    assert attempt.perf is not None
    assert attempt.perf.stt == {"finish_status": "ok", "transcript_chars": 18}
    assert attempt.perf.turn_detection == {"reason": "endpoint", "confidence": 0.92}
    tool_call = next(line for line in attempt.trace if line.turn_type == "tool_call")
    assert "smart_home" in (tool_call.code or "")
    tool_result = next(line for line in attempt.trace if line.turn_type == "tool_result")
    assert "EXCEPTION" in (tool_result.output or "")


@pytest.mark.asyncio
async def test_get_user_turn_detail_missing_returns_none():
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(find_one_doc=None),
        conversations=FakeCollection(),
        protocol_runs=FakeCollection(),
    )
    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        detail = await get_user_turn_detail("owner-1", "missing")
    assert detail is None


@pytest.mark.asyncio
async def test_get_user_turn_detail_orphaned_telemetry_returns_none():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    perf_doc = {
        "turn_id": "turn-orphan",
        "owner_id": "owner-1",
        "source": "user",
        "status": "completed",
        "started_at": now,
    }
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(docs=[perf_doc], find_one_doc=perf_doc),
        conversations=FakeCollection(),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        detail = await get_user_turn_detail("owner-1", "turn-orphan")

    assert detail is None


@pytest.mark.asyncio
async def test_get_user_turn_detail_supports_system_activity_drill_down():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    turn_id = "turn-system-1"
    perf_doc = {
        "turn_id": turn_id,
        "owner_id": "owner-1",
        "source": "system",
        "status": "completed",
        "started_at": now,
    }
    conversations = [
        {
            "timestamp": now,
            "role": "assistant",
            "source": "system",
            "content": "Background evaluation complete",
            "metadata": {"turn_id": turn_id, "delivery": "silent"},
        }
    ]
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(docs=[perf_doc], find_one_doc=perf_doc),
        conversations=FakeCollection(conversations),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        detail = await get_user_turn_detail("owner-1", turn_id)

    assert detail is not None
    assert detail.kind == "system"
    assert detail.attempts[0].trace[0].content == "Background evaluation complete"


@pytest.mark.asyncio
async def test_list_user_turns_filters_by_node():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    perf_docs = [
        {
            "turn_id": "turn-a",
            "owner_id": "owner-1",
            "source": "user",
            "modality": "voice",
            "status": "completed",
            "node_id": "sat-1",
            "node_label": "Kitchen",
            "started_at": now,
        },
    ]
    conversations = [
        {
            "timestamp": now,
            "role": "user",
            "source": "user",
            "content": "dim the lights",
            "metadata": {"turn_id": "turn-a", "node_id": "sat-1"},
        },
    ]
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(docs=perf_docs),
        conversations=FakeCollection(conversations),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        items = await list_user_turns("owner-1", node_id="sat-1", limit=10)

    assert len(items) == 1
    assert items[0].kind == "user"
    assert items[0].id == "turn-a"
    assert "dim the lights" in items[0].summary


@pytest.mark.asyncio
async def test_list_user_turns_survives_real_mongo_projection():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    conversations = [
        {
            "timestamp": now,
            "role": "user",
            "source": "user",
            "content": "mute yourself",
            "metadata": {
                "turn_id": "turn-a",
                "node_id": "sat-1",
                "node_label": "Bedroom Satellite",
            },
        },
    ]
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(),
        conversations=ProjectingCollection(conversations),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        items = await list_user_turns("owner-1", limit=10)

    assert len(items) == 1
    assert items[0].id == "turn-a"
    assert items[0].summary == "mute yourself"


@pytest.mark.asyncio
async def test_list_user_turns_is_driven_by_conversation_rows():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    perf_docs = [
        {
            "turn_id": "turn-visible",
            "owner_id": "owner-1",
            "source": "user",
            "modality": "voice",
            "status": "completed",
            "node_id": "sat-1",
            "node_label": "Kitchen",
            "started_at": now,
        },
        {
            "turn_id": "turn-orphan",
            "owner_id": "owner-1",
            "source": "user",
            "modality": "voice",
            "status": "completed",
            "node_id": "sat-1",
            "node_label": "Kitchen",
            "started_at": now,
        },
    ]
    conversations = [
        {
            "timestamp": now,
            "role": "user",
            "source": "user",
            "content": "what time is it",
            "metadata": {"turn_id": "turn-visible", "node_id": "sat-1"},
        },
    ]
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(docs=perf_docs),
        conversations=FakeCollection(conversations),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        items = await list_user_turns("owner-1", limit=10)

    assert [item.id for item in items] == ["turn-visible"]
    assert items[0].summary == "what time is it"
    assert items[0].source == "Kitchen"


@pytest.mark.asyncio
async def test_list_user_turns_uses_content_without_matching_telemetry():
    now = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    conversations = [
        {
            "timestamp": now,
            "role": "user",
            "source": "user",
            "content": "hello",
            "metadata": {"turn_id": "turn-visible", "node_id": "browser-1"},
        },
    ]
    fake_db = SimpleNamespace(
        turn_runs=FakeCollection(),
        conversations=FakeCollection(conversations),
        protocol_runs=FakeCollection(),
    )

    with patch("core.operations.service.mongodb", SimpleNamespace(db=fake_db)):
        items = await list_user_turns("owner-1", limit=10)

    assert len(items) == 1
    assert items[0].id == "turn-visible"
    assert items[0].summary == "hello"
    assert items[0].source == "browser-1"


@pytest.mark.asyncio
async def test_get_user_turn_route_404s_for_missing_turn():
    from api.routes.operations import get_user_turn

    with patch("api.routes.operations.get_user_turn_detail", return_value=None):
        with pytest.raises(Exception) as exc:
            await get_user_turn("missing")

    assert getattr(exc.value, "status_code", None) == 404
