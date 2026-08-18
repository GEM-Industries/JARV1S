"""Configuration loading for the JARV1S satellite service."""

from __future__ import annotations

import argparse
import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.jarvis-satellite/config.toml").expanduser()


@dataclass(frozen=True, slots=True)
class SatelliteConfig:
    backend_url: str = "ws://localhost:8000/api/v1/ws"
    device_token: str | None = None
    timezone: str = "UTC"
    node_id: str | None = None
    node_label: str | None = None
    capabilities: tuple[str, ...] = ("mic", "speaker")
    location_provider: str | None = None
    room_id: str | None = None
    room_name: str | None = None
    ha_area_id: str | None = None
    ha_device_id: str | None = None
    ha_entity_id: str | None = None

    state_dir: Path = Path("~/.jarvis-satellite").expanduser()
    audio_backend: str = "auto"
    input_device: int | str | None = None
    output_device: int | str | None = None
    input_channels: int = 1
    input_channel_index: int = 0
    playback_channels: int = 1
    input_frame_samples: int = 1_536
    mic_queue_max_chunks: int = 50
    reconnect_base_delay_s: float = 3.0
    reconnect_max_delay_s: float = 30.0
    heartbeat_interval_s: float = 5.0
    heartbeat_timeout_s: float = 20.0
    playback_end_settle_s: float = 0.5
    tts_end_timeout_s: float = 2.0
    auto_activate: bool = False
    tool_cues_enabled: bool = True
    log_level: str = "INFO"

    # On-device PASSIVE wake: idle rooms stop streaming PCM until local wake or
    # an active host session (listening/speaking) needs barge-in audio.
    edge_wakeword: bool = False
    wakeword_model_path: str | None = None
    wakeword_sensitivity: float = 0.70
    wakeword_patience: int = 3
    wakeword_vad_threshold: float = 0.5
    wake_preroll_seconds: float = 3.0

    led_enabled: bool = False
    xvf_host_path: str | None = None
    led_brightness: int = 80

    @property
    def identity_path(self) -> Path:
        return self.state_dir / "identity.json"

    @property
    def resolved_wakeword_model_path(self) -> Path:
        if self.wakeword_model_path:
            return Path(self.wakeword_model_path).expanduser()
        return self.state_dir / "models" / "Jarvis.onnx"


def load_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        return {}
    return data


def _coerce_device(value: Any) -> int | str | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return text


def _coerce_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ("mic", "speaker")
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list | tuple):
        return tuple(str(part).strip() for part in value if str(part).strip())
    return ("mic", "speaker")


def _coerce_float(value: Any) -> float:
    return float(value)


def _coerce_int(value: Any) -> int:
    return int(value)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _filtered_config_values(raw: dict[str, Any]) -> dict[str, Any]:
    values = dict(raw)
    if "state_dir" in values:
        values["state_dir"] = Path(str(values["state_dir"])).expanduser()
    if "input_device" in values:
        values["input_device"] = _coerce_device(values["input_device"])
    if "output_device" in values:
        values["output_device"] = _coerce_device(values["output_device"])
    if "capabilities" in values:
        values["capabilities"] = _coerce_tuple(values["capabilities"])
    for key in (
        "reconnect_base_delay_s",
        "reconnect_max_delay_s",
        "heartbeat_interval_s",
        "heartbeat_timeout_s",
        "playback_end_settle_s",
        "tts_end_timeout_s",
        "wakeword_sensitivity",
        "wakeword_vad_threshold",
        "wake_preroll_seconds",
    ):
        if key in values:
            values[key] = _coerce_float(values[key])
    for key in (
        "input_channels",
        "input_channel_index",
        "playback_channels",
        "input_frame_samples",
        "led_brightness",
        "wakeword_patience",
    ):
        if key in values:
            values[key] = _coerce_int(values[key])
    for key in ("led_enabled", "tool_cues_enabled", "edge_wakeword"):
        if key in values:
            values[key] = _coerce_bool(values[key])
    if "wakeword_model_path" in values and values["wakeword_model_path"] is not None:
        values["wakeword_model_path"] = str(values["wakeword_model_path"])

    allowed = set(SatelliteConfig.__dataclass_fields__)
    return {key: value for key, value in values.items() if key in allowed}


def _env_values() -> dict[str, Any]:
    prefix = "JARVIS_SATELLITE_"
    mapping = {
        "BACKEND_URL": "backend_url",
        "DEVICE_TOKEN": "device_token",
        "TIMEZONE": "timezone",
        "NODE_ID": "node_id",
        "NODE_LABEL": "node_label",
        "CAPABILITIES": "capabilities",
        "LOCATION_PROVIDER": "location_provider",
        "ROOM_ID": "room_id",
        "ROOM_NAME": "room_name",
        "HA_AREA_ID": "ha_area_id",
        "HA_DEVICE_ID": "ha_device_id",
        "HA_ENTITY_ID": "ha_entity_id",
        "STATE_DIR": "state_dir",
        "AUDIO_BACKEND": "audio_backend",
        "INPUT_DEVICE": "input_device",
        "OUTPUT_DEVICE": "output_device",
        "INPUT_CHANNELS": "input_channels",
        "INPUT_CHANNEL_INDEX": "input_channel_index",
        "PLAYBACK_CHANNELS": "playback_channels",
        "TTS_END_TIMEOUT_S": "tts_end_timeout_s",
        "LOG_LEVEL": "log_level",
        "TOOL_CUES_ENABLED": "tool_cues_enabled",
        "LED_ENABLED": "led_enabled",
        "XVF_HOST_PATH": "xvf_host_path",
        "LED_BRIGHTNESS": "led_brightness",
        "EDGE_WAKEWORD": "edge_wakeword",
        "WAKEWORD_MODEL_PATH": "wakeword_model_path",
        "WAKEWORD_SENSITIVITY": "wakeword_sensitivity",
        "WAKEWORD_PATIENCE": "wakeword_patience",
        "WAKEWORD_VAD_THRESHOLD": "wakeword_vad_threshold",
        "WAKE_PREROLL_SECONDS": "wake_preroll_seconds",
    }
    raw: dict[str, Any] = {}
    for env_key, config_key in mapping.items():
        value = os.getenv(f"{prefix}{env_key}")
        if value is not None:
            raw[config_key] = value
    return _filtered_config_values(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a JARV1S voice satellite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--backend-url")
    parser.add_argument("--device-token")
    parser.add_argument("--timezone")
    parser.add_argument("--node-id")
    parser.add_argument("--node-label")
    parser.add_argument("--room-id")
    parser.add_argument("--room-name")
    parser.add_argument("--ha-area-id")
    parser.add_argument("--input-device")
    parser.add_argument("--output-device")
    parser.add_argument("--input-channels", type=int)
    parser.add_argument("--input-channel-index", type=int)
    parser.add_argument("--playback-channels", type=int)
    parser.add_argument("--audio-backend", choices=("auto", "pyaudio", "alsa"))
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--log-level")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--dry-run-audio", action="store_true")
    parser.add_argument("--activate", action="store_true", help="Send voice.activate after connect.")
    return parser


def load_config(args: argparse.Namespace) -> SatelliteConfig:
    file_values = _filtered_config_values(load_config_file(args.config.expanduser()))
    env_values = _env_values()
    config = SatelliteConfig(**file_values)
    config = replace(config, **env_values)

    cli_values = {
        "backend_url": args.backend_url,
        "device_token": args.device_token,
        "timezone": args.timezone,
        "node_id": args.node_id,
        "node_label": args.node_label,
        "room_id": args.room_id,
        "room_name": args.room_name,
        "ha_area_id": args.ha_area_id,
        "input_device": _coerce_device(args.input_device),
        "output_device": _coerce_device(args.output_device),
        "input_channels": getattr(args, "input_channels", None),
        "input_channel_index": getattr(args, "input_channel_index", None),
        "playback_channels": getattr(args, "playback_channels", None),
        "audio_backend": args.audio_backend,
        "state_dir": args.state_dir.expanduser() if args.state_dir else None,
        "log_level": args.log_level,
        "auto_activate": True if args.activate else None,
    }
    clean_cli = {key: value for key, value in cli_values.items() if value is not None}
    return replace(config, **clean_cli)
