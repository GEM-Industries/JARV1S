"""Cursor local SDK adapter for mode=code.

Pinned to cursor-sdk. Import is lazy so a missing package disables Cursor
without blocking JARV1S startup or Claude work.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Optional

from core.credentials.store import credential_store
from plugins.agents.workers.base import (
    CodeWorkSpec,
    EventEmitter,
    WorkerEvent,
    WorkerResult,
    WorkerRunError,
    WorkerStartupError,
    mcp_servers_by_name,
)

logger = logging.getLogger(__name__)

_SETTING_SOURCES = ("user", "project", "plugins")
_FALLBACK_MODEL_IDS = ("grok-4.6", "auto-smart", "auto", "composer-2.5")
_cached_model: str | None = None


def _load_sdk() -> SimpleNamespace:
    try:
        from cursor_sdk import (
            AgentOptions,
            AsyncClient,
            CursorAgentError,
            LocalAgentOptions,
            SendOptions,
        )
    except ImportError as exc:
        raise WorkerStartupError(
            "Cursor SDK is not available in this install.",
            code="cursor_runtime_unavailable",
        ) from exc
    return SimpleNamespace(
        AgentOptions=AgentOptions,
        AsyncClient=AsyncClient,
        CursorAgentError=CursorAgentError,
        LocalAgentOptions=LocalAgentOptions,
        SendOptions=SendOptions,
    )


def _model_id(model: Any) -> str:
    return str(getattr(model, "id", None) or model or "")


def _resolve_model_from_catalog(models: list[Any]) -> str:
    ids = [_model_id(model) for model in models if _model_id(model)]
    for preferred in _FALLBACK_MODEL_IDS:
        if preferred in ids:
            return preferred
    if ids:
        return ids[0]
    raise WorkerStartupError(
        "No Cursor models are available for this account.",
        code="cursor_runtime_unavailable",
    )


def _catalog_models(api_key: str) -> list[Any]:
    """Cursor cloud catalog. Pass the stored key; env fallback is not used."""
    try:
        from cursor_sdk import Cursor
    except ImportError as exc:
        raise WorkerStartupError(
            "Cursor SDK is not available in this install.",
            code="cursor_runtime_unavailable",
        ) from exc
    models = Cursor.models.list(api_key=api_key)
    return list(getattr(models, "items", None) or models or [])


def _host_model(api_key: str) -> str:
    global _cached_model
    if _cached_model is not None:
        return _cached_model
    _cached_model = _resolve_model_from_catalog(_catalog_models(api_key))
    return _cached_model


def _best_effort_tool_input(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tool_label(name: str, inp: dict[str, Any]) -> str:
    for key in ("file_path", "path", "target_file", "command"):
        if inp.get(key):
            return f"{name}: {str(inp[key])[:80]}"
    return name


async def _cancel_run(run: Any) -> None:
    if run is None:
        return
    cancel = getattr(run, "cancel", None)
    if not callable(cancel):
        return
    try:
        result = cancel()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
        logger.debug("Cursor run cancel failed: %s", exc)


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    dumped = {
        field: getattr(usage, field, None)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "reasoning_tokens",
        )
        if getattr(usage, field, None) is not None
    }
    return dumped or None


class CursorLocalWorker:
    kind = "cursor_local"
    inspect_label = "Cursor"
    inspect_binaries = ("cursor",)
    inspect_via = "ide"
    inspect_app = "Cursor"

    async def execute(self, spec: CodeWorkSpec, emit: EventEmitter) -> WorkerResult:
        api_key = credential_store.get_stored_secret("CURSOR_API_KEY")
        if not api_key:
            raise WorkerStartupError(
                "Cursor API key is not configured.",
                code="background_credentials_unavailable",
            )

        sdk = _load_sdk()
        mcp_dict = mcp_servers_by_name(spec.mcp_servers)
        prompt = spec.prompt
        if spec.system_prompt and not spec.resume_session_id:
            prompt = f"{spec.system_prompt}\n\n{spec.prompt}"

        local = sdk.LocalAgentOptions(cwd=spec.cwd, setting_sources=list(_SETTING_SOURCES))
        run = None
        session_id = spec.resume_session_id
        external_run_id: Optional[str] = None
        text_parts: list[str] = []
        last_text = ""

        try:
            model = await asyncio.to_thread(_host_model, api_key)
            async with await sdk.AsyncClient.launch_bridge(
                workspace=spec.cwd,
                allow_api_key_env_fallback=False,
            ) as client:
                options_kwargs: dict[str, Any] = {
                    "model": model,
                    "api_key": api_key,
                    "local": local,
                }
                if mcp_dict:
                    options_kwargs["mcp_servers"] = mcp_dict
                if spec.title:
                    options_kwargs["name"] = spec.title[:80]
                options = sdk.AgentOptions(**options_kwargs)

                if spec.resume_session_id:
                    agent_cm = await client.resume_agent(spec.resume_session_id, options)
                else:
                    agent_cm = await client.create_agent(options)

                async with agent_cm as agent:
                    session_id = getattr(agent, "agent_id", None) or session_id
                    if session_id:
                        await emit(WorkerEvent(kind="external_handle", session_id=session_id))

                    send_options = sdk.SendOptions(mcp_servers=mcp_dict) if mcp_dict else None
                    run = await agent.send(prompt, send_options)
                    run_id = getattr(run, "id", None) or getattr(run, "run_id", None)
                    if run_id:
                        external_run_id = str(run_id)
                        await emit(
                            WorkerEvent(
                                kind="external_handle",
                                session_id=session_id,
                                external_run_id=external_run_id,
                            )
                        )

                    async for message in run.stream():
                        text, tool_event = _normalize_message(message)
                        if text:
                            text_parts.append(text)
                            last_text = text
                            await emit(WorkerEvent(kind="text", text=text))
                        if tool_event is not None:
                            await emit(tool_event)

                    result = await run.wait()
                    status = str(getattr(result, "status", "") or "")
                    result_text = str(getattr(result, "result", "") or "").strip()
                    if status == "error":
                        raise WorkerRunError(result_text or "Cursor run failed.")
                    if status == "cancelled":
                        raise asyncio.CancelledError()

                    full_result = (result_text or "".join(text_parts) or last_text).strip()
                    duration = getattr(result, "duration_ms", None)
                    return WorkerResult(
                        text=full_result,
                        summary=last_text or full_result[:500],
                        session_id=session_id,
                        external_run_id=external_run_id,
                        duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
                        usage=_usage_dict(getattr(result, "usage", None)),
                    )
        except asyncio.CancelledError:
            await _cancel_run(run)
            raise
        except WorkerStartupError:
            raise
        except WorkerRunError:
            raise
        except sdk.CursorAgentError as exc:
            message = str(exc) or "Cursor could not start this run."
            if run is not None:
                raise WorkerRunError(message) from exc
            raise WorkerStartupError(
                message,
                code="cursor_runtime_unavailable",
            ) from exc
        except Exception as exc:
            message = str(exc)
            if run is None and ("bridge" in message.lower() or "launch" in message.lower()):
                raise WorkerStartupError(
                    "Cursor local runtime is unavailable on this Mac.",
                    code="cursor_runtime_unavailable",
                ) from exc
            raise


def _normalize_message(message: Any) -> tuple[str | None, WorkerEvent | None]:
    kind = getattr(message, "type", None)
    if kind == "assistant":
        payload = getattr(message, "message", None)
        content = getattr(payload, "content", None) if payload is not None else None
        texts: list[str] = []
        tool_event = None
        for block in content or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = str(getattr(block, "text", "") or "")
                if text:
                    texts.append(text)
            elif block_type == "tool_use":
                inp = _best_effort_tool_input(getattr(block, "input", None))
                name = str(getattr(block, "name", None) or "tool")
                tool_event = WorkerEvent(
                    kind="tool_start",
                    tool=_tool_label(name, inp),
                    tool_name=name,
                    tool_input=inp,
                    tool_use_id=str(getattr(block, "id", None) or ""),
                )
        return ("".join(texts) or None, tool_event)

    if kind == "tool_call":
        name = str(getattr(message, "name", None) or "tool")
        inp = _best_effort_tool_input(getattr(message, "args", None))
        status = str(getattr(message, "status", None) or "")
        call_id = str(getattr(message, "call_id", None) or "")
        if status in {"completed", "error"}:
            return (
                None,
                WorkerEvent(
                    kind="tool_result",
                    tool=_tool_label(name, inp),
                    tool_name=name,
                    tool_input=inp,
                    tool_use_id=call_id,
                    result_content=getattr(message, "result", None),
                    is_error=status == "error",
                ),
            )
        return (
            None,
            WorkerEvent(
                kind="tool_start",
                tool=_tool_label(name, inp),
                tool_name=name,
                tool_input=inp,
                tool_use_id=call_id,
            ),
        )

    return None, None


def probe_cursor_account(api_key: str) -> None:
    """Sync account probe used by credential validation. Raises on auth failure."""
    global _cached_model
    _cached_model = None
    _host_model(api_key)
