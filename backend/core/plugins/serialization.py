"""Shared formatting for LLM-facing tool outputs."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def tool_output_data(value: Any) -> Any:
    """Convert structured tool returns to JSON-safe data."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {
            str(key): tool_output_data(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [tool_output_data(item) for item in value]
    return value


def format_tool_output(value: Any) -> Any:
    """Return compact JSON for structured values; preserve plain strings."""
    if isinstance(value, str):
        return value
    if isinstance(value, (BaseModel, dict, list, tuple)):
        return json.dumps(tool_output_data(value), default=str, separators=(",", ":"))
    return value
