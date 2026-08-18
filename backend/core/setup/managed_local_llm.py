"""JARV1S-managed local LLM (isolated Ollama sidecar on :11435)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import httpx
import psutil
from pydantic import BaseModel

from core.setup.llm_config import resolve_llm_config_sync
from core.setup.validation import validate_llm_credentials

logger = logging.getLogger(__name__)

ManagedStatus = Literal[
    "unsupported",
    "runtime_down",
    "absent",
    "downloading",
    "ready",
    "failed",
]

_DOWNLOAD_PAUSED_DETAIL = "Download paused. Resume anytime — progress is kept."


def _manifest_path() -> Path:
    """Resolve the single managed-runtime manifest.

    Packaged runtime ships a sibling copy; the dev repo reads the desktop source
    directly so there is exactly one checked-in manifest.
    """
    override = (os.environ.get("JARVIS_MANAGED_LLM_MANIFEST") or "").strip()
    if override:
        return Path(override)
    bundled = Path(__file__).with_name("managed_llm_manifest.json")
    if bundled.is_file():
        return bundled
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "apps" / "desktop" / "managed-llm" / "manifest.json"


@dataclass(frozen=True, slots=True)
class ManagedLlmManifest:
    base_url: str
    native_api_base: str
    model_id: str
    model_digest: str
    model_label: str
    model_license_url: str
    supersedes: tuple[str, ...]
    min_memory_bytes: int
    min_disk_bytes: int
    approx_download_bytes: int


@dataclass
class _PullState:
    status: ManagedStatus = "absent"
    detail: str = ""
    completed_bytes: int = 0
    total_bytes: int = 0
    task: asyncio.Task[None] | None = None
    last_error: str = ""


_pull = _PullState()
_pull_lock = asyncio.Lock()


class ManagedLlmStatus(BaseModel):
    status: ManagedStatus
    runtime_ready: bool = False
    model_id: str
    model_label: str
    model_installed: bool = False
    model_size_bytes: int = 0
    approx_download_bytes: int = 0
    min_memory_bytes: int = 0
    min_disk_bytes: int = 0
    supported: bool = True
    model_license_url: str = ""
    completed_bytes: int = 0
    total_bytes: int = 0
    detail: str | None = None
    active: bool = False


def _parse_supersedes(raw: Any, current_model_id: str) -> tuple[str, ...]:
    """Prior baselines to purge after a release bumps model_id. Declared in the manifest."""
    value = raw.get("supersedes") if isinstance(raw, dict) else None
    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        model_id = str(item or "").strip()
        if not model_id or model_id == current_model_id or model_id in seen:
            continue
        seen.add(model_id)
        out.append(model_id)
    return tuple(out)


@lru_cache(maxsize=1)
def load_manifest() -> ManagedLlmManifest:
    raw = json.loads(_manifest_path().read_text(encoding="utf-8"))
    host = str(raw["host"])
    port = int(raw["port"])
    if host != "127.0.0.1" or not 1 <= port <= 65535:
        raise ValueError("Managed Ollama must use a valid IPv4 loopback port.")
    native_api_base = f"http://{host}:{port}"
    model_id = str(raw["model_id"])
    return ManagedLlmManifest(
        base_url=f"{native_api_base}/v1",
        native_api_base=native_api_base,
        model_id=model_id,
        model_digest=str(raw.get("model_digest") or ""),
        model_label=str(raw.get("model_label") or model_id),
        model_license_url=str(raw.get("model_license_url") or ""),
        supersedes=_parse_supersedes(raw, model_id),
        min_memory_bytes=int(raw["min_memory_bytes"]),
        min_disk_bytes=int(raw["min_disk_bytes"]),
        approx_download_bytes=int(raw.get("approx_download_bytes") or raw["min_disk_bytes"]),
    )


def _managed_ready_marker_path() -> Path | None:
    data_dir = (os.environ.get("JARVIS_DATA_DIR") or "").strip()
    if not data_dir:
        return None
    return Path(data_dir) / "run" / "managed-ollama.ready"


def managed_runtime_env_ready() -> bool:
    """True only when the desktop supervisor marked the managed runtime as live."""
    if (os.environ.get("JARVIS_MANAGED_LLM_URL") or "").strip():
        return True
    marker = _managed_ready_marker_path()
    return bool(marker and marker.is_file())


def is_managed_active_config(*, provider: str, base_url: str, model: str) -> bool:
    manifest = load_manifest()
    return (
        provider == "ollama"
        and base_url.rstrip("/") == manifest.base_url
        and model == manifest.model_id
    )


def machine_preflight(manifest: ManagedLlmManifest | None = None) -> tuple[bool, str, int, int]:
    manifest = manifest or load_manifest()
    memory_bytes = int(psutil.virtual_memory().total)
    data_dir = (os.environ.get("JARVIS_DATA_DIR") or "").strip()
    disk_path = Path(data_dir) if data_dir else Path.home()
    while not disk_path.exists() and disk_path != disk_path.parent:
        disk_path = disk_path.parent
    free_disk_bytes = int(psutil.disk_usage(str(disk_path)).free)
    if memory_bytes < manifest.min_memory_bytes:
        return (
            False,
            (
                f"This Mac needs at least {manifest.min_memory_bytes // (1024**3)} GB of memory "
                f"for the on-device model."
            ),
            memory_bytes,
            free_disk_bytes,
        )
    if free_disk_bytes < manifest.min_disk_bytes:
        return (
            False,
            (
                f"Free at least {manifest.min_disk_bytes // (1024**3)} GB of disk space "
                "before installing the on-device model."
            ),
            memory_bytes,
            free_disk_bytes,
        )
    return True, "", memory_bytes, free_disk_bytes


async def _fetch_tags(native_api_base: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
            response = await client.get(f"{native_api_base}/api/tags")
            if response.status_code >= 400:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _model_entry(tags: dict[str, Any] | None, model_id: str) -> dict[str, Any] | None:
    if not tags:
        return None
    models = tags.get("models")
    if not isinstance(models, list):
        return None
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "")
        if name == model_id or name.startswith(f"{model_id}:"):
            return item
    return None


async def _delete_ollama_model(native_api_base: str, model_id: str) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=2.0)) as client:
        response = await client.request(
            "DELETE",
            f"{native_api_base}/api/delete",
            json={"model": model_id},
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise RuntimeError(
                f"Could not remove {model_id} ({response.status_code}): {response.text}"
            )


async def purge_superseded_models() -> None:
    """Delete prior managed baselines listed in the release manifest (best-effort)."""
    if not managed_runtime_env_ready():
        return
    manifest = load_manifest()
    if not manifest.supersedes:
        return
    tags = await _fetch_tags(manifest.native_api_base)
    if not tags:
        return
    for model_id in manifest.supersedes:
        if not _model_entry(tags, model_id):
            continue
        try:
            await _delete_ollama_model(manifest.native_api_base, model_id)
            logger.info("Purged superseded managed model %s", model_id)
        except Exception:
            logger.warning("Could not purge superseded managed model %s", model_id, exc_info=True)


async def get_managed_status() -> ManagedLlmStatus:
    manifest = load_manifest()
    pulling = bool(_pull.task and not _pull.task.done())
    if pulling:
        supported, support_detail = True, ""
    else:
        supported, support_detail, _, _ = machine_preflight(manifest)
    runtime_ready = managed_runtime_env_ready()
    tags = await _fetch_tags(manifest.native_api_base) if runtime_ready else None
    if runtime_ready and tags is None:
        runtime_ready = False
    entry = _model_entry(tags, manifest.model_id)
    installed = entry is not None
    installed_digest = str((entry or {}).get("digest") or "")
    model_size = int((entry or {}).get("size") or 0)

    active_cfg = resolve_llm_config_sync()
    active = is_managed_active_config(
        provider=active_cfg.provider,
        base_url=active_cfg.base_url,
        model=active_cfg.model,
    )

    if pulling:
        status: ManagedStatus = "downloading"
        detail = _pull.detail or "Downloading the on-device model…"
    elif not supported:
        status = "unsupported"
        detail = support_detail
    elif not runtime_ready:
        status = "runtime_down"
        detail = "The on-device model runtime is not running."
    elif installed:
        if manifest.model_digest and installed_digest != manifest.model_digest:
            status = "failed"
            detail = "Installed model digest does not match the JARV1S baseline."
        else:
            status = "ready"
            detail = "On-device model is ready."
    elif _pull.status == "failed" and _pull.last_error:
        status = "failed"
        detail = _pull.last_error
    else:
        status = "absent"
        detail = (
            _pull.detail
            if _pull.detail == _DOWNLOAD_PAUSED_DETAIL
            else "On-device model is not installed yet."
        )

    return ManagedLlmStatus(
        status=status,
        runtime_ready=runtime_ready,
        model_id=manifest.model_id,
        model_label=manifest.model_label,
        model_installed=installed,
        model_size_bytes=model_size,
        approx_download_bytes=manifest.approx_download_bytes,
        min_memory_bytes=manifest.min_memory_bytes,
        min_disk_bytes=manifest.min_disk_bytes,
        supported=supported,
        model_license_url=manifest.model_license_url,
        completed_bytes=_pull.completed_bytes,
        total_bytes=_pull.total_bytes or (manifest.approx_download_bytes if status == "downloading" else 0),
        detail=detail,
        active=active,
    )


def _mark_pull_paused() -> None:
    _pull.status = "absent"
    _pull.detail = _DOWNLOAD_PAUSED_DETAIL
    _pull.last_error = ""


async def _run_pull(manifest: ManagedLlmManifest) -> None:
    _pull.status = "downloading"
    _pull.detail = "Downloading the on-device model…"
    _pull.last_error = ""
    _pull.completed_bytes = 0
    _pull.total_bytes = manifest.approx_download_bytes
    try:
        await purge_superseded_models()
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            async with client.stream(
                "POST",
                f"{manifest.native_api_base}/api/pull",
                json={"model": manifest.model_id, "stream": True},
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(f"Model download failed ({response.status_code}): {body}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    status = str(event.get("status") or "")
                    completed = event.get("completed")
                    total = event.get("total")
                    if isinstance(completed, int):
                        _pull.completed_bytes = completed
                    if isinstance(total, int) and total > 0:
                        _pull.total_bytes = total
                    if status:
                        _pull.detail = status
                    if status == "success":
                        break
                    if "error" in event:
                        raise RuntimeError(str(event.get("error") or "Model download failed."))

        tags = await _fetch_tags(manifest.native_api_base)
        entry = _model_entry(tags, manifest.model_id)
        if not entry:
            raise RuntimeError("Model download finished but the model is not available.")
        digest = str(entry.get("digest") or "")
        if manifest.model_digest and digest != manifest.model_digest:
            raise RuntimeError("Downloaded model digest does not match the JARV1S baseline.")

        validation = await validate_llm_credentials(
            provider="ollama",
            api_key="",
            model=manifest.model_id,
            base_url=manifest.base_url,
        )
        if not validation.ok:
            raise RuntimeError(validation.message)

        _pull.status = "ready"
        _pull.detail = "On-device model is ready."
        _pull.completed_bytes = _pull.total_bytes or _pull.completed_bytes
    except asyncio.CancelledError:
        _mark_pull_paused()
        raise
    except Exception as exc:
        logger.exception("Managed local model install failed")
        _pull.status = "failed"
        _pull.last_error = str(exc)
        _pull.detail = str(exc)


async def start_managed_install() -> ManagedLlmStatus:
    manifest = load_manifest()
    supported, detail, *_ = machine_preflight(manifest)
    if not supported:
        _pull.status = "unsupported"
        _pull.last_error = detail
        return await get_managed_status()
    if not managed_runtime_env_ready():
        _pull.status = "runtime_down"
        _pull.last_error = "Start the on-device runtime before installing the model."
        return await get_managed_status()

    async with _pull_lock:
        if _pull.task and not _pull.task.done():
            return await get_managed_status()
        current = await get_managed_status()
        if current.status == "ready":
            return current
        _pull.task = asyncio.create_task(_run_pull(manifest))
    return await get_managed_status()


async def cancel_managed_install() -> ManagedLlmStatus:
    """Stop an in-flight pull. Partial blobs stay on disk for a resumable retry."""
    async with _pull_lock:
        task = _pull.task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            _pull.task = None
        elif _pull.status != "downloading":
            return await get_managed_status()
        _mark_pull_paused()
    return await get_managed_status()


async def unload_managed_model() -> None:
    """Release managed model memory without stopping the Ollama sidecar."""
    if not managed_runtime_env_ready():
        return
    manifest = load_manifest()
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=2.0)) as client:
        response = await client.post(
            f"{manifest.native_api_base}/api/generate",
            json={
                "model": manifest.model_id,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            },
        )
        response.raise_for_status()


async def remove_managed_model() -> ManagedLlmStatus:
    if _pull.task and not _pull.task.done():
        return await cancel_managed_install()

    manifest = load_manifest()
    active_cfg = resolve_llm_config_sync()
    if is_managed_active_config(
        provider=active_cfg.provider,
        base_url=active_cfg.base_url,
        model=active_cfg.model,
    ):
        raise ValueError("Switch to another model provider before removing the on-device model.")
    if not managed_runtime_env_ready():
        raise RuntimeError("The on-device model runtime is not running.")

    await _delete_ollama_model(manifest.native_api_base, manifest.model_id)
    await purge_superseded_models()

    _pull.status = "absent"
    _pull.detail = "On-device model removed."
    _pull.last_error = ""
    _pull.completed_bytes = 0
    _pull.total_bytes = 0
    return await get_managed_status()
