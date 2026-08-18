import pytest

from core.llm.providers import get_llm_provider
from core.setup.local_llm import (
    _models_from_ollama_tags,
    _models_from_openai_list,
    discover_local_llm_runtimes,
)
from core.setup.llm_config import (
    LlmConfigSource,
    LOCAL_DUMMY_API_KEY,
    llm_config_store,
    resolve_llm_config_sync,
)
from core.setup.models import ConfigureLlmRequest, SetupStateResponse
from core.setup.validation import validate_llm_credentials


def test_local_presets_do_not_require_api_key():
    for name in ("ollama", "lmstudio", "llamacpp"):
        preset = get_llm_provider(name)
        assert preset.requires_api_key is False
        assert preset.credential_names == ()


def test_models_from_ollama_tags_maps_names():
    models = _models_from_ollama_tags({"models": [{"name": "qwen3:8b"}, {"name": "llama3.2"}]})
    assert models == ["qwen3:8b", "llama3.2"]


def test_models_from_openai_list_maps_ids():
    models = _models_from_openai_list({"data": [{"id": "local-model"}, {"id": "qwen3-8b"}]})
    assert models == ["local-model", "qwen3-8b"]


@pytest.mark.asyncio
async def test_discover_local_llm_runtimes_returns_all_targets(monkeypatch):
    async def _fake_probe(target):
        from core.setup.models import LocalLlmRuntime

        return LocalLlmRuntime(
            runtime=target["runtime"],
            label=target["label"],
            base_url=target["base_url"],
            reachable=target["runtime"] == "ollama",
            models=["qwen3:8b"] if target["runtime"] == "ollama" else [],
        )

    monkeypatch.setattr("core.setup.local_llm._probe_target", _fake_probe)
    runtimes = await discover_local_llm_runtimes()
    assert {runtime.runtime for runtime in runtimes} == {"ollama", "lmstudio", "llamacpp"}
    ollama = next(runtime for runtime in runtimes if runtime.runtime == "ollama")
    assert ollama.reachable is True
    assert ollama.models == ["qwen3:8b"]


def test_resolve_llm_config_uses_persisted_cache(monkeypatch):
    llm_config_store._cache = {
        "provider": "ollama",
        "model": "qwen3:8b",
        "base_url": "http://127.0.0.1:11434/v1",
    }
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    config = resolve_llm_config_sync()
    assert config.provider == "ollama"
    assert config.model == "qwen3:8b"
    assert config.source == LlmConfigSource.PERSISTED
    assert config.api_key == LOCAL_DUMMY_API_KEY
    assert config.attemptable is True
    llm_config_store.clear_cache()


def test_provider_env_vars_do_not_make_setup_attemptable(monkeypatch):
    llm_config_store.clear_cache()
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-key-12345678")

    config = resolve_llm_config_sync()

    assert config.source == LlmConfigSource.DEFAULT
    assert config.attemptable is False


def test_cloud_key_from_credential_store_makes_persisted_config_attemptable(monkeypatch):
    llm_config_store._cache = {
        "provider": "openrouter",
        "model": "google/gemma-4-26b-a4b-it",
        "base_url": "https://openrouter.ai/api/v1",
    }
    monkeypatch.setattr(
        "core.credentials.store.credential_store.get_stored_secret",
        lambda name: "sk-stored-key-12345678" if name == "OPENROUTER_API_KEY" else None,
    )
    monkeypatch.setattr(
        "core.credentials.store.credential_store.mode_for_stored_secret",
        lambda name: None,
    )

    config = resolve_llm_config_sync()

    assert config.source == LlmConfigSource.PERSISTED
    assert config.api_key == "sk-stored-key-12345678"
    assert config.attemptable is True
    llm_config_store.clear_cache()


@pytest.mark.asyncio
async def test_configure_llm_saves_without_initializing_runtime(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    async def _save(*, provider: str, model: str, base_url: str) -> None:
        calls.append((provider, model, base_url))

    async def _build_state() -> SetupStateResponse:
        from core.setup.models import LlmSetupStatus, ReadinessPhase

        return SetupStateResponse(
            phase=ReadinessPhase.NEEDS_SETUP,
            core_ready=False,
            chat_enabled=False,
            voice_enabled=False,
            llm=LlmSetupStatus(provider="ollama", configured=True, model="qwen3:8b"),
            capability_lanes=[],
        )

    async def _unexpected_init(*_args, **_kwargs):
        raise AssertionError("configure_llm should not initialize runtime")

    async def _validate(**_kwargs):
        from core.setup.models import ValidationResult

        return ValidationResult(ok=True, message="ok")

    monkeypatch.setattr("core.setup.service.llm_config_store.save", _save)
    monkeypatch.setattr("core.setup.service.build_setup_state", _build_state)
    monkeypatch.setattr("core.setup.service.jarvis_runtime.initialize_if_ready", _unexpected_init)
    monkeypatch.setattr("core.setup.service.validate_llm_credentials", _validate)

    from core.setup.service import configure_llm

    await configure_llm(
        ConfigureLlmRequest(
            provider="ollama",
            model="qwen3:8b",
            base_url="http://127.0.0.1:11434/v1",
        )
    )

    assert calls == [("ollama", "qwen3:8b", "http://127.0.0.1:11434/v1")]


@pytest.mark.asyncio
async def test_validate_local_llm_skips_api_key(monkeypatch):
    class _FakeLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def initialize(self):
            return None

        @property
        def is_initialized(self):
            return True

        async def chat(self, *_args, **_kwargs):
            return "ok"

    monkeypatch.setattr("core.setup.validation.LLMService", _FakeLLM)
    monkeypatch.setattr(
        "core.setup.validation.httpx.AsyncClient", lambda *args, **kwargs: _AsyncClient()
    )

    result = await validate_llm_credentials(
        provider="ollama",
        api_key="",
        model="qwen3:8b",
        base_url="http://127.0.0.1:11434/v1",
    )
    assert result.ok is True


class _AsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        class _Response:
            status_code = 200

            def json(self):
                return {"data": [{"id": "qwen3:8b"}]}

        return _Response()
