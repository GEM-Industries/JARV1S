"""Computed capability setup lanes for first-run and settings surfaces."""

from __future__ import annotations

from core import settings
from core.credentials.store import credential_store
from core.integrations import integrations
from core.llm.providers import LOCAL_LLM_PROVIDERS
from core.setup.llm_config import resolve_llm_config_sync
from core.setup.models import CapabilityLaneStatus
from core.setup.runtime import jarvis_runtime
from core.voice.config import resolve_voice_config_sync


def compute_capability_lanes(
    *,
    apple_speech_healthy: bool | None = None,
    local_tts_healthy: bool | None = None,
) -> list[CapabilityLaneStatus]:
    """Return current capability lane states without live provider probes."""
    llm_config = resolve_llm_config_sync()
    return [
        _llm_lane(llm_config),
        _voice_input_lane(apple_speech_healthy=apple_speech_healthy),
        _voice_output_lane(local_tts_healthy=local_tts_healthy),
        _weather_lane(),
        _search_lane(),
        _background_agents_lane(),
        _smart_home_lane(),
        _integrations_lane(),
        _satellites_lane(),
    ]

def _llm_lane(llm_config) -> CapabilityLaneStatus:
    if not llm_config.attemptable:
        return CapabilityLaneStatus(
            id="llm",
            label="Assistant brain",
            lane_type="local_service" if llm_config.provider in LOCAL_LLM_PROVIDERS else "api_key_optional",
            status="needs_action",
            detail="Configure your language model in setup.",
        )

    lane_type = "local_service" if not llm_config.requires_api_key else "api_key_optional"
    if jarvis_runtime.core_ready:
        detail = f"{llm_config.provider}"
        if llm_config.model:
            detail = f"{llm_config.provider} — {llm_config.model}"
        return CapabilityLaneStatus(
            id="llm",
            label="Assistant brain",
            lane_type=lane_type,
            status="ready" if not llm_config.requires_api_key else "configured",
            detail=detail,
        )

    if llm_config.requires_api_key:
        detail = "Cloud provider configured — initialize runtime to start chatting."
    else:
        detail = "Local model configured but server is not reachable. Start your runtime and retry."

    return CapabilityLaneStatus(
        id="llm",
        label="Assistant brain",
        lane_type=lane_type,
        status="degraded",
        detail=detail,
    )


def _voice_input_lane(*, apple_speech_healthy: bool | None) -> CapabilityLaneStatus:
    voice_config = resolve_voice_config_sync()
    if voice_config.stt_provider == "apple_speech":
        if apple_speech_healthy is not True:
            return CapabilityLaneStatus(
                id="voice_input",
                label="Voice input",
                lane_type="local_service",
                status="degraded",
                detail="On-device Speech is not ready — grant permission or wait for assets",
            )
        return CapabilityLaneStatus(
            id="voice_input",
            label="Voice input",
            lane_type="local_service",
            status="ready",
            detail="On-device Apple Speech",
        )
    cartesia = credential_store.get_stored_secret("CARTESIA_API_KEY")
    if cartesia:
        return CapabilityLaneStatus(
            id="voice_input",
            label="Voice input",
            lane_type="api_key_optional",
            status="configured",
            detail="Cartesia streaming STT configured",
        )
    return CapabilityLaneStatus(
        id="voice_input",
        label="Voice input",
        lane_type="api_key_optional",
        status="optional",
        detail="Use on-device Speech or add Cartesia for cloud voice input",
    )


def _voice_output_lane(*, local_tts_healthy: bool | None = None) -> CapabilityLaneStatus:
    config = resolve_voice_config_sync()
    if config.tts_provider == "off":
        return CapabilityLaneStatus(
            id="voice_output",
            label="Voice output",
            lane_type="api_key_optional",
            status="optional",
            detail="Text replies only — enable Cartesia or On this Mac in Voice settings",
        )
    if config.tts_provider == "local":
        if local_tts_healthy is not True:
            return CapabilityLaneStatus(
                id="voice_output",
                label="Voice output",
                lane_type="local_service",
                status="degraded",
                detail="On-device speech helper is not ready",
            )
        return CapabilityLaneStatus(
            id="voice_output",
            label="Voice output",
            lane_type="local_service",
            status="ready",
            detail="On-device Kokoro TTS",
        )
    cartesia = credential_store.get_stored_secret("CARTESIA_API_KEY")
    if cartesia and config.cartesia_voice_id:
        return CapabilityLaneStatus(
            id="voice_output",
            label="Voice output",
            lane_type="api_key_optional",
            status="configured",
            detail="Cartesia TTS configured",
        )
    if cartesia:
        return CapabilityLaneStatus(
            id="voice_output",
            label="Voice output",
            lane_type="api_key_optional",
            status="needs_action",
            detail="Clone or select a Cartesia voice in Voice settings",
        )
    return CapabilityLaneStatus(
        id="voice_output",
        label="Voice output",
        lane_type="api_key_optional",
        status="needs_action",
        detail="Add a Cartesia key or switch spoken replies Off",
    )


def _weather_lane() -> CapabilityLaneStatus:
    if integrations.is_available("weather"):
        return CapabilityLaneStatus(
            id="weather",
            label="Weather",
            lane_type="keyless",
            status="ready",
            detail="Open-Meteo — no API key required",
        )
    return CapabilityLaneStatus(
        id="weather",
        label="Weather",
        lane_type="keyless",
        status="unavailable",
        detail="Weather plugin not loaded",
    )


def _search_lane() -> CapabilityLaneStatus:
    from plugins.search.client import get_search_provider_status

    status = get_search_provider_status()
    if status.searxng_configured and status.exa_configured:
        return CapabilityLaneStatus(
            id="search",
            label="Web search",
            lane_type="local_service",
            status="configured",
            detail="SearXNG local search with Exa quality upgrade",
        )
    if status.searxng_configured:
        return CapabilityLaneStatus(
            id="search",
            label="Web search",
            lane_type="local_service",
            status="ready",
            detail="SearXNG JSON search configured",
        )
    if status.exa_configured:
        return CapabilityLaneStatus(
            id="search",
            label="Web search",
            lane_type="api_key_optional",
            status="configured",
            detail="Exa search configured",
        )
    return CapabilityLaneStatus(
        id="search",
        label="Web search",
        lane_type="keyless",
        status="ready",
        detail="Built-in web search — no API key required",
    )


def _background_agents_lane() -> CapabilityLaneStatus:
    if not credential_store.get_stored_secret("ANTHROPIC_API_KEY"):
        return CapabilityLaneStatus(
            id="background_agents",
            label="Background agents",
            lane_type="api_key_optional",
            status="optional",
            detail="Add an Anthropic API key in Settings to enable delegated tasks.",
        )
    if jarvis_runtime.background_agent_ready:
        return CapabilityLaneStatus(
            id="background_agents",
            label="Background agents",
            lane_type="api_key_optional",
            status="configured",
            detail=f"Background model configured — {settings.BACKGROUND_AGENT_MODEL}",
        )
    detail = "Anthropic key stored — initialize runtime to enable delegated tasks."
    if jarvis_runtime.background_agent_last_error:
        detail = f"Background agent runtime unavailable: {jarvis_runtime.background_agent_last_error}"
    return CapabilityLaneStatus(
        id="background_agents",
        label="Background agents",
        lane_type="api_key_optional",
        status="degraded",
        detail=detail,
    )


def _smart_home_lane() -> CapabilityLaneStatus:
    from plugins.smart_home.config import is_ha_configured

    if is_ha_configured():
        return CapabilityLaneStatus(
            id="smart_home",
            label="Smart home",
            lane_type="manual_handoff",
            status="configured",
            detail="Home Assistant connected",
        )
    return CapabilityLaneStatus(
        id="smart_home",
        label="Smart home",
        lane_type="manual_handoff",
        status="needs_action",
        detail="Open Home → Home Assistant",
    )


def _integrations_lane() -> CapabilityLaneStatus:
    from core.auth.providers import has_product_metadata

    composio_key = credential_store.get_stored_secret("COMPOSIO_API_KEY")
    google_ready = has_product_metadata("google")
    microsoft_ready = has_product_metadata("microsoft")
    oauth_connectable = google_ready or microsoft_ready

    if composio_key and oauth_connectable:
        return CapabilityLaneStatus(
            id="integrations",
            label="Connected apps",
            lane_type="oauth_consent",
            status="ready",
            detail="Google/Microsoft consent and Composio — connect in Settings",
        )
    if composio_key:
        return CapabilityLaneStatus(
            id="integrations",
            label="Connected apps",
            lane_type="brokered_connect",
            status="configured",
            detail="Composio broker available — connect apps in Settings",
        )
    if oauth_connectable:
        return CapabilityLaneStatus(
            id="integrations",
            label="Connected apps",
            lane_type="oauth_consent",
            status="ready",
            detail="Connect Google or Microsoft in Settings — browser consent only",
        )
    return CapabilityLaneStatus(
        id="integrations",
        label="Connected apps",
        lane_type="oauth_consent",
        status="optional",
        detail="Google/Microsoft OAuth or Composio — configure in Settings",
    )


def _satellites_lane() -> CapabilityLaneStatus:
    return CapabilityLaneStatus(
        id="satellites",
        label="Room satellites",
        lane_type="manual_handoff",
        status="optional",
        detail="Pair room satellites after core setup",
    )
