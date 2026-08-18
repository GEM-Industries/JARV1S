"""Tests for the unified ToolResult contract.

Covers: dispatcher auth sentinels, manifest schema elision, and ui_handler
outcome consumption.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.decorators import tool, get_tool_meta
from core.plugins.capabilities import (
    CapabilityCall,
    CapabilityErrorDetail,
    CapabilityOutcome,
    InvocationStatus,
)
from core.plugins.dispatcher import dispatcher
from core.plugins.result import ToolResult
from core.plugins.types import UIEnvelope, WidgetLayout


def _envelope(widget_id: str = "w-1") -> UIEnvelope:
    return UIEnvelope(
        widget_id=widget_id,
        component="TestWidget",
        data={"x": 1},
        layout=WidgetLayout(),
        title="t",
    )


@pytest.mark.asyncio
async def test_dispatcher_translates_runtime_auth_errors(monkeypatch):
    from core.plugins.registry import PluginRegistry
    from core.plugins.types import JarvisPlugin, PluginMetadata

    class _Plugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="authwrap")

        @tool(inject=["calendar"])
        async def run(self, calendar=None) -> str:
            raise RuntimeError("auth failed")

    registry = PluginRegistry()
    registry.plugins["authwrap"] = _Plugin()
    registry.rebuild_capabilities()
    monkeypatch.setattr(dispatcher, "_registry", registry)

    handler = AsyncMock(return_value=CapabilityErrorDetail(
        code="reauth_needed",
        message="calendar requires re-authorization. Call connect_integration to push a setup card.",
    ))
    with patch("core.plugins.dispatcher.handle_integration_auth_error", handler):
        outcome = await dispatcher.dispatch(
            CapabilityCall(capability="authwrap.run", arguments={}, call_id="tcall-3")
        )

    assert outcome.status == InvocationStatus.BLOCKED
    assert outcome.data is None
    assert outcome.error is not None
    assert outcome.error.code == "reauth_needed"


def test_tool_result_annotation_produces_empty_schema():
    """`-> ToolResult` must not leak {content, ui} into the manifest."""
    @tool
    async def run() -> ToolResult:
        """doc."""
        return ToolResult(content="", ui=[])

    meta = get_tool_meta(run)
    assert meta is not None
    assert meta["return_schema"] == {}


def test_list_model_return_type_produces_schema():
    """PEP-563 `-> list[Model]` on a class method must resolve at decoration time."""
    from plugins.automations import AutomationsPlugin

    meta = get_tool_meta(AutomationsPlugin.list_available_triggers)
    assert meta is not None
    schema = meta["return_schema"]
    assert schema.get("type") == "array"
    assert schema["items"]["$ref"] == "#/defs/TriggerInfo"
    assert "TriggerInfo" in schema["$defs"]


@pytest.mark.asyncio
async def test_process_ui_action_extracts_tool_result(monkeypatch):
    """process_ui_action returns (content, ui[0]) from the dispatcher outcome."""
    from core.plugins import ui_handler

    env = _envelope("xyz")
    monkeypatch.setattr(
        ui_handler.dispatcher,
        "dispatch",
        AsyncMock(return_value=CapabilityOutcome(
            call_id="tcall-1",
            capability="p.t",
            status=InvocationStatus.SUCCEEDED,
            data="done",
            ui_events=[env],
        )),
    )

    result, ui_update = await ui_handler.process_ui_action("p", "t", {})
    assert result == "done"
    assert ui_update is env


@pytest.mark.asyncio
async def test_process_ui_action_tool_result_with_empty_ui(monkeypatch):
    from core.plugins import ui_handler

    monkeypatch.setattr(
        ui_handler.dispatcher,
        "dispatch",
        AsyncMock(return_value=CapabilityOutcome(
            call_id="tcall-2",
            capability="p.t",
            status=InvocationStatus.SUCCEEDED,
            data="ack",
            ui_events=[],
        )),
    )

    result, ui_update = await ui_handler.process_ui_action("p", "t", {})
    assert result == "ack"
    assert ui_update is None
