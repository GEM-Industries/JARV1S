"""
Database & Persistence Plugin for Jarvis.
Provides tools for storing and retrieving tool-specific data and managing conversation history.
"""
from typing import Dict, Any, Optional, Callable, TypeVar
import logging
from pydantic import BaseModel
from services.database.mongodb import mongodb
from core.context import get_connection_id, get_owner_id
from core.decorators import tool
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

@tool
async def clear_conversation_history() -> str:
    """
    Permanently delete all conversation history for the current user. Does not affect tool data.
    Use when the user asks to clear, wipe, delete, or reset the conversation/chat history.
    """
    owner_id = get_owner_id()
    logger.info(f"Clearing conversation history for owner {owner_id}")
    
    deleted_count = await mongodb.clear_conversation_history(owner_id)

    # Notify the frontend to wipe its transcript immediately
    try:
        from api.websockets.connection import manager
        from api.websockets.types import WSMessageType
        await manager.send_voice_response(get_connection_id(), WSMessageType.CLEAR_TRANSCRIPT, {})
    except Exception as e:
        logger.warning(f"Could not notify frontend of history clear: {e}")

    return f"Deleted {deleted_count} messages."

class DbPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="db",
        version="1.0.0",
        description="Database and persistence utilities.",
        hidden=True,
    )

    # Tools are module-level functions (not bound methods), so auto-discovery
    # doesn't apply — keep the explicit override.
    def get_tools(self) -> Dict[str, Callable]:
        return {
            "store_tool_data": store_tool_data,
            "get_tool_data": get_tool_data,
            "delete_tool_data": delete_tool_data,
            "clear_conversation_history": clear_conversation_history,
        }
