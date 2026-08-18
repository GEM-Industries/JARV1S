from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from core.context import RuntimeIdentity, ToolRuntimeContext, bind_tool_context, reset_tool_context


@pytest.fixture
def tool_context() -> Iterator[Any]:
    """Bind a default tool context and provide a factory for custom contexts."""

    @contextmanager
    def bind(
        *,
        owner_id: str = "owner-1",
        connection_id: str = "conn-1",
        timezone: str = "UTC",
        node_id: str | None = None,
        speaker_id: str | None = None,
        location_ref: dict[str, Any] | None = None,
        location: dict[str, float] | None = None,
        extras: dict[str, Any] | None = None,
    ):
        token = bind_tool_context(
            ToolRuntimeContext(
                identity=RuntimeIdentity(
                    owner_id=owner_id,
                    connection_id=connection_id,
                    node_id=node_id,
                    speaker_id=speaker_id,
                    location_ref=location_ref,
                ),
                timezone=timezone,
                location=location,
                extras=extras or {},
            )
        )
        try:
            yield
        finally:
            reset_tool_context(token)

    with bind():
        yield bind


@pytest.fixture
def invoke_tool():
    """Call plugin tool logic directly, bypassing the @tool wrapper."""

    async def invoke(plugin: Any, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        method = getattr(plugin, tool_name)
        wrapped = getattr(method, "__wrapped__", None)
        if wrapped is not None:
            return await wrapped(plugin, *args, **kwargs)
        return await method(*args, **kwargs)

    return invoke


@pytest.fixture
def invoke_capability():
    """Invoke through the capability dispatcher (harness boundary tests)."""

    async def invoke(fqn: str, *args: Any, **kwargs: Any) -> Any:
        from core.plugins.capabilities import (
            InvocationLedger,
            bind_invocation_ledger,
            reset_invocation_ledger,
            reset_invocation_source,
            set_invocation_source,
        )
        from core.plugins.dispatcher import dispatcher

        ledger = InvocationLedger()
        ledger_token = bind_invocation_ledger(ledger)
        source_token = set_invocation_source("test")
        try:
            result = await dispatcher.invoke(fqn, *args, **kwargs)
            return result, ledger
        finally:
            reset_invocation_source(source_token)
            reset_invocation_ledger(ledger_token)

    return invoke


class FakeToolDataStore:
    """In-memory substitute for plugins.db tool-data helpers."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    async def store_tool_data(self, tool_name: str, data: dict[str, Any]) -> None:
        self.data[tool_name] = data

    async def get_tool_data(self, tool_name: str) -> dict[str, Any]:
        return self.data.get(tool_name, {})

    def install(self, monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
        monkeypatch.setattr(module, "store_tool_data", self.store_tool_data)
        monkeypatch.setattr(module, "get_tool_data", self.get_tool_data)


@pytest.fixture
def fake_tool_data_store(monkeypatch: pytest.MonkeyPatch) -> FakeToolDataStore:
    """Patch plugins.db tool-data helpers with an in-memory store."""
    from plugins import db

    store = FakeToolDataStore()
    store.install(monkeypatch, db)
    return store
