"""Jarvis Host setup and readiness models."""

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RuntimeRole(str, Enum):
    """Where this process runs relative to the Jarvis Host."""

    HOST_LOCAL = "host_local"
    # Future: CLIENT_REMOTE, SATELLITE_NODE


class ReadinessPhase(str, Enum):
    NEEDS_SETUP = "needs_setup"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"


class ValidationFailureCode(str, Enum):
    MISSING_KEY = "missing_key"
    PLACEHOLDER_KEY = "placeholder_key"
    BAD_KEY = "bad_key"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_OR_BILLING = "quota_or_billing"
    RATE_LIMITED = "rate_limited"
    BAD_ENDPOINT = "bad_endpoint"
    MODEL_UNAVAILABLE = "model_unavailable"
    NETWORK_UNREACHABLE = "network_unreachable"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN = "unknown"


class ServiceStatus(BaseModel):
    name: str
    status: Literal["up", "down", "not_configured", "optional"]
    detail: Optional[str] = None


class LlmSetupStatus(BaseModel):
    provider: str
    configured: bool
    source: Optional[str] = None
    masked_suffix: Optional[str] = None
    model: Optional[str] = None
    action_capable: Optional[bool] = None


class CapabilityLaneStatus(BaseModel):
    id: str
    label: str
    lane_type: Literal[
        "keyless",
        "api_key_optional",
        "oauth_consent",
        "brokered_connect",
        "manual_handoff",
        "local_service",
    ] = "api_key_optional"
    status: Literal[
        "ready",
        "configured",
        "needs_action",
        "degraded",
        "unavailable",
        "optional",
    ]
    detail: Optional[str] = None


class SetupStateResponse(BaseModel):
    role: RuntimeRole = RuntimeRole.HOST_LOCAL
    phase: ReadinessPhase
    core_ready: bool
    chat_enabled: bool
    voice_enabled: bool
    action_enabled: bool = False
    services: list[ServiceStatus] = Field(default_factory=list)
    llm: LlmSetupStatus
    capability_lanes: list[CapabilityLaneStatus] = Field(default_factory=list)
    blocking_reason: Optional[str] = None
    next_action: Optional[str] = None


class LlmProviderOption(BaseModel):
    id: str
    label: str
    signup_url: str
    default_model: str
    recommended_model: str
    stability: Literal["stable", "preview"]
    credential_names: list[str]
    key_stored: bool = False
    masked_suffix: Optional[str] = None


class ConfigureLlmRequest(BaseModel):
    provider: str
    api_key: str = ""
    model: Optional[str] = None
    base_url: Optional[str] = None


class LocalLlmRuntime(BaseModel):
    runtime: str
    label: str
    base_url: str
    reachable: bool
    models: list[str] = Field(default_factory=list)
    detail: Optional[str] = None


class ValidationResult(BaseModel):
    ok: bool
    code: Optional[ValidationFailureCode] = None
    message: str
    next_action: Optional[str] = None
    recommended_model: Optional[str] = None
    action_capable: Optional[bool] = None


class RuntimeInitResponse(BaseModel):
    phase: ReadinessPhase
    core_ready: bool
    message: str


class ActivateLlmResponse(BaseModel):
    phase: ReadinessPhase
    core_ready: bool
    message: str
    state: SetupStateResponse
