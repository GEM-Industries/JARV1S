"""
Plugin Types and Interfaces for Jarvis AI Assistant.
"""

import inspect
from abc import ABC
from typing import Dict, Any, Optional, Callable, ClassVar
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field


class PluginContractError(TypeError):
    """Raised when a plugin subclass violates the JarvisPlugin contract."""


class PluginMetadata(BaseModel):
    """Typed, immutable plugin metadata.

    Unknown fields are rejected (``extra="forbid"``) so typos fail at import time.
    Frozen so a single instance can live at class scope as a ``ClassVar``.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str = "0.1.0"
    description: str = ""
    hidden: bool = False
    routable: bool = True
    composio_app: Optional[str] = None
    dependencies: list[str] = []
    utterances: list[str] = []
    capabilities: list[str] = []


class WidgetSize:
    """Semantic size constants for widgets."""
    MINI = "mini"
    SMALL = "small"
    WIDE = "wide"
    TALL = "tall"
    LARGE = "large"
    LARGE_WIDE = "large-wide"
    HERO = "hero"
    FULL = "full-width"


class WidgetLayout(BaseModel):
    """Layout metadata for the widget."""
    size: str = WidgetSize.SMALL
    priority: int = 10
    group: Optional[str] = None


class UIEnvelope(BaseModel):
    """
    Standard contract for Server-Driven UI.
    Plugins return this to request a specific widget render on the client.
    """
    widget_id: str
    component: str
    data: Dict[str, Any]
    layout: WidgetLayout = Field(default_factory=WidgetLayout)
    title: str = "Widget"
    expires_at: Optional[int] = None
    created_at: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    pinned: bool = False


class JarvisPlugin(ABC):
    """
    Base class for all Jarvis plugins that provide tools.
    Ensures a consistent interface for the PluginRegistry and LLM.

    Subclasses declare ``metadata`` as a class-level :class:`PluginMetadata`
    and decorate tool methods with ``@tool``; ``get_tools()`` discovers them
    automatically. Override ``get_tools()`` only when tools are not bound
    methods (e.g. module-level functions or dynamically-built tool maps).
    """

    metadata: ClassVar[PluginMetadata]

    def __init_subclass__(cls, register: bool = True, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if inspect.isabstract(cls) or not register:
            return
        meta = cls.__dict__.get("metadata")
        if not isinstance(meta, PluginMetadata):
            raise PluginContractError(
                f"{cls.__name__} must declare `metadata = PluginMetadata(...)` at class scope"
            )
        expected = cls.__module__.rsplit(".", 1)[-1]
        if meta.name != expected:
            raise PluginContractError(
                f"{cls.__name__}.metadata.name={meta.name!r} must match module name {expected!r}"
            )

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def description(self) -> str:
        return self.metadata.description

    def get_tools(self) -> Dict[str, Callable]:
        """Auto-discover ``@tool``-decorated methods. Override for non-method tools."""
        return {
            name: method
            for name, method in inspect.getmembers(self, inspect.ismethod)
            if getattr(method, "_tool_meta", None) is not None
        }

    async def initialize(self, config: Dict[str, Any] = None) -> None:
        """Optional setup hook called after plugin discovery."""
        pass

    async def register_integrations(self) -> None:
        """Register external service clients with the IntegrationManager."""
        pass

    async def shutdown(self) -> None:
        """Optional teardown hook called before a plugin is deregistered."""
        pass
