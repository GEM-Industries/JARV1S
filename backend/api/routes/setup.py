from fastapi import APIRouter, Depends, HTTPException

from api.deps.device_auth import require_device
from core.setup.managed_local_llm import ManagedLlmStatus
from core.setup.models import (
    ActivateLlmResponse,
    ConfigureLlmRequest,
    LlmProviderOption,
    LocalLlmRuntime,
    RuntimeInitResponse,
    SetupStateResponse,
    ValidationResult,
)
from core.setup import service as setup_service

router = APIRouter(
    prefix="/setup",
    tags=["setup"],
    dependencies=[Depends(require_device)],
)


@router.get("/state", response_model=SetupStateResponse)
async def get_setup_state() -> SetupStateResponse:
    return await setup_service.get_setup_state()


@router.get("/providers", response_model=list[LlmProviderOption])
async def list_providers() -> list[LlmProviderOption]:
    return setup_service.list_llm_providers()


@router.get("/llm/local/discover", response_model=list[LocalLlmRuntime])
async def discover_local_llms() -> list[LocalLlmRuntime]:
    return await setup_service.discover_local_llms()


@router.get("/llm/local/managed/status", response_model=ManagedLlmStatus)
async def managed_local_status() -> ManagedLlmStatus:
    return await setup_service.managed_local_status()


@router.post("/llm/local/managed/install", response_model=ManagedLlmStatus)
async def managed_local_install() -> ManagedLlmStatus:
    try:
        return await setup_service.managed_local_install()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm/local/managed/cancel", response_model=ManagedLlmStatus)
async def managed_local_cancel() -> ManagedLlmStatus:
    try:
        return await setup_service.managed_local_cancel()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/llm/local/managed/model", response_model=ManagedLlmStatus)
async def managed_local_remove() -> ManagedLlmStatus:
    try:
        return await setup_service.managed_local_remove()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm/local/managed/activate", response_model=ActivateLlmResponse)
async def activate_managed_local() -> ActivateLlmResponse:
    try:
        return await setup_service.activate_managed_local()
    except setup_service.LlmConfigurationValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.result.model_dump(mode="json")) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm/activate", response_model=ActivateLlmResponse)
async def activate_llm(request: ConfigureLlmRequest) -> ActivateLlmResponse:
    try:
        return await setup_service.activate_llm(request)
    except setup_service.LlmConfigurationValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.result.model_dump(mode="json")) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/llm/test", response_model=ValidationResult)
async def test_llm(request: ConfigureLlmRequest) -> ValidationResult:
    return await setup_service.run_llm_credential_check(
        provider=request.provider,
        api_key=request.api_key,
        model=request.model,
        base_url=request.base_url,
    )


@router.post("/runtime/initialize", response_model=RuntimeInitResponse)
async def initialize_runtime() -> RuntimeInitResponse:
    return await setup_service.initialize_runtime()
