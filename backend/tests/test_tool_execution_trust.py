import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.config import settings
from plugins.agents import (
    AgentsPlugin,
    RESTART_INTERRUPTED_RESULT,
    _dispatch_result,
    _normalize_agent_cwd,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dispatch_without_cwd_normalizes_to_project_root():
    assert _normalize_agent_cwd(None) == str(settings.BASE_DIR.parent)
    assert _normalize_agent_cwd("") == str(settings.BASE_DIR.parent)


def test_dispatch_result_has_stable_parseable_shape():
    payload = json.loads(
        _dispatch_result(
            ok=False,
            error_code="source_limit_reached",
            error_message="limit reached",
            message="Task not started: limit reached",
        )
    )

    assert payload == {
        "ok": False,
        "task_id": None,
        "mode": None,
        "error_code": "source_limit_reached",
        "error_message": "limit reached",
        "message": "Task not started: limit reached",
    }


@pytest.mark.asyncio
async def test_jarvis_dispatch_runtime_unavailable_does_not_fallback_to_code(
    monkeypatch,
):
    plugin = AgentsPlugin()
    plugin._semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(plugin, "_get_background_agent", AsyncMock(return_value=None))
    dispatch_code = AsyncMock()
    monkeypatch.setattr(plugin, "_dispatch", dispatch_code)

    payload = json.loads(await plugin.dispatch(prompt="check Gmail", mode="jarvis"))

    assert payload["ok"] is False
    assert payload["task_id"] is None
    assert payload["mode"] is None
    assert payload["error_code"] == "jarvis_runtime_unavailable"
    dispatch_code.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_source_limit_failure_does_not_create_task():
    plugin = AgentsPlugin()
    plugin._semaphore = asyncio.Semaphore(1)
    plugin._source_counts["voice"] = settings.AGENT_MAX_PER_SOURCE

    result = await plugin._prepare_task(
        prompt="do work",
        cwd=str(settings.BASE_DIR.parent),
        mode="code",
        max_turns=1,
        max_budget_usd=0.01,
        source="voice",
        trigger_ref=None,
        depth=0,
    )

    assert result == {
        "error_code": "source_limit_reached",
        "error_message": (
            f"Source 'voice' already has {settings.AGENT_MAX_PER_SOURCE} active "
            f"task(s) (limit: {settings.AGENT_MAX_PER_SOURCE})."
        ),
    }


@pytest.mark.asyncio
async def test_startup_recovery_marks_stale_running_tasks_failed(monkeypatch):
    plugin = AgentsPlugin()
    collection = SimpleNamespace(
        update_many=AsyncMock(return_value=SimpleNamespace(modified_count=2))
    )
    monkeypatch.setattr(
        "plugins.agents.mongodb.get_collection", lambda _name: collection
    )

    recovered = await plugin._recover_interrupted_tasks()

    assert recovered == 2
    filt, update = collection.update_many.await_args.args
    assert filt == {"status": "running"}
    fields = update["$set"]
    assert fields["status"] == "failed"
    assert fields["result"] == RESTART_INTERRUPTED_RESULT
    assert fields["progress_summary"] == "Interrupted by backend restart."
    assert fields["interrupted_reason"] == "backend_restart"
    assert fields["completed_at"] is not None
    assert fields["expires_at"] is not None


@pytest.mark.asyncio
async def test_get_result_returns_full_completed_result(monkeypatch):
    plugin = AgentsPlugin()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "task_id": "task-1",
                "status": "completed",
                "progress_summary": "short preview",
                "result": "full completed result",
                "artifacts": [{"path": "/tmp/jarvis-output.test"}],
            }
        )
    )
    monkeypatch.setattr(
        "plugins.agents.mongodb.get_collection", lambda _name: collection
    )

    payload = json.loads(await plugin.get_result("task-1"))

    assert payload["ok"] is True
    assert payload["task_id"] == "task-1"
    assert payload["status"] == "completed"
    assert payload["result"] == "full completed result"
    assert payload["artifacts"] == [{"path": "/tmp/jarvis-output.test"}]


@pytest.mark.asyncio
async def test_get_result_without_task_id_returns_latest_finished_task(monkeypatch):
    plugin = AgentsPlugin()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "task_id": "latest-task",
                "status": "failed",
                "result": "failure reason",
            }
        )
    )
    monkeypatch.setattr(
        "plugins.agents.mongodb.get_collection", lambda _name: collection
    )

    payload = json.loads(await plugin.get_result())

    assert payload["ok"] is True
    assert payload["task_id"] == "latest-task"
    assert payload["result"] == "failure reason"
    query = collection.find_one.await_args.args[0]
    assert query["status"] == {"$in": ["completed", "failed"]}
    assert collection.find_one.await_args.kwargs["sort"] == [
        ("completed_at", -1),
        ("created_at", -1),
    ]


@pytest.mark.asyncio
async def test_get_status_points_completed_tasks_to_get_result(monkeypatch):
    plugin = AgentsPlugin()
    collection = SimpleNamespace(
        find_one=AsyncMock(
            return_value={
                "task_id": "task-1",
                "status": "completed",
                "progress_summary": "truncated preview",
            }
        )
    )
    monkeypatch.setattr(
        "plugins.agents.mongodb.get_collection", lambda _name: collection
    )

    status = await plugin.get_status("task-1")

    assert "status=completed" in status
    assert "truncated preview" not in status
    assert 'jarvis.agents.get_result(task_id="task-1")' in status


def test_protocols_capture_tool_truthfulness_invariants():
    protocols = (REPO_ROOT / "backend/core/prompts/persona/protocols.yaml").read_text()
    agents_source = (REPO_ROOT / "backend/plugins/agents/__init__.py").read_text()
    agents_source_flat = " ".join(agents_source.split())

    assert "the tool result is the source of truth" in protocols.casefold()
    assert "blocked pending approval" in protocols
    assert "action has NOT executed yet" in protocols
    assert "Do NOT infer that the target was already changed" in protocols
    assert "If ok=false, no task was started." in agents_source
    assert "Do not dispatch the same work again unless the user explicitly asks" in agents_source_flat
