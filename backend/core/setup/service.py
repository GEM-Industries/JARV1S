"""Jarvis Host setup orchestration."""

from __future__ import annotations

import logging

from core.credentials.store import credential_store
from core.llm.providers import LLM_PROVIDER_PRESETS, get_llm_provider, normalize_llm_provider
from core.setup.llm_config import LlmConfigSource, llm_config_store, resolve_llm_config
from core.setup.local_llm import discover_local_llm_runtimes
from core.setup.managed_local_llm import (
    ManagedLlmStatus,
    cancel_managed_install,
    get_managed_status,
    is_managed_active_config,
    load_manifest,
    remove_managed_model,
    start_managed_install,
    unload_managed_model,
)
from core.setup.models import (
    ActivateLlmResponse,
    ConfigureLlmRequest,
    LlmProviderOption,
    LocalLlmRuntime,
    RuntimeInitResponse,
    SetupStateResponse,
    ValidationResult,
)
from core.setup.readiness import build_setup_state, get_readiness_phase
from core.setup.runtime import jarvis_runtime
from core.setup.validation import validate_llm_credentials

logger = logging.getLogger(__name__)


class LlmConfigurationValidationError(ValueError):
    def __init__(self, result: ValidationResult) -> None:
        super().__init__(result.message)
        self.result = result


async def get_setup_state() -> SetupStateResponse:
    return await build_setup_state()


def _provider_key_status(provider_name: str) -> tuple[bool, str | None]:
    stored = credential_store.resolve_llm_api_key(provider_name)
    if not stored:
        return False, None
    return True, credential_store.mask_secret(stored)


def _resolve_api_key(provider_name: str, api_key: str) -> str:
    stripped = api_key.strip()
    if stripped:
        return stripped
    resolved = credential_store.resolve_llm_api_key(provider_name)
    return resolved or ""


def list_llm_providers() -> list[LlmProviderOption]:
    options: list[LlmProviderOption] = []
    for preset in LLM_PROVIDER_PRESETS.values():
        if preset.name == "custom" or not preset.requires_api_key:
            continue
        key_stored, masked_suffix = _provider_key_status(preset.name)
        options.append(
            LlmProviderOption(
                id=preset.name,
                label=preset.label,
                signup_url=preset.signup_url,
                default_model=preset.recommended_model,
                recommended_model=preset.recommended_model,
                stability=preset.stability,
                credential_names=list(preset.credential_names),
                key_stored=key_stored,
                masked_suffix=masked_suffix,
            )
        )
    return options


async def discover_local_llms() -> list[LocalLlmRuntime]:
    return await discover_local_llm_runtimes()


async def configure_llm(request: ConfigureLlmRequest) -> None:
    provider_name = normalize_llm_provider(request.provider)
    preset = get_llm_provider(provider_name)
    model = request.model or preset.model
    base_url = (request.base_url or preset.base_url).rstrip("/")
    if not model:
        raise ValueError("Model is required.")
    if not base_url:
        raise ValueError("Base URL is required.")

    if preset.requires_api_key:
        resolved_key = _resolve_api_key(provider_name, request.api_key)
        if not resolved_key:
            raise ValueError("API key is required for this provider.")
    else:
        resolved_key = ""

    validation = await validate_llm_credentials(
        provider=provider_name,
        api_key=resolved_key,
        model=model,
        base_url=base_url,
    )
    if not validation.ok:
        raise LlmConfigurationValidationError(validation)

    new_key = request.api_key.strip()
    primary_credential = preset.credential_names[0] if preset.credential_names else None
    previous_key = (
        credential_store.get_stored_secret(primary_credential)
        if primary_credential
        else None
    )
    if new_key and primary_credential:
        credential_store.set_secret(primary_credential, new_key)
    try:
        await llm_config_store.save(provider=provider_name, model=model, base_url=base_url)
    except Exception:
        if new_key and primary_credential:
            if previous_key:
                credential_store.set_secret(primary_credential, previous_key)
            else:
                credential_store.delete_secret(primary_credential)
        raise


async def activate_llm(request: ConfigureLlmRequest) -> ActivateLlmResponse:
    """Validate, persist, and initialize atomically; roll back on init failure."""
    previous = await resolve_llm_config()
    previous_snapshot = (
        {
            "provider": previous.provider,
            "model": previous.model,
            "base_url": previous.base_url,
        }
        if previous.source == LlmConfigSource.PERSISTED
        else None
    )
    candidate_provider = normalize_llm_provider(request.provider)
    candidate_preset = get_llm_provider(candidate_provider)
    new_key = request.api_key.strip()
    credential_name = (
        candidate_preset.credential_names[0]
        if new_key and candidate_preset.credential_names
        else None
    )
    previous_candidate_key = (
        credential_store.get_stored_secret(credential_name) if credential_name else None
    )

    await configure_llm(request)
    ok = await jarvis_runtime.initialize_if_ready(force=True)
    if ok:
        current = await resolve_llm_config()
        if (
            is_managed_active_config(
                provider=previous.provider,
                base_url=previous.base_url,
                model=previous.model,
            )
            and not is_managed_active_config(
                provider=current.provider,
                base_url=current.base_url,
                model=current.model,
            )
        ):
            try:
                await unload_managed_model()
            except Exception as exc:
                logger.warning("Could not unload inactive managed model: %s", exc)
        state = await build_setup_state()
        return ActivateLlmResponse(
            phase=get_readiness_phase(),
            core_ready=True,
            message="Model activated.",
            state=state,
        )

    detail = jarvis_runtime.last_error or "Could not initialize the selected model."
    if credential_name:
        if previous_candidate_key:
            credential_store.set_secret(credential_name, previous_candidate_key)
        else:
            credential_store.delete_secret(credential_name)
    if previous_snapshot:
        await llm_config_store.save(**previous_snapshot)
        await jarvis_runtime.initialize_if_ready(force=True)
    else:
        await llm_config_store.delete()
        jarvis_runtime.core_ready = False

    state = await build_setup_state()
    return ActivateLlmResponse(
        phase=get_readiness_phase(),
        core_ready=False,
        message=detail,
        state=state,
    )


async def run_llm_credential_check(
    *,
    provider: str,
    api_key: str,
    model: str | None = None,
    base_url: str | None = None,
) -> ValidationResult:
    provider_name = normalize_llm_provider(provider)
    resolved_key = _resolve_api_key(provider_name, api_key)
    return await validate_llm_credentials(
        provider=provider_name,
        api_key=resolved_key,
        model=model,
        base_url=base_url,
    )


async def initialize_runtime() -> RuntimeInitResponse:
    ok = await jarvis_runtime.initialize_if_ready(force=True)
    phase = get_readiness_phase()
    if ok:
        return RuntimeInitResponse(
            phase=phase,
            core_ready=True,
            message="Jarvis runtime is ready.",
        )
    detail = jarvis_runtime.last_error or "Configure your LLM provider first."
    return RuntimeInitResponse(
        phase=phase,
        core_ready=False,
        message=detail,
    )


async def managed_local_status() -> ManagedLlmStatus:
    return await get_managed_status()


async def managed_local_install() -> ManagedLlmStatus:
    return await start_managed_install()


async def managed_local_cancel() -> ManagedLlmStatus:
    return await cancel_managed_install()


async def managed_local_remove() -> ManagedLlmStatus:
    return await remove_managed_model()


async def activate_managed_local() -> ActivateLlmResponse:
    status = await get_managed_status()
    if status.status != "ready":
        raise ValueError(status.detail or "On-device model is not ready.")
    manifest = load_manifest()
    return await activate_llm(
        ConfigureLlmRequest(
            provider="ollama",
            model=manifest.model_id,
            base_url=manifest.base_url,
            api_key="",
        )
    )
