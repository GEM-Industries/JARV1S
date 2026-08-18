"""API routes package."""

from api.routes.base import router as base_router
from api.routes.auth import router as auth_router
from api.routes.history import router as history_router
from api.routes.webhooks import router as webhooks_router
from api.routes.integrations import router as integrations_router
from api.routes.push import router as push_router
from api.routes.tasks import router as tasks_router
from api.routes.snapshots import router as snapshots_router
from api.routes.activity import router as activity_router
from api.routes.automations import router as automations_router
from api.routes.protocols import router as protocols_router
from api.routes.schedules import router as schedules_router
from api.routes.operations import router as operations_router
from api.routes.device_auth import router as device_auth_router
from api.routes.setup import router as setup_router
from api.routes.smart_home import router as smart_home_router
from api.routes.presence import router as presence_router
from api.routes.preferences import router as preferences_router
from api.routes.credentials import router as credentials_router
from api.routes.voice import router as voice_router
from api.routes.ingress import router as ingress_router

__all__ = [
    "base_router",
    "auth_router",
    "history_router",
    "webhooks_router",
    "integrations_router",
    "push_router",
    "tasks_router",
    "snapshots_router",
    "activity_router",
    "automations_router",
    "protocols_router",
    "schedules_router",
    "operations_router",
    "device_auth_router",
    "setup_router",
    "smart_home_router",
    "presence_router",
    "preferences_router",
    "credentials_router",
    "voice_router",
    "ingress_router",
]
