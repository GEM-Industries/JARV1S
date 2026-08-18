"""
Unified tool return envelope.

Mirrors MCP ``CallToolResult`` (content / structuredContent) and LangChain
``ToolMessage`` (content / artifact): the string is what the LLM sees; the
``ui`` list is side-channel widgets for the frontend, never shown to the model.
See docs/proposals/TOOL_RESULT_CONTRACT.md.
"""

from pydantic import BaseModel, Field

from core.plugins.types import UIEnvelope


class ToolResult(BaseModel):
    """Envelope for tool output.

    - ``content`` — the string the LLM sees.
    - ``ui``      — widgets pushed to the frontend. Never shown to the model.

    String helpers treat the envelope as its observation so plugin unit tests
    that call tools directly still match on the LLM-visible text.
    """

    content: str
    ui: list[UIEnvelope] = Field(default_factory=list)

    def __str__(self) -> str:
        return self.content

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and item in self.content

    def startswith(self, prefix: str, *args, **kwargs) -> bool:
        return self.content.startswith(prefix, *args, **kwargs)
