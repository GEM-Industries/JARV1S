"""Read-only discovery for local OpenAI-compatible LLM runtimes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from core.setup.models import LocalLlmRuntime

logger = logging.getLogger(__name__)

_DISCOVERY_TIMEOUT = httpx.Timeout(1.0, connect=0.5)

_LOCAL_TARGETS: tuple[dict[str, str], ...] = (
    {
        "runtime": "ollama",
        "label": "Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "probe_url": "http://127.0.0.1:11434/api/tags",
        "probe_kind": "ollama_tags",
    },
    {
        "runtime": "lmstudio",
        "label": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "probe_url": "http://127.0.0.1:1234/v1/models",
        "probe_kind": "openai_models",
    },
    {
        "runtime": "llamacpp",
        "label": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "probe_url": "http://127.0.0.1:8080/v1/models",
        "probe_kind": "openai_models",
    },
)


def _models_from_ollama_tags(payload: dict[str, Any]) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for item in models:
        if isinstance(item, dict):
            name = item.get("name") or item.get("model")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _models_from_openai_list(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                names.append(model_id.strip())
    return names


async def _probe_target(target: dict[str, str]) -> LocalLlmRuntime:
    runtime = target["runtime"]
    label = target["label"]
    base_url = target["base_url"]
    probe_url = target["probe_url"]
    probe_kind = target["probe_kind"]

    try:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT) as client:
            response = await client.get(probe_url)
            if response.status_code >= 400:
                return LocalLlmRuntime(
                    runtime=runtime,
                    label=label,
                    base_url=base_url,
                    reachable=False,
                    models=[],
                    detail=f"Runtime not reachable at {base_url}",
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Unexpected discovery response")
            if probe_kind == "ollama_tags":
                models = _models_from_ollama_tags(payload)
            else:
                models = _models_from_openai_list(payload)
            detail = None
            if not models:
                detail = "Server reachable but no models were listed."
            return LocalLlmRuntime(
                runtime=runtime,
                label=label,
                base_url=base_url,
                reachable=True,
                models=models,
                detail=detail,
            )
    except Exception as exc:
        logger.debug("Local LLM discovery miss for %s: %s", runtime, exc)
        return LocalLlmRuntime(
            runtime=runtime,
            label=label,
            base_url=base_url,
            reachable=False,
            models=[],
            detail=f"Not detected — install/start {label} to use a local model.",
        )


async def discover_local_llm_runtimes() -> list[LocalLlmRuntime]:
    results = await asyncio.gather(*(_probe_target(target) for target in _LOCAL_TARGETS))
    return sorted(results, key=lambda item: (not item.reachable, item.label.lower()))
