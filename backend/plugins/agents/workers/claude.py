"""Claude Agent SDK adapter for mode=code."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any, Optional

from core.agent.sdk import (
    SDKClient,
    AgentOptions,
    ResultMessage,
    AssistantMessage,
    UserMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from core.config import settings
from core.credentials.store import credential_store
from plugins.agents.workers.base import (
    CodeWorkSpec,
    EventEmitter,
    WorkerEvent,
    WorkerResult,
    WorkerStartupError,
    mcp_servers_by_name,
)

logger = logging.getLogger(__name__)

SDK_CLEANUP_TIMEOUT = 5.0


def _extract_pid(client: SDKClient) -> int | None:
    try:
        proc = getattr(client, "_transport", None)
        proc = getattr(proc, "_process", None)
        return proc.pid if proc is not None else None
    except Exception:
        return None


async def _graceful_kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        logger.debug("Sent SIGTERM to subprocess pid=%d", pid)
    except ProcessLookupError:
        return
    except Exception as exc:
        logger.debug("SIGTERM pid=%d failed: %s", pid, exc)
        return

    await asyncio.sleep(1)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return

    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning("SIGKILL required for subprocess pid=%d (did not exit after SIGTERM)", pid)
    except ProcessLookupError:
        pass
    except Exception as exc:
        logger.debug("SIGKILL pid=%d failed: %s", pid, exc)


def _tool_detail(name: str, inp: dict[str, Any]) -> str:
    if name == "Bash" and inp.get("command"):
        return f"Bash: {str(inp['command'])[:80]}"
    if name in ("Read", "Write", "Edit", "MultiEdit") and inp.get("file_path"):
        return f"{name}: {inp['file_path']}"
    return name


class ClaudeCodeWorker:
    kind = "claude_code"
    inspect_label = "Claude Code"
    inspect_binaries = ("claude",)
    inspect_via = "terminal"

    async def execute(self, spec: CodeWorkSpec, emit: EventEmitter) -> WorkerResult:
        anthropic_key = credential_store.get_stored_secret("ANTHROPIC_API_KEY")
        if not anthropic_key:
            raise WorkerStartupError(
                "Anthropic API key is not configured.",
                code="background_credentials_unavailable",
            )

        model = settings.BACKGROUND_AGENT_MODEL
        if "/" in model:
            model = model.split("/", 1)[1]

        options = AgentOptions(
            model=model,
            effort=settings.LLM_HEADLESS_REASONING_EFFORT,
            max_turns=spec.max_turns,
            cwd=spec.cwd,
            permission_mode="bypassPermissions",
            system_prompt=spec.system_prompt,
            mcp_servers=mcp_servers_by_name(spec.mcp_servers),
            env={"ANTHROPIC_API_KEY": anthropic_key},
            resume=spec.resume_session_id or None,
            setting_sources=["user", "project"],
            max_budget_usd=spec.max_budget_usd,
        )

        client = SDKClient(options=options)
        text_parts: list[str] = []
        last_text = ""
        session_id = spec.resume_session_id
        cost_usd: Optional[float] = None
        result_text: Optional[str] = None
        duration_ms: Optional[int] = None
        num_turns: Optional[int] = None
        usage: Optional[dict[str, Any]] = None
        child_pid: int | None = None

        try:
            await client.connect()
            child_pid = _extract_pid(client)
            if child_pid:
                from plugins.agents import register_child_pid

                register_child_pid(child_pid)
                logger.debug("Claude code worker running as pid=%d", child_pid)

            await client.query(spec.prompt)

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    if not session_id and msg.session_id:
                        session_id = msg.session_id
                        await emit(WorkerEvent(kind="external_handle", session_id=session_id))

                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            text_parts.append(block.text)
                            last_text = block.text
                            await emit(WorkerEvent(kind="text", text=block.text))
                        elif isinstance(block, ToolUseBlock):
                            inp = block.input or {}
                            await emit(
                                WorkerEvent(
                                    kind="tool_start",
                                    tool=_tool_detail(block.name, inp),
                                    tool_name=block.name,
                                    tool_input=inp if isinstance(inp, dict) else {},
                                    tool_use_id=str(block.id),
                                )
                            )

                elif isinstance(msg, UserMessage):
                    for block in msg.content:
                        if not isinstance(block, ToolResultBlock):
                            continue
                        await emit(
                            WorkerEvent(
                                kind="tool_result",
                                tool_use_id=str(block.tool_use_id),
                                result_content=block.content,
                                is_error=bool(block.is_error),
                            )
                        )
                    if msg.tool_use_result is not None:
                        await emit(
                            WorkerEvent(
                                kind="tool_result",
                                result_content=msg.tool_use_result,
                            )
                        )

                elif isinstance(msg, ResultMessage):
                    session_id = msg.session_id or session_id
                    cost_usd = msg.total_cost_usd if msg.total_cost_usd > 0 else None
                    if msg.result:
                        result_text = msg.result
                    duration_ms = msg.duration_ms
                    num_turns = msg.num_turns
                    if msg.usage is not None:
                        usage = (
                            dict(msg.usage)
                            if isinstance(msg.usage, dict)
                            else {"raw": msg.usage}
                        )

            full_result = (result_text or "".join(text_parts) or last_text).strip()
            return WorkerResult(
                text=full_result,
                summary=last_text or full_result[:500],
                session_id=session_id,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                num_turns=num_turns,
                usage=usage,
            )
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=SDK_CLEANUP_TIMEOUT)
            except Exception:
                if child_pid:
                    await _graceful_kill_pid(child_pid)
            finally:
                if child_pid:
                    from plugins.agents import unregister_child_pid

                    unregister_child_pid(child_pid)
