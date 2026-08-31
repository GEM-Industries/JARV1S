import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.agents import client as agents_client
from plugins.agents.workers import claude as claude_worker


@pytest.mark.asyncio
async def test_run_agent_sets_anthropic_key_from_credential_store(monkeypatch):
    class FakeCollection:
        async def find_one(self, *_args, **_kwargs):
            return None

        async def update_one(self, *_args, **_kwargs):
            return None

    class FakeMongo:
        def get_collection(self, _name):
            return FakeCollection()

    class FakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            assert "ANTHROPIC_API_KEY" not in os.environ
            assert self.options.env == {"ANTHROPIC_API_KEY": "sk-ant-background"}
            assert self.options.effort == "medium"

        async def query(self, _prompt):
            return None

        async def receive_response(self):
            if False:
                yield None

        async def disconnect(self):
            return None

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(agents_client, "mongodb", FakeMongo())
    monkeypatch.setattr(claude_worker, "SDKClient", FakeSDKClient)
    monkeypatch.setattr(agents_client, "_complete_task", AsyncMock())
    monkeypatch.setattr(
        claude_worker.credential_store,
        "get_stored_secret",
        lambda name: "sk-ant-background" if name == "ANTHROPIC_API_KEY" else None,
    )

    await agents_client._run_agent(
        task_id="task-123",
        owner_id="geoff",
        prompt="test",
        cwd="/tmp",
        max_turns=1,
        mcp_servers=[],
        system_prompt="system",
        worker_kind="claude_code",
    )

    agents_client._complete_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_agent_fails_without_stored_anthropic_key(monkeypatch):
    class FakeMongo:
        def get_collection(self, _name):
            return SimpleNamespace(find_one=AsyncMock(return_value=None))

    sdk_client = AsyncMock()
    fail_task = AsyncMock()
    monkeypatch.setattr(agents_client, "mongodb", FakeMongo())
    monkeypatch.setattr(claude_worker, "SDKClient", sdk_client)
    monkeypatch.setattr(agents_client, "_fail_task", fail_task)
    monkeypatch.setattr(
        claude_worker.credential_store,
        "get_stored_secret",
        lambda _name: None,
    )

    await agents_client._run_agent(
        task_id="task-123",
        owner_id="geoff",
        prompt="test",
        cwd="/tmp",
        max_turns=1,
        mcp_servers=[],
        system_prompt="system",
        worker_kind="claude_code",
    )

    fail_task.assert_awaited_once_with(
        "task-123",
        "geoff",
        "Anthropic API key is not configured.",
    )
    sdk_client.assert_not_called()
