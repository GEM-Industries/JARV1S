"""Host-level voice runtime configuration service."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Literal, cast

from pydantic import BaseModel

from core.credentials.store import credential_store
from core.voice.config import (
    LOCAL_VOICE_IDS,
    STTProvider,
    TTSProvider,
    UpdateVoiceRuntimeConfigRequest,
    VoiceRuntimeConfig,
    macos_supports_apple_speech,
    normalize_local_voice_id,
    resolve_voice_config,
    to_runtime_config,
    voice_config_store,
)
from core.voice.runtime import switchable_stt, switchable_tts
from core.voice.stt_service import AppleSpeechHelperClient
from core.voice.tts_service import LocalTTSHelperClient, build_tts_backend

logger = logging.getLogger(__name__)

_APPLE_SPEECH_PROBE_TIMEOUT_S = 3.0
_LOCAL_TTS_PROBE_TIMEOUT_S = 3.0
_VOICE_INPUT_STATUS_TTL_S = 15.0
_VOICE_OUTPUT_STATUS_TTL_S = 15.0
_voice_input_status_cache: dict[STTProvider, tuple[float, VoiceInputStatus]] = {}
_voice_output_status_cache: dict[TTSProvider, tuple[float, VoiceOutputStatus]] = {}

VoiceInputState = Literal[
    "ready",
    "needs_permission",
    "needs_assets",
    "unavailable",
    "missing_key",
    "unsupported",
]

VoiceOutputState = Literal[
    "ready",
    "unavailable",
    "missing_key",
    "needs_voice",
    "unsupported",
]


class VoiceInputStatus(BaseModel):
    provider: STTProvider
    ready: bool
    state: VoiceInputState
    detail: str | None = None


class VoiceOutputStatus(BaseModel):
    provider: TTSProvider
    ready: bool
    state: VoiceOutputState
    detail: str | None = None


class LocalVoicePreview(BaseModel):
    audio: str
    sample_rate: int


LOCAL_VOICE_PREVIEW_TEXT = "At your service, sir."


def _invalidate_voice_input_status_cache() -> None:
    _voice_input_status_cache.clear()


def _invalidate_voice_output_status_cache() -> None:
    _voice_output_status_cache.clear()


def _exception_detail(exc: Exception) -> str:
    return str(exc).strip() or type(exc).__name__


async def get_voice_config() -> VoiceRuntimeConfig:
    return to_runtime_config(await resolve_voice_config())


async def probe_apple_speech_status() -> dict:
    """Query the Apple Speech helper for structured readiness."""
    config = await resolve_voice_config()
    client = AppleSpeechHelperClient(url=config.apple_speech_url)
    try:
        async with asyncio.timeout(_APPLE_SPEECH_PROBE_TIMEOUT_S):
            return await client.status()
    except Exception as exc:
        return {
            "ready": False,
            "state": "unavailable",
            "detail": f"Apple Speech helper unreachable: {_exception_detail(exc)}",
        }


async def probe_local_tts_status() -> dict:
    """Query the local Kokoro helper for structured readiness."""
    config = await resolve_voice_config()
    if not (config.local_tts_url or "").strip():
        return {
            "ready": False,
            "state": "unavailable",
            "detail": "Local TTS helper is not running.",
        }
    client = LocalTTSHelperClient(url=config.local_tts_url)
    try:
        async with asyncio.timeout(_LOCAL_TTS_PROBE_TIMEOUT_S):
            return await client.status()
    except Exception as exc:
        return {
            "ready": False,
            "state": "unavailable",
            "detail": f"Local TTS helper unreachable: {_exception_detail(exc)}",
        }


def _status_from_helper_payload(status: dict) -> VoiceInputStatus:
    state_raw = str(status.get("state") or ("ready" if status.get("ready") else "unavailable"))
    state: VoiceInputState
    if state_raw in {
        "ready",
        "needs_permission",
        "needs_assets",
        "unavailable",
        "missing_key",
        "unsupported",
    }:
        state = cast(VoiceInputState, state_raw)
    else:
        state = "unavailable"
    detail = status.get("detail") or status.get("message")
    return VoiceInputStatus(
        provider="apple_speech",
        ready=bool(status.get("ready")),
        state=state,
        detail=str(detail) if detail else None,
    )


async def get_voice_input_status(
    *,
    provider: STTProvider | None = None,
    force: bool = False,
) -> VoiceInputStatus:
    now = asyncio.get_running_loop().time()
    config = await resolve_voice_config()
    selected_provider = provider or config.stt_provider
    cached = _voice_input_status_cache.get(selected_provider)
    if (
        not force
        and cached is not None
        and now - cached[0] < _VOICE_INPUT_STATUS_TTL_S
    ):
        return cached[1]

    if selected_provider == "cartesia":
        if credential_store.get_stored_secret("CARTESIA_API_KEY"):
            result = VoiceInputStatus(
                provider="cartesia",
                ready=True,
                state="ready",
                detail="Cartesia streaming STT configured",
            )
        else:
            result = VoiceInputStatus(
                provider="cartesia",
                ready=False,
                state="missing_key",
                detail="Store a Cartesia API key before using cloud voice input.",
            )
        _voice_input_status_cache[selected_provider] = (now, result)
        return result

    if not macos_supports_apple_speech():
        result = VoiceInputStatus(
            provider="apple_speech",
            ready=False,
            state="unsupported",
            detail="On-device Speech requires macOS 26 or later.",
        )
        _voice_input_status_cache[selected_provider] = (now, result)
        return result

    if not (config.apple_speech_url or "").strip():
        result = VoiceInputStatus(
            provider="apple_speech",
            ready=False,
            state="unavailable",
            detail="Apple Speech helper is not running.",
        )
        _voice_input_status_cache[selected_provider] = (now, result)
        return result

    result = _status_from_helper_payload(await probe_apple_speech_status())
    _voice_input_status_cache[selected_provider] = (now, result)
    return result


async def get_voice_output_status(
    *,
    provider: TTSProvider | None = None,
    force: bool = False,
) -> VoiceOutputStatus:
    now = asyncio.get_running_loop().time()
    config = await resolve_voice_config()
    selected_provider = provider or config.tts_provider
    cached = _voice_output_status_cache.get(selected_provider)
    if (
        not force
        and cached is not None
        and now - cached[0] < _VOICE_OUTPUT_STATUS_TTL_S
    ):
        return cached[1]

    if selected_provider == "off":
        result = VoiceOutputStatus(
            provider="off",
            ready=True,
            state="ready",
            detail="Text replies only",
        )
        _voice_output_status_cache[selected_provider] = (now, result)
        return result

    if selected_provider == "cartesia":
        if not credential_store.get_stored_secret("CARTESIA_API_KEY"):
            result = VoiceOutputStatus(
                provider="cartesia",
                ready=False,
                state="missing_key",
                detail="Store a Cartesia API key before enabling spoken replies.",
            )
        elif not config.cartesia_voice_id:
            result = VoiceOutputStatus(
                provider="cartesia",
                ready=False,
                state="needs_voice",
                detail="Clone or select a Cartesia voice in Voice settings.",
            )
        else:
            result = VoiceOutputStatus(
                provider="cartesia",
                ready=True,
                state="ready",
                detail="Cartesia TTS configured",
            )
        _voice_output_status_cache[selected_provider] = (now, result)
        return result

    status = await probe_local_tts_status()
    state_raw = str(status.get("state") or ("ready" if status.get("ready") else "unavailable"))
    state: VoiceOutputState
    if state_raw in {"ready", "unavailable", "missing_key", "needs_voice", "unsupported"}:
        state = cast(VoiceOutputState, state_raw)
    else:
        state = "unavailable"
    detail = status.get("detail") or status.get("message")
    result = VoiceOutputStatus(
        provider="local",
        ready=bool(status.get("ready")),
        state=state,
        detail=str(detail) if detail else None,
    )
    _voice_output_status_cache[selected_provider] = (now, result)
    return result


async def preview_local_voice() -> LocalVoicePreview:
    """Synthesize a short sample with the currently selected on-device voice."""
    config = await resolve_voice_config()
    if config.tts_provider != "local":
        raise ValueError("Select On this Mac spoken replies before previewing a voice.")
    chunks: list[bytes] = []
    try:
        async for chunk in switchable_tts.generate_audio_stream(LOCAL_VOICE_PREVIEW_TEXT):
            if chunk:
                chunks.append(chunk)
    except Exception as exc:
        raise ValueError(str(exc).strip() or "Could not generate a voice preview.") from exc
    if not chunks:
        raise ValueError("Could not generate a voice preview.")
    return LocalVoicePreview(
        audio=base64.b64encode(b"".join(chunks)).decode("ascii"),
        sample_rate=switchable_tts.sample_rate,
    )


async def prepare_voice_input() -> VoiceInputStatus:
    """Prepare the selected voice input provider (Apple Speech assets/permissions)."""
    config = await resolve_voice_config()
    if config.stt_provider == "cartesia":
        return await get_voice_input_status()

    if not macos_supports_apple_speech():
        return await get_voice_input_status()

    if not (config.apple_speech_url or "").strip():
        return await get_voice_input_status(provider="apple_speech", force=True)

    client = AppleSpeechHelperClient(url=config.apple_speech_url)
    _invalidate_voice_input_status_cache()
    try:
        # Matches helper prepare timeout — AssetInventory downloads can be large.
        async with asyncio.timeout(600.0):
            status = await client.prepare()
    except Exception as exc:
        logger.warning("Apple Speech prepare failed: %s", exc)
        result = VoiceInputStatus(
            provider="apple_speech",
            ready=False,
            state="unavailable",
            detail=f"Could not prepare on-device speech: {exc}",
        )
        _invalidate_voice_input_status_cache()
        return result

    result = _status_from_helper_payload(status)
    _voice_input_status_cache["apple_speech"] = (asyncio.get_running_loop().time(), result)
    return result


async def clone_cartesia_voice(
    *,
    clip: bytes,
    filename: str,
    content_type: str,
    name: str,
    language: str,
) -> VoiceRuntimeConfig:
    api_key = credential_store.get_stored_secret("CARTESIA_API_KEY")
    if not api_key:
        raise ValueError("Store a Cartesia API key before cloning a voice.")

    from cartesia import AsyncCartesia  # type: ignore[import-not-found]

    client = AsyncCartesia(api_key=api_key)
    try:
        voice = await client.voices.clone(
            clip=(filename, clip, content_type),
            language=language,
            name=name,
            extra_headers={"Cartesia-Version": "2026-03-01"},
        )
    finally:
        await client.close()

    return await update_voice_config(
        UpdateVoiceRuntimeConfigRequest(
            tts_provider="cartesia",
            cartesia_voice_id=voice.id,
        )
    )


async def update_voice_config(request: UpdateVoiceRuntimeConfigRequest) -> VoiceRuntimeConfig:
    current = await resolve_voice_config()
    stt_provider = request.stt_provider or current.stt_provider
    if stt_provider == "cartesia" and not credential_store.get_stored_secret("CARTESIA_API_KEY"):
        raise ValueError("Store a Cartesia API key before selecting Cartesia voice input.")
    if request.stt_provider == "apple_speech" and not macos_supports_apple_speech():
        raise ValueError("On-device Speech requires macOS 26 or later.")

    tts_provider = current.tts_provider
    if "tts_provider" in request.model_fields_set and request.tts_provider is not None:
        tts_provider = request.tts_provider

    cartesia_voice_id = current.cartesia_voice_id
    if "cartesia_voice_id" in request.model_fields_set:
        cartesia_voice_id = (request.cartesia_voice_id or "").strip() or None

    local_voice_id = current.local_voice_id
    if "local_voice_id" in request.model_fields_set and request.local_voice_id is not None:
        requested_local = request.local_voice_id.strip()
        if requested_local and requested_local not in LOCAL_VOICE_IDS:
            raise ValueError("Unknown on-device voice.")
        local_voice_id = normalize_local_voice_id(requested_local or None)

    if tts_provider == "cartesia":
        if not credential_store.get_stored_secret("CARTESIA_API_KEY"):
            raise ValueError("Store a Cartesia API key before enabling spoken replies.")
        if not cartesia_voice_id:
            raise ValueError("Select or clone a Cartesia voice before enabling spoken replies.")
    elif tts_provider == "local":
        if local_voice_id not in LOCAL_VOICE_IDS:
            raise ValueError("Unknown on-device voice.")
        local_status = await get_voice_output_status(provider="local", force=True)
        if not local_status.ready:
            raise ValueError(local_status.detail or "Local TTS helper is not ready.")

    candidate_config = current.__class__(
        stt_provider=stt_provider,
        tts_provider=tts_provider,
        cartesia_voice_id=cartesia_voice_id,
        local_voice_id=local_voice_id,
        source=current.source,
    )
    tts_changed = (
        tts_provider != current.tts_provider
        or cartesia_voice_id != current.cartesia_voice_id
        or local_voice_id != current.local_voice_id
    )
    candidate_backend = None
    if tts_changed:
        candidate_backend = build_tts_backend(candidate_config)
        if tts_provider != "off":
            warmed = await candidate_backend.initialize()
            if not warmed:
                await candidate_backend.close()
                raise ValueError("Could not initialize spoken replies for the selected provider.")

    await voice_config_store.save(
        stt_provider=stt_provider,
        tts_provider=tts_provider,
        cartesia_voice_id=cartesia_voice_id,
        local_voice_id=local_voice_id,
    )
    if stt_provider != current.stt_provider:
        _invalidate_voice_input_status_cache()
        await switchable_stt.refresh()
    if tts_changed:
        _invalidate_voice_output_status_cache()
        if candidate_backend is not None:
            await switchable_tts.promote(candidate_backend, candidate_config.tts_signature)
        else:
            await switchable_tts.refresh()
    return await get_voice_config()


async def ensure_voice_config_available() -> None:
    current = await resolve_voice_config()
    if current.stt_provider != "cartesia" or credential_store.get_stored_secret("CARTESIA_API_KEY"):
        return

    fallback = "apple_speech" if macos_supports_apple_speech() else "cartesia"
    if fallback == "cartesia":
        # No usable cloud key and no Apple Speech — leave config as-is.
        return

    await voice_config_store.save(
        stt_provider="apple_speech",
        tts_provider=current.tts_provider,
        cartesia_voice_id=current.cartesia_voice_id,
        local_voice_id=current.local_voice_id,
    )
    await switchable_stt.refresh()
