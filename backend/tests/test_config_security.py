import pytest

from core.config import EnvironmentType, Settings


def test_production_forces_device_auth_and_safe_cors() -> None:
    settings = Settings(
        ENVIRONMENT=EnvironmentType.PRODUCTION,
        DEVICE_AUTH_REQUIRED=False,
        DEVICE_AUTH_DEV_BYPASS=True,
        FRONTEND_ORIGIN="https://jarvis.example.ts.net",
        BACKEND_CORS_ORIGINS=["*"],
        CORS_ORIGINS=[],
    )

    assert settings.DEVICE_AUTH_REQUIRED is True
    assert settings.DEVICE_AUTH_DEV_BYPASS is False
    assert settings.BACKEND_CORS_ORIGINS == ["https://jarvis.example.ts.net"]
    assert settings.CORS_ORIGINS == ["https://jarvis.example.ts.net"]


def test_production_disables_api_documentation(monkeypatch) -> None:
    from core.config import settings
    from main import create_application

    monkeypatch.setattr(settings, "ENVIRONMENT", EnvironmentType.PRODUCTION)

    application = create_application()

    assert application.openapi_url is None
    assert application.docs_url is None
    assert application.redoc_url is None


def test_anthropic_api_key_is_optional_contributor_override() -> None:
    settings = Settings(ANTHROPIC_API_KEY="sk-ant-env-only")
    assert "ANTHROPIC_API_KEY" in Settings.model_fields
    assert settings.ANTHROPIC_API_KEY == "sk-ant-env-only"
