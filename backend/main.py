import asyncio
import contextlib
import logging
import os
import time

# Set these at the absolute start to ensure they affect all library imports
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from core import settings
from core.setup.llm_config import llm_config_store, resolve_llm_config
from plugins.smart_home.config import ha_config_store
from core.setup.runtime import jarvis_runtime
from core.voice.config import voice_config_store
from core.auth import auth_manager
from core.integrations.composio_gateway import get_composio_gateway
from core.integrations.lifecycle import reconcile_composio_startup
from core.integrations.manager import integrations
from core.integrations.mcp import shutdown_all_mcp_clients
from core.integrations.mcp.bridge import load_mcp_bridges
from core.plugins.registry import registry
from core.tool_router import tool_router
from api.routes import (
    base_router,
    auth_router,
    history_router,
    webhooks_router,
    integrations_router,
    push_router,
    tasks_router,
    snapshots_router,
    activity_router,
    automations_router,
    protocols_router,
    schedules_router,
    operations_router,
    device_auth_router,
    setup_router,
    smart_home_router,
    presence_router,
    preferences_router,
    credentials_router,
    voice_router,
    ingress_router,
)
from api.websockets.routes import router as ws_router
from services.automation import automation_service
from services.database.mongodb import mongodb
from services.diagnostics import diagnostics_service
from services.errors import setup_error_handlers
from services.events import event_bus, Event, EventType
from services.log_buffer import (
    HumanReadableContextFormatter,
    LogContextFilter,
    log_buffer,
)
from services.prefetch import prefetch_service
from services.push.registry import push_registry
from services.inbound_events import inbound_event_service
from core.triggers.scheduler import trigger_scheduler
from core.attention.reconcile import attention_reconcile_service
from services.system_pulse import system_pulse
from core.integrations.external_ingress import external_ingress_store

# Configure logging
logging.basicConfig(
    level=settings.LOG_LEVEL.value,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
root_logger = logging.getLogger()
context_filter = LogContextFilter()
context_formatter = HumanReadableContextFormatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
for handler in root_logger.handlers:
    handler.addFilter(context_filter)
    handler.setFormatter(context_formatter)

# Attach rolling in-memory buffer for diagnostic snapshot capture
if log_buffer not in root_logger.handlers:
    root_logger.addHandler(log_buffer)
# Set debug level for voice module if needed
logging.getLogger('core.voice').setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

# Silence noisy third-party loggers
for noisy_logger in ['numba', 'matplotlib', 'fsspec', 'httpcore', 'httpx', 'openai', 'urllib3', 'huggingface_hub', 'websockets', 'cartesia']:
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

# Extra-aggressive silence for LLM client internals
logging.getLogger("openai._base_client").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_DEFAULT_EXECUTOR_MAX_WORKERS = 8


def _configure_default_executor() -> None:
    """Bound asyncio.to_thread workers used by voice and blocking adapters."""
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(
            max_workers=_DEFAULT_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="jarvis-asyncio",
        )
    )
    logger.info("Default asyncio executor configured: max_workers=%d", _DEFAULT_EXECUTOR_MAX_WORKERS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown.

    Core path (blocks first request): database, config, plugins/tools, LLM runtime.
    Optional services start in the background after the app can accept traffic.
    """
    logger.info("Starting Jarvis AI Assistant...")
    startup_started = time.perf_counter()
    _configure_default_executor()

    try:
        group_started = time.perf_counter()
        await mongodb.connect()
        from core.pending_inputs import cancel_orphaned_pending_inputs
        cancelled_inputs = await cancel_orphaned_pending_inputs()
        if cancelled_inputs:
            logger.warning("Cancelled %d orphaned pending input(s) after restart", cancelled_inputs)
        logger.info(
            "lifespan group database: %.0fms",
            (time.perf_counter() - group_started) * 1000,
        )
    except Exception as e:
        logger.error("Failed to connect to MongoDB: %s", e)

    # Wire AuthManager into the Integration Gate now that MongoDB is connected
    group_started = time.perf_counter()
    integrations.set_auth_manager(auth_manager)
    await llm_config_store.load_persisted()
    await voice_config_store.load_persisted()
    await ha_config_store.load_persisted()
    await external_ingress_store.load()
    logger.info(
        "lifespan group config: %.0fms",
        (time.perf_counter() - group_started) * 1000,
    )

    group_started = time.perf_counter()
    await event_bus.start()

    from core.home import seed_home
    seed_home()

    await registry.load_plugins()
    # Load persisted disabled plugins before tool router embeds utterances,
    # so disabled plugins are excluded from the router's vector index.
    await registry.load_disabled()
    # Auto-bridge MCP servers (packaged mcp_servers.json + home/mcp.json).
    # Runs after bespoke plugins so bespoke always wins on name collision.
    await load_mcp_bridges()
    logger.info(
        "lifespan group plugins: %.0fms",
        (time.perf_counter() - group_started) * 1000,
    )

    group_started = time.perf_counter()
    if (await resolve_llm_config()).attemptable:
        if await jarvis_runtime.initialize_if_ready():
            logger.info("Jarvis runtime ready at startup")
        else:
            await tool_router.initialize(llm_service=None)
            logger.warning("Jarvis Host setup required: %s", jarvis_runtime.last_error)
    else:
        await tool_router.initialize(llm_service=None)
        logger.info("LLM not configured — Jarvis Host setup required before chat")
    logger.info(
        "lifespan group llm_runtime: %.0fms",
        (time.perf_counter() - group_started) * 1000,
    )

    # Last-resort handler: if the async lifespan teardown never completes
    # (e.g. Ctrl-C during shutdown, OOM), SIGKILL any tracked child PIDs.
    def _emergency_reap() -> None:
        from plugins.agents import _force_kill_tracked_children
        _force_kill_tracked_children()

    import atexit
    atexit.register(_emergency_reap)

    background_task = asyncio.create_task(
        _start_background_services(),
        name="jarvis-background-startup",
    )
    logger.info(
        "lifespan core ready: %.0fms (background services starting)",
        (time.perf_counter() - startup_started) * 1000,
    )

    yield

    logger.info("Shutting down Jarvis AI Assistant...")

    if not background_task.done():
        background_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background_task

    await push_registry.stop()
    await inbound_event_service.stop()
    await diagnostics_service.stop()

    # Cancel running background agents before stopping services
    agents = registry.plugins.get("agents")
    if agents:
        await agents.shutdown()

    await prefetch_service.stop()
    await attention_reconcile_service.stop()
    await system_pulse.stop()
    await automation_service.stop()
    await trigger_scheduler.stop()
    await shutdown_all_mcp_clients()

    composio_gw = get_composio_gateway()
    if composio_gw:
        await composio_gw.shutdown()

    await integrations.shutdown()

    await event_bus.publish(Event(type=EventType.SYSTEM_SHUTDOWN, source="api"))
    await event_bus.stop()

    await mongodb.disconnect()


async def _start_background_services() -> None:
    """Optional services that must not delay health/setup reachability.

    Local background loops and Composio tool warm-up run concurrently so a
    returning user can reach connected-app tools sooner without blocking launch.
    """
    started = time.perf_counter()
    try:
        group_names = ("local services", "Composio warm-up")
        results = await asyncio.gather(
            _start_local_background_services(),
            _warm_composio_integrations(),
            return_exceptions=True,
        )
        for name, result in zip(group_names, results, strict=True):
            if isinstance(result, BaseException):
                logger.error("Background startup group failed: %s", name, exc_info=result)
        await event_bus.publish(
            Event(type=EventType.SYSTEM_STARTUP, source="api", data={"version": settings.VERSION})
        )
        logger.info(
            "lifespan group background_services: %.0fms",
            (time.perf_counter() - started) * 1000,
        )
    except asyncio.CancelledError:
        logger.info("Background startup cancelled during shutdown")
        raise
    except Exception:
        logger.exception("Background startup failed")


async def _start_optional_service(
    name: str,
    start: Callable[[], Awaitable[object]],
) -> None:
    try:
        await start()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Optional service failed to start: %s", name)


async def _start_local_background_services() -> None:
    group_started = time.perf_counter()
    services: list[tuple[str, Callable[[], Awaitable[object]]]] = [
        ("trigger scheduler", trigger_scheduler.start),
        ("automation", automation_service.start),
        ("push registry", push_registry.start),
        ("inbound events", inbound_event_service.start),
        ("diagnostics", diagnostics_service.start),
        ("attention reconciliation", attention_reconcile_service.start),
    ]
    if settings.SYSTEM_PULSE_ENABLED:
        services.append(("system pulse", system_pulse.start))
    if settings.PREFETCH_ENABLED:
        from api.websockets.handlers import orchestrator as _voice_orchestrator

        services.append(
            ("prefetch", lambda: prefetch_service.start(_voice_orchestrator))
        )

    for name, start in services:
        await _start_optional_service(name, start)

    # Recover any tasks that were running when the server last restarted
    if mongodb.db is not None:
        await _start_optional_service(
            "background task recovery",
            lambda: mongodb.db.background_tasks.update_many(
                {"status": "running"},
                {"$set": {"status": "failed", "result": "Server restarted during execution"}},
            ),
        )
    logger.info(
        "lifespan group local_background: %.0fms",
        (time.perf_counter() - group_started) * 1000,
    )


async def _warm_composio_integrations() -> None:
    """Best-effort: connected-app tools become available after launch, not before."""
    group_started = time.perf_counter()
    composio_gw = get_composio_gateway()
    if not composio_gw:
        return
    try:
        await composio_gw.ensure_webhook_subscription()
    except Exception as e:
        logger.warning("Composio webhook subscription failed at startup: %s", e)

    # Reload tools for Composio apps connected in a previous session.
    # The OAuth callback only fires for new connections — this catches the rest.
    for result in await reconcile_composio_startup():
        logger.info("Composio startup: '%s' — %s", result.name, result.message)
    logger.info(
        "lifespan group composio_warmup: %.0fms",
        (time.perf_counter() - group_started) * 1000,
    )

def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        title=settings.PROJECT_NAME,
        description=settings.DESCRIPTION,
        version=settings.VERSION,
        openapi_url=(
            None
            if settings.is_production
            else f"{settings.API_V1_STR}/openapi.json"
        ),
        docs_url=None if settings.is_production else f"{settings.API_V1_STR}/docs",
        redoc_url=None if settings.is_production else f"{settings.API_V1_STR}/redoc",
        debug=settings.DEBUG,
        lifespan=lifespan
    )

    # Configure CORS
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Set up error handlers
    setup_error_handlers(application)

    # Include routers
    application.include_router(base_router, prefix=settings.API_V1_STR)
    application.include_router(auth_router, prefix=settings.API_V1_STR)
    application.include_router(device_auth_router, prefix=settings.API_V1_STR)
    application.include_router(history_router, prefix=settings.API_V1_STR)
    application.include_router(webhooks_router, prefix=settings.API_V1_STR)
    application.include_router(integrations_router, prefix=settings.API_V1_STR)
    application.include_router(push_router, prefix=settings.API_V1_STR)
    application.include_router(tasks_router, prefix=settings.API_V1_STR)
    application.include_router(activity_router, prefix=settings.API_V1_STR)
    application.include_router(automations_router, prefix=settings.API_V1_STR)
    application.include_router(protocols_router, prefix=settings.API_V1_STR)
    application.include_router(schedules_router, prefix=settings.API_V1_STR)
    application.include_router(operations_router, prefix=settings.API_V1_STR)
    application.include_router(snapshots_router, prefix=settings.API_V1_STR)
    application.include_router(setup_router, prefix=settings.API_V1_STR)
    application.include_router(smart_home_router, prefix=settings.API_V1_STR)
    application.include_router(presence_router, prefix=settings.API_V1_STR)
    application.include_router(preferences_router, prefix=settings.API_V1_STR)
    application.include_router(credentials_router, prefix=settings.API_V1_STR)
    application.include_router(voice_router, prefix=settings.API_V1_STR)
    application.include_router(ingress_router, prefix=settings.API_V1_STR)
    application.include_router(ws_router, prefix=settings.API_V1_STR)

    frontend_path = Path(__file__).parent.parent / "frontend"
    dist_path = Path(os.environ["JARVIS_FRONTEND_DIST"]) if os.environ.get("JARVIS_FRONTEND_DIST") else frontend_path / "dist"
    serve_app_mode = os.environ.get("JARVIS_APP_MODE", "0") == "1"

    if serve_app_mode and dist_path.exists():
        assets_path = dist_path / "assets"
        if assets_path.exists():
            application.mount("/assets", StaticFiles(directory=str(assets_path)), name="assets")
        sounds_path = dist_path / "sounds"
        if sounds_path.exists():
            application.mount("/sounds", StaticFiles(directory=str(sounds_path)), name="sounds")

        @application.get("/")
        async def spa_index():
            return FileResponse(
                dist_path / "index.html",
                headers={"Cache-Control": "no-store"},
            )

        @application.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            candidate = dist_path / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(
                dist_path / "index.html",
                headers={"Cache-Control": "no-store"},
            )
    else:
        static_dir = dist_path if dist_path.exists() else frontend_path
        application.mount("/static", StaticFiles(directory=str(static_dir), html=True), name="static")

    @application.middleware("http")
    async def log_requests(request: Request, call_next: Callable) -> Response:
        logger.debug("Request: %s %s", request.method, request.url.path)
        response = await call_next(request)
        logger.debug("Response: %s", response.status_code)
        return response

    return application

app = create_application()
