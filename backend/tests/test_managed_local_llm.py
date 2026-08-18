import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from core.setup import managed_local_llm as managed
from core.setup.llm_config import LlmConfigSource, ResolvedLlmConfig, llm_config_store
from core.setup.models import ConfigureLlmRequest


def test_load_manifest_has_isolated_endpoint():
    managed.load_manifest.cache_clear()
    manifest = managed.load_manifest()
    assert manifest.base_url.endswith(":11435/v1")
    assert manifest.model_id == "gemma4:e4b-mlx"
    assert manifest.min_memory_bytes >= 16 * 1024**3
    assert "gemma4:12b-mlx" in manifest.supersedes
    assert manifest.model_id not in manifest.supersedes


def test_parse_supersedes_skips_current_and_duplicates():
    assert managed._parse_supersedes(
        {"supersedes": ["gemma4:12b-mlx", "gemma4:e4b-mlx", "gemma4:12b-mlx", ""]},
        "gemma4:e4b-mlx",
    ) == ("gemma4:12b-mlx",)
    assert managed._parse_supersedes({}, "gemma4:e4b-mlx") == ()


def test_machine_preflight_rejects_low_memory(monkeypatch):
    class _Mem:
        total = 8 * 1024**3

    class _Disk:
        free = 100 * 1024**3

    monkeypatch.setattr(managed.psutil, "virtual_memory", lambda: _Mem())
    monkeypatch.setattr(managed.psutil, "disk_usage", lambda _path: _Disk())
    supported, detail, *_ = managed.machine_preflight()
    assert supported is False
    assert "memory" in detail.lower()


def test_is_managed_active_config_matches_manifest():
    manifest = managed.load_manifest()
    assert managed.is_managed_active_config(
        provider="ollama",
        base_url=manifest.base_url,
        model=manifest.model_id,
    )
    assert not managed.is_managed_active_config(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model=manifest.model_id,
    )


@pytest.mark.asyncio
async def test_status_runtime_down_without_env(monkeypatch):
    monkeypatch.delenv("JARVIS_MANAGED_LLM_URL", raising=False)
    monkeypatch.setattr(
        managed,
        "machine_preflight",
        lambda _m=None: (True, "", 32 * 1024**3, 100 * 1024**3),
    )
    status = await managed.get_managed_status()
    assert status.status == "runtime_down"
    assert status.runtime_ready is False


@pytest.mark.asyncio
async def test_status_ready_when_model_present(monkeypatch):
    manifest = managed.load_manifest()
    monkeypatch.setenv("JARVIS_MANAGED_LLM_URL", manifest.native_api_base)
    monkeypatch.setattr(
        managed,
        "machine_preflight",
        lambda _m=None: (True, "", 32 * 1024**3, 100 * 1024**3),
    )

    async def _tags(_base):
        return {
            "models": [
                {
                    "name": manifest.model_id,
                    "digest": "sha256:abc",
                    "size": 8_800_000_000,
                }
            ]
        }

    monkeypatch.setattr(managed, "_fetch_tags", _tags)
    monkeypatch.setattr(
        managed,
        "resolve_llm_config_sync",
        lambda: ResolvedLlmConfig(
            provider="ollama",
            model=manifest.model_id,
            base_url=manifest.base_url,
            requires_api_key=False,
            api_key="local",
            source=LlmConfigSource.PERSISTED,
        ),
    )
    managed._pull.status = "absent"
    managed._pull.last_error = ""
    managed._pull.task = None
    status = await managed.get_managed_status()
    assert status.status == "ready"
    assert status.model_installed is True
    assert status.active is True


@pytest.mark.asyncio
async def test_installed_model_supersedes_stale_pull_failure(monkeypatch):
    manifest = managed.load_manifest()
    monkeypatch.setenv("JARVIS_MANAGED_LLM_URL", manifest.native_api_base)
    monkeypatch.setattr(
        managed,
        "machine_preflight",
        lambda _m=None: (True, "", 32 * 1024**3, 100 * 1024**3),
    )

    async def _tags(_base):
        return {"models": [{"name": manifest.model_id, "digest": "sha256:valid"}]}

    monkeypatch.setattr(managed, "_fetch_tags", _tags)
    monkeypatch.setattr(managed, "resolve_llm_config_sync", _resolved_openrouter)
    managed._pull.status = "failed"
    managed._pull.last_error = "transient validation failure"
    managed._pull.task = None

    status = await managed.get_managed_status()

    assert status.status == "ready"


@pytest.mark.asyncio
async def test_status_rejects_mismatched_model_digest(monkeypatch):
    manifest = replace(managed.load_manifest(), model_digest="sha256:expected")
    monkeypatch.setattr(managed, "load_manifest", lambda: manifest)
    monkeypatch.setenv("JARVIS_MANAGED_LLM_URL", manifest.native_api_base)
    monkeypatch.setattr(
        managed,
        "machine_preflight",
        lambda _m=None: (True, "", 32 * 1024**3, 100 * 1024**3),
    )

    async def _tags(_base):
        return {
            "models": [
                {
                    "name": manifest.model_id,
                    "digest": "sha256:unexpected",
                    "size": 8_800_000_000,
                }
            ]
        }

    monkeypatch.setattr(managed, "_fetch_tags", _tags)
    monkeypatch.setattr(managed, "resolve_llm_config_sync", _resolved_openrouter)
    managed._pull.status = "absent"
    managed._pull.last_error = ""
    managed._pull.task = None

    status = await managed.get_managed_status()

    assert status.status == "failed"
    assert "digest" in (status.detail or "").lower()


@pytest.mark.asyncio
async def test_remove_requires_inactive_managed_config(monkeypatch):
    manifest = managed.load_manifest()
    monkeypatch.setenv("JARVIS_MANAGED_LLM_URL", manifest.native_api_base)
    monkeypatch.setattr(
        managed,
        "resolve_llm_config_sync",
        lambda: ResolvedLlmConfig(
            provider="ollama",
            model=manifest.model_id,
            base_url=manifest.base_url,
            requires_api_key=False,
            api_key="local",
            source=LlmConfigSource.PERSISTED,
        ),
    )
    with pytest.raises(ValueError, match="Switch to another"):
        await managed.remove_managed_model()


@pytest.mark.asyncio
async def test_remove_during_download_pauses_pull(monkeypatch):
    managed._pull.status = "downloading"
    managed._pull.detail = "pulling"

    async def _never_finish():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            managed._mark_pull_paused()
            raise

    managed._pull.task = asyncio.create_task(_never_finish())
    monkeypatch.setattr(
        managed,
        "get_managed_status",
        AsyncMock(
            return_value=managed.ManagedLlmStatus(
                status="absent",
                model_id="gemma4:e4b-mlx",
                model_label="Gemma 4 E4B",
                detail=managed._DOWNLOAD_PAUSED_DETAIL,
            )
        ),
    )
    try:
        status = await managed.remove_managed_model()
        assert status.status == "absent"
        assert "paused" in (status.detail or "").lower()
        assert managed._pull.task is None or managed._pull.task.done()
    finally:
        if managed._pull.task and not managed._pull.task.done():
            managed._pull.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await managed._pull.task
        managed._pull.task = None
        managed._pull.status = "absent"


@pytest.mark.asyncio
async def test_cancel_managed_install_marks_paused(monkeypatch):
    async def _never_finish():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            managed._mark_pull_paused()
            raise

    managed._pull.task = asyncio.create_task(_never_finish())
    managed._pull.status = "downloading"
    monkeypatch.setattr(
        managed,
        "get_managed_status",
        AsyncMock(
            return_value=managed.ManagedLlmStatus(
                status="absent",
                model_id="gemma4:e4b-mlx",
                model_label="Gemma 4 E4B",
                detail=managed._DOWNLOAD_PAUSED_DETAIL,
            )
        ),
    )
    try:
        status = await managed.cancel_managed_install()
        assert status.status == "absent"
        assert managed._pull.detail == managed._DOWNLOAD_PAUSED_DETAIL
    finally:
        managed._pull.task = None
        managed._pull.status = "absent"


@pytest.mark.asyncio
async def test_status_preserves_paused_detail(monkeypatch):
    manifest = managed.load_manifest()
    monkeypatch.setenv("JARVIS_MANAGED_LLM_URL", manifest.native_api_base)
    monkeypatch.setattr(
        managed,
        "machine_preflight",
        lambda _m=None: (True, "", 32 * 1024**3, 100 * 1024**3),
    )
    monkeypatch.setattr(managed, "_fetch_tags", AsyncMock(return_value={"models": []}))
    monkeypatch.setattr(managed, "resolve_llm_config_sync", _resolved_openrouter)
    managed._pull.status = "absent"
    managed._pull.detail = managed._DOWNLOAD_PAUSED_DETAIL
    managed._pull.last_error = ""
    managed._pull.task = None

    status = await managed.get_managed_status()

    assert status.status == "absent"
    assert "paused" in (status.detail or "").lower()


@pytest.mark.asyncio
async def test_purge_superseded_models_uses_manifest(monkeypatch):
    manifest = replace(managed.load_manifest(), supersedes=("old-baseline:tag",))
    monkeypatch.setattr(managed, "load_manifest", lambda: manifest)
    monkeypatch.setattr(managed, "managed_runtime_env_ready", lambda: True)

    async def _tags(_base):
        return {"models": [{"name": "old-baseline:tag", "size": 1}]}

    deleted: list[str] = []

    async def _delete(_base, model_id):
        deleted.append(model_id)

    monkeypatch.setattr(managed, "_fetch_tags", _tags)
    monkeypatch.setattr(managed, "_delete_ollama_model", _delete)

    await managed.purge_superseded_models()

    assert deleted == ["old-baseline:tag"]

@pytest.mark.asyncio
async def test_switching_from_managed_local_unloads_model_after_activation(monkeypatch):
    from core.setup import service as setup_service
    from core.setup.models import LlmSetupStatus, SetupStateResponse
    from core.setup.readiness import ReadinessPhase

    manifest = managed.load_manifest()
    previous = ResolvedLlmConfig(
        provider="ollama",
        model=manifest.model_id,
        base_url=manifest.base_url,
        requires_api_key=False,
        api_key="local",
        source=LlmConfigSource.PERSISTED,
    )
    current = _resolved_openrouter()
    resolved = iter((previous, current))

    async def _resolve():
        return next(resolved)

    class _Runtime:
        last_error = None

        async def initialize_if_ready(self, *, force: bool = False):
            return True

    state = SetupStateResponse(
        phase=ReadinessPhase.READY,
        core_ready=True,
        chat_enabled=True,
        voice_enabled=True,
        llm=LlmSetupStatus(provider=current.provider, configured=True, model=current.model),
    )
    unload = AsyncMock()
    monkeypatch.setattr(setup_service, "resolve_llm_config", _resolve)
    monkeypatch.setattr(setup_service, "configure_llm", AsyncMock())
    monkeypatch.setattr(setup_service, "jarvis_runtime", _Runtime())
    monkeypatch.setattr(setup_service, "unload_managed_model", unload)
    monkeypatch.setattr(setup_service, "build_setup_state", AsyncMock(return_value=state))
    monkeypatch.setattr(setup_service, "get_readiness_phase", lambda: ReadinessPhase.READY)

    result = await setup_service.activate_llm(
        ConfigureLlmRequest(provider="openrouter", model=current.model)
    )

    assert result.core_ready is True
    unload.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_activate_llm_rolls_back_config_and_key_on_init_failure(monkeypatch):
    from core.setup import service as setup_service

    secrets = {"OPENROUTER_API_KEY": "previous-key"}
    llm_config_store._cache = {
        "provider": "openrouter",
        "model": "google/gemma-4-26b-a4b-it",
        "base_url": "https://openrouter.ai/api/v1",
    }

    async def _fake_configure(request: ConfigureLlmRequest):
        if request.api_key:
            setup_service.credential_store.set_secret("OPENROUTER_API_KEY", request.api_key)
        await llm_config_store.save(
            provider=request.provider,
            model=request.model or "x",
            base_url=request.base_url or "http://127.0.0.1:11435/v1",
        )
        from core.setup.models import SetupStateResponse
        from core.setup.readiness import ReadinessPhase
        from core.setup.models import LlmSetupStatus

        return SetupStateResponse(
            phase=ReadinessPhase.NEEDS_SETUP,
            core_ready=False,
            chat_enabled=False,
            voice_enabled=False,
            llm=LlmSetupStatus(provider=request.provider, configured=True, model=request.model),
        )

    class _Runtime:
        core_ready = False
        last_error = "boom"

        async def initialize_if_ready(self, *, force: bool = False):
            return False

    async def _resolve_previous():
        return _resolved_openrouter()

    monkeypatch.setattr(setup_service, "configure_llm", _fake_configure)
    monkeypatch.setattr(setup_service, "jarvis_runtime", _Runtime())
    monkeypatch.setattr(setup_service, "resolve_llm_config", _resolve_previous)
    monkeypatch.setattr(
        setup_service.credential_store,
        "get_stored_secret",
        lambda name: secrets.get(name),
    )
    monkeypatch.setattr(
        setup_service.credential_store,
        "set_secret",
        lambda name, value: secrets.__setitem__(name, value),
    )
    monkeypatch.setattr(
        setup_service.credential_store,
        "delete_secret",
        lambda name: secrets.pop(name, None),
    )

    async def _build_state():
        from core.setup.models import LlmSetupStatus, SetupStateResponse
        from core.setup.readiness import ReadinessPhase

        cached = llm_config_store._cache or {}
        return SetupStateResponse(
            phase=ReadinessPhase.DEGRADED,
            core_ready=False,
            chat_enabled=False,
            voice_enabled=False,
            llm=LlmSetupStatus(
                provider=cached.get("provider", ""),
                configured=True,
                model=cached.get("model"),
            ),
        )

    monkeypatch.setattr(setup_service, "build_setup_state", _build_state)
    monkeypatch.setattr(setup_service, "get_readiness_phase", lambda: __import__(
        "core.setup.readiness", fromlist=["ReadinessPhase"]
    ).ReadinessPhase.DEGRADED)

    saved = {}

    async def _save(**kwargs):
        saved.update(kwargs)
        llm_config_store._cache = dict(kwargs)

    monkeypatch.setattr(llm_config_store, "save", _save)

    result = await setup_service.activate_llm(
        ConfigureLlmRequest(
            provider="openrouter",
            api_key="replacement-key",
            model="different-model",
        )
    )
    assert result.core_ready is False
    assert saved["provider"] == "openrouter"
    assert saved["model"] == "google/gemma-4-26b-a4b-it"
    assert secrets["OPENROUTER_API_KEY"] == "previous-key"


def _resolved_openrouter() -> ResolvedLlmConfig:
    return ResolvedLlmConfig(
        provider="openrouter",
        model="google/gemma-4-26b-a4b-it",
        base_url="https://openrouter.ai/api/v1",
        requires_api_key=True,
        api_key="sk-test-12345678",
        source=LlmConfigSource.PERSISTED,
    )
