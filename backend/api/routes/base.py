from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.setup.llm_config import resolve_llm_config
from core.setup.readiness import get_readiness_phase
from core.setup.models import ReadinessPhase
from core.setup.runtime import jarvis_runtime
from core.version import VersionInfo, get_version_info
from services.database.mongodb import mongodb

router = APIRouter()


@router.get("/")
async def root():
    """Root endpoint returning a welcome message."""
    return {"message": "Welcome to Jarvis AI Assistant API"}


@router.get("/health")
async def health_check():
    """Host infrastructure health for launchers and watchdogs.

    Keeps this endpoint cheap: database + readiness only. Do not probe voice
    helpers or other optional capabilities here — those are demand-checked when
    the user enables them. Returns HTTP 503 when the database is down so clients
    that only check status codes fail closed. LLM/setup incompleteness stays
    HTTP 200 with ``needs_setup`` / ``degraded`` so first-run setup remains
    reachable.
    """
    db_up = await mongodb.health_check()
    phase = get_readiness_phase()
    llm_config = await resolve_llm_config()
    core_ready = phase == ReadinessPhase.READY and jarvis_runtime.core_ready
    infra_up = db_up

    if not infra_up:
        overall = "unavailable"
    elif phase == ReadinessPhase.NEEDS_SETUP:
        overall = "needs_setup"
    elif phase == ReadinessPhase.READY and core_ready:
        overall = "healthy"
    else:
        overall = "degraded"

    payload = {
        "status": overall,
        "phase": phase.value,
        "core_ready": core_ready,
        "services": {
            "api": "up",
            "database": "up" if db_up else "down",
            "llm": "up" if llm_config.attemptable and core_ready else "not_configured",
            # Voice is capability-ready, not launch-ready. StatusBar / Start voice
            # probe the helper on demand via /voice/input/status.
            "voice": "optional",
        },
    }
    if not infra_up:
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/version")
async def version() -> VersionInfo:
    """Support/debug metadata for the local Jarvis Host."""
    return get_version_info()
