import asyncio
import json
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

def test_dispatch_without_cwd_does_not_default_to_project_root():
    assert _normalize_agent_cwd(None) is None
    assert _normalize_agent_cwd("") is None
    assert _normalize_agent_cwd("~/dev/shop").endswith("/dev/shop")


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
        "work_id": None,
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

    assert recovered == 4
    assert collection.update_many.await_count == 2
    open_filt, open_update = collection.update_many.await_args_list[0].args
    legacy_filt, legacy_update = collection.update_many.await_args_list[1].args
    assert open_filt == {"status": "running", "open": True}
    assert "expires_at" not in open_update["$set"]
    assert legacy_filt == {"status": "running", "open": {"$ne": True}}
    assert legacy_update["$set"]["expires_at"] is not None
    assert legacy_update["$set"]["interrupted_reason"] == "backend_restart"


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, _n):
        return self

    async def to_list(self, length=None):
        return self.docs[:length] if length else list(self.docs)


class _Tasks:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *_args, **_kwargs):
        return _Cursor(self.docs)

    async def find_one(self, filt, _projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filt.items() if k != "owner_id"):
                return doc
        return None


def _patch_tasks(monkeypatch, docs):
    collection = _Tasks(docs)
    monkeypatch.setattr("plugins.agents.work.mongodb.get_collection", lambda _name: collection)
    monkeypatch.setattr("plugins.agents.mongodb.get_collection", lambda _name: collection)
    return collection


@pytest.mark.asyncio
async def test_get_result_returns_full_completed_result(monkeypatch):
    plugin = AgentsPlugin()
    _patch_tasks(
        monkeypatch,
        [
            {
                "task_id": "task-1",
                "work_id": "work-1",
                "title": "2713 review",
                "open": True,
                "status": "completed",
                "progress_summary": "short preview",
                "result": "full completed result",
                "artifacts": [{"path": "/tmp/jarvis-output.test"}],
            }
        ],
    )

    payload = json.loads(await plugin.get_result("task-1"))

    assert payload["ok"] is True
    assert payload["task_id"] == "task-1"
    assert payload["work_id"] == "work-1"
    assert payload["status"] == "completed"
    assert payload["result"] == "full completed result"
    assert payload["open"] is True
    assert "resume" in payload["message"]
    assert payload["artifacts"] == [{"path": "/tmp/jarvis-output.test"}]


@pytest.mark.asyncio
async def test_get_result_without_target_returns_latest_open_finished(monkeypatch):
    plugin = AgentsPlugin()
    _patch_tasks(
        monkeypatch,
        [
            {
                "task_id": "latest-task",
                "work_id": "work-2",
                "title": "Quoting ports",
                "open": True,
                "status": "failed",
                "result": "failure reason",
            }
        ],
    )

    payload = json.loads(await plugin.get_result())

    assert payload["ok"] is True
    assert payload["task_id"] == "latest-task"
    assert payload["result"] == "failure reason"


@pytest.mark.asyncio
async def test_get_status_keeps_completed_open_work_resumable(monkeypatch):
    plugin = AgentsPlugin()
    _patch_tasks(
        monkeypatch,
        [
            {
                "task_id": "task-1",
                "work_id": "work-1",
                "title": "2713 review",
                "open": True,
                "status": "completed",
                "cwd": "/tmp/aetheron-connect-v2",
                "result": "Smallest fix is the judge gate.",
            }
        ],
    )

    status = await plugin.get_status("2713 review")

    assert "2713 review" in status
    assert "completed" in status
    assert "Still open — resume to continue" in status
    assert "Do not recall()" in status
