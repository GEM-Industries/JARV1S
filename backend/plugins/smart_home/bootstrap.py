"""Home Assistant Container bootstrap for JARV1S via Docker."""

from __future__ import annotations

import asyncio
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from plugins.smart_home.bootstrap_config import (
    BOOTSTRAP_COMPOSE_FILE,
    BOOTSTRAP_CONFIG_DIR,
    BOOTSTRAP_CONTAINER_NAME,
    BOOTSTRAP_DATA_DIR,
    BOOTSTRAP_HA_URL,
    BOOTSTRAP_HOST_PORT,
    HA_CONTAINER_IMAGE,
)
from plugins.smart_home.ha_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
    HomeAssistantOnboardingError,
    normalize_ha_url,
)

ProgressCallback = Callable[[str], None]

BOOTSTRAP_START_TIMEOUT_S = 600
BOOTSTRAP_POLL_INTERVAL_S = 3.0


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    base_url: str
    long_lived_token: str
    onboarding_complete: bool


def default_progress(message: str) -> None:
    print(message, flush=True)


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_daemon_reachable() -> bool:
    if not docker_available():
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def docker_compose_command() -> list[str] | None:
    if shutil.which("docker"):
        try:
            result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return ["docker", "compose"]
        except (subprocess.TimeoutExpired, OSError):
            pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def bootstrap_supported() -> tuple[bool, str | None]:
    if not docker_available():
        return False, (
            "Docker is not installed or not on PATH. "
            "Install Docker Desktop (macOS/Windows) or Docker Engine (Linux)."
        )
    if not docker_daemon_reachable():
        return False, "Docker is installed but not running. Start Docker Desktop and try again."
    if docker_compose_command() is None:
        return False, "Docker Compose is not available (need `docker compose` or `docker-compose`)."
    return True, None


def write_compose_file() -> Path:
    BOOTSTRAP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    BOOTSTRAP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    content = f"""services:
  homeassistant:
    container_name: {BOOTSTRAP_CONTAINER_NAME}
    image: {HA_CONTAINER_IMAGE}
    volumes:
      - {BOOTSTRAP_CONFIG_DIR}:/config
    ports:
      - "127.0.0.1:{BOOTSTRAP_HOST_PORT}:8123"
    restart: unless-stopped
"""
    BOOTSTRAP_COMPOSE_FILE.write_text(content, encoding="utf-8")
    return BOOTSTRAP_COMPOSE_FILE


def _run_compose(args: list[str], *, progress: ProgressCallback) -> None:
    compose = docker_compose_command()
    if compose is None:
        raise RuntimeError("Docker Compose is not available")
    cmd = [*compose, "-f", str(BOOTSTRAP_COMPOSE_FILE), *args]
    progress(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Docker compose failed: {detail or result.returncode}")


def pull_image(*, progress: ProgressCallback = default_progress) -> None:
    progress(f"Pulling Home Assistant image ({HA_CONTAINER_IMAGE}) — this can take a minute...")
    result = subprocess.run(
        ["docker", "pull", HA_CONTAINER_IMAGE],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Failed to pull HA image: {detail}")


def start_container(*, progress: ProgressCallback = default_progress) -> None:
    write_compose_file()
    progress("Starting Home Assistant container...")
    _run_compose(["up", "-d"], progress=progress)


def stop_container(*, progress: ProgressCallback = default_progress) -> None:
    if not BOOTSTRAP_COMPOSE_FILE.exists():
        return
    progress("Stopping Home Assistant container...")
    _run_compose(["down"], progress=progress)


async def wait_for_home_assistant(
    base_url: str = BOOTSTRAP_HA_URL,
    *,
    timeout_s: float = BOOTSTRAP_START_TIMEOUT_S,
    progress: ProgressCallback = default_progress,
) -> None:
    progress("Waiting for Home Assistant first boot...")
    deadline = time.monotonic() + timeout_s
    client = HomeAssistantClient(base_url=base_url)
    try:
        while time.monotonic() < deadline:
            try:
                resp = await client._http.get(f"{normalize_ha_url(base_url)}/api/onboarding")
                if resp.status_code == 200:
                    progress("Home Assistant is responding.")
                    return
            except Exception:
                pass
            try:
                resp = await client._http.get(f"{normalize_ha_url(base_url)}/api/")
                if resp.status_code in {200, 401}:
                    progress("Home Assistant API is up.")
                    return
            except Exception:
                pass
            await asyncio.sleep(BOOTSTRAP_POLL_INTERVAL_S)
    finally:
        await client.aclose()
    raise HomeAssistantConnectionError(
        f"Home Assistant did not become ready within {int(timeout_s)}s at {base_url}"
    )


async def bootstrap_fresh_instance(
    *,
    owner_name: str = "JARV1S Owner",
    username: str = "jarvis",
    password: str,
    base_url: str = BOOTSTRAP_HA_URL,
    analytics_opt_in: bool = False,
    progress: ProgressCallback = default_progress,
) -> BootstrapResult:
    """Full onboarding: user → core_config → analytics → integration → long-lived token."""
    client = HomeAssistantClient(base_url=base_url)
    try:
        progress("Onboarding Home Assistant...")
        long_lived = await client.complete_bootstrap_onboarding(
            owner_name=owner_name,
            username=username,
            password=password,
            analytics_opt_in=analytics_opt_in,
        )
        complete = not await client.onboarding_pending()
        progress("Minted JARV1S long-lived access token.")
        return BootstrapResult(
            base_url=normalize_ha_url(base_url),
            long_lived_token=long_lived,
            onboarding_complete=complete,
        )
    finally:
        await client.aclose()


async def run_bootstrap(
    *,
    password: str,
    owner_name: str = "JARV1S Owner",
    username: str = "jarvis",
    pull: bool = True,
    progress: ProgressCallback = default_progress,
) -> BootstrapResult:
    ok, reason = bootstrap_supported()
    if not ok:
        raise RuntimeError(reason or "Bootstrap is not supported on this host")

    progress(
        f"Bootstrap host: {platform.system()} | image: {HA_CONTAINER_IMAGE} | url: {BOOTSTRAP_HA_URL}"
    )
    if pull:
        pull_image(progress=progress)
    start_container(progress=progress)
    await wait_for_home_assistant(progress=progress)
    result = await bootstrap_fresh_instance(
        owner_name=owner_name,
        username=username,
        password=password,
        progress=progress,
    )
    progress("Home Assistant bootstrap complete — JARV1S can connect.")
    return result
