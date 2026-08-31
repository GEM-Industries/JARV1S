import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.agents.workers import (
    default_worker_kind,
    lineage_worker_kind,
    worker_for_kind,
    worker_ready,
)
from plugins.agents.workers.base import (
    CodeWorkSpec,
    WorkerEvent,
    WorkerRunError,
    WorkerStartupError,
)
from plugins.agents.workers.claude import ClaudeCodeWorker
from plugins.agents.workers import cursor as cursor_worker
from plugins.agents.workers.cursor import CursorLocalWorker, _resolve_model_from_catalog


class _CursorAgentError(Exception):
    pass


def _sdk(client_cls, error_cls=_CursorAgentError):
    return SimpleNamespace(
        AgentOptions=SimpleNamespace,
        AsyncClient=client_cls,
        CursorAgentError=error_cls,
        LocalAgentOptions=SimpleNamespace,
        SendOptions=SimpleNamespace,
    )


def _patch_cursor(monkeypatch, client_cls, error_cls=_CursorAgentError):
    monkeypatch.setattr(
        "plugins.agents.workers.cursor.credential_store.get_stored_secret",
        lambda name: "cursor-key" if name == "CURSOR_API_KEY" else None,
    )
    monkeypatch.setattr("plugins.agents.workers.cursor._cached_model", None)
    monkeypatch.setattr(
        "plugins.agents.workers.cursor._host_model",
        lambda api_key: "auto-smart",
    )
    monkeypatch.setattr(
        "plugins.agents.workers.cursor._load_sdk",
        lambda: _sdk(client_cls, error_cls),
    )


def test_default_worker_prefers_cursor_when_connected(monkeypatch):
    monkeypatch.setattr(
        "plugins.agents.workers.base.credential_store.get_stored_secret",
        lambda name: "k" if name == "CURSOR_API_KEY" else None,
    )
    assert default_worker_kind() == "cursor_local"
    assert isinstance(worker_for_kind("cursor_local"), CursorLocalWorker)


def test_default_worker_falls_back_to_claude(monkeypatch):
    monkeypatch.setattr(
        "plugins.agents.workers.base.credential_store.get_stored_secret",
        lambda name: "k" if name == "ANTHROPIC_API_KEY" else None,
    )
    assert default_worker_kind() == "claude_code"
    assert isinstance(worker_for_kind("claude_code"), ClaudeCodeWorker)


def test_lineage_pins_worker_even_if_cursor_is_now_default(monkeypatch):
    monkeypatch.setattr(
        "plugins.agents.workers.base.credential_store.get_stored_secret",
        lambda name: "k" if name == "CURSOR_API_KEY" else None,
    )
    assert default_worker_kind() == "cursor_local"
    assert lineage_worker_kind({"worker_kind": "claude_code"}) == "claude_code"
    assert lineage_worker_kind({}) == "claude_code"
    assert not worker_ready("claude_code")
    assert worker_ready("cursor_local")


def test_cursor_model_prefers_grok_then_catalog_fallback():
    assert _resolve_model_from_catalog(
        [
            SimpleNamespace(id="composer-2.5"),
            SimpleNamespace(id="auto-smart"),
            SimpleNamespace(id="grok-4.6"),
        ]
    ) == "grok-4.6"
    assert _resolve_model_from_catalog(
        [SimpleNamespace(id="composer-2.5"), SimpleNamespace(id="auto-smart")]
    ) == "auto-smart"
    assert _resolve_model_from_catalog([SimpleNamespace(id="composer-2.5")]) == "composer-2.5"


@pytest.mark.asyncio
async def test_cursor_worker_captures_ids_and_cancels_run(monkeypatch):
    events: list[WorkerEvent] = []

    class FakeRun:
        def __init__(self):
            self.id = "run-1"
            self.status = "running"
            self.cancelled = False

        async def stream(self):
            if False:
                yield None

        async def wait(self):
            await asyncio.sleep(60)
            return SimpleNamespace(status="finished", result="done", duration_ms=1, usage=None)

        async def cancel(self):
            self.cancelled = True
            self.status = "cancelled"

    class FakeAgent:
        agent_id = "agent-abc"

        async def send(self, prompt, *args, **kwargs):
            assert "Review the checkout PR" in prompt
            return FakeRun()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        @classmethod
        async def launch_bridge(cls, workspace=None, **kwargs):
            assert kwargs.get("allow_api_key_env_fallback") is False
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def create_agent(self, options):
            assert options.api_key == "cursor-key"
            return FakeAgent()

        async def resume_agent(self, *_args, **_kwargs):
            raise AssertionError("resume should not be used on first dispatch")

    _patch_cursor(monkeypatch, FakeClient)

    async def emit(event: WorkerEvent) -> None:
        events.append(event)

    spec = CodeWorkSpec(
        prompt="Review the checkout PR",
        cwd="/tmp/repo",
        max_turns=4,
        mcp_servers=[{"name": "gmail", "url": "https://example/mcp"}],
        system_prompt="You are JARV1S.",
        title="checkout PR",
    )
    worker = CursorLocalWorker()
    task = asyncio.create_task(worker.execute(spec, emit))
    for _ in range(20):
        if any(event.external_run_id == "run-1" for event in events):
            break
        await asyncio.sleep(0)
    assert any(event.session_id == "agent-abc" for event in events)
    assert any(event.external_run_id == "run-1" for event in events)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_cursor_startup_error_does_not_look_like_run_failure(monkeypatch):
    class FakeClient:
        @classmethod
        async def launch_bridge(cls, workspace=None, **_kwargs):
            raise _CursorAgentError("auth failed")

    _patch_cursor(monkeypatch, FakeClient)
    worker = CursorLocalWorker()
    with pytest.raises(WorkerStartupError) as exc:
        await worker.execute(
            CodeWorkSpec(prompt="x", cwd="/tmp", max_turns=1, mcp_servers=[], system_prompt=""),
            AsyncMock(),
        )
    assert exc.value.code == "cursor_runtime_unavailable"


@pytest.mark.asyncio
async def test_cursor_run_error_is_distinct_from_startup(monkeypatch):
    class FakeRun:
        id = "run-9"
        status = "running"

        async def stream(self):
            if False:
                yield None

        async def wait(self):
            return SimpleNamespace(status="error", result="merge conflict", duration_ms=2, usage=None)

    class FakeAgent:
        agent_id = "agent-z"

        async def send(self, *_args, **_kwargs):
            return FakeRun()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        @classmethod
        async def launch_bridge(cls, workspace=None, **kwargs):
            assert kwargs.get("allow_api_key_env_fallback") is False
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def create_agent(self, _options):
            return FakeAgent()

    _patch_cursor(monkeypatch, FakeClient)
    worker = CursorLocalWorker()
    with pytest.raises(WorkerRunError, match="merge conflict"):
        await worker.execute(
            CodeWorkSpec(prompt="x", cwd="/tmp", max_turns=1, mcp_servers=[], system_prompt=""),
            AsyncMock(),
        )


@pytest.mark.asyncio
async def test_cursor_agent_error_after_run_starts_is_run_failure(monkeypatch):
    class FakeRun:
        id = "run-2"
        status = "running"

        async def stream(self):
            raise _CursorAgentError("stream died")
            yield None

        async def wait(self):
            raise AssertionError("wait should not run")

    class FakeAgent:
        agent_id = "agent-z"

        async def send(self, *_args, **_kwargs):
            return FakeRun()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeClient:
        @classmethod
        async def launch_bridge(cls, workspace=None, **kwargs):
            assert kwargs.get("allow_api_key_env_fallback") is False
            return cls()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def create_agent(self, _options):
            return FakeAgent()

    _patch_cursor(monkeypatch, FakeClient)
    worker = CursorLocalWorker()
    with pytest.raises(WorkerRunError, match="stream died"):
        await worker.execute(
            CodeWorkSpec(prompt="x", cwd="/tmp", max_turns=1, mcp_servers=[], system_prompt=""),
            AsyncMock(),
        )


def test_host_model_sends_stored_key_to_cloud_catalog(monkeypatch):
    seen: list[str] = []

    def fake_catalog(api_key: str):
        seen.append(api_key)
        return [SimpleNamespace(id="composer-2.5"), SimpleNamespace(id="grok-4.6")]

    monkeypatch.setattr(cursor_worker, "_cached_model", None)
    monkeypatch.setattr(cursor_worker, "_catalog_models", fake_catalog)
    assert cursor_worker._host_model("stored-key") == "grok-4.6"
    assert cursor_worker._host_model("stored-key") == "grok-4.6"
    assert seen == ["stored-key"]


def test_probe_refreshes_cached_model_for_a_new_key(monkeypatch):
    catalogs = {
        "old-key": [SimpleNamespace(id="composer-2.5")],
        "new-key": [SimpleNamespace(id="grok-4.6")],
    }
    monkeypatch.setattr(cursor_worker, "_cached_model", None)
    monkeypatch.setattr(
        cursor_worker,
        "_catalog_models",
        lambda api_key: catalogs[api_key],
    )
    cursor_worker.probe_cursor_account("old-key")
    assert cursor_worker._host_model("old-key") == "composer-2.5"
    cursor_worker.probe_cursor_account("new-key")
    assert cursor_worker._host_model("new-key") == "grok-4.6"
