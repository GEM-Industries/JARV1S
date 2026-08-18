"""Contract tests for capability dispatcher + registry catalog."""

from __future__ import annotations

import inspect

import pytest

from core.decorators import tool
from core.plugins.capabilities import (
    InvocationLedger,
    InvocationStatus,
    bind_invocation_ledger,
    reset_invocation_ledger,
)
from core.plugins.dispatcher import CapabilityError, dispatcher
from core.plugins.registry import PluginRegistry, build_capability_definition
from core.plugins.types import JarvisPlugin, PluginMetadata


class _EchoPlugin(JarvisPlugin, register=False):
    metadata = PluginMetadata(name="echo", description="echo tools")

    @tool
    async def say(self, message: str, shout: bool = False) -> str:
        """Echo a message."""
        return message.upper() if shout else message


@pytest.fixture
def echo_registry(monkeypatch):
    reg = PluginRegistry()
    plugin = _EchoPlugin()
    reg.plugins["echo"] = plugin
    reg.rebuild_capabilities()
    monkeypatch.setattr("core.plugins.registry.registry", reg)
    monkeypatch.setattr("core.plugins.dispatcher.dispatcher._registry", reg)
    yield reg


@pytest.mark.asyncio
async def test_manifest_catalog_and_invoke_agree(echo_registry):
    definition = echo_registry.get_capability("echo.say")
    assert definition is not None
    assert list(definition.visible_signature.parameters) == ["message", "shout"]
    assert definition.documentation.startswith("Echo a message")

    ledger = InvocationLedger()
    token = bind_invocation_ledger(ledger)
    try:
        result = await dispatcher.invoke("echo.say", message="hi")
    finally:
        reset_invocation_ledger(token)

    assert result == "hi"
    assert ledger.records[0].capability == "echo.say"
    assert ledger.records[0].status == InvocationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_dispatcher_rejects_unknown_and_invalid_args(echo_registry):
    with pytest.raises(CapabilityError, match="Unknown capability"):
        await dispatcher.invoke("echo.missing")

    with pytest.raises(CapabilityError, match="Correct signature"):
        await dispatcher.invoke("echo.say", unexpected="x")


@pytest.mark.asyncio
async def test_dispatcher_records_timeout_as_interrupted(echo_registry):
    class _SlowPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="slow", description="slow")

        @tool
        async def hang(self) -> str:
            raise TimeoutError("provider timed out")

    echo_registry.plugins["slow"] = _SlowPlugin()
    echo_registry.rebuild_capabilities()

    ledger = InvocationLedger()
    token = bind_invocation_ledger(ledger)
    try:
        with pytest.raises(TimeoutError, match="provider timed out"):
            await dispatcher.invoke("slow.hang")
    finally:
        reset_invocation_ledger(token)

    assert ledger.records[0].status == InvocationStatus.INTERRUPTED
    assert ledger.records[0].error_type == "TimeoutError"


@pytest.mark.asyncio
async def test_dispatcher_records_nested_invocations(echo_registry):
    class _NestedPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="nested", description="nested")

        @tool
        async def outer(self) -> str:
            return await dispatcher.invoke("nested.inner", message="x")

        @tool
        async def inner(self, message: str) -> str:
            return message

    plugin = _NestedPlugin()
    echo_registry.plugins["nested"] = plugin
    echo_registry.rebuild_capabilities()

    ledger = InvocationLedger()
    token = bind_invocation_ledger(ledger)
    try:
        result = await dispatcher.invoke("nested.outer")
    finally:
        reset_invocation_ledger(token)

    assert result == "x"
    assert [record.capability for record in ledger.records] == ["nested.outer", "nested.inner"]
    assert all(record.status == InvocationStatus.SUCCEEDED for record in ledger.records)


def test_build_definition_hides_injected_params():
    class _InjectPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="injectdemo")

        @tool(inject=["client"])
        async def run(self, city: str, client: object) -> str:
            return city

    plugin = _InjectPlugin()
    func = plugin.get_tools()["run"]
    definition = build_capability_definition(plugin, "run", func, enabled=True)
    assert list(definition.visible_signature.parameters) == ["city"]
    assert "client" not in definition.visible_signature.parameters
    assert definition.injected == ("client",)
    assert definition.provider_name == "injectdemo__run"
    assert "city" in definition.input_schema.get("properties", {})
    assert "client" not in definition.input_schema.get("properties", {})


def test_provider_name_round_trip_and_collision(echo_registry):
    definition = echo_registry.get_capability("echo.say")
    assert definition is not None
    resolved = echo_registry.resolve_provider_name(definition.provider_name)
    assert resolved is not None
    assert resolved.fqn == "echo.say"
    tools = echo_registry.provider_tools({"echo.say"})
    assert tools[0]["function"]["name"] == definition.provider_name
    assert tools[0]["function"]["parameters"]["properties"]["message"]["type"] == "string"


def test_mcp_input_schema_is_retained():
    class _McpPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="mcpdemo")
        _capability_source = "mcp"

        def get_tools(self):
            async def run(*, city: str) -> str:
                return city

            run._mcp_input_schema = {  # type: ignore[attr-defined]
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
            }
            run._tool_meta = {  # type: ignore[attr-defined]
                "inject": (),
                "signature": inspect.signature(run),
                "return_schema": {},
            }
            return {"run": run}

    plugin = _McpPlugin()
    func = plugin.get_tools()["run"]
    definition = build_capability_definition(plugin, "run", func, enabled=True)
    assert definition.input_schema["properties"]["city"]["description"] == "City name"
    assert definition.input_schema["required"] == ["city"]


@pytest.mark.asyncio
async def test_dispatch_validates_schema_and_returns_outcome(echo_registry):
    from core.plugins.capabilities import CapabilityCall

    ledger = InvocationLedger()
    token = bind_invocation_ledger(ledger)
    try:
        outcome = await dispatcher.dispatch(
            CapabilityCall(capability="echo.say", arguments={"message": "hi"}, call_id="tcall-1")
        )
        blocked = await dispatcher.dispatch(
            CapabilityCall(capability="echo.say", arguments={"unexpected": "x"}, call_id="tcall-2")
        )
    finally:
        reset_invocation_ledger(token)

    assert outcome.status == InvocationStatus.SUCCEEDED
    assert outcome.data == "hi"
    assert outcome.call_id == "tcall-1"
    assert blocked.status == InvocationStatus.NOT_EXECUTED
    assert blocked.error is not None
    assert ledger.records[0].status == InvocationStatus.SUCCEEDED
    assert ledger.records[1].status == InvocationStatus.NOT_EXECUTED


@pytest.mark.asyncio
async def test_dispatch_unwraps_tool_result_and_json_data(echo_registry):
    from core.plugins.capabilities import CapabilityCall
    from core.plugins.result import ToolResult
    from core.plugins.types import UIEnvelope, WidgetLayout

    env = UIEnvelope(widget_id="w-1", component="Test", data={"x": 1}, layout=WidgetLayout(), title="t")

    class _UiPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="uidemo")

        @tool
        async def show(self) -> ToolResult:
            return ToolResult(content="shown", ui=[env])

        @tool
        async def report(self) -> dict:
            return {"ok": True, "nested": {"drop": None, "keep": 1}}

    echo_registry.plugins["uidemo"] = _UiPlugin()
    echo_registry.rebuild_capabilities()

    shown = await dispatcher.dispatch(
        CapabilityCall(capability="uidemo.show", arguments={}, call_id="tcall-ui")
    )
    report = await dispatcher.dispatch(
        CapabilityCall(capability="uidemo.report", arguments={}, call_id="tcall-json")
    )

    assert shown.status == InvocationStatus.SUCCEEDED
    assert shown.data == "shown"
    assert shown.ui_events[0].widget_id == "w-1"
    assert report.data == {"ok": True, "nested": {"keep": 1}}


@pytest.mark.asyncio
async def test_dispatch_injection_and_typed_blocked(echo_registry, monkeypatch):
    from core.plugins.capabilities import CapabilityCall, CapabilityErrorDetail

    class _AuthPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="authdemo")

        @tool(inject=["calendar"])
        async def run(self, city: str, calendar=None) -> str:
            return f"{city}:{calendar}"

        @tool
        async def approve(self) -> CapabilityErrorDetail:
            return CapabilityErrorDetail(
                code="approval_needed",
                message="Approval needed: delete everything. The action has not executed yet.",
            )

    echo_registry.plugins["authdemo"] = _AuthPlugin()
    echo_registry.rebuild_capabilities()

    async def fake_get(name):
        assert name == "calendar"
        return "client"

    monkeypatch.setattr("core.plugins.dispatcher.integrations.get", fake_get)

    injected = await dispatcher.dispatch(
        CapabilityCall(capability="authdemo.run", arguments={"city": "Sydney"}, call_id="tcall-in")
    )
    blocked = await dispatcher.dispatch(
        CapabilityCall(capability="authdemo.approve", arguments={}, call_id="tcall-block")
    )

    assert injected.data == "Sydney:client"
    assert blocked.status == InvocationStatus.BLOCKED
    assert blocked.data is None
    assert blocked.error is not None
    assert blocked.error.code == "approval_needed"
    assert "has not executed yet" in blocked.observation()


@pytest.mark.asyncio
async def test_dispatch_typed_failure_and_blocked(echo_registry):
    from core.plugins.capabilities import CapabilityCall, CapabilityErrorDetail

    class _TypedPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="typedemo")

        @tool
        async def miss(self) -> CapabilityErrorDetail:
            return CapabilityErrorDetail(code="not_found", message="Task not found.")

        @tool
        async def approve(self) -> CapabilityErrorDetail:
            return CapabilityErrorDetail(
                code="approval_needed",
                message="Approval needed: delete everything. The action has not executed yet.",
            )

    echo_registry.plugins["typedemo"] = _TypedPlugin()
    echo_registry.rebuild_capabilities()

    missed = await dispatcher.dispatch(
        CapabilityCall(capability="typedemo.miss", arguments={}, call_id="tcall-miss")
    )
    blocked = await dispatcher.dispatch(
        CapabilityCall(capability="typedemo.approve", arguments={}, call_id="tcall-ok")
    )
    invoked = await dispatcher.invoke("typedemo.miss")

    assert missed.status == InvocationStatus.FAILED
    assert missed.data is None
    assert missed.error is not None
    assert missed.error.code == "not_found"
    assert missed.observation() == "Task not found."

    assert blocked.status == InvocationStatus.BLOCKED
    assert blocked.data is None
    assert blocked.error is not None
    assert blocked.error.code == "approval_needed"
    assert "has not executed yet" in blocked.observation()

    assert isinstance(invoked, CapabilityErrorDetail)
    assert invoked.code == "not_found"
    assert invoked.message == "Task not found."
    from core.plugins.capabilities import CapabilityCall

    class _BoomPlugin(JarvisPlugin, register=False):
        metadata = PluginMetadata(name="boom2")

        @tool
        async def explode(self) -> str:
            raise RuntimeError("provider down")

    echo_registry.plugins["boom2"] = _BoomPlugin()
    echo_registry.rebuild_capabilities()

    ledger = InvocationLedger()
    token = bind_invocation_ledger(ledger)
    try:
        outcome = await dispatcher.dispatch(
            CapabilityCall(capability="boom2.explode", arguments={}, call_id="tcall-fail")
        )
    finally:
        reset_invocation_ledger(token)

    assert outcome.status == InvocationStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.message == "provider down"
    assert ledger.records[0].status == InvocationStatus.FAILED
