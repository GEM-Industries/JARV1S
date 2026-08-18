import asyncio
from pathlib import Path

import pytest

from jarvis_satellite.config import SatelliteConfig
from jarvis_satellite.led import (
    COLOR_BLUE,
    COLOR_ERROR,
    COLOR_GREEN,
    COLOR_WARNING,
    EFFECT_BREATH,
    EFFECT_SINGLE,
    OFF_APPEARANCE,
    SatelliteLedController,
    appearance_for_stage,
    idle_appearance,
)


def make_controller(tmp_path: Path) -> tuple[SatelliteLedController, list[tuple[str, int]]]:
    calls: list[tuple[str, int]] = []

    def runner(command: str, value: int) -> None:
        calls.append((command, value))

    host = tmp_path / "xvf_host"
    host.write_text("")

    controller = SatelliteLedController(
        SatelliteConfig(
            led_enabled=True,
            xvf_host_path=str(host),
            led_brightness=80,
        ),
        runner=runner,
    )
    return controller, calls


class CloseableFailingRunner:
    def __init__(self) -> None:
        self.closed = False

    def __call__(self, command: str, value: int) -> None:
        raise RuntimeError("usb unavailable")

    def close(self) -> None:
        self.closed = True


def test_appearance_for_stage_idle_is_off():
    appearance = appearance_for_stage("idle", brightness=80)
    assert appearance == OFF_APPEARANCE


def test_idle_appearance_soft_muted_is_orange():
    appearance = idle_appearance(brightness=80, soft_muted=True)
    assert appearance.effect == EFFECT_SINGLE
    assert appearance.color == COLOR_WARNING
    assert appearance.brightness == 80


def test_idle_appearance_quiet_is_off():
    appearance = idle_appearance(brightness=80, attention_mode="quiet")
    assert appearance == OFF_APPEARANCE


def test_idle_appearance_paused_is_red():
    appearance = idle_appearance(brightness=80, soft_muted=True, attention_mode="paused")
    assert appearance.effect == EFFECT_SINGLE
    assert appearance.color == COLOR_ERROR
    assert appearance.brightness == 80


def test_appearance_for_stage_waking_is_green_fade():
    appearance = appearance_for_stage("waking", brightness=80)
    assert appearance.effect == EFFECT_BREATH
    assert appearance.color == COLOR_GREEN
    assert appearance.speed == 1


def test_appearance_for_stage_listening_is_steady_blue():
    appearance = appearance_for_stage("listening", brightness=80)
    assert appearance.effect == EFFECT_SINGLE
    assert appearance.color == COLOR_BLUE
    assert appearance.brightness == 80


def test_appearance_for_stage_speaking_is_pulsing_green():
    appearance = appearance_for_stage("speaking", brightness=80)
    assert appearance.effect == EFFECT_BREATH
    assert appearance.color == COLOR_GREEN
    assert appearance.speed == 1


def test_appearance_for_stage_thinking_is_pulsing_blue():
    appearance = appearance_for_stage("thinking", brightness=80)
    assert appearance.effect == EFFECT_BREATH
    assert appearance.color == COLOR_BLUE


@pytest.mark.asyncio
async def test_led_controller_dedupes_repeated_stage(tmp_path: Path):
    controller, calls = make_controller(tmp_path)

    await controller.set_stage("idle")
    await controller.set_stage("idle")

    assert len(calls) == 1
    assert calls[0] == ("LED_EFFECT", 0)


@pytest.mark.asyncio
async def test_led_controller_reassert_bypasses_dedupe(tmp_path: Path):
    controller, calls = make_controller(tmp_path)

    await controller.set_stage("idle")
    await controller.reassert()

    assert calls == [("LED_EFFECT", 0), ("LED_EFFECT", 0)]


@pytest.mark.asyncio
async def test_led_controller_updates_idle_soft_mute_context(tmp_path: Path):
    controller, calls = make_controller(tmp_path)

    await controller.update_context(stage="idle", soft_muted=True)

    assert calls[-3:] == [
        ("LED_COLOR", COLOR_WARNING),
        ("LED_BRIGHTNESS", 80),
        ("LED_EFFECT", 3),
    ]


@pytest.mark.asyncio
async def test_led_controller_updates_idle_paused_context(tmp_path: Path):
    controller, calls = make_controller(tmp_path)

    await controller.update_context(stage="idle", soft_muted=True, attention_mode="paused")

    assert calls[-3:] == [
        ("LED_COLOR", COLOR_ERROR),
        ("LED_BRIGHTNESS", 80),
        ("LED_EFFECT", 3),
    ]


@pytest.mark.asyncio
async def test_led_controller_applies_new_stage(tmp_path: Path):
    controller, calls = make_controller(tmp_path)

    await controller.set_stage("idle")
    await controller.set_stage("listening")

    assert calls[-3:] == [
        ("LED_COLOR", COLOR_BLUE),
        ("LED_BRIGHTNESS", 80),
        ("LED_EFFECT", 3),
    ]


@pytest.mark.asyncio
async def test_led_controller_applies_effect_last_for_fade(tmp_path: Path):
    controller, calls = make_controller(tmp_path)

    await controller.set_stage("speaking")

    assert calls == [
        ("LED_COLOR", COLOR_GREEN),
        ("LED_BRIGHTNESS", 80),
        ("LED_SPEED", 1),
        ("LED_EFFECT", 1),
    ]


@pytest.mark.asyncio
async def test_led_controller_disconnected_turns_off(tmp_path: Path):
    calls: list[tuple[str, int]] = []

    def runner(command: str, value: int) -> None:
        calls.append((command, value))

    host = tmp_path / "xvf_host"
    host.write_text("")

    controller = SatelliteLedController(
        SatelliteConfig(
            led_enabled=True,
            xvf_host_path=str(host),
        ),
        runner=runner,
    )

    await controller.set_stage("listening")
    await controller.set_disconnected()

    assert calls[-1] == ("LED_EFFECT", 0)


@pytest.mark.asyncio
async def test_led_controller_disabled_is_noop():
    controller = SatelliteLedController(
        SatelliteConfig(led_enabled=False, xvf_host_path="/missing/xvf_host"),
    )
    assert controller.available is False
    await controller.set_stage("speaking")


@pytest.mark.asyncio
async def test_led_controller_fail_soft_on_runner_error(tmp_path: Path):
    host = tmp_path / "xvf_host"
    host.write_text("")

    def runner(command: str, value: int) -> None:
        raise RuntimeError("usb unavailable")

    controller = SatelliteLedController(
        SatelliteConfig(
            led_enabled=True,
            xvf_host_path=str(host),
        ),
        runner=runner,
    )

    await controller.set_stage("idle")
    assert controller.available is False
    await controller.set_stage("listening")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_led_controller_recovers_with_recreated_runner(tmp_path: Path):
    calls: list[tuple[str, int]] = []
    failing = CloseableFailingRunner()

    def working_runner(command: str, value: int) -> None:
        calls.append((command, value))

    runners = [failing, working_runner]

    controller = SatelliteLedController(
        SatelliteConfig(
            led_enabled=True,
            xvf_host_path=str(tmp_path / "xvf_host"),
        ),
        runner_factory=lambda: runners.pop(0),
    )

    await controller.set_stage("idle")
    assert controller.available is False
    assert failing.closed is True

    await controller.reassert()

    assert controller.available is True
    assert calls == [("LED_EFFECT", 0)]
