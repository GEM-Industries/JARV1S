"""Voice runtime configuration and owner speaker-profile API."""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from api.deps.device_auth import require_device, require_owner_id
from core.voice import service as voice_service
from core.voice.config import STTProvider, TTSProvider, UpdateVoiceRuntimeConfigRequest, VoiceRuntimeConfig
from core.voice.service import LocalVoicePreview, VoiceInputStatus, VoiceOutputStatus
from core.voice.speaker_profile import (
    MAX_CLIP_BYTES,
    REQUIRED_CLIP_COUNT,
    SpeakerProfileError,
    SpeakerProfileStatus,
    delete_profile,
    get_profile_status,
    write_profile,
)
from core.voice.wakeword.check import (
    MAX_WAKE_CHECK_BYTES,
    WakeCheckError,
    WakeCheckResult,
    check_wake_phrase,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"], dependencies=[Depends(require_device)])
MAX_BASE64_CLIP_LENGTH = 4 * ((MAX_CLIP_BYTES + 2) // 3)
MAX_BASE64_WAKE_CHECK_LENGTH = 4 * ((MAX_WAKE_CHECK_BYTES + 2) // 3)
MAX_CLONE_CLIP_BYTES = 10 * 1024 * 1024
CLONE_AUDIO_EXTENSIONS = {".flac", ".mp3", ".mpeg", ".mpga", ".oga", ".ogg", ".wav", ".webm"}
EncodedClip = Annotated[str, Field(min_length=4, max_length=MAX_BASE64_CLIP_LENGTH)]
EncodedWakeCheckClip = Annotated[
    str,
    Field(min_length=4, max_length=MAX_BASE64_WAKE_CHECK_LENGTH),
]


class SpeakerProfileStatusResponse(BaseModel):
    status: str
    updated_at: datetime | None = None


class UpsertSpeakerProfileRequest(BaseModel):
    clips: list[EncodedClip] = Field(
        ...,
        min_length=REQUIRED_CLIP_COUNT,
        max_length=REQUIRED_CLIP_COUNT,
    )


class WakeCheckRequest(BaseModel):
    clip: EncodedWakeCheckClip


class WakeCheckResponse(BaseModel):
    status: Literal["recognized", "not_detected", "speaker_mismatch"]


def _status_response(status: SpeakerProfileStatus) -> SpeakerProfileStatusResponse:
    return SpeakerProfileStatusResponse(
        status=status.status,
        updated_at=status.updated_at,
    )


async def _reload_owner_verifiers(owner_id: str) -> int:
    from api.websockets.connection import manager

    async def reload_session(session) -> bool:
        try:
            wakeword = getattr(session.processor, "wakeword_service", None)
            if wakeword is not None:
                await asyncio.to_thread(wakeword.reload_verifiers)
                return True
            shared = getattr(session, "speaker_verifier", None)
            if shared is None:
                return False
            await asyncio.to_thread(shared.reload_profile)
        except Exception:
            logger.exception(
                "Failed to reload speaker verifiers | owner=%s connection=%s",
                owner_id,
                session.connection_id,
            )
            return False
        return True

    reloads = [
        reload_session(session)
        for session in manager.list_owner_sessions(owner_id)
    ]
    if not reloads:
        return 0
    return sum(await asyncio.gather(*reloads))


def _decode_clip(raw: str, *, clip_index: int | None = None) -> bytes:
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise SpeakerProfileError(
            "processing_failed",
            "Clip is not valid base64 PCM"
            if clip_index is None
            else f"Clip {clip_index} is not valid base64 PCM",
            clip_index=clip_index,
        ) from exc


def _decode_clips(raw_clips: list[str]) -> list[bytes]:
    return [_decode_clip(raw, clip_index=index) for index, raw in enumerate(raw_clips, start=1)]


def _decode_wake_check_clip(raw: str) -> bytes:
    try:
        return base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise WakeCheckError("processing_failed", "Clip is not valid base64 PCM") from exc


def _profile_error_detail(exc: SpeakerProfileError) -> dict[str, str | int]:
    detail: dict[str, str | int] = {"reason": exc.reason, "message": str(exc)}
    if exc.clip_index is not None:
        detail["clip_index"] = exc.clip_index
    return detail


@router.get("/config", response_model=VoiceRuntimeConfig)
async def get_voice_config() -> VoiceRuntimeConfig:
    return await voice_service.get_voice_config()


@router.patch("/config", response_model=VoiceRuntimeConfig)
async def update_voice_config(request: UpdateVoiceRuntimeConfigRequest) -> VoiceRuntimeConfig:
    try:
        return await voice_service.update_voice_config(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/input/status", response_model=VoiceInputStatus)
async def get_voice_input_status(
    provider: STTProvider | None = None,
    force: bool = False,
) -> VoiceInputStatus:
    return await voice_service.get_voice_input_status(provider=provider, force=force)


@router.post("/input/prepare", response_model=VoiceInputStatus)
async def prepare_voice_input() -> VoiceInputStatus:
    return await voice_service.prepare_voice_input()


@router.get("/output/status", response_model=VoiceOutputStatus)
async def get_voice_output_status(provider: TTSProvider | None = None) -> VoiceOutputStatus:
    return await voice_service.get_voice_output_status(provider=provider)


@router.post("/output/preview", response_model=LocalVoicePreview)
async def preview_local_voice() -> LocalVoicePreview:
    try:
        return await voice_service.preview_local_voice()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/clone", response_model=VoiceRuntimeConfig)
async def clone_cartesia_voice(
    clip: UploadFile = File(...),
    name: str = Form("JARV1S voice", min_length=1, max_length=100),
    language: str = Form("en", min_length=2, max_length=5),
) -> VoiceRuntimeConfig:
    suffix = Path(clip.filename or "").suffix.lower()
    if suffix not in CLONE_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Use a FLAC, MP3, OGG, WAV, or WebM audio clip.",
        )

    audio = await clip.read(MAX_CLONE_CLIP_BYTES + 1)
    if not audio:
        raise HTTPException(status_code=400, detail="The voice clip is empty.")
    if len(audio) > MAX_CLONE_CLIP_BYTES:
        raise HTTPException(status_code=413, detail="The voice clip must be smaller than 10 MB.")

    try:
        return await voice_service.clone_cartesia_voice(
            clip=audio,
            filename=clip.filename or f"voice{suffix}",
            content_type=clip.content_type or "application/octet-stream",
            name=name.strip(),
            language=language.strip().lower(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Cartesia voice cloning failed")
        raise HTTPException(
            status_code=502,
            detail="Cartesia could not clone this clip. Use clear speech no longer than 10 seconds.",
        ) from exc


@router.get("/speaker-profile", response_model=SpeakerProfileStatusResponse)
async def get_speaker_profile(owner_id: str = Depends(require_owner_id)) -> SpeakerProfileStatusResponse:
    return _status_response(get_profile_status(owner_id))


@router.put("/speaker-profile", response_model=SpeakerProfileStatusResponse)
async def upsert_speaker_profile(
    request: UpsertSpeakerProfileRequest,
    owner_id: str = Depends(require_owner_id),
) -> SpeakerProfileStatusResponse:
    try:
        clips = _decode_clips(request.clips)
        status = await asyncio.to_thread(write_profile, owner_id, clips)
    except SpeakerProfileError as exc:
        raise HTTPException(status_code=400, detail=_profile_error_detail(exc)) from exc
    except Exception as exc:
        logger.exception("Speaker profile upsert failed for owner=%s", owner_id)
        raise HTTPException(status_code=500, detail="Failed to save speaker profile") from exc

    reloaded = await _reload_owner_verifiers(owner_id)
    logger.info("Speaker profile upserted | owner=%s reloaded_sessions=%d", owner_id, reloaded)
    return _status_response(status)


@router.delete("/speaker-profile", response_model=SpeakerProfileStatusResponse)
async def remove_speaker_profile(owner_id: str = Depends(require_owner_id)) -> SpeakerProfileStatusResponse:
    status = await asyncio.to_thread(delete_profile, owner_id)
    reloaded = await _reload_owner_verifiers(owner_id)
    logger.info("Speaker profile deleted | owner=%s reloaded_sessions=%d", owner_id, reloaded)
    return _status_response(status)


@router.post("/wake-check", response_model=WakeCheckResponse)
async def wake_check(
    request: WakeCheckRequest,
    owner_id: str = Depends(require_owner_id),
) -> WakeCheckResponse:
    try:
        pcm = _decode_wake_check_clip(request.clip)
        result: WakeCheckResult = await asyncio.to_thread(check_wake_phrase, pcm, owner_id=owner_id)
    except WakeCheckError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": exc.reason, "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("Wake check failed for owner=%s", owner_id)
        raise HTTPException(status_code=500, detail="Failed to check wake phrase") from exc

    return WakeCheckResponse(status=result.status)
