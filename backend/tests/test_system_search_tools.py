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


@pytest.fixture(autouse=True)
def reset_tool_index(monkeypatch):
    monkeypatch.setattr(system_plugin, "_tool_index_signature", None)
    monkeypatch.setattr(system_plugin, "_tool_index", {})


@pytest.mark.asyncio
async def test_search_tools_returns_signature_and_argument_guidance(monkeypatch):
    class FakeRegistry:
        def __init__(self):
            self.plugins = {
                "scheduler": SimpleNamespace(
                    name="scheduler",
                    description="scheduler",
                    get_tools=lambda: {"cancel_alert": cancel_alert},
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

    async def immediate_to_thread(fn, *args):
        return fn(*args)

    from core.plugins import registry as registry_module
    from services import embeddings

    def fail_embed(_texts):
        raise AssertionError("lexical tool search should not embed obvious matches")

    monkeypatch.setattr(registry_module, "registry", FakeRegistry())
    monkeypatch.setattr(system_plugin.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(embeddings.embedding_service, "embed", fail_embed)
    monkeypatch.setattr(embeddings.embedding_service, "embed_one", fail_embed)
    monkeypatch.setattr(
        embeddings.embedding_service,
        "cosine_similarity",
        lambda _query, vector: vector[0],
    )

    result = await system_plugin.SystemPlugin().search_tools("cancel alert")
    [tool_card] = result.tools

    assert tool_card.model_dump() == {
        "fqn": "scheduler.cancel_alert",
        "name": "cancel_alert",
        "plugin": "scheduler",
        "description": "Cancel one alert occurrence or an entire recurring series.",
        "parameters": {
            "series_id": "Cancels the durable recurring series.",
            "instance_id": "Cancels one concrete pending notification occurrence.",
        },
    }
