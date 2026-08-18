from types import SimpleNamespace

import pytest

from core.setup.models import ReadinessPhase
from core.setup.readiness import (
    SetupNotReadyError,
    _service_recovery_action,
    get_readiness_phase,
    require_llm_ready,
)
from core.setup.runtime import jarvis_runtime
from tests.test_setup_helpers import _cloud_config, _local_config, _unconfigured_config


@pytest.fixture(autouse=True)
def reset_runtime():
    jarvis_runtime.core_ready = False
    jarvis_runtime.initializing = False
    jarvis_runtime.last_error = None
    yield


def test_require_llm_ready_missing_key(monkeypatch):
    monkeypatch.setattr(
        "core.setup.readiness.resolve_llm_config_sync",
        lambda: _unconfigured_config(),
    )
    with pytest.raises(SetupNotReadyError) as exc:
        require_llm_ready()
    assert exc.value.code == "missing_llm_key"


def test_require_llm_ready_initializing(monkeypatch):
    jarvis_runtime.initializing = True
    monkeypatch.setattr(
        "core.setup.readiness.resolve_llm_config_sync",
        lambda: _cloud_config(),
    )
    with pytest.raises(SetupNotReadyError) as exc:
        require_llm_ready()
    assert exc.value.code == "initializing"


def test_require_llm_ready_degraded_when_runtime_unreachable(monkeypatch):
    monkeypatch.setattr(
        "core.setup.readiness.resolve_llm_config_sync",
        lambda: _cloud_config(),
    )
    jarvis_runtime.core_ready = False
    with pytest.raises(SetupNotReadyError) as exc:
        require_llm_ready()
    assert exc.value.code == "llm_unreachable"


def test_get_readiness_phase_ready(monkeypatch):
    monkeypatch.setattr(
        "core.setup.readiness.resolve_llm_config_sync",
        lambda: _cloud_config(),
    )
    jarvis_runtime.core_ready = True
    assert get_readiness_phase() == ReadinessPhase.READY


def test_get_readiness_phase_degraded_for_local_without_runtime(monkeypatch):
    monkeypatch.setattr(
        "core.setup.readiness.resolve_llm_config_sync",
        lambda: _local_config(),
    )
    jarvis_runtime.core_ready = False
    assert get_readiness_phase() == ReadinessPhase.DEGRADED


def test_service_recovery_action_uses_desktop_copy_in_app_mode(monkeypatch):
    monkeypatch.setenv("JARVIS_APP_MODE", "1")
    assert _service_recovery_action() == (
        "Restart JARV1S. If this continues, open the desktop app logs."
    )


def test_service_recovery_action_preserves_contributor_docker_copy(monkeypatch):
    monkeypatch.delenv("JARVIS_APP_MODE", raising=False)
    assert _service_recovery_action() == "Start Docker and run `task db`."


@pytest.mark.asyncio
async def test_build_setup_state_reports_placeholder(monkeypatch):
    from core.setup.readiness import build_setup_state

    async def _healthy() -> bool:
        return True

    async def _voice_ready():
        return SimpleNamespace(
            provider="apple_speech",
            ready=True,
            detail="On-device Apple Speech",
        )

    def _factory(_config):
        return object()

    monkeypatch.setattr("core.setup.readiness.mongodb.health_check", _healthy)
    monkeypatch.setattr("core.setup.readiness.get_voice_input_status", _voice_ready)
    config = _cloud_config(api_key="your_openrouter_key")
    monkeypatch.setattr("core.setup.readiness.resolve_llm_config_sync", lambda: config)

    async def _resolve():
        return config

    monkeypatch.setattr("core.setup.readiness.resolve_llm_config", _resolve)

    import core.integrations.manager as manager_mod

    test_manager = manager_mod.IntegrationManager()
    test_manager.register("weather", _factory, config_keys=[])
    test_manager.register("search", _factory, config_keys=["EXA_API_KEY"])
    monkeypatch.setattr("core.setup.lanes.integrations", test_manager)

    state = await build_setup_state()
    assert state.phase == ReadinessPhase.NEEDS_SETUP
    assert state.core_ready is False
    assert state.llm.configured is False
    assert "placeholder" in (state.blocking_reason or "").lower()

    weather_lane = next(lane for lane in state.capability_lanes if lane.id == "weather")
    assert weather_lane.status == "ready"
    assert weather_lane.lane_type == "keyless"
