"""
Database & Persistence Plugin for Jarvis.
Module-level store helpers are an internal API for other plugins.
The LLM-facing tool closes the working conversation window without deleting history.
"""
from typing import Dict, Any, TypeVar
import logging
from pydantic import BaseModel
from services.database.mongodb import mongodb
from core.context import get_connection_id, get_node_id, get_owner_id
from core.decorators import tool
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.types import JarvisPlugin, PluginMetadata

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Module-level functions for other plugins to import directly
async def store_tool_data(tool_name: str, data: Dict[str, Any]) -> None:
    """
    Save JSON data for a specific tool.
    Args:
        tool_name: "weather", "spotify"
        data: {"key": "value"}
    """
    await mongodb.store_tool_data(get_owner_id(), tool_name, data)

async def get_tool_data(tool_name: str) -> Dict[str, Any]:
    """
    Retrieve JSON data for a specific tool.
    Args:
        tool_name: "weather"
    """
    return await mongodb.get_tool_data(get_owner_id(), tool_name)

async def load_models(tool_name: str, model: type[T], *, key: str = "items") -> list[T]:
    """Load a list of Pydantic documents from a user-scoped tool_data doc.

    Multiple callers can share the same `tool_name` by using different `key=`
    values (e.g. `profile` stores facts under `key="facts"`). Storage remains
    the kv-under-one-doc shape; concurrency is last-write-wins.
    """
    data = await get_tool_data(tool_name)
    return [model(**d) for d in data.get(key, [])]

async def save_models(tool_name: str, items: list[BaseModel], *, key: str = "items") -> None:
    """Replace the list of Pydantic documents under `key` in a user-scoped tool_data doc.

    Note: `store_tool_data` overwrites the whole document, so sharing one
    `tool_name` across multiple `key=` values requires loading-and-merging
    externally if both keys must coexist in the same write path.
    """
    await store_tool_data(tool_name, {key: [i.model_dump(mode="json") for i in items]})

async def delete_tool_data(tool_name: str) -> None:
    """Clear all data for a specific tool."""
    await mongodb.delete_tool_data(get_owner_id(), tool_name)


class DbPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="db",
        version="1.0.0",
        description="Start a fresh working conversation without deleting stored history.",
        hidden=True,
    )

    @tool
    async def reset_conversation_window(self) -> str | CapabilityErrorDetail:
        """
        Start a fresh working conversation on this device. Does not delete stored history.
        Use when the user asks to start over, forget what you were just talking about, or clear/reset the current chat.
        Older turns stay saved and searchable with recall().
        """
        owner_id = get_owner_id()
        node_id = get_node_id()
        if not node_id:
            return CapabilityErrorDetail(
                code="no_node",
                message="This turn has no device to reset. Try again from a room speaker, desktop, or browser.",
            )

        await mongodb.set_conversation_window_reset(owner_id, node_id)

        try:
            from api.websockets.connection import manager
            from api.websockets.types import WSMessageType
            await manager.send_voice_response(get_connection_id(), WSMessageType.CLEAR_TRANSCRIPT, {})
        except Exception as e:
            logger.warning("Could not notify frontend of conversation window reset: %s", e)

        return "Started a fresh conversation on this device. Earlier chat is still saved."
