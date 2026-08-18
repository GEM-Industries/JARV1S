import pytest

from core.llm.providers import get_llm_provider
from core.setup.models import (
    ConfigureLlmRequest,
    LlmSetupStatus,
    ReadinessPhase,
    SetupStateResponse,
    ValidationFailureCode,
    ValidationResult,
)
from core.setup.service import configure_llm, list_llm_providers, run_llm_credential_check


def test_cerebras_provider_preset():
    preset = get_llm_provider("cerebras")
    assert preset.base_url == "https://api.cerebras.ai/v1"
    assert preset.model == "gemma-4-31b"
    assert preset.credential_names == ("CEREBRAS_API_KEY",)
    assert preset.label == "Cerebras"
    assert preset.signup_url
    assert preset.stability == "preview"


def test_google_ai_studio_recommended_model():
    preset = get_llm_provider("google-ai-studio")
    assert preset.label == "Google AI Studio"
    assert preset.model == "gemini-3.5-flash"
    assert preset.stability == "stable"


def test_list_llm_providers_includes_key_status(monkeypatch):
    monkeypatch.setattr(
        "core.setup.service.credential_store.resolve_llm_api_key",
        lambda provider: {
            "openrouter": "sk-openrouter-12345678",
            "deepinfra": None,
        }.get(provider),
    )
    monkeypatch.setattr(
        "core.setup.service.credential_store.mask_secret",
        lambda value: f"…{value[-4:]}" if value else None,
    )

    providers = {provider.id: provider for provider in list_llm_providers()}
    assert providers["openrouter"].key_stored is True
    assert providers["openrouter"].masked_suffix == "…5678"
    assert providers["deepinfra"].key_stored is False
    assert providers["cerebras"].key_stored is False
    assert providers["cerebras"].stability == "preview"
    assert providers["cerebras"].recommended_model == "gemma-4-31b"
    assert providers["google-ai-studio"].signup_url == "https://aistudio.google.com/apikey"


@pytest.mark.asyncio
async def test_configure_llm_uses_stored_key_when_api_key_omitted(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    set_secret_calls: list[str] = []

    async def _save(*, provider: str, model: str, base_url: str) -> None:
        calls.append((provider, model, base_url))

    async def _build_state() -> SetupStateResponse:
        return SetupStateResponse(
            phase=ReadinessPhase.READY,
            core_ready=True,
            chat_enabled=True,
            voice_enabled=False,
            llm=LlmSetupStatus(
                provider="deepinfra", configured=True, model="google/gemma-4-26B-A4B-it"
            ),
            capability_lanes=[],
        )

    async def _validate(**_kwargs) -> ValidationResult:
        return ValidationResult(ok=True, message="ok")

    monkeypatch.setattr(
        "core.setup.service.credential_store.resolve_llm_api_key",
        lambda provider: "sk-stored-deepinfra-12345678" if provider == "deepinfra" else None,
    )
    monkeypatch.setattr(
        "core.setup.service.credential_store.set_secret",
        lambda env, value: set_secret_calls.append(env),
    )
    monkeypatch.setattr("core.setup.service.llm_config_store.save", _save)
    monkeypatch.setattr("core.setup.service.build_setup_state", _build_state)
    monkeypatch.setattr("core.setup.service.validate_llm_credentials", _validate)

    await configure_llm(
        ConfigureLlmRequest(
            provider="deepinfra",
            model="google/gemma-4-26B-A4B-it",
        )
    )

    assert calls == [
        ("deepinfra", "google/gemma-4-26B-A4B-it", "https://api.deepinfra.com/v1/openai")
    ]
    assert set_secret_calls == []


@pytest.mark.asyncio
async def test_configure_llm_does_not_persist_failed_validation(monkeypatch):
    saves: list[str] = []
    secrets: list[str] = []

    async def _validate(**_kwargs) -> ValidationResult:
        return ValidationResult(
            ok=False,
            code=ValidationFailureCode.MODEL_UNAVAILABLE,
            message="not found",
            recommended_model="gemini-3.5-flash",
        )

    async def _save(**_kwargs) -> None:
        saves.append("config")

    monkeypatch.setattr("core.setup.service.validate_llm_credentials", _validate)
    monkeypatch.setattr("core.setup.service.llm_config_store.save", _save)
    monkeypatch.setattr(
        "core.setup.service.credential_store.set_secret",
        lambda *_args, **_kwargs: secrets.append("key"),
    )

    from core.setup.service import LlmConfigurationValidationError

    with pytest.raises(LlmConfigurationValidationError) as exc:
        await configure_llm(
            ConfigureLlmRequest(
                provider="google-ai-studio",
                api_key="valid-looking-key",
                model="missing-model",
            )
        )

    assert exc.value.result.recommended_model == "gemini-3.5-flash"
    assert saves == []
    assert secrets == []


@pytest.mark.asyncio
async def test_configure_llm_restores_previous_key_when_config_save_fails(monkeypatch):
    secrets = {"GOOGLE_AI_STUDIO_API_KEY": "previous-key"}

    async def _validate(**_kwargs) -> ValidationResult:
        return ValidationResult(ok=True, message="ok")

    async def _save(**_kwargs) -> None:
        raise RuntimeError("config write failed")

    monkeypatch.setattr("core.setup.service.validate_llm_credentials", _validate)
    monkeypatch.setattr("core.setup.service.llm_config_store.save", _save)
    monkeypatch.setattr(
        "core.setup.service.credential_store.get_stored_secret",
        lambda name: secrets.get(name),
    )
    monkeypatch.setattr(
        "core.setup.service.credential_store.set_secret",
        lambda name, value: secrets.__setitem__(name, value),
    )

    with pytest.raises(RuntimeError, match="config write failed"):
        await configure_llm(
            ConfigureLlmRequest(
                provider="google-ai-studio",
                api_key="replacement-key",
            )
        )

    assert secrets["GOOGLE_AI_STUDIO_API_KEY"] == "previous-key"


@pytest.mark.asyncio
async def test_configure_llm_requires_key_when_none_stored(monkeypatch):
    monkeypatch.setattr(
        "core.setup.service.credential_store.resolve_llm_api_key",
        lambda _provider: None,
    )

    with pytest.raises(ValueError, match="API key is required"):
        await configure_llm(ConfigureLlmRequest(provider="deepinfra"))


@pytest.mark.asyncio
async def test_run_llm_credential_check_uses_stored_key(monkeypatch):
    captured: dict[str, str] = {}

    async def _validate(*, provider: str, api_key: str, model=None, base_url=None):
        captured["provider"] = provider
        captured["api_key"] = api_key
        from core.setup.models import ValidationResult

        return ValidationResult(ok=True, message="ok")

    monkeypatch.setattr(
        "core.setup.service.credential_store.resolve_llm_api_key",
        lambda provider: "sk-stored-openrouter-12345678" if provider == "openrouter" else None,
    )
    monkeypatch.setattr("core.setup.service.validate_llm_credentials", _validate)

    result = await run_llm_credential_check(provider="openrouter", api_key="")

    assert result.ok is True
    assert captured["api_key"] == "sk-stored-openrouter-12345678"


@pytest.mark.asyncio
async def test_model_switch_reads_vault_stored_llm_keys(monkeypatch, tmp_path):
    cred_dir = tmp_path / "credentials"
    monkeypatch.setattr("core.credentials.store._CREDENTIALS_DIR", cred_dir)
    monkeypatch.setattr("core.credentials.store._ENCRYPTED_FILE", cred_dir / "secrets.enc")
    monkeypatch.setattr("core.credentials.store._SALT_FILE", cred_dir / "secrets.salt")
    monkeypatch.setenv("JARVIS_CREDENTIAL_PASSPHRASE", "model-switch-passphrase")

    from core.credentials.store import CredentialStore

    store = CredentialStore()
    monkeypatch.setattr("core.setup.service.credential_store", store)

    store.set_secret("OPENROUTER_API_KEY", "sk-openrouter-12345678")
    store.set_secret("CEREBRAS_API_KEY", "sk-cerebras-12345678")

    providers = {provider.id: provider for provider in list_llm_providers()}
    assert providers["openrouter"].key_stored is True
    assert providers["cerebras"].key_stored is True
    assert store.resolve_llm_api_key("openrouter") == "sk-openrouter-12345678"
    assert store.resolve_llm_api_key("cerebras") == "sk-cerebras-12345678"
