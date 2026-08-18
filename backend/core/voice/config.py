"""Resolved voice runtime configuration for host-level provider choices."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from core import settings

_VOICE_CONFIG_KEY = "voice_config"

STTProvider = Literal["apple_speech", "cartesia"]
TTSProvider = Literal["off", "cartesia", "local"]

DEFAULT_LOCAL_VOICE_ID = "af_heart"
LOCAL_VOICE_IDS: tuple[str, ...] = (
    "af_heart",
    "af_bella",
    "af_sarah",
    "am_michael",
    "am_adam",
    "bf_emma",
    "bm_george",
)


class VoiceConfigSource(str, Enum):
    PERSISTED = "persisted"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class ResolvedVoiceConfig:
    stt_provider: STTProvider
    tts_provider: TTSProvider
    cartesia_voice_id: str | None
    local_voice_id: str
    source: VoiceConfigSource

    @property
    def signature(self) -> tuple[str, str | None]:
        return (self.stt_provider, settings.VOICE.apple_speech_url)

    @property
    def tts_signature(self) -> tuple[str, str | None, str | None]:
        voice = (
            self.cartesia_voice_id
            if self.tts_provider == "cartesia"
            else self.local_voice_id
            if self.tts_provider == "local"
            else None
        )
        return (self.tts_provider, voice, settings.VOICE.local_tts_url or None)

    @property
    def apple_speech_url(self) -> str:
        """Runtime-only helper URL injected by the desktop supervisor (or env)."""
        return settings.VOICE.apple_speech_url

    @property
    def local_tts_url(self) -> str:
        """Runtime-only Kokoro helper URL injected by the desktop supervisor (or env)."""
        return settings.VOICE.local_tts_url


class VoiceRuntimeConfig(BaseModel):
    stt_provider: STTProvider
    tts_provider: TTSProvider
    cartesia_voice_id: str | None
    local_voice_id: str
    source: VoiceConfigSource


class UpdateVoiceRuntimeConfigRequest(BaseModel):
    stt_provider: STTProvider | None = None
    tts_provider: TTSProvider | None = None
    cartesia_voice_id: str | None = None
    local_voice_id: str | None = None


def macos_supports_apple_speech() -> bool:
    if sys.platform != "darwin":
        return False
    version = platform.mac_ver()[0]
    if not version:
        return False
    try:
        major = int(version.split(".", 1)[0])
    except ValueError:
        return False
    return major >= 26


def default_stt_provider() -> STTProvider:
    if macos_supports_apple_speech():
        return "apple_speech"
    return "cartesia"


def normalize_local_voice_id(value: object | None) -> str:
    voice = str(value or "").strip()
    if voice in LOCAL_VOICE_IDS:
        return voice
    return DEFAULT_LOCAL_VOICE_ID


def _normalize_stt_provider(value: object) -> STTProvider:
    if value == "cartesia":
        return "cartesia"
    if value in {"apple_speech", "local_streaming"}:
        return "apple_speech"
    return default_stt_provider()


def _normalize_tts_provider(
    value: object | None,
    *,
    cartesia_voice_id: str | None,
) -> TTSProvider:
    if value == "local":
        return "local"
    if value == "cartesia":
        return "cartesia"
    if value == "off":
        return "off"
    # Legacy docs: a Cartesia voice ID without an explicit provider meant spoken replies.
    if cartesia_voice_id:
        return "cartesia"
    return "off"


def _persisted_from_doc(doc: dict) -> dict[str, str | None]:
    cartesia_voice_id = None
    if doc.get("cartesia_voice_id"):
        cartesia_voice_id = str(doc["cartesia_voice_id"]).strip() or None
    elif doc.get("tts_voice_id"):
        cartesia_voice_id = str(doc["tts_voice_id"]).strip() or None

    local_voice_id = normalize_local_voice_id(doc.get("local_voice_id"))
    tts_provider = _normalize_tts_provider(doc.get("tts_provider"), cartesia_voice_id=cartesia_voice_id)
    return {
        "stt_provider": _normalize_stt_provider(doc.get("stt_provider")),
        "tts_provider": tts_provider,
        "cartesia_voice_id": cartesia_voice_id,
        "local_voice_id": local_voice_id,
    }


class VoiceConfigStore:
    def __init__(self) -> None:
        self._cache: dict[str, str | None] | None = None

    async def load_persisted(self) -> dict[str, str | None] | None:
        if self._cache is not None:
            return self._cache
        try:
            from services.database.mongodb import mongodb

            col = mongodb.get_collection("system_config")
            doc = await col.find_one({"_id": _VOICE_CONFIG_KEY})
            if not doc:
                return None
            self._cache = _persisted_from_doc(doc)
            return self._cache
        except Exception:
            return None

    async def save(
        self,
        *,
        stt_provider: STTProvider,
        tts_provider: TTSProvider,
        cartesia_voice_id: str | None,
        local_voice_id: str,
    ) -> None:
        from services.database.mongodb import mongodb

        payload = {
            "stt_provider": stt_provider,
            "tts_provider": tts_provider,
            "cartesia_voice_id": cartesia_voice_id,
            "local_voice_id": normalize_local_voice_id(local_voice_id),
        }
        col = mongodb.get_collection("system_config")
        # Drop legacy fields so they are not treated as product config.
        await col.update_one(
            {"_id": _VOICE_CONFIG_KEY},
            {
                "$set": payload,
                "$unset": {
                    "local_stt_url": "",
                    "local_stt_protocol": "",
                    "tts_voice_id": "",
                },
            },
            upsert=True,
        )
        self._cache = payload

    def clear_cache(self) -> None:
        self._cache = None


voice_config_store = VoiceConfigStore()


def resolve_voice_config_sync() -> ResolvedVoiceConfig:
    persisted = voice_config_store._cache
    if persisted:
        cartesia_voice_id = (
            str(persisted["cartesia_voice_id"]).strip()
            if persisted.get("cartesia_voice_id")
            else None
        )
        return ResolvedVoiceConfig(
            stt_provider=_normalize_stt_provider(persisted.get("stt_provider")),
            tts_provider=_normalize_tts_provider(
                persisted.get("tts_provider"),
                cartesia_voice_id=cartesia_voice_id,
            ),
            cartesia_voice_id=cartesia_voice_id,
            local_voice_id=normalize_local_voice_id(persisted.get("local_voice_id")),
            source=VoiceConfigSource.PERSISTED,
        )

    return ResolvedVoiceConfig(
        stt_provider=default_stt_provider(),
        tts_provider="off",
        cartesia_voice_id=None,
        local_voice_id=DEFAULT_LOCAL_VOICE_ID,
        source=VoiceConfigSource.DEFAULT,
    )


async def resolve_voice_config() -> ResolvedVoiceConfig:
    await voice_config_store.load_persisted()
    return resolve_voice_config_sync()


def to_runtime_config(config: ResolvedVoiceConfig) -> VoiceRuntimeConfig:
    return VoiceRuntimeConfig(
        stt_provider=config.stt_provider,
        tts_provider=config.tts_provider,
        cartesia_voice_id=config.cartesia_voice_id,
        local_voice_id=config.local_voice_id,
        source=config.source,
    )
