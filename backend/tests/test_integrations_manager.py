import pytest

from core.integrations.manager import IntegrationManager


class _CloseTrackingClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_lazy_config_resolves_secret_after_register(monkeypatch):
    monkeypatch.setattr(
        "core.integrations.manager.credential_store.get_secret",
        lambda name: None,
    )
    manager = IntegrationManager()
    seen_configs: list[dict] = []

    def factory(config: dict):
        seen_configs.append(dict(config))
        return _CloseTrackingClient()

    manager.register("search", factory, config_keys=["EXA_API_KEY"])
    assert manager.is_available("search") is False

    monkeypatch.setattr(
        "core.integrations.manager.credential_store.get_secret",
        lambda name: "exa-key" if name == "EXA_API_KEY" else None,
    )

    assert manager.is_available("search") is True
    client = await manager.get("search")
    assert isinstance(client, _CloseTrackingClient)
    assert seen_configs[-1]["EXA_API_KEY"] == "exa-key"


@pytest.mark.asyncio
async def test_reset_closes_cached_client(monkeypatch):
    manager = IntegrationManager()

    def factory(_config):
        return _CloseTrackingClient()

    manager.register("weather", factory, config_keys=[])
    client = await manager.get("weather")
    assert client.closed is False

    await manager.reset("weather")
    assert client.closed is True


@pytest.mark.asyncio
async def test_unregister_closes_and_removes_factory():
    manager = IntegrationManager()

    def factory(_config):
        return _CloseTrackingClient()

    manager.register("background_agent", factory, config_keys=[])
    client = await manager.get("background_agent")

    await manager.unregister("background_agent")

    assert client.closed is True
    assert manager.is_available("background_agent") is False
    with pytest.raises(KeyError):
        await manager.get("background_agent")


@pytest.mark.asyncio
async def test_refresh_hook_receives_fresh_config(monkeypatch):
    manager = IntegrationManager()
    hook_configs: list[dict] = []
    values = {"EXA_API_KEY": None}

    def factory(config: dict):
        return _CloseTrackingClient()

    async def refresh(_client, config: dict) -> None:
        hook_configs.append(dict(config))

    manager.register("search", factory, config_keys=["EXA_API_KEY"], refresh=refresh)

    monkeypatch.setattr(
        "core.integrations.manager.credential_store.get_secret",
        lambda name: values.get(name),
    )

    values["EXA_API_KEY"] = "first"
    await manager.get("search")
    values["EXA_API_KEY"] = "second"
    await manager.get("search")

    assert hook_configs[-1]["EXA_API_KEY"] == "second"


@pytest.mark.asyncio
async def test_shutdown_closes_all_clients():
    manager = IntegrationManager()
    clients: list[_CloseTrackingClient] = []

    def factory(_config):
        client = _CloseTrackingClient()
        clients.append(client)
        return client

    manager.register("weather", factory, config_keys=[])
    manager.register("search", factory, config_keys=[])
    await manager.get("weather")
    await manager.get("search")

    await manager.shutdown()
    assert all(client.closed for client in clients)
