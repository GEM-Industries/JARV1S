from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest


class FakeCollection:
    def __init__(self):
        self.docs: list[dict[str, Any]] = []
        self.updated: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def insert_one(self, doc: dict[str, Any]):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("input_id"))

    async def find_one(
        self, filt: dict[str, Any], projection: dict[str, int] | None = None, sort=None
    ):
        matches = [doc for doc in self.docs if self._matches(doc, filt)]
        if sort:
            key, direction = sort[0]
            matches.sort(
                key=lambda doc: doc.get(key)
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=direction < 0,
            )
        if not matches:
            return None
        doc = dict(matches[0])
        if projection and projection.get("_id") == 0:
            doc.pop("_id", None)
        return doc

    async def update_one(self, filt: dict[str, Any], update: dict[str, Any]):
        self.updated.append((filt, update))
        for doc in self.docs:
            if self._matches(doc, filt):
                self._apply_update(doc, update)
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    async def find_one_and_update(
        self, filt: dict[str, Any], update: dict[str, Any], return_document=None
    ):
        self.updated.append((filt, update))
        for doc in self.docs:
            if self._matches(doc, filt):
                self._apply_update(doc, update)
                result = dict(doc)
                result.pop("_id", None)
                return result
        return None

    async def update_many(self, filt: dict[str, Any], update: dict[str, Any]):
        count = 0
        for doc in self.docs:
            if self._matches(doc, filt):
                self._apply_update(doc, update)
                count += 1
        return SimpleNamespace(modified_count=count)

    def _matches(self, doc: dict[str, Any], filt: dict[str, Any]) -> bool:
        for key, expected in filt.items():
            actual = self._get(doc, key)
            if isinstance(expected, dict):
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
                if (
                    "$exists" in expected
                    and (actual is not None) != expected["$exists"]
                ):
                    return False
                continue
            if actual != expected:
                return False
        return True

    def _get(self, doc: dict[str, Any], dotted: str):
        value: Any = doc
        for part in dotted.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    def _apply_update(self, doc: dict[str, Any], update: dict[str, Any]) -> None:
        for key, value in update.get("$set", {}).items():
            self._set(doc, key, value)

    def _set(self, doc: dict[str, Any], dotted: str, value: Any) -> None:
        target = doc
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value


@pytest.fixture
def fake_pending(monkeypatch):
    from core import pending_inputs

    pending = FakeCollection()
    background = FakeCollection()

    def get_collection(name: str):
        if name == "pending_inputs":
            return pending
        if name == "background_tasks":
            return background
        raise AssertionError(name)

    pending_inputs._callbacks.clear()
    pending_inputs._waiters.clear()
    monkeypatch.setattr(pending_inputs.mongodb, "get_collection", get_collection)
    monkeypatch.setattr(pending_inputs, "push_ui", lambda env: None)
    return pending_inputs, pending, background


@pytest.mark.asyncio
async def test_pending_input_resolves_specific_approval_by_id(fake_pending):
    pending_inputs, pending, _ = fake_pending
    ran: list[str] = []

    async def first():
        ran.append("first")
        return "first ran"

    async def second():
        ran.append("second")
        return "second ran"

    one = await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="first?",
        source={"type": "foreground_turn"},
        callback=first,
        publish="none",
    )
    two = await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="second?",
        source={"type": "foreground_turn"},
        callback=second,
        publish="none",
    )

    result = await pending_inputs.resolve_pending_input(
        owner_id="geoff",
        input_id=one["input_id"],
        decision="approve",
    )

    assert result == "first ran"
    assert ran == ["first"]
    assert pending.docs[0]["status"] == "approved"
    assert pending.docs[1]["input_id"] == two["input_id"]
    assert pending.docs[1]["status"] == "pending"


@pytest.mark.asyncio
async def test_waiter_resolution_does_not_require_callback(fake_pending):
    pending_inputs, _, _ = fake_pending
    doc = await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="approve task?",
        source={"type": "background_task", "id": "task-1", "task_id": "task-1"},
        create_waiter=True,
        publish="none",
    )

    result = await pending_inputs.resolve_pending_input(
        owner_id="geoff",
        input_id=doc["input_id"],
        decision="approve",
    )
    decision = await pending_inputs.wait_for_pending_input(doc["input_id"])

    assert result == "Approved."
    assert decision == "approved"


@pytest.mark.asyncio
async def test_resolve_pending_from_utterance_approves_yes_please(fake_pending):
    from core.plugins.consent import resolve_pending_from_utterance

    pending_inputs, _, _ = fake_pending
    ran: list[str] = []

    async def action():
        ran.append("deleted")
        return "Deleted automation Slack mentions."

    await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="Permanently delete automation 'Slack mentions'?",
        source={"type": "foreground_turn"},
        callback=action,
        publish="none",
    )

    result = await resolve_pending_from_utterance("geoff", "Yes, please.")

    assert result == "Deleted automation Slack mentions."
    assert ran == ["deleted"]


@pytest.mark.asyncio
async def test_resolve_pending_from_utterance_ignores_yes_without_pending(fake_pending):
    from core.plugins.consent import resolve_pending_from_utterance

    assert await resolve_pending_from_utterance("geoff", "yes") is None


@pytest.mark.asyncio
async def test_resolve_pending_from_utterance_ignores_non_exact_yes(fake_pending):
    from core.plugins.consent import resolve_pending_from_utterance

    pending_inputs, _, _ = fake_pending

    async def action():
        return "should not run"

    await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="Permanently delete automation 'Slack mentions'?",
        source={"type": "foreground_turn"},
        callback=action,
        publish="none",
    )

    assert await resolve_pending_from_utterance("geoff", "yes delete it") is None


@pytest.mark.asyncio
async def test_resolve_pending_from_utterance_denies_nope(fake_pending):
    from core.plugins.consent import resolve_pending_from_utterance

    pending_inputs, _, _ = fake_pending
    ran: list[str] = []

    async def action():
        ran.append("deleted")
        return "Deleted."

    await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="Permanently delete automation 'Slack mentions'?",
        source={"type": "foreground_turn"},
        callback=action,
        publish="none",
    )

    result = await resolve_pending_from_utterance("geoff", "nope")

    assert result == "Cancelled."
    assert ran == []


@pytest.mark.asyncio
async def test_duplicate_pending_input_reuses_existing_row_and_latest_callback(
    fake_pending,
):
    pending_inputs, pending, _ = fake_pending
    ran: list[str] = []

    async def first():
        ran.append("first")
        return "first ran"

    async def second():
        ran.append("second")
        return "second ran"

    source = {"type": "foreground_turn", "id": ""}
    first_doc = await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="Delete event?",
        detail="Event ID: evt-1",
        source=source,
        callback=first,
        publish="none",
    )
    second_doc = await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="Delete event?",
        detail="Event ID: evt-1",
        source=source,
        callback=second,
        publish="none",
    )

    result = await pending_inputs.resolve_pending_input(
        owner_id="geoff",
        input_id=second_doc["input_id"],
        decision="approve",
    )

    assert first_doc["input_id"] == second_doc["input_id"]
    assert len(pending.docs) == 1
    assert result == "second ran"
    assert ran == ["second"]


@pytest.mark.asyncio
async def test_background_waiter_timeout_publishes_expired_widget(
    fake_pending, monkeypatch
):
    pending_inputs, pending, _ = fake_pending
    publish = AsyncMock()
    monkeypatch.setattr(pending_inputs.event_bus, "publish", publish)
    doc = await pending_inputs.create_pending_input(
        owner_id="geoff",
        prompt="approve task?",
        source={"type": "background_task", "id": "task-1", "task_id": "task-1"},
        create_waiter=True,
        publish="none",
    )

    decision = await pending_inputs.wait_for_pending_input(
        doc["input_id"], timeout_s=0.01
    )

    assert decision == "expired"
    assert pending.docs[0]["status"] == "expired"
    payload = publish.await_args.args[0].data["envelope"]["data"]
    assert payload["input_id"] == doc["input_id"]
    assert payload["status"] == "expired"
    assert payload["result"] == "The pending action expired."


@pytest.mark.asyncio
async def test_approval_needed_trigger_uses_trigger_delivery(monkeypatch):
    from plugins.agents import client

    create_instance = AsyncMock(return_value=SimpleNamespace(id="trg-approval"))
    publish = AsyncMock()
    monkeypatch.setattr(client.trigger_service, "create_instance", create_instance)
    monkeypatch.setattr(client.event_bus, "publish", publish)

    await client._publish_approval_needed_trigger(
        owner_id="geoff",
        task_id="task-1",
        input_id="inp-1",
        prompt="Write approval_test.txt?",
    )

    kwargs = create_instance.await_args.kwargs
    assert kwargs["dedup_key"] == "task-approval:inp-1"
    assert kwargs["attention"].level == "urgent"
    assert kwargs["attention"].sound == "chime"
    assert kwargs["action"].decision == "tell"
    assert kwargs["action"].content_type == "task_result"
    assert kwargs["action"].reply_grounding == {}
    assert kwargs["source_event"] == {
        "task_id": "task-1",
        "input_id": "inp-1",
        "owner_id": "geoff",
    }
    assert kwargs["management"].provider == "agents"
    assert kwargs["management"].resource_id == "task-1"
    event = publish.await_args.args[0]
    assert event.type.value == "trigger.due"
    assert event.data == {"instance_id": "trg-approval", "owner_id": "geoff"}


@pytest.mark.asyncio
async def test_orphan_cleanup_cancels_runtime_bound_inputs(fake_pending):
    pending_inputs, pending, background = fake_pending
    await pending.insert_one(
        {
            "input_id": "inp-1",
            "owner_id": "geoff",
            "kind": "approval",
            "status": "pending",
            "runtime_bound": True,
            "source": {"type": "background_task", "id": "task-1"},
        }
    )
    await background.insert_one(
        {
            "task_id": "task-1",
            "attention": "approval",
            "pending_input": {"input_id": "inp-1"},
            "live_status": "Waiting",
        }
    )

    count = await pending_inputs.cancel_orphaned_pending_inputs()

    assert count == 1
    assert pending.docs[0]["status"] == "cancelled"
    assert background.docs[0]["attention"] == "none"
    assert background.docs[0]["pending_input"] is None
    assert background.docs[0]["live_status"] is None


@pytest.mark.asyncio
async def test_foreground_require_consent_compatibility(fake_pending, tool_context):
    from core.plugins.consent import execute_pending, require_consent

    async def action():
        return "ran"

    with tool_context(owner_id="geoff", connection_id="conn", timezone="UTC"):
        blocked = await require_consent("Run it?", action, detail="detail")
        assert blocked.code == "approval_needed"
        assert blocked.message == "Approval needed: Run it? The action has not executed yet."
        assert await execute_pending("geoff") == "ran"


@pytest.mark.asyncio
async def test_jarvis_background_approval_pauses_and_resumes(monkeypatch, fake_pending):
    import plugins.agents as agents_mod
    from core.agent.agent import AgentEvent, AgentEventType
    from core.plugins.consent import require_consent
    from plugins.agents import AgentsPlugin

    pending_inputs, pending, _ = fake_pending
    task_col = FakeCollection()
    await task_col.insert_one(
        {
            "task_id": "task-1",
            "owner_id": "geoff",
            "status": "running",
            "mode": "jarvis",
            "prompt": "do it",
            "source": "voice",
            "progress_summary": "Starting…",
            "live_status": "Starting…",
            "attention": "none",
            "pending_input": None,
            "trace": [],
            "events": [],
            "artifacts": [],
            "activity": [],
            "created_at": 1,
        }
    )

    class FakeAgent:
        llm = SimpleNamespace(model="test-model", supports_reasoning_effort=False)

        async def process_stream(self, *args, **kwargs):
            async def action():
                return "approved action ran"

            output = await require_consent(
                "Approve background write?", action, detail="write file"
            )
            content = output.message if hasattr(output, "message") else output
            yield AgentEvent(type=AgentEventType.TOOL_OUTPUT, content=content)
            yield AgentEvent(type=AgentEventType.TEXT, content="All done.")

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)

    async def fake_prepare_task(**kwargs):
        return "task-1", task_col

    monkeypatch.setattr(
        plugin, "_get_background_agent", AsyncMock(return_value=FakeAgent())
    )
    monkeypatch.setattr(plugin, "_prepare_task", fake_prepare_task)
    monkeypatch.setattr(
        agents_mod,
        "build_background_context",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(agents_mod, "_complete_task", AsyncMock())
    monkeypatch.setattr(agents_mod, "_fail_task", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_task_event", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_widget", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_task_progress_receipt", AsyncMock())
    monkeypatch.setattr(agents_mod, "_publish_approval_needed_trigger", AsyncMock())

    from core import tool_router as tool_router_mod

    monkeypatch.setattr(
        tool_router_mod.tool_router, "route", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(pending_inputs.event_bus, "publish", AsyncMock())

    result = await plugin._dispatch_inprocess(
        prompt="do it",
        cwd="/tmp",
        max_budget_usd=1.0,
    )
    assert '"ok": true' in result

    for _ in range(20):
        if pending.docs:
            break
        await __import__("asyncio").sleep(0.01)

    assert pending.docs[0]["prompt"] == "Approve background write?"
    assert task_col.docs[0]["attention"] == "approval"
    assert task_col.docs[0]["pending_input"]["input_id"] == pending.docs[0]["input_id"]
    agents_mod._publish_approval_needed_trigger.assert_awaited_once_with(
        owner_id="geoff",
        task_id="task-1",
        input_id=pending.docs[0]["input_id"],
        prompt="Approve background write?",
    )

    task = plugin._running_tasks["task-1"]
    await pending_inputs.resolve_pending_input(
        owner_id="geoff",
        input_id=pending.docs[0]["input_id"],
        decision="approve",
    )
    await task

    assert agents_mod._complete_task.await_count == 1
    assert task_col.docs[0]["attention"] == "none"
    assert task_col.docs[0]["pending_input"] is None
    assert any(
        item["kind"] == "approval_requested" for item in task_col.docs[0]["trace"]
    )
    assert any(
        item["kind"] == "approval_resolved" for item in task_col.docs[0]["trace"]
    )


@pytest.mark.asyncio
async def test_dispatch_inprocess_captures_reasoning_trace(monkeypatch):
    import plugins.agents as agents_mod
    from core.agent.agent import AgentEvent, AgentEventType
    from plugins.agents import AgentsPlugin

    task_col = FakeCollection()
    await task_col.insert_one(
        {
            "task_id": "task-1",
            "owner_id": "geoff",
            "status": "running",
            "mode": "jarvis",
            "prompt": "analyze",
            "source": "voice",
            "progress_summary": "Starting…",
            "live_status": "Starting…",
            "attention": "none",
            "pending_input": None,
            "trace": [],
            "events": [],
            "artifacts": [],
            "activity": [],
            "created_at": 1,
        }
    )

    class FakeAgent:
        llm = SimpleNamespace(model="claude-opus-4-8", supports_reasoning_effort=True)

        async def process_stream(self, *args, **kwargs):
            yield AgentEvent(type=AgentEventType.REASONING, content="Let me think. ")
            yield AgentEvent(type=AgentEventType.REASONING, content="More thinking.")
            yield AgentEvent(type=AgentEventType.TEXT, content="Final answer.")

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)

    async def fake_prepare_task(**kwargs):
        return "task-1", task_col

    monkeypatch.setattr(
        plugin, "_get_background_agent", AsyncMock(return_value=FakeAgent())
    )
    monkeypatch.setattr(plugin, "_prepare_task", fake_prepare_task)
    monkeypatch.setattr(
        agents_mod,
        "build_background_context",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(agents_mod, "_complete_task", AsyncMock())
    monkeypatch.setattr(agents_mod, "_fail_task", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_task_event", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_widget", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_task_progress_receipt", AsyncMock())

    from core import tool_router as tool_router_mod

    monkeypatch.setattr(
        tool_router_mod.tool_router, "route", AsyncMock(return_value=[])
    )

    result = await plugin._dispatch_inprocess(
        prompt="analyze", cwd="/tmp", max_budget_usd=1.0
    )
    assert '"ok": true' in result

    task = plugin._running_tasks["task-1"]
    await task

    reasoning = [
        item for item in task_col.docs[0]["trace"] if item["kind"] == "reasoning"
    ]
    assert len(reasoning) == 1
    assert reasoning[0]["text_preview"] == "Let me think. More thinking."
    assert any(item["kind"] == "text" for item in task_col.docs[0]["trace"])


@pytest.mark.asyncio
async def test_dispatch_inprocess_forwards_ui_updates(monkeypatch):
    import plugins.agents as agents_mod
    from core.agent.agent import AgentEvent, AgentEventType
    from plugins.agents import AgentsPlugin

    task_col = FakeCollection()
    await task_col.insert_one(
        {
            "task_id": "task-1",
            "owner_id": "geoff",
            "status": "running",
            "mode": "jarvis",
            "prompt": "analyze",
            "source": "voice",
            "progress_summary": "Starting…",
            "live_status": "Starting…",
            "attention": "none",
            "pending_input": None,
            "trace": [],
            "events": [],
            "artifacts": [],
            "activity": [],
            "created_at": 1,
        }
    )

    envelope = {
        "widget_id": "content-1",
        "component": "ContentWidget",
        "data": {"title": "Analysis", "sections": []},
    }

    class FakeAgent:
        llm = SimpleNamespace(model="claude-opus-4-8", supports_reasoning_effort=True)

        async def process_stream(self, *args, **kwargs):
            yield AgentEvent(
                type=AgentEventType.UI_UPDATE,
                content=__import__("json").dumps(envelope),
            )
            yield AgentEvent(type=AgentEventType.TEXT, content="Done.")

    plugin = AgentsPlugin()
    plugin._semaphore = __import__("asyncio").Semaphore(1)
    push_ui = AsyncMock()

    async def fake_prepare_task(**kwargs):
        return "task-1", task_col

    monkeypatch.setattr(
        plugin, "_get_background_agent", AsyncMock(return_value=FakeAgent())
    )
    monkeypatch.setattr(plugin, "_prepare_task", fake_prepare_task)
    monkeypatch.setattr(
        agents_mod,
        "build_background_context",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(agents_mod, "_complete_task", AsyncMock())
    monkeypatch.setattr(agents_mod, "_fail_task", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_task_event", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_ui_envelope", push_ui)
    monkeypatch.setattr(agents_mod, "_push_widget", AsyncMock())
    monkeypatch.setattr(agents_mod, "_push_task_progress_receipt", AsyncMock())

    from core import tool_router as tool_router_mod

    monkeypatch.setattr(
        tool_router_mod.tool_router, "route", AsyncMock(return_value=[])
    )

    result = await plugin._dispatch_inprocess(
        prompt="analyze", cwd="/tmp", max_budget_usd=1.0
    )
    assert '"ok": true' in result

    task = plugin._running_tasks["task-1"]
    await task

    push_ui.assert_awaited_once_with("geoff", envelope)
