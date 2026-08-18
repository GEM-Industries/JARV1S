"""Pinned Home Assistant bootstrap settings."""

from __future__ import annotations

from pathlib import Path

from core.config import settings

# Pin the HA Container image used for bootstrap and fixture capture.
# Bump deliberately; re-run capture_ha_fixtures + fixture drift tests when changing.
HA_CONTAINER_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.8.1"

# Loopback URL for IndieAuth client_id during bootstrap (must match token exchange).
BOOTSTRAP_HA_URL = "http://127.0.0.1:8123"

BOOTSTRAP_CONTAINER_NAME = "jarvis-homeassistant"
BOOTSTRAP_HOST_PORT = 8123

BOOTSTRAP_DATA_DIR = settings.DATA_DIR / "home-assistant"
BOOTSTRAP_CONFIG_DIR = BOOTSTRAP_DATA_DIR / "config"
BOOTSTRAP_COMPOSE_FILE = BOOTSTRAP_DATA_DIR / "docker-compose.yml"

# Fixtures captured against HA_CONTAINER_IMAGE — see tests/fixtures/ha/manifest.json
FIXTURE_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ha" / "manifest.json"
)
