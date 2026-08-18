"""
SDK Abstraction Layer.

Single import point for the agent SDK. All plugin code imports from here —
never directly from the SDK package. Exposes INSTRUCTION_FILE and SKILLS_DIR
constants so JARV1S writes config files to the correct paths regardless of
which SDK is active.
"""

try:
    from claude_agent_sdk import ClaudeSDKClient as SDKClient  # type: ignore[import]
    from claude_agent_sdk import (  # type: ignore[import]
        ClaudeAgentOptions as AgentOptions,
        ResultMessage,
        AssistantMessage,
        SystemMessage,
    )
    from claude_agent_sdk import UserMessage  # type: ignore[import]
    from claude_agent_sdk.types import TextBlock, ToolResultBlock, ToolUseBlock  # type: ignore[import]

    INSTRUCTION_FILE = "CLAUDE.md"
    SKILLS_DIR = ".claude/skills"
    _ACTIVE_SDK = "claude"
except ImportError:
    from opencode_agent_sdk import (  # type: ignore[import]
        SDKClient,
        AgentOptions,
        ResultMessage,
        AssistantMessage,
        SystemMessage,
        UserMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )

    INSTRUCTION_FILE = "AGENTS.md"
    SKILLS_DIR = ".opencode/skills"
    _ACTIVE_SDK = "opencode"

__all__ = [
    "SDKClient",
    "AgentOptions",
    "ResultMessage",
    "AssistantMessage",
    "SystemMessage",
    "UserMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "INSTRUCTION_FILE",
    "SKILLS_DIR",
    "_ACTIVE_SDK",
]
