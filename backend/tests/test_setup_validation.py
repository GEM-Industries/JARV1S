import httpx
import pytest
from openai import NotFoundError

from core.setup.models import ValidationFailureCode
from core.setup.validation import validate_llm_credentials


@pytest.mark.asyncio
async def test_validate_missing_key():
    result = await validate_llm_credentials(provider="openrouter", api_key="")
    assert result.ok is False
    assert result.code == ValidationFailureCode.MISSING_KEY


@pytest.mark.asyncio
async def test_validate_placeholder_key():
    result = await validate_llm_credentials(provider="openrouter", api_key="your_openrouter_key")
    assert result.ok is False
    assert result.code == ValidationFailureCode.PLACEHOLDER_KEY


@pytest.mark.asyncio
async def test_validate_bad_endpoint():
    result = await validate_llm_credentials(
        provider="openrouter",
        api_key="sk-test-key-12345678",
        base_url="https://openrouter.ai/api/v1/chat/completions",
    )
    assert result.ok is False
    assert result.code == ValidationFailureCode.BAD_ENDPOINT


@pytest.mark.asyncio
async def test_validation_passes_explicit_provider_name(monkeypatch):
    captured = {}

    class _Response:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return _Response()

    class _Llm:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def initialize(self):
            return None

        async def chat(self, *_args, **_kwargs):
            return "ok"

    monkeypatch.setattr("core.setup.validation.httpx.AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr("core.setup.validation.LLMService", _Llm)

    result = await validate_llm_credentials(
        provider="google-ai-studio",
        api_key="valid-looking-key",
    )

    assert result.ok is True
    assert captured["provider_name"] == "google-ai-studio"
    assert captured["model"] == "gemini-3.5-flash"


@pytest.mark.asyncio
async def test_model_unavailable_returns_recommended_model(monkeypatch):
    class _Response:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return _Response()

    class _Llm:
        def __init__(self, **_kwargs):
            pass

        async def initialize(self):
            return None

        async def chat(self, *_args, **_kwargs):
            raise NotFoundError(
                "missing",
                response=httpx.Response(404, request=httpx.Request("POST", "https://example.test")),
                body=None,
            )

    monkeypatch.setattr("core.setup.validation.httpx.AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr("core.setup.validation.LLMService", _Llm)

    result = await validate_llm_credentials(
        provider="google-ai-studio",
        api_key="valid-looking-key",
        model="removed-model",
    )

    assert result.code == ValidationFailureCode.MODEL_UNAVAILABLE
    assert result.recommended_model == "gemini-3.5-flash"
