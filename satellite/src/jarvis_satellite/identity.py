"""Presence identity helpers for a JARV1S satellite node."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from .config import SatelliteConfig


def load_or_create_node_id(config: SatelliteConfig) -> str:
    if config.node_id:
        return config.node_id

    path = config.identity_path
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            node_id = str(data.get("node_id") or "").strip()
            if node_id:
                return node_id
        except (OSError, json.JSONDecodeError):
            pass

    hostname = socket.gethostname().strip().lower().replace("_", "-") or "satellite"
    node_id = f"{hostname}-{uuid4().hex[:8]}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"node_id": node_id}, indent=2) + "\n", encoding="utf-8")
    return node_id


def build_presence_params(config: SatelliteConfig, node_id: str) -> dict[str, str]:
    params: dict[str, str] = {
        "timezone": config.timezone,
        "node_id": node_id,
        "capabilities": ",".join(config.capabilities),
        "client_surface": "satellite",
    }
    optional = {
        "node_label": config.node_label,
        "location_provider": config.location_provider,
        "room_id": config.room_id,
        "room_name": config.room_name,
        "ha_area_id": config.ha_area_id,
        "ha_device_id": config.ha_device_id,
        "ha_entity_id": config.ha_entity_id,
    }
    if (config.room_id or config.room_name) and not config.location_provider:
        optional["location_provider"] = "manual"
    if (config.ha_area_id or config.ha_device_id or config.ha_entity_id) and not config.location_provider:
        optional["location_provider"] = "home_assistant"

    for key, value in optional.items():
        if value:
            params[key] = value
    return params


def build_websocket_url(config: SatelliteConfig, node_id: str, *, ticket: str | None = None) -> str:
    parts = urlsplit(config.backend_url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=False))
    params = build_presence_params(config, node_id)
    if ticket:
        params["ticket"] = ticket
    query = urlencode(existing | params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def ensure_state_dir(config: SatelliteConfig) -> Path:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config.state_dir
