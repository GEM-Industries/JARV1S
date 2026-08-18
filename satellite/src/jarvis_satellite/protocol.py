"""JARV1S WebSocket protocol helpers used by the satellite client."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

INPUT_SAMPLE_RATE = 16_000
INPUT_CHANNELS = 1
INPUT_SAMPLE_WIDTH_BYTES = 2
INPUT_FRAME_SAMPLES = 1_536
DEFAULT_TTS_SAMPLE_RATE = 24_000
NODE_REPLACED_CLOSE_CODE = 4001


class MessageType(str, Enum):
    SYSTEM_CONNECT = "system.connect"
    SYSTEM_ERROR = "system.error"
    SYSTEM_PING = "system.ping"
    SYSTEM_PONG = "system.pong"
    SYSTEM_STOP = "system.stop"
    USER_AUDIO = "user_audio"
    JARVIS_AUDIO = "jarvis_audio"
    AUDIO_CUE = "audio.cue"
    TTS_END = "audio.tts_end"
    PLAYBACK_END = "audio.playback_end"
    VOICE_ACTIVATE = "voice.activate"
    MUTE = "audio.mute"
    SPEECH_START = "speech.start"
    STATUS = "status.update"
    PREFERENCES_UPDATE = "preferences.update"
    NOTIFICATION_SOUND = "notification.sound"
    CLIENT_DIAGNOSTICS = "client.diagnostics"


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Client-to-server message envelope."""

    id: str
    type: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "data": self.data}


def message_id(prefix: str = "sat") -> str:
    return f"{prefix}-{time.time_ns()}"


def encode_audio(audio: bytes) -> str:
    return base64.b64encode(audio).decode("utf-8")


def decode_audio(encoded: str) -> bytes:
    return base64.b64decode(encoded)


def user_audio_message(audio: bytes) -> OutboundMessage:
    return OutboundMessage(
        id=message_id("audio"),
        type=MessageType.USER_AUDIO.value,
        data={"audio": encode_audio(audio), "encoding": "base64"},
    )


def playback_end_message(turn_id: str | None = None) -> OutboundMessage:
    data: dict[str, Any] = {}
    if turn_id:
        data["turn_id"] = turn_id
    return OutboundMessage(id=message_id("playback-end"), type=MessageType.PLAYBACK_END.value, data=data)


def ping_message() -> OutboundMessage:
    now_ms = int(time.time() * 1000)
    return OutboundMessage(
        id=message_id("ping"),
        type=MessageType.SYSTEM_PING.value,
        data={"timestamp": now_ms},
    )


def voice_activate_message() -> OutboundMessage:
    return OutboundMessage(id=message_id("voice-activate"), type=MessageType.VOICE_ACTIVATE.value, data={})


def mute_message() -> OutboundMessage:
    return OutboundMessage(id=message_id("mute"), type=MessageType.MUTE.value, data={})


def client_diagnostics_message(
    events: list[dict[str, Any]],
    *,
    dropped_count: int = 0,
) -> OutboundMessage:
    return OutboundMessage(
        id=message_id("client-diag"),
        type=MessageType.CLIENT_DIAGNOSTICS.value,
        data={"events": events, "dropped_count": dropped_count},
    )

