"""Authoritative Jarvis Host readiness gate."""

from __future__ import annotations

import logging
import os
from typing import Optional

from core.credentials.store import credential_store
from core.setup.lanes import compute_capability_lanes
from core.setup.llm_config import resolve_llm_config, resolve_llm_config_sync
from core.setup.models import (
    LlmSetupStatus,
    ReadinessPhase,
    RuntimeRole,
    ServiceStatus,
    SetupStateResponse,
)
from core.setup.placeholders import is_placeholder_api_key
from core.setup.runtime import jarvis_runtime
from core.voice.service import get_voice_input_status, get_voice_output_status
from services.database.mongodb import mongodb

logger = logging.getLogger(__name__)


def _service_recovery_action() -> str:
    if os.environ.get("JARVIS_APP_MODE") == "1":
        return "Restart JARV1S. If this continues, open the desktop app logs."
    return "Start Docker and run `task db`."


class SetupNotReadyError(Exception):
    def __init__(self, message: str, *, code: str = "setup_required", next_action: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.next_action = next_action


def get_readiness_phase() -> ReadinessPhase:
    if jarvis_runtime.initializing:
        return ReadinessPhase.INITIALIZING

    config = resolve_llm_config_sync()
    if not config.attemptable:
        return ReadinessPhase.NEEDS_SETUP
    if jarvis_runtime.core_ready:
        return ReadinessPhase.READY
    return ReadinessPhase.DEGRADED


def require_llm_ready() -> None:
    """Fail closed when Jarvis cannot run LLM-backed turns."""
    phase = get_readiness_phase()
    if phase == ReadinessPhase.INITIALIZING:
        raise SetupNotReadyError(
            "JARV1S is still starting up.",
            code="initializing",
            next_action="Wait a moment and try again.",
        )

    config = resolve_llm_config_sync()
    if not config.attemptable:
        raise SetupNotReadyError(
            "Configure your language model provider before chatting.",
            code="missing_llm_key",
            next_action="Open setup and configure your assistant brain.",
        )
    if phase == ReadinessPhase.DEGRADED:
        raise SetupNotReadyError(
            "Jarvis language model server is not reachable.",
            code="llm_unreachable",
            next_action="Start your local model server or check your provider settings, then retry.",
        )
    if not jarvis_runtime.core_ready:
        raise SetupNotReadyError(
            "Jarvis runtime is not ready yet.",
            code="runtime_not_ready",
            next_action="Complete setup or run runtime initialization.",
        )


async def build_setup_state() -> SetupStateResponse:
    db_up = await mongodb.health_check()

    llm_config = await resolve_llm_config()
    phase = get_readiness_phase()
    core_ready = phase == ReadinessPhase.READY

    blocking_reason: Optional[str] = None
    next_action: Optional[str] = None
    if not db_up:
        blocking_reason = "Database is not reachable."
        next_action = _service_recovery_action()
    elif not llm_config.attemptable:
        if llm_config.requires_api_key and is_placeholder_api_key(llm_config.api_key):
            blocking_reason = "Language model API key is missing or still a placeholder."
            next_action = "Choose a provider and paste a real API key."
        else:
            blocking_reason = "Language model is not configured."
            next_action = "Open setup and configure your assistant brain."
    elif phase == ReadinessPhase.DEGRADED:
        blocking_reason = "Language model is configured but not reachable."
        next_action = "Start your local model server or verify your provider endpoint, then retry."
    elif not jarvis_runtime.core_ready:
        blocking_reason = "Jarvis runtime has not finished initializing."
        next_action = "Save your provider settings and initialize the runtime."

    voice_input_status = await get_voice_input_status()
    voice_input_ready = voice_input_status.ready
    apple_speech_healthy = (
        voice_input_ready if voice_input_status.provider == "apple_speech" else None
    )
    voice_output_status = await get_voice_output_status()
    local_tts_healthy = (
        voice_output_status.ready if voice_output_status.provider == "local" else None
    )

    return SetupStateResponse(
        role=RuntimeRole.HOST_LOCAL,
        phase=phase,
        core_ready=core_ready,
        chat_enabled=core_ready and db_up,
        voice_enabled=core_ready and voice_input_ready,
        action_enabled=core_ready and db_up and bool(llm_config.action_capable),
        services=[
            ServiceStatus(name="database", status="up" if db_up else "down"),
            ServiceStatus(
                name="llm",
                status="up" if core_ready else ("not_configured" if not llm_config.attemptable else "down"),
            ),
            ServiceStatus(
                name="voice",
                status="up" if voice_input_ready else "optional",
                detail=(
                    voice_input_status.detail
                    or ("Voice input ready" if voice_input_ready else "Optional — configure later in Settings")
                ),
            ),
        ],
        llm=LlmSetupStatus(
            provider=llm_config.provider,
            configured=llm_config.attemptable,
            source=llm_config.key_mode.value if llm_config.key_mode else llm_config.source.value,
            masked_suffix=credential_store.mask_secret(llm_config.api_key) if llm_config.requires_api_key else None,
            model=llm_config.model or None,
            action_capable=llm_config.action_capable,
        ),
        capability_lanes=compute_capability_lanes(
            apple_speech_healthy=apple_speech_healthy,
            local_tts_healthy=local_tts_healthy,
        ),
        blocking_reason=blocking_reason,
        next_action=next_action,
    )
