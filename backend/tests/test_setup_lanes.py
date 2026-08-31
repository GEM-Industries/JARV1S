import json

from core.integrations.manager import IntegrationManager
from core.setup.lanes import compute_capability_lanes
from core.setup.runtime import jarvis_runtime
from tests.test_setup_helpers import _local_config, _unconfigured_config


def test_weather_lane_ready_when_integration_registered():
    manager = IntegrationManager()

    def _factory(_config):
        return object()

    manager.register("weather", _factory, config_keys=[])

    import core.setup.lanes as lanes_mod

    original = lanes_mod.integrations
    lanes_mod.integrations = manager
    try:
        lanes = compute_capability_lanes()
    finally:
        lanes_mod.integrations = original

    weather = next(lane for lane in lanes if lane.id == "weather")
    assert weather.status == "ready"
    assert weather.lane_type == "keyless"
    assert "Open-Meteo" in (weather.detail or "")


def test_llm_lane_needs_action_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.resolve_llm_config_sync",
        lambda: _unconfigured_config(),
    )
    llm = next(lane for lane in compute_capability_lanes() if lane.id == "llm")
    assert llm.status == "needs_action"


def test_llm_lane_degraded_when_local_configured_but_runtime_down(monkeypatch):
    monkeypatch.setattr("core.setup.lanes.resolve_llm_config_sync", lambda: _local_config())
    jarvis_runtime.core_ready = False
    llm = next(lane for lane in compute_capability_lanes() if lane.id == "llm")
    assert llm.status == "degraded"
    assert llm.lane_type == "local_service"


def test_voice_output_lane_optional_without_cartesia(monkeypatch):
    monkeypatch.setattr("core.setup.lanes.credential_store.get_stored_secret", lambda _name: None)
    voice_output = next(lane for lane in compute_capability_lanes() if lane.id == "voice_output")
    assert voice_output.status == "optional"
    assert "Text replies" in (voice_output.detail or "")


def test_voice_output_lane_needs_voice_after_key_is_stored(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: "key" if name == "CARTESIA_API_KEY" else None,
    )
    monkeypatch.setattr(
        "core.setup.lanes.resolve_voice_config_sync",
        lambda: type(
            "VoiceConfig",
            (),
            {
                "stt_provider": "apple_speech",
                "tts_provider": "cartesia",
                "cartesia_voice_id": None,
                "local_voice_id": "af_heart",
            },
        )(),
    )

    voice_output = next(lane for lane in compute_capability_lanes() if lane.id == "voice_output")

    assert voice_output.status == "needs_action"
    assert "Clone or select" in (voice_output.detail or "")


def test_voice_output_lane_degraded_when_local_helper_down(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.resolve_voice_config_sync",
        lambda: type(
            "VoiceConfig",
            (),
            {
                "stt_provider": "apple_speech",
                "tts_provider": "local",
                "cartesia_voice_id": None,
                "local_voice_id": "af_heart",
            },
        )(),
    )

    voice_output = next(
        lane
        for lane in compute_capability_lanes(local_tts_healthy=False)
        if lane.id == "voice_output"
    )
    assert voice_output.status == "degraded"
    assert voice_output.lane_type == "local_service"


def test_voice_input_lane_ready_when_apple_speech_healthy(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.resolve_voice_config_sync",
        lambda: type(
            "VoiceConfig",
            (),
            {
                "stt_provider": "apple_speech",
                "tts_provider": "off",
                "cartesia_voice_id": None,
                "local_voice_id": "af_heart",
            },
        )(),
    )

    voice_input = next(
        lane
        for lane in compute_capability_lanes(apple_speech_healthy=True)
        if lane.id == "voice_input"
    )

    assert voice_input.status == "ready"
    assert "Apple Speech" in (voice_input.detail or "")


def test_voice_input_lane_degraded_until_local_probe_succeeds(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.resolve_voice_config_sync",
        lambda: type(
            "VoiceConfig",
            (),
            {
                "stt_provider": "apple_speech",
                "tts_provider": "off",
                "cartesia_voice_id": None,
                "local_voice_id": "af_heart",
            },
        )(),
    )

    voice_input = next(
        lane for lane in compute_capability_lanes() if lane.id == "voice_input"
    )

    assert voice_input.status == "degraded"


def test_search_lane_ready_without_optional_providers(monkeypatch):
    monkeypatch.setattr("plugins.search.client.settings.SEARXNG_URL", None)
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda _name: None,
    )

    lanes = compute_capability_lanes()
    search = next(lane for lane in lanes if lane.id == "search")
    assert search.status == "ready"
    assert search.lane_type == "keyless"
    assert "Built-in web search" in (search.detail or "")


def test_integrations_lane_ready_when_product_oauth_configured(monkeypatch, tmp_path):
    path = tmp_path / "product_oauth.json"
    path.write_text(
        json.dumps({"google": {"client_id": "cid", "client_secret": "secret"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_PRODUCT_OAUTH", str(path))
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: None,
    )

    lanes = compute_capability_lanes()
    integrations = next(lane for lane in lanes if lane.id == "integrations")
    assert integrations.status == "ready"
    assert integrations.lane_type == "oauth_consent"
    assert "Google or Microsoft" in (integrations.detail or "")


def test_search_lane_ready_when_searxng_configured(monkeypatch):
    monkeypatch.setattr(
        "plugins.search.client.settings.SEARXNG_URL",
        "http://127.0.0.1:8080",
    )
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda _name: None,
    )

    lanes = compute_capability_lanes()
    search = next(lane for lane in lanes if lane.id == "search")
    assert search.status == "ready"
    assert search.lane_type == "local_service"
    assert "SearXNG" in (search.detail or "")


def test_search_lane_configured_when_exa_key_present(monkeypatch):
    monkeypatch.setattr("plugins.search.client.settings.SEARXNG_URL", None)
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: "exa-test-key" if name == "EXA_API_KEY" else None,
    )

    lanes = compute_capability_lanes()
    search = next(lane for lane in lanes if lane.id == "search")
    assert search.status == "configured"
    assert search.lane_type == "api_key_optional"
    assert "Exa" in (search.detail or "")


def test_search_lane_configured_when_both_providers(monkeypatch):
    monkeypatch.setattr(
        "plugins.search.client.settings.SEARXNG_URL",
        "http://127.0.0.1:8080",
    )
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: "exa-test-key" if name == "EXA_API_KEY" else None,
    )

    lanes = compute_capability_lanes()
    search = next(lane for lane in lanes if lane.id == "search")
    assert search.status == "configured"
    assert "SearXNG" in (search.detail or "")
    assert "Exa" in (search.detail or "")


def test_background_agents_lane_optional_without_key(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: None,
    )

    background_agents = next(
        lane for lane in compute_capability_lanes() if lane.id == "background_agents"
    )

    assert background_agents.status == "optional"
    assert "Anthropic" in (background_agents.detail or "")


def test_background_agents_lane_configured_when_cursor_connected(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: "cursor-key" if name == "CURSOR_API_KEY" else None,
    )
    monkeypatch.setattr(jarvis_runtime, "background_agent_ready", False)

    background_agents = next(
        lane for lane in compute_capability_lanes() if lane.id == "background_agents"
    )

    assert background_agents.status == "configured"
    assert "Cursor" in (background_agents.detail or "")


def test_background_agents_lane_configured_when_ready(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: "sk-ant-test-key" if name == "ANTHROPIC_API_KEY" else None,
    )
    monkeypatch.setattr(jarvis_runtime, "background_agent_ready", True)
    monkeypatch.setattr(jarvis_runtime, "background_agent_last_error", None)

    background_agents = next(
        lane for lane in compute_capability_lanes() if lane.id == "background_agents"
    )

    assert background_agents.status == "configured"
    assert background_agents.lane_type == "api_key_optional"


def test_background_agents_lane_degraded_when_key_fails(monkeypatch):
    monkeypatch.setattr(
        "core.setup.lanes.credential_store.get_stored_secret",
        lambda name: "sk-ant-test-key" if name == "ANTHROPIC_API_KEY" else None,
    )
    monkeypatch.setattr(jarvis_runtime, "background_agent_ready", False)
    monkeypatch.setattr(jarvis_runtime, "background_agent_last_error", "bad key")

    background_agents = next(
        lane for lane in compute_capability_lanes() if lane.id == "background_agents"
    )

    assert background_agents.status == "degraded"
    assert "bad key" in (background_agents.detail or "")
