#!/usr/bin/env python3
"""Probe background-agent transport: enable thinking, visibility, CodeAct loop safety."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from core.config import settings
from core.credentials.store import credential_store
from core.llm.providers import get_llm_provider
from core.llm.service import LLMService


def classify_transport_world(base_url: str, *, visibility_ok: bool, enable_ok: bool) -> str:
    url = (base_url or "").lower()
    if "api.anthropic.com" in url:
        return "world_3_native_anthropic" if visibility_ok else "world_1_anthropic_openai_shim"
    if visibility_ok and enable_ok:
        return "world_2_gateway"
    if visibility_ok:
        return "world_3_native_or_gateway"
    return "unknown"


async def _stream_probe(service: LLMService, *, effort: str | None, prompt: str) -> dict:
    started = time.perf_counter()
    reasoning_chars = 0
    content_chars = 0

    async for chunk in service.chat_stream(
        user_message=prompt,
        reasoning_effort=effort,
        temperature=0.2,
    ):
        if chunk.kind == "reasoning":
            reasoning_chars += len(chunk.text)
        else:
            content_chars += len(chunk.text)

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "effort": effort,
        "elapsed_ms": round(elapsed_ms, 1),
        "reasoning_chars": reasoning_chars,
        "content_chars": content_chars,
    }


async def _codeact_loop_probe(service: LLMService) -> dict:
    history = [
        {
            "role": "assistant",
            "content": 'Let me check.\n<tool_call>\nprint("probe")\n</tool_call>',
        },
        {"role": "user", "content": "<tool_result>\nprobe ok\n</tool_result>"},
    ]
    started = time.perf_counter()
    error = None
    content = ""
    reasoning_chars = 0
    try:
        async for chunk in service.chat_stream(
            user_message="",
            conversation_history=history,
            reasoning_effort="high",
            temperature=0.2,
        ):
            if chunk.kind == "reasoning":
                reasoning_chars += len(chunk.text)
            else:
                content += chunk.text
    except Exception as exc:
        error = str(exc)

    return {
        "ok": error is None,
        "error": error,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "content_chars": len(content),
        "reasoning_chars": reasoning_chars,
    }


async def run_probe() -> dict:
    api_key = credential_store.get_stored_secret("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not configured"}

    provider = get_llm_provider("anthropic")
    service = LLMService(
        api_key=api_key,
        base_url=provider.base_url,
        model=settings.BACKGROUND_AGENT_MODEL,
        provider_name="anthropic",
        request_timeout_s=settings.BACKGROUND_AGENT_HTTP_TIMEOUT_S,
    )
    await service.initialize()
    if not service.is_initialized:
        return {"error": "background agent LLM client failed to initialize"}

    prompt = "Think briefly, then answer in one sentence: what is 17 + 25?"
    baseline = await _stream_probe(service, effort=None, prompt=prompt)
    effort_on = await _stream_probe(service, effort="high", prompt=prompt)
    codeact = await _codeact_loop_probe(service)

    enable_ok = effort_on["elapsed_ms"] > baseline["elapsed_ms"] + 50 or effort_on["reasoning_chars"] > 0
    visibility_ok = effort_on["reasoning_chars"] > 0
    world = classify_transport_world(
        provider.base_url,
        visibility_ok=visibility_ok,
        enable_ok=enable_ok,
    )

    if world.startswith("world_1"):
        recommended = "native_messages_api"
    elif world.startswith("world_3"):
        recommended = "native_messages_api"
    elif visibility_ok:
        recommended = "openai_compat_gateway"
    else:
        recommended = "investigate"

    reasoning_field = None
    if visibility_ok:
        reasoning_field = (
            "thinking_delta"
            if "api.anthropic.com" in provider.base_url
            else "reasoning_content"
        )

    return {
        "base_url": provider.base_url,
        "model": settings.BACKGROUND_AGENT_MODEL,
        "baseline": baseline,
        "effort_high": effort_on,
        "codeact_loop": codeact,
        "enable_ok": enable_ok,
        "visibility_ok": visibility_ok,
        "codeact_loop_ok": codeact["ok"],
        "reasoning_field": reasoning_field,
        "transport_world": world,
        "recommended_transport": recommended,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe background-agent transport capabilities")
    parser.parse_args()
    result = asyncio.run(run_probe())
    print(json.dumps(result, indent=2))
    if result.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
