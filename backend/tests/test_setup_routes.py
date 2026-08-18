from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.routes.setup as setup_routes
from core.setup.models import (
    ConfigureLlmRequest,
    ReadinessPhase,
    RuntimeRole,
    SetupStateResponse,
    ValidationFailureCode,
    ValidationResult,
)


def _state() -> SetupStateResponse:
    return SetupStateResponse(
        role=RuntimeRole.HOST_LOCAL,
        phase=ReadinessPhase.NEEDS_SETUP,
        core_ready=False,
        chat_enabled=False,
        voice_enabled=False,
        llm={"provider": "openrouter", "configured": False},
    )


@pytest.mark.asyncio
async def test_get_setup_state_delegates_to_service(monkeypatch):
    async def fake_get_setup_state():
        return _state()

    monkeypatch.setattr(setup_routes.setup_service, "get_setup_state", fake_get_setup_state)

    result = await setup_routes.get_setup_state()

    assert result.role == RuntimeRole.HOST_LOCAL
    assert result.core_ready is False


@pytest.mark.asyncio
async def test_activate_llm_maps_store_errors_to_bad_request(monkeypatch):
    async def fake_activate_llm(_request):
        raise RuntimeError("Encrypted credential store requires JARVIS_CREDENTIAL_PASSPHRASE")

    monkeypatch.setattr(setup_routes.setup_service, "activate_llm", fake_activate_llm)

    with pytest.raises(HTTPException) as exc:
        await setup_routes.activate_llm(
            ConfigureLlmRequest(provider="openrouter", api_key="sk-test-key")
        )

    assert exc.value.status_code == 400
    assert "JARVIS_CREDENTIAL_PASSPHRASE" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_activate_llm_returns_structured_validation_failure(monkeypatch):
    result = ValidationResult(
        ok=False,
        code=ValidationFailureCode.MODEL_UNAVAILABLE,
        message="Model not found.",
        next_action="Use the recommended model.",
        recommended_model="gemini-3.5-flash",
    )

    async def fake_activate_llm(_request):
        raise setup_routes.setup_service.LlmConfigurationValidationError(result)

    monkeypatch.setattr(setup_routes.setup_service, "activate_llm", fake_activate_llm)

    with pytest.raises(HTTPException) as exc:
        await setup_routes.activate_llm(
            ConfigureLlmRequest(provider="google-ai-studio", api_key="test-key")
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "model_unavailable"
    assert exc.value.detail["recommended_model"] == "gemini-3.5-flash"


@pytest.mark.asyncio
async def test_test_llm_route_passes_request_fields(monkeypatch):
    seen = {}

    async def fake_run_llm_credential_check(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(ok=True, message="ok", code=None, next_action=None)

    monkeypatch.setattr(
        setup_routes.setup_service, "run_llm_credential_check", fake_run_llm_credential_check
    )

    result = await setup_routes.test_llm(
        ConfigureLlmRequest(
            provider="openrouter",
            api_key="sk-test-key",
            model="model-a",
            base_url="https://example.test/v1",
        )
    )

    assert result.ok is True
    assert seen == {
        "provider": "openrouter",
        "api_key": "sk-test-key",
        "model": "model-a",
        "base_url": "https://example.test/v1",
    }
