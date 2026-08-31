from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.context import (
    RuntimeIdentity,
    ToolRuntimeContext,
    bind_tool_context,
    get_connection_id,
    get_owner_id,
    reset_tool_context,
)
from core.plugins.capabilities import CapabilityErrorDetail


class EmptyCursor:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def to_list(self, length):
        return []


def _bind_identity():
    return bind_tool_context(
        ToolRuntimeContext(
            identity=RuntimeIdentity(
                owner_id="geoff",
                connection_id="conn-browser",
                node_id="browser-node",
            ),
            timezone="Australia/Sydney",
        )
    )


def test_runtime_identity_exposes_owner_and_connection_ids():
    token = _bind_identity()
    try:
        assert get_owner_id() == "geoff"
        assert get_connection_id() == "conn-browser"
        assert "user_id" not in ToolRuntimeContext(
            identity=RuntimeIdentity(
                owner_id="geoff",
                connection_id="conn-browser",
            )
        ).as_dict()
    finally:
        reset_tool_context(token)


@pytest.mark.asyncio
async def test_reset_conversation_window_uses_owner_node_and_clears_transcript(monkeypatch):
    from api.websockets.connection import manager
    from api.websockets.types import WSMessageType
    from plugins.db import DbPlugin

    set_reset = AsyncMock()
    send_voice_response = AsyncMock()
    monkeypatch.setattr("plugins.db.mongodb.set_conversation_window_reset", set_reset)
    monkeypatch.setattr(manager, "send_voice_response", send_voice_response)

    token = _bind_identity()
    try:
        result = await DbPlugin().reset_conversation_window()
    finally:
        reset_tool_context(token)

    assert result == "Started a fresh conversation on this device. Earlier chat is still saved."
    set_reset.assert_awaited_once_with("geoff", "browser-node")
    send_voice_response.assert_awaited_once_with(
        "conn-browser",
        WSMessageType.CLEAR_TRANSCRIPT,
        {},
    )


@pytest.mark.asyncio
async def test_reset_conversation_window_requires_node(monkeypatch):
    from plugins.db import DbPlugin

    set_reset = AsyncMock()
    monkeypatch.setattr("plugins.db.mongodb.set_conversation_window_reset", set_reset)

    token = bind_tool_context(
        ToolRuntimeContext(
            identity=RuntimeIdentity(
                owner_id="geoff",
                connection_id="conn-browser",
            ),
            timezone="Australia/Sydney",
        )
    )
    try:
        result = await DbPlugin().reset_conversation_window()
    finally:
        reset_tool_context(token)

    assert isinstance(result, CapabilityErrorDetail)
    assert result.code == "no_node"
    set_reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_ui_action_binds_owner_scoped_tool_context(monkeypatch):
    from api.websockets import handlers
    from api.websockets.models import WSMessage
    from api.websockets.types import WSMessageType

    async def fake_process_ui_action(plugin_name, tool_name, args):
        assert plugin_name == "test"
        assert tool_name == "probe"
        assert args == {"x": 1}
        assert get_owner_id() == "geoff"
        assert get_connection_id() == "conn-browser"
        return "ok", None

    session = SimpleNamespace(
        owner_id="geoff",
        connection_id="conn-browser",
        presence=SimpleNamespace(
            node_id="browser-node",
            device_kind="browser",
            location=SimpleNamespace(model_dump=lambda: {"room_id": "bedroom"}),
        ),
        context={"timezone": "Australia/Sydney", "location": {"lat": -33.86}},
    )
    fake_manager = SimpleNamespace(
        get_session=MagicMock(return_value=session),
        send_message=AsyncMock(),
    )
    monkeypatch.setattr(handlers, "manager", fake_manager)
    monkeypatch.setattr(handlers, "process_ui_action", fake_process_ui_action)

    await handlers.handle_ui_action(
        "conn-browser",
        WSMessage(
            type=WSMessageType.UI_ACTION,
            data={"plugin": "test", "tool": "probe", "args": {"x": 1}},
        ),
    )

    fake_manager.send_message.assert_awaited_once()
    response = fake_manager.send_message.await_args.args[1]
    assert response.type == WSMessageType.STATUS
    assert response.data == {"result": "ok"}


@pytest.mark.asyncio
async def test_owner_scoped_plugins_query_mongo_owner_id(monkeypatch):
    from plugins import scheduler, setups

    token = _bind_identity()
    try:
        alert_find = MagicMock(return_value=EmptyCursor())
        monkeypatch.setattr(
            scheduler.mongodb,
            "db",
            SimpleNamespace(
                trigger_instances=SimpleNamespace(find=alert_find),
                trigger_rules=SimpleNamespace(find=MagicMock(return_value=EmptyCursor())),
            ),
        )
        await scheduler.SchedulerPlugin().get_alerts()
        assert alert_find.call_args.args[0]["owner_id"] == "geoff"

        find_setups = AsyncMock(return_value=[])
        monkeypatch.setattr(setups, "find_managed_setups", find_setups)
        await setups.SetupsPlugin().find(setup_type="automation")
        find_setups.assert_awaited_once_with(
            "geoff",
            query=None,
            status=None,
            setup_type="automation",
        )
    finally:
        reset_tool_context(token)
