"""ReSpeaker XVF3800 status LED sync for the JARV1S satellite."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import struct
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import usb.core  # type: ignore[import-untyped]
import usb.util  # type: ignore[import-untyped]

from .config import SatelliteConfig

logger = logging.getLogger(__name__)

# Saturated hardware colors; the XVF3800 ring washes out soft UI palette values.
COLOR_BLUE = 0x1AA7FF
COLOR_GREEN = 0x20E080
COLOR_WARNING = 0xFF9F1A
COLOR_ERROR = 0xFF3B30

# ReSpeaker LED_EFFECT values: 0=off, 1=breath, 3=single color.
EFFECT_OFF = 0
EFFECT_BREATH = 1
EFFECT_SINGLE = 3

PULSE_STAGES = frozenset({"transcribing", "thinking", "composing_tool", "running_tool", "speaking"})
ACTIVE_STAGES = frozenset({"waking", "listening", *PULSE_STAGES})
USB_VENDOR_ID = 0x2886
USB_PRODUCT_ID = 0x001A
USB_TIMEOUT_MS = 5_000
LED_REASSERT_INTERVAL_S = 30.0
USB_COMMANDS = {
    "LED_EFFECT": (20, 12, "uint8"),
    "LED_BRIGHTNESS": (20, 13, "uint8"),
    "LED_SPEED": (20, 15, "uint8"),
    "LED_COLOR": (20, 16, "uint32"),
}


@dataclass(frozen=True, slots=True)
class LedAppearance:
    effect: int
    color: int
    brightness: int
    speed: int = 1


OFF_APPEARANCE = LedAppearance(EFFECT_OFF, 0, 0)


def appearance_for_stage(stage: str, *, brightness: int) -> LedAppearance:
    """Map a JARV1S agent stage to ReSpeaker LED settings."""
    if stage not in ACTIVE_STAGES:
        return OFF_APPEARANCE
    if stage == "waking":
        return LedAppearance(EFFECT_BREATH, COLOR_GREEN, brightness, speed=1)
    if stage == "listening":
        return LedAppearance(EFFECT_SINGLE, COLOR_BLUE, brightness)
    if stage in PULSE_STAGES:
        color = COLOR_GREEN if stage == "speaking" else COLOR_BLUE
        return LedAppearance(EFFECT_BREATH, color, brightness, speed=1)
    return OFF_APPEARANCE


def idle_appearance(
    *,
    brightness: int,
    soft_muted: bool = False,
    attention_mode: str = "active",
) -> LedAppearance:
    """Show only states the user must know before talking in a bedroom."""
    if attention_mode == "paused":
        return LedAppearance(EFFECT_SINGLE, COLOR_ERROR, brightness)
    if soft_muted:
        return LedAppearance(EFFECT_SINGLE, COLOR_WARNING, brightness)
    return OFF_APPEARANCE


def _run_xvf_host(path: Path, command: str, value: int) -> None:
    result = subprocess.run(
        [str(path), command, "--values", str(value)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"xvf_host {command} {value} failed: {detail or result.returncode}")


class LedRunner(Protocol):
    def __call__(self, command: str, value: int) -> None: ...


class UsbLedRunner:
    """Minimal in-process XVF3800 control path for fast LED updates."""

    def __init__(self) -> None:
        self._device = usb.core.find(idVendor=USB_VENDOR_ID, idProduct=USB_PRODUCT_ID)
        if self._device is None:
            raise RuntimeError("ReSpeaker XVF3800 USB device not found")

    def __call__(self, command: str, value: int) -> None:
        resid, cmdid, value_type = USB_COMMANDS[command]
        if value_type == "uint8":
            payload = value.to_bytes(1, byteorder="little")
        elif value_type == "uint32":
            payload = struct.pack("<I", value)
        else:
            raise RuntimeError(f"Unsupported XVF3800 value type: {value_type}")

        self._device.ctrl_transfer(
            usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0,
            cmdid,
            resid,
            payload,
            USB_TIMEOUT_MS,
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            usb.util.dispose_resources(self._device)


class SubprocessLedRunner:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self, command: str, value: int) -> None:
        _run_xvf_host(self._path, command, value)


class SatelliteLedController:
    """Optional status LED driver; fail-soft when xvf_host is unavailable."""

    def __init__(
        self,
        config: SatelliteConfig,
        *,
        runner: LedRunner | None = None,
        runner_factory: Callable[[], LedRunner | None] | None = None,
        reassert_interval_s: float = LED_REASSERT_INTERVAL_S,
    ) -> None:
        self._enabled = config.led_enabled
        self._brightness = max(1, min(255, config.led_brightness))
        self._runner: LedRunner | None = runner
        self._runner_factory = runner_factory
        if self._runner_factory is None and runner is None:
            self._runner_factory = self._build_runner
        self._active_key: tuple[int, int, int, int] | None = None
        self._stage = "idle"
        self._soft_muted = False
        self._attention_mode = "active"
        self._warned = False
        self._reassert_interval_s = reassert_interval_s
        self._reassert_task: asyncio.Task[None] | None = None
        self._xvf_host_path = config.xvf_host_path

        if not self._enabled:
            return

        if self._runner is not None:
            return

        self._runner = self._build_runner()

    @property
    def available(self) -> bool:
        return self._enabled and self._runner is not None

    def start(self) -> None:
        if not self._enabled or self._reassert_task is not None:
            return
        self._reassert_task = asyncio.create_task(self._reassert_loop(), name="led-reassert")

    async def stop(self) -> None:
        task = self._reassert_task
        self._reassert_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def reassert(self) -> None:
        await self._render(force=True)

    def _build_runner(self) -> LedRunner | None:
        try:
            return UsbLedRunner()
        except Exception as exc:
            logger.warning("Direct LED USB control unavailable: %s", exc)

        if self._xvf_host_path:
            candidate = Path(self._xvf_host_path).expanduser()
            if candidate.is_file():
                return SubprocessLedRunner(candidate)
            elif not self._warned:
                logger.warning("LED sync enabled but xvf_host not found at %s", candidate)
                self._warned = True

        if not self._warned:
            logger.warning("LED sync enabled but no ReSpeaker control path is available")
            self._warned = True
        return None

    async def set_stage(self, stage: str) -> None:
        self._stage = stage
        await self._render()

    async def update_context(
        self,
        *,
        stage: str | None = None,
        soft_muted: bool | None = None,
        attention_mode: str | None = None,
    ) -> None:
        if stage is not None:
            self._stage = stage
        if soft_muted is not None:
            self._soft_muted = soft_muted
        if attention_mode is not None:
            self._attention_mode = attention_mode
        await self._render()

    async def set_waking(self) -> None:
        await self.set_stage("waking")

    async def set_connected(self) -> None:
        self._stage = "idle"
        await self._render(force=True)

    async def set_disconnected(self) -> None:
        await self._apply(LedAppearance(EFFECT_OFF, 0, 0), force=True)

    async def _reassert_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reassert_interval_s)
            await self.reassert()

    async def _render(self, *, force: bool = False) -> None:
        if self._stage in ACTIVE_STAGES:
            appearance = appearance_for_stage(self._stage, brightness=self._brightness)
        else:
            appearance = idle_appearance(
                brightness=self._brightness,
                soft_muted=self._soft_muted,
                attention_mode=self._attention_mode,
            )
        await self._apply(appearance, force=force)

    async def _apply(self, appearance: LedAppearance, *, force: bool = False) -> None:
        if not self._enabled:
            return
        if self._runner is None and self._runner_factory is not None:
            self._runner = self._runner_factory()
        if self._runner is None:
            return

        key = (appearance.effect, appearance.color, appearance.brightness, appearance.speed)
        if not force and key == self._active_key:
            return

        runner = self._runner

        def apply_sync() -> None:
            if appearance.effect == EFFECT_OFF:
                runner("LED_EFFECT", EFFECT_OFF)
                return
            runner("LED_COLOR", appearance.color)
            runner("LED_BRIGHTNESS", appearance.brightness)
            if appearance.effect == EFFECT_BREATH:
                runner("LED_SPEED", appearance.speed)
            runner("LED_EFFECT", appearance.effect)

        try:
            await asyncio.to_thread(apply_sync)
        except Exception as exc:
            if not self._warned:
                logger.warning("LED sync failed; will retry on next render: %s", exc)
                self._warned = True
            self._dispose_runner(runner)
            self._runner = None
            self._active_key = None
            return

        self._active_key = key
        logger.debug(
            "LED stage applied effect=%s color=0x%06X brightness=%s speed=%s",
            appearance.effect,
            appearance.color,
            appearance.brightness,
            appearance.speed,
        )

    @staticmethod
    def _dispose_runner(runner: LedRunner) -> None:
        close = getattr(runner, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def build_led_controller(
    config: SatelliteConfig,
    *,
    runner: LedRunner | None = None,
) -> SatelliteLedController:
    return SatelliteLedController(config, runner=runner)
