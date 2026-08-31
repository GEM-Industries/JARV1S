from types import SimpleNamespace

import pytest

from core.decorators import tool
from plugins import system as system_plugin


@tool
async def cancel_alert(series_id: str | None = None, instance_id: str | None = None) -> str:
    """
    Cancel one alert occurrence or an entire recurring series.

    Args:
        series_id: Cancels the durable recurring series.
        instance_id: Cancels one concrete pending notification occurrence.
    """
    return "ok"


@tool
async def dim_lights(query: str, amount: str = "normal") -> str:
    """Dim matching lights by a relative amount."""
    return "ok"


def _fake_registry(*tools: tuple[str, object]):
    class FakeRegistry:
        def __init__(self):
            self.plugins = {
                "scheduler": SimpleNamespace(
                    name="scheduler",
                    description="scheduler",
                    get_tools=lambda: {name: func for name, func in tools},
                ),
            }
            self._capabilities = {}
            self.rebuild_capabilities()

        @staticmethod
        def is_enabled(_name: str) -> bool:
            return True

        def rebuild_capabilities(self) -> None:
            from core.plugins.registry import build_capability_definition

            capabilities = {}
            for plugin in self.plugins.values():
                for tool_name, func in plugin.get_tools().items():
                    definition = build_capability_definition(
                        plugin,
                        tool_name,
                        func,
                        enabled=True,
                    )
                    capabilities[definition.fqn] = definition
            self._capabilities = capabilities

        def get_capability(self, fqn: str):
            return self._capabilities.get(fqn)

        def iter_capabilities(self, *, enabled_only: bool = True):
            for definition in self._capabilities.values():
                if enabled_only and not definition.enabled:
                    continue
                yield definition

    return FakeRegistry()


@pytest.fixture(autouse=True)
def reset_tool_index(monkeypatch):
    monkeypatch.setattr(system_plugin, "_tool_index_signature", None)
    monkeypatch.setattr(system_plugin, "_tool_index", {})


async def _search(monkeypatch, query: str, *tools: tuple[str, object]):
    async def immediate_to_thread(fn, *args):
        return fn(*args)

    from core.plugins import registry as registry_module
    from services import embeddings

    def fail_embed(_texts):
        raise AssertionError("lexical tool search should not embed obvious matches")

    monkeypatch.setattr(registry_module, "registry", _fake_registry(*tools))
    monkeypatch.setattr(system_plugin.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(embeddings.embedding_service, "embed", fail_embed)
    monkeypatch.setattr(embeddings.embedding_service, "embed_one", fail_embed)
    monkeypatch.setattr(
        embeddings.embedding_service,
        "cosine_similarity",
        lambda _query, vector: vector[0],
    )
    return await system_plugin.SystemPlugin().search_tools(query)


@pytest.mark.asyncio
async def test_search_tools_returns_signature_and_argument_guidance(monkeypatch):
    result = await _search(monkeypatch, "cancel alert", ("cancel_alert", cancel_alert))
    [tool_card] = result.tools

    assert tool_card.fqn == "scheduler.cancel_alert"
    assert tool_card.name == "cancel_alert"
    assert tool_card.plugin == "scheduler"
    assert tool_card.description == "Cancel one alert occurrence or an entire recurring series."
    assert "series_id" in tool_card.signature
    assert "instance_id" in tool_card.signature
    assert tool_card.parameters == {
        "series_id": "Cancels the durable recurring series.",
        "instance_id": "Cancels one concrete pending notification occurrence.",
    }


@pytest.mark.asyncio
async def test_search_tools_fills_parameter_names_without_args_docs(monkeypatch):
    result = await _search(monkeypatch, "dim lights", ("dim_lights", dim_lights))
    [tool_card] = result.tools

    assert tool_card.fqn == "scheduler.dim_lights"
    assert "query" in tool_card.signature
    assert tool_card.parameters == {
        "query": "required",
        "amount": "optional",
    }
