import pytest
import httpx

from core.credentials import service as credentials_service
from core.credentials.models import CredentialCardStatus
from core.credentials.store import CredentialStore
from core.integrations.composio_gateway import get_composio_gateway, reset_composio_gateway
from core.integrations.external_ingress import ExternalIngressState
from core.integrations import external_ingress


@pytest.fixture(autouse=True)
def stub_external_ingress(monkeypatch):
    async def get_state():
        return ExternalIngressState()

    monkeypatch.setattr(external_ingress, "get_external_ingress_state", get_state)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    cred_dir = tmp_path / "credentials"
    monkeypatch.setattr("core.credentials.store._CREDENTIALS_DIR", cred_dir)
    monkeypatch.setattr("core.credentials.store._ENCRYPTED_FILE", cred_dir / "secrets.enc")
    monkeypatch.setattr("core.credentials.store._SALT_FILE", cred_dir / "secrets.salt")
    monkeypatch.setenv("JARVIS_CREDENTIAL_PASSPHRASE", "test-passphrase")
    store = CredentialStore()
    monkeypatch.setattr("core.credentials.service.credential_store", store)
    monkeypatch.setattr("core.integrations.composio_gateway.credential_store", store)
    return store


@pytest.mark.asyncio
async def test_list_masks_stored_secret(isolated_store):
    isolated_store.set_secret("EXA_API_KEY", "exa-test-key-12345678")
    isolated_store.set_secret("ANTHROPIC_API_KEY", "sk-ant-test-key-12345678")

    response = await credentials_service.list_credentials()
    exa = next(item for item in response.items if item.id == "exa")
    background_agents = next(item for item in response.items if item.id == "background_agents")

    assert exa.status == CredentialCardStatus.STORED
    assert exa.masked_suffix == "…5678"
    assert exa.detail == "Exa search upgrade is active."
    assert background_agents.status == CredentialCardStatus.STORED
    assert background_agents.masked_suffix == "…5678"
    assert background_agents.detail == "Background agent runtime is available."
    cursor_coding = next(item for item in response.items if item.id == "cursor_coding")
    assert cursor_coding.status == CredentialCardStatus.MISSING
    assert cursor_coding.docs_url == "https://cursor.com/dashboard/integrations"
    assert cursor_coding.docs_label == "Create API key"
    assert "New code work will use Cursor" in cursor_coding.description
    assert "external triggers" in response.external_triggers.detail.lower()


@pytest.mark.asyncio
async def test_env_only_reports_deprecated(isolated_store, monkeypatch):
    monkeypatch.setattr(isolated_store, "_read_env", lambda name: "exa-env-key-12345678" if name == "EXA_API_KEY" else None)

    response = await credentials_service.list_credentials()
    exa = next(item for item in response.items if item.id == "exa")

    assert exa.status == CredentialCardStatus.ENV_DEPRECATED
    assert exa.source == "env"


def test_anthropic_key_resolves_for_background_agents(isolated_store):
    isolated_store._read_env = lambda _name: None
    isolated_store.set_secret("ANTHROPIC_API_KEY", "sk-ant-current")

    assert isolated_store.resolve_llm_api_key("anthropic") == "sk-ant-current"


@pytest.mark.asyncio
async def test_save_exa_resets_search_integration(isolated_store, monkeypatch):
    reset_calls: list[str | None] = []

    async def fake_reset(name=None):
        reset_calls.append(name)

    monkeypatch.setattr("core.credentials.service.integrations.reset", fake_reset)

    result = await credentials_service.save_credential("exa", "exa-test-key-12345678")

    assert result.ok is True
    assert result.card.status == CredentialCardStatus.STORED
    assert reset_calls == ["search"]
    assert isolated_store.get_stored_secret("EXA_API_KEY") == "exa-test-key-12345678"


@pytest.mark.asyncio
async def test_save_composio_resets_gateway(isolated_store, monkeypatch):
    reset_calls: list[str | None] = []

    async def fake_reset(name=None):
        reset_calls.append(name)

    monkeypatch.setattr("core.credentials.service.integrations.reset", fake_reset)
    await reset_composio_gateway()

    await credentials_service.save_credential("composio", "ak_test_composio_key_1234")

    assert get_composio_gateway() is not None
    assert reset_calls == [None]


@pytest.mark.asyncio
async def test_save_background_agents_reinitializes_runtime(isolated_store, monkeypatch):
    initialize_calls: list[bool] = []

    async def fake_initialize_if_ready(*, force=False):
        initialize_calls.append(force)
        return True

    monkeypatch.setattr(
        "core.setup.runtime.jarvis_runtime.initialize_if_ready",
        fake_initialize_if_ready,
    )

    result = await credentials_service.save_credential(
        "background_agents",
        "sk-ant-test-key-12345678",
    )

    assert result.ok is True
    assert result.card.status == CredentialCardStatus.STORED
    assert initialize_calls == [True]
    assert isolated_store.get_stored_secret("ANTHROPIC_API_KEY") == "sk-ant-test-key-12345678"


@pytest.mark.asyncio
async def test_cartesia_key_restarts_tts_client(isolated_store, monkeypatch):
    from api.websockets.handlers import tts

    calls: list[str] = []

    async def fake_ensure():
        return None

    async def fake_close():
        calls.append("close")

    async def fake_initialize():
        calls.append("initialize")
        return True

    async def fake_validate(_credential_id, _value):
        return type("Validation", (), {"ok": True, "message": "Validated"})()

    monkeypatch.setattr(credentials_service, "ensure_voice_config_available", fake_ensure)
    monkeypatch.setattr(credentials_service, "validate_credential", fake_validate)
    monkeypatch.setattr(
        "core.voice.config.resolve_voice_config_sync",
        lambda: type("Cfg", (), {"tts_provider": "cartesia"})(),
    )
    monkeypatch.setattr(tts, "close", fake_close)
    monkeypatch.setattr(tts, "initialize", fake_initialize)

    await credentials_service.save_credential("cartesia", "sk_car_test_key_12345678")
    await credentials_service.remove_credential("cartesia")

    assert calls == ["close", "initialize", "close"]


@pytest.mark.asyncio
async def test_cartesia_validation_probes_provider(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        credentials_service.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await credentials_service.validate_credential(
        "cartesia", "sk_car_test_key_12345678"
    )

    assert result.ok is True
    assert requests[0].headers["x-api-key"] == "sk_car_test_key_12345678"


@pytest.mark.asyncio
async def test_cartesia_validation_rejects_bad_key(monkeypatch):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": "unauthorized"})
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        credentials_service.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await credentials_service.validate_credential(
        "cartesia", "sk_car_bad_key_12345678"
    )

    assert result.ok is False
    assert "rejected" in result.message


@pytest.mark.asyncio
async def test_invalid_cartesia_key_is_not_stored(isolated_store, monkeypatch):
    async def reject(_credential_id, _value):
        return type(
            "Validation",
            (),
            {"ok": False, "message": "Cartesia rejected this API key."},
        )()

    monkeypatch.setattr(credentials_service, "validate_credential", reject)

    with pytest.raises(ValueError, match="rejected"):
        await credentials_service.save_credential(
            "cartesia", "sk_car_bad_key_12345678"
        )

    assert isolated_store.get_stored_secret("CARTESIA_API_KEY") is None


@pytest.mark.asyncio
async def test_remove_credential_clears_store(isolated_store, monkeypatch):
    async def fake_reset(_name=None):
        return None

    monkeypatch.setattr("core.credentials.service.integrations.reset", fake_reset)
    isolated_store.set_secret("CARTESIA_API_KEY", "sk_car_test_key_12345678")

    result = await credentials_service.remove_credential("cartesia")

    assert result.ok is True
    assert isolated_store.get_stored_secret("CARTESIA_API_KEY") is None
    assert result.card.status == CredentialCardStatus.MISSING


def test_unknown_credential_raises():
    with pytest.raises(KeyError):
        credentials_service.get_spec("unknown")


@pytest.mark.asyncio
async def test_cursor_key_is_validated_before_save(isolated_store, monkeypatch):
    async def reject(_credential_id, _value):
        return type("Validation", (), {"ok": False, "message": "Cursor rejected this API key."})()

    monkeypatch.setattr(credentials_service, "validate_credential", reject)

    with pytest.raises(ValueError, match="rejected"):
        await credentials_service.save_credential("cursor_coding", "cursor-bad-key-12345678")

    assert isolated_store.get_stored_secret("CURSOR_API_KEY") is None
