from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.routes.tasks import TaskDetail
from core.plugins.consent import _consent_resolver, require_consent
from plugins.agents import client
from plugins.agents.task_review import (
    append_trace,
    activity_from_tool_output,
    file_snapshot,
    task_trace_item,
    verify_file_artifact,
    written_artifacts_from_output,
)


@pytest.fixture(autouse=True)
def _configured_anthropic_key(monkeypatch):
    monkeypatch.setattr(
        client.credential_store,
        "get_stored_secret",
        lambda name: "test-key" if name == "ANTHROPIC_API_KEY" else None,
    )


def test_file_artifact_verification_uses_cwd(tmp_path: Path):
    artifact_path = tmp_path / "report.txt"
    artifact_path.write_text("done")

    artifact = verify_file_artifact("report.txt", cwd=str(tmp_path), source="code")

    assert artifact == {
        "path": str(artifact_path),
        "source": "code",
        "exists_verified": True,
        "exists": True,
        "size_bytes": 4,
        "changed": None,
    }


def test_jarvis_output_extracts_written_artifact(tmp_path: Path):
    artifact_path = tmp_path / "calculator_app.html"
    artifact_path.write_text("<html></html>")

    artifacts = written_artifacts_from_output(
        f"Written {artifact_path} (13 bytes).",
        cwd=str(tmp_path),
        source="jarvis",
    )

    assert artifacts[0]["path"] == str(artifact_path)
    assert artifacts[0]["exists_verified"] is True


def test_file_snapshot_never_raises_on_git_revision_syntax(tmp_path: Path):
    snap = file_snapshot("~30", cwd=str(tmp_path))
    assert snap["exists"] is False


def test_activity_rows_are_based_on_observed_output():
    activity = activity_from_tool_output("Found 3 events", source="jarvis")

    assert activity == {
        "source": "jarvis",
        "status": "completed",
        "summary": "Found 3 events",
    }


def test_trace_helpers_cap_previews_without_regex_inference():
    item = task_trace_item(
        kind="tool_call",
        ts=123,
        span_id="span-1",
        tool="jarvis.gmail.search_emails, jarvis.calendar.list_events",
        code="x" * 9000,
        status="running",
    )
    trace = append_trace([], item)

    assert trace[0]["tool"] == "jarvis.gmail.search_emails, jarvis.calendar.list_events"
    assert len(trace[0]["code"]) == 8000


@pytest.mark.asyncio
async def test_code_runtime_captures_and_verifies_sdk_file_artifacts(tmp_path: Path, monkeypatch):
    artifact_path = tmp_path / "app.html"

    class FakeToolUseBlock:
        id = "tool-use-write"
        name = "Write"
        input = {"file_path": str(artifact_path)}

    class FakeAssistantMessage:
        session_id = "session-1"
        content = [FakeToolUseBlock()]

    class FakeResultMessage:
        session_id = "session-1"
        total_cost_usd = 0.01
        result = None
        duration_ms = 100
        num_turns = 1
        usage = None

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield FakeAssistantMessage()
            artifact_path.write_text("<html></html>")
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(client, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(client, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(client, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(client, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(client.mongodb, "get_collection", lambda name: collection)
    monkeypatch.setattr(client, "_push_task_event", AsyncMock())
    complete = AsyncMock()
    monkeypatch.setattr(client, "_complete_task", complete)

    await client._run_agent(
        task_id="task-1",
        owner_id="geoff",
        prompt="write file",
        cwd=str(tmp_path),
        max_turns=5,
        mcp_servers=[],
        system_prompt="system",
    )

    artifact_update = next(
        call.args[1]["$set"]["artifacts"]
        for call in collection.update_one.await_args_list
        if "artifacts" in call.args[1].get("$set", {})
    )
    trace_update = next(
        call.args[1]["$set"]["trace"]
        for call in collection.update_one.await_args_list
        if "trace" in call.args[1].get("$set", {})
    )
    assert artifact_update[0]["path"] == str(artifact_path)
    assert artifact_update[0]["exists_verified"] is True
    assert artifact_update[0]["changed"] is True
    assert any(item["kind"] == "tool_call" and item["tool"].startswith("Write:") for item in trace_update)
    assert complete.await_args.kwargs["result"] != "(no output)"


@pytest.mark.asyncio
async def test_multi_edit_edits_paths_are_captured_as_artifacts(tmp_path: Path, monkeypatch):
    first = tmp_path / "one.py"
    second = tmp_path / "two.py"
    first.write_text("one")
    second.write_text("two")

    class FakeToolUseBlock:
        id = "tool-use-multiedit"
        name = "MultiEdit"
        input = {
            "edits": [
                {"file_path": str(first), "old_string": "one", "new_string": "ONE"},
                {"file_path": str(second), "old_string": "two", "new_string": "TWO"},
            ]
        }

    class FakeAssistantMessage:
        session_id = "session-1"
        content = [FakeToolUseBlock()]

    class FakeResultMessage:
        session_id = "session-1"
        total_cost_usd = 0.01
        result = "Edited files."
        duration_ms = 100
        num_turns = 1
        usage = None

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield FakeAssistantMessage()
            first.write_text("ONE")
            second.write_text("TWO")
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(client, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(client, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(client, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(client, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(client.mongodb, "get_collection", lambda name: collection)
    monkeypatch.setattr(client, "_push_task_event", AsyncMock())
    monkeypatch.setattr(client, "_complete_task", AsyncMock())

    await client._run_agent(
        task_id="task-1",
        owner_id="geoff",
        prompt="edit files",
        cwd=str(tmp_path),
        max_turns=5,
        mcp_servers=[],
        system_prompt="system",
    )

    artifact_update = next(
        call.args[1]["$set"]["artifacts"]
        for call in collection.update_one.await_args_list
        if "artifacts" in call.args[1].get("$set", {})
    )
    assert {artifact["path"] for artifact in artifact_update} == {str(first), str(second)}
    assert all(artifact["changed"] is True for artifact in artifact_update)


@pytest.mark.asyncio
async def test_unchanged_write_is_not_persisted_as_artifact(tmp_path: Path, monkeypatch):
    artifact_path = tmp_path / "unchanged.txt"
    artifact_path.write_text("same")

    class FakeToolUseBlock:
        id = "tool-use-write"
        name = "Write"
        input = {"file_path": str(artifact_path)}

    class FakeAssistantMessage:
        session_id = "session-1"
        content = [FakeToolUseBlock()]

    class FakeResultMessage:
        session_id = "session-1"
        total_cost_usd = 0.01
        result = "No change needed."
        duration_ms = 100
        num_turns = 1
        usage = None

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield FakeAssistantMessage()
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(client, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(client, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(client, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(client, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(client.mongodb, "get_collection", lambda name: collection)
    monkeypatch.setattr(client, "_push_task_event", AsyncMock())
    monkeypatch.setattr(client, "_complete_task", AsyncMock())

    await client._run_agent(
        task_id="task-1",
        owner_id="geoff",
        prompt="write file",
        cwd=str(tmp_path),
        max_turns=5,
        mcp_servers=[],
        system_prompt="system",
    )

    artifact_updates = [
        call.args[1]["$set"]["artifacts"]
        for call in collection.update_one.await_args_list
        if "artifacts" in call.args[1].get("$set", {})
    ]
    assert artifact_updates == []


@pytest.mark.asyncio
async def test_code_runtime_uses_result_message_result(tmp_path: Path, monkeypatch):
    class FakeAssistantMessage:
        session_id = "session-1"
        content = []

    class FakeResultMessage:
        session_id = "session-1"
        total_cost_usd = 0.01
        result = "Audit complete: modular architecture is sound."
        duration_ms = 5000
        num_turns = 3
        usage = {"input_tokens": 100, "output_tokens": 50}

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield FakeAssistantMessage()
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(client, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(client, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(client, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(client.mongodb, "get_collection", lambda name: collection)
    monkeypatch.setattr(client, "_push_task_event", AsyncMock())
    complete = AsyncMock()
    monkeypatch.setattr(client, "_complete_task", complete)

    await client._run_agent(
        task_id="task-1",
        owner_id="geoff",
        prompt="audit",
        cwd=str(tmp_path),
        max_turns=5,
        mcp_servers=[],
        system_prompt="system",
    )

    assert complete.await_args.kwargs["result"] == "Audit complete: modular architecture is sound."
    assert complete.await_args.kwargs["duration_ms"] == 5000
    assert complete.await_args.kwargs["num_turns"] == 3
    assert complete.await_args.kwargs["usage"] == {"input_tokens": 100, "output_tokens": 50}


@pytest.mark.asyncio
async def test_read_only_tool_produces_no_artifacts(tmp_path: Path, monkeypatch):
    read_path = tmp_path / "main.py"
    read_path.write_text("print('hi')")

    class FakeToolUseBlock:
        id = "tool-use-read"
        name = "Read"
        input = {"file_path": str(read_path)}

    class FakeAssistantMessage:
        session_id = "session-1"
        content = [FakeToolUseBlock()]

    class FakeResultMessage:
        session_id = "session-1"
        total_cost_usd = 0.01
        result = "Read main.py successfully."
        duration_ms = 100
        num_turns = 1
        usage = None

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield FakeAssistantMessage()
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(client, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(client, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(client, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(client, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(client.mongodb, "get_collection", lambda name: collection)
    monkeypatch.setattr(client, "_push_task_event", AsyncMock())
    complete = AsyncMock()
    monkeypatch.setattr(client, "_complete_task", complete)

    await client._run_agent(
        task_id="task-1",
        owner_id="geoff",
        prompt="read main.py",
        cwd=str(tmp_path),
        max_turns=5,
        mcp_servers=[],
        system_prompt="system",
    )

    artifact_updates = [
        call.args[1]["$set"]["artifacts"]
        for call in collection.update_one.await_args_list
        if "artifacts" in call.args[1].get("$set", {})
    ]
    assert artifact_updates == []
    assert complete.await_args.kwargs["result"] == "Read main.py successfully."


@pytest.mark.asyncio
async def test_bash_with_git_revision_syntax_produces_no_artifacts(tmp_path: Path, monkeypatch):
    class FakeToolUseBlock:
        id = "tool-use-bash"
        name = "Bash"
        input = {"command": "git log HEAD~30 --oneline"}

    class FakeAssistantMessage:
        session_id = "session-1"
        content = [FakeToolUseBlock()]

    class FakeResultMessage:
        session_id = "session-1"
        total_cost_usd = 0.01
        result = "commit list"
        duration_ms = 50
        num_turns = 1
        usage = None

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield FakeAssistantMessage()
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(client, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(client, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(client, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(client, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(client.mongodb, "get_collection", lambda name: collection)
    monkeypatch.setattr(client, "_push_task_event", AsyncMock())
    complete = AsyncMock()
    monkeypatch.setattr(client, "_complete_task", complete)

    await client._run_agent(
        task_id="task-1",
        owner_id="geoff",
        prompt="git history",
        cwd=str(tmp_path),
        max_turns=5,
        mcp_servers=[],
        system_prompt="system",
    )

    artifact_updates = [
        call.args[1]["$set"]["artifacts"]
        for call in collection.update_one.await_args_list
        if "artifacts" in call.args[1].get("$set", {})
    ]
    assert artifact_updates == []
    assert complete.await_args.kwargs["result"] == "commit list"


@pytest.mark.asyncio
async def test_user_message_tool_result_recorded_in_trace(tmp_path: Path, monkeypatch):
    class FakeToolUseBlock:
        id = "tool-use-1"
        name = "Bash"
        input = {"command": "echo hi"}

    class FakeToolResultBlock:
        tool_use_id = "tool-use-1"
        content = "hi\n"
        is_error = False

    class FakeAssistantMessage:
        session_id = "session-1"
        content = [FakeToolUseBlock()]

    class FakeUserMessage:
        content = [FakeToolResultBlock()]
        tool_use_result = None

    class FakeResultMessage:
        session_id = "session-1"
        total_cost_usd = 0.01
        result = "done"
        duration_ms = 10
        num_turns = 1
        usage = None

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield FakeAssistantMessage()
            yield FakeUserMessage()
            yield FakeResultMessage()

        async def disconnect(self):
            return None

    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(client, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(client, "AgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(client, "ToolUseBlock", FakeToolUseBlock)
    monkeypatch.setattr(client, "ToolResultBlock", FakeToolResultBlock)
    monkeypatch.setattr(client, "UserMessage", FakeUserMessage)
    monkeypatch.setattr(client, "AssistantMessage", FakeAssistantMessage)
    monkeypatch.setattr(client, "ResultMessage", FakeResultMessage)
    monkeypatch.setattr(client.mongodb, "get_collection", lambda name: collection)
    monkeypatch.setattr(client, "_push_task_event", AsyncMock())
    monkeypatch.setattr(client, "_complete_task", AsyncMock())

    await client._run_agent(
        task_id="task-1",
        owner_id="geoff",
        prompt="run",
        cwd=str(tmp_path),
        max_turns=5,
        mcp_servers=[],
        system_prompt="system",
    )

    trace_updates = [
        call.args[1]["$set"]["trace"]
        for call in collection.update_one.await_args_list
        if "trace" in call.args[1].get("$set", {})
    ]
    final_trace = trace_updates[-1]
    tool_results = [item for item in final_trace if item.get("kind") == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["result_preview"] == "hi"
    assert tool_results[0]["status"] == "completed"


def test_task_detail_accepts_current_evidence_fields():
    task = TaskDetail(
        task_id="task-1",
        status="completed",
        source="voice",
        prompt="do work",
        progress_summary="done",
        live_status=None,
        attention="none",
        pending_input=None,
        cost_usd=None,
        created_at=1,
        completed_at=2,
        cwd="/tmp",
        max_turns=10,
        max_budget_usd=1.0,
        result="done",
        session_id=None,
        duration_ms=1200,
        num_turns=4,
        usage={"input_tokens": 10},
        events=[],
        artifacts=[],
        activity=[],
        trace=[],
    )

    assert task.artifacts == []
    assert task.activity == []
    assert task.trace == []
    assert task.duration_ms == 1200
    assert task.num_turns == 4
    assert task.usage == {"input_tokens": 10}
    assert task.attention == "none"
    assert task.pending_input is None
    assert task.live_status is None


@pytest.mark.asyncio
async def test_require_consent_accepts_sync_and_async_resolvers():
    async def action():
        return "ran"

    token = _consent_resolver.set(lambda desc, detail: True)
    try:
        assert await require_consent("sync", action) == "ran"
    finally:
        _consent_resolver.reset(token)

    async def async_resolver(desc: str, detail: str) -> bool:
        return True

    token = _consent_resolver.set(async_resolver)
    try:
        assert await require_consent("async", action) == "ran"
    finally:
        _consent_resolver.reset(token)
