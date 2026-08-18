"""
Tool decorator for Jarvis.

Attaches metadata (inject list, signature, and return schema) to
tool functions so the catalog, router, and LLM adapters can describe them.
Runtime injection, auth translation, and ToolResult unwrapping live in the
capability dispatcher.
"""

import inspect
import logging
import sys
from typing import Any, Callable, get_type_hints

from pydantic import TypeAdapter

from core.plugins.result import ToolResult

logger = logging.getLogger(__name__)


# Primitives produce noisy "type: integer" schemas with no useful information
# for the LLM — the visible signature already carries the type.
# ``ToolResult`` is unwrapped to its ``content`` string by the dispatcher,
# so its schema must not leak into the catalog either.
_SKIP_RETURN_TYPES: frozenset[Any] = frozenset({None, type(None), str, int, float, bool, bytes, Any, ToolResult})

# Many tools share return annotations (e.g. several calendar tools return
# CalendarEvent). Cache json_schema output by annotation to avoid rebuilding.
_SCHEMA_CACHE: dict[Any, dict[str, Any]] = {}


def _resolved_return_type(fn: Callable) -> Any:
    """Resolve PEP-563 string annotations to concrete types for schema extraction."""
    annotation = inspect.signature(fn).return_annotation
    if annotation is inspect.Signature.empty or not isinstance(annotation, str):
        return annotation

    module = sys.modules.get(fn.__module__)
    globalns = getattr(module, "__dict__", {})
    try:
        return get_type_hints(fn, globalns=globalns, localns=globalns).get("return", annotation)
    except Exception:
        return annotation


def _extract_return_schema(fn: Callable) -> dict[str, Any]:
    """JSON schema for a tool's return type, or {} when unhelpful.

    TypeAdapter handles Pydantic models, unions, generics, and dataclasses via
    one path. Errors are non-fatal — the catalog just omits the schema.
    """
    annotation = _resolved_return_type(fn)
    if annotation is inspect.Signature.empty or annotation in _SKIP_RETURN_TYPES:
        return {}

    try:
        cached = _SCHEMA_CACHE.get(annotation)
    except TypeError:
        cached = None  # unhashable annotation; skip cache
    if cached:
        return cached

    try:
        schema = TypeAdapter(annotation).json_schema(ref_template="#/defs/{model}")
    except Exception as exc:
        logger.debug("return-schema extraction failed for %s: %s", fn.__qualname__, exc)
        return {}

    try:
        _SCHEMA_CACHE[annotation] = schema
    except TypeError:
        pass
    return schema


def tool(
    func: Callable | None = None,
    *,
    inject: list[str] | None = None,
):
    """
    Decorator to attach metadata to a tool function.

    Args:
        inject: Parameter names to hide from the LLM and auto-inject at runtime.

    Example:
        @tool(inject=["client"])
        async def get_weather(self, city: str, client: WeatherClient):
            ...
    """
    inject_params: tuple[str, ...] = tuple(inject or ())

    def decorator(fn: Callable) -> Callable:
        sig = inspect.signature(fn)
        fn._tool_meta = {  # type: ignore[attr-defined]
            "inject": inject_params,
            "signature": sig,
            "return_schema": _extract_return_schema(fn),
        }
        return fn

    return decorator(func) if func is not None else decorator


def get_tool_meta(func: Callable) -> dict[str, Any] | None:
    """Return the `_tool_meta` dict attached by `@tool`, or None."""
    return getattr(func, "_tool_meta", None)
