from types import SimpleNamespace

import pytest

from core import tool_router
from core.routing.policies import BASELINE_POLICY, RoutingPolicy, SYSTEM_POLICY, VOICE_POLICY


class _FakeRegistry:
    def __init__(self, plugins: dict | None = None):
        self.plugins = plugins or {
            "calendar": SimpleNamespace(
                name="calendar",
                description="calendar",
                get_tools=lambda: {"list_events": object()},
            ),
            "files": SimpleNamespace(
                name="files",
                description="files",
                get_tools=lambda: {"open_file": object()},
            ),
            "gmail": SimpleNamespace(
                name="gmail",
                description="gmail",
                get_tools=lambda: {"send_email": object()},
            ),
            "google_maps": SimpleNamespace(
                name="google_maps",
                description="maps",
                get_tools=lambda: {"GOOGLE_MAPS_GET_ROUTE": object()},
            ),
            "habits": SimpleNamespace(
                name="habits",
                description="habits",
                get_tools=lambda: {"log_habit_by_name": object()},
            ),
            "protocol": SimpleNamespace(
                name="protocol",
                description="protocol",
                get_tools=lambda: {"delete_protocol": object()},
            ),
            "scheduler": SimpleNamespace(
                name="scheduler",
                description="scheduler",
                get_tools=lambda: {"create_reminder": object()},
            ),
            "slack": SimpleNamespace(
                name="slack",
                description="slack",
                get_tools=lambda: {"send_message": object()},
            ),
            "spotify": SimpleNamespace(
                name="spotify",
                description="spotify",
                get_tools=lambda: {"play": object()},
            ),
            "time": SimpleNamespace(
                name="time",
                description="time",
                get_tools=lambda: {"time_in": object()},
            ),
            "todo": SimpleNamespace(
                name="todo",
                description="todo",
                get_tools=lambda: {"toggle_task": object()},
            ),
            "weather": SimpleNamespace(
                name="weather",
                description="weather",
                get_tools=lambda: {"get_weather": object()},
            ),
        }
        self._capabilities = {}
        self.rebuild_capabilities()

    def is_enabled(self, name: str) -> bool:
        return name in self.plugins

    def rebuild_capabilities(self) -> None:
        from core.plugins.registry import build_capability_definition

        capabilities = {}
        for plugin_name, plugin in self.plugins.items():
            if not hasattr(plugin, "name"):
                plugin.name = plugin_name
            for tool_name, func in plugin.get_tools().items():
                if not callable(func):
                    continue
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

    def resolve_provider_name(self, name: str):
        for definition in self._capabilities.values():
            if definition.provider_name == name:
                return definition
        return None

    def provider_tools(self, fqns):
        tools = []
        for fqn in sorted(fqns):
            definition = self._capabilities.get(fqn)
            if definition is None or not definition.enabled:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": definition.provider_name,
                    "description": definition.description or definition.name,
                    "parameters": definition.input_schema or {"type": "object", "properties": {}},
                },
            })
        return tools

    def estimate_schema_stats(self, fqns) -> tuple[int, int]:
        import json

        from core.routing.helpers import schema_chars_to_tokens

        payload = self.provider_tools(fqns)
        if not payload:
            return 0, 0
        chars = len(json.dumps(payload, separators=(",", ":")))
        return chars, schema_chars_to_tokens(chars)


async def _tiny_tool() -> str:
    """Small tool."""
    return "ok"


_LARGE_DOC = "Large tool. " + ("Schema budget filler. " * 80)


async def _large_tool() -> str:
    return "ok"


_large_tool.__doc__ = _LARGE_DOC


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setattr(tool_router, "registry", _FakeRegistry())
    monkeypatch.setattr(tool_router.perf, "start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_router.perf, "end", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(tool_router.embedding_service, "embed_one", lambda _utterance: [1.0])
    monkeypatch.setattr(
        tool_router.embedding_service,
        "embed",
        lambda utterances: [[1.0] for _utterance in utterances],
    )
    monkeypatch.setattr(
        tool_router.embedding_service,
        "cosine_similarity",
        lambda query_vec, plugin_vec: (
            plugin_vec[0]
            if len(query_vec) == len(plugin_vec) == 1
            else sum(a * b for a, b in zip(query_vec, plugin_vec))
        ),
    )
    monkeypatch.setattr(tool_router.ToolRouter, "_schema_stats", lambda _self, _routed: (100, 25))
    return tool_router.ToolRouter()


@pytest.fixture
def router_with_real_schema_stats(monkeypatch):
    fake = _FakeRegistry({
        "large": SimpleNamespace(
            name="large",
            description="large",
            get_tools=lambda: {"large_tool": _large_tool},
        ),
        "tiny": SimpleNamespace(
            name="tiny",
            description="tiny",
            get_tools=lambda: {"tiny_tool": _tiny_tool},
        ),
    })
    monkeypatch.setattr(tool_router, "registry", fake)
    monkeypatch.setattr(tool_router.perf, "start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_router.perf, "end", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(tool_router.embedding_service, "embed_one", lambda _utterance: [1.0])
    monkeypatch.setattr(
        tool_router.embedding_service,
        "cosine_similarity",
        lambda _query_vec, plugin_vec: plugin_vec[0],
    )
    return tool_router.ToolRouter()


@pytest.mark.asyncio
async def test_route_uses_best_plugin_fallback_when_threshold_misses(router):
    router._utterance_vectors = {
        "google_maps": [[0.61]],
        "time": [[0.603]],
        "weather": [[0.55]],
    }

    routed = await router.route(
        "Hey Jarvis, how far away is the University of Sydney from me.",
        session_id="geoff",
        policy=BASELINE_POLICY,
    )

    assert routed == {"google_maps.GOOGLE_MAPS_GET_ROUTE"}
    assert router.get_last_diagnostics("geoff").matched_plugins == ["google_maps"]


@pytest.mark.asyncio
async def test_route_still_returns_none_below_fallback_floor(router):
    router._utterance_vectors = {
        "google_maps": [[0.59]],
        "time": [[0.58]],
    }

    routed = await router.route("thanks", session_id="geoff", policy=BASELINE_POLICY)

    assert routed == set()
    assert router.get_last_diagnostics("geoff").matched_plugins == []


@pytest.mark.asyncio
async def test_delivered_route_carries_plugin_to_one_user_turn(router):
    router._utterance_vectors = {
        "habits": [[0.1]],
        "weather": [[0.2]],
    }
    router.record_route_carryover(
        "geoff",
        tools={"habits.log_habit_by_name"},
    )

    routed = await router.route(
        "I went to bed at about three.",
        session_id="geoff",
        policy=VOICE_POLICY,
    )

    assert routed == {"habits.log_habit_by_name"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "session_carryover"
    assert diagnostics.used_session_carryover is True

    second_route = await router.route(
        "Still tired.",
        session_id="geoff",
        policy=VOICE_POLICY,
    )
    assert second_route == set()


@pytest.mark.asyncio
async def test_direct_topic_change_and_delivered_route_are_both_available(router):
    router._utterance_vectors = {
        "habits": [[0.1]],
        "weather": [[0.9]],
    }
    router.record_route_carryover(
        "geoff",
        tools={"habits.log_habit_by_name"},
    )

    routed = await router.route(
        "What's the weather today?",
        session_id="geoff",
        policy=VOICE_POLICY,
    )

    assert routed == {
        "habits.log_habit_by_name",
        "weather.get_weather",
    }
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.used_session_carryover is True


@pytest.mark.asyncio
async def test_system_route_does_not_consume_pending_user_carryover(router, monkeypatch):
    monkeypatch.setattr(
        tool_router.embedding_service,
        "embed_one",
        lambda text: [1.0, 0.0] if "create a reminder" in text.lower() else [0.0, 0.0],
    )
    router._utterance_vectors = {
        "scheduler": [[1.0, 0.0]],
        "habits": [[0.0, 1.0]],
    }
    router.record_route_carryover(
        "geoff",
        tools={"habits.log_habit_by_name"},
    )

    await router.route(
        "Create a reminder.",
        session_id="geoff",
        policy=SYSTEM_POLICY,
    )

    routed = await router.route(
        "I went to bed at about three.",
        session_id="geoff",
        policy=VOICE_POLICY,
    )

    assert routed == {"habits.log_habit_by_name"}


def test_latest_delivered_route_replaces_previous_carryover(router):
    router.record_route_carryover(
        "geoff",
        tools={"habits.log_habit_by_name"},
    )
    router.record_route_carryover("geoff", tools=set())

    assert "geoff" not in router._pending_route_carryover


@pytest.mark.asyncio
async def test_route_returns_empty_when_router_index_missing(router):
    router._utterance_vectors = {}

    routed = await router.route(
        "turn on all the lights",
        session_id="geoff",
        policy=VOICE_POLICY,
    )

    assert routed == set()
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "router_uninitialized"


@pytest.mark.asyncio
async def test_initialize_raises_when_embedding_index_cannot_be_built(monkeypatch):
    router = tool_router.ToolRouter()
    plugin = SimpleNamespace(
        metadata=SimpleNamespace(utterances=["turn on the lights"], routable=True),
        description="Smart home",
        get_tools=lambda: {"control_lights": object()},
    )
    monkeypatch.setattr(
        tool_router,
        "registry",
        SimpleNamespace(
            plugins={"smart_home": plugin},
            is_enabled=lambda _name: True,
        ),
    )
    monkeypatch.setattr(tool_router.embedding_service, "warmup", lambda: None)
    monkeypatch.setattr(
        tool_router.embedding_service,
        "embed",
        lambda _utterances: (_ for _ in ()).throw(RuntimeError("missing ONNX model")),
    )

    with pytest.raises(RuntimeError, match="semantic routing index"):
        await router.initialize(llm_service=None)


@pytest.mark.asyncio
async def test_route_keeps_threshold_matches_over_fallback(router):
    router._utterance_vectors = {
        "google_maps": [[0.81]],
        "time": [[0.76]],
        "weather": [[0.61]],
    }

    routed = await router.route(
        "what is the weather and travel time",
        session_id="geoff",
        policy=BASELINE_POLICY,
    )

    assert routed == {
        "google_maps.GOOGLE_MAPS_GET_ROUTE",
        "time.time_in",
    }


@pytest.mark.asyncio
async def test_budget_aware_multi_intent_can_route_more_than_two_plugins(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "calendar" in text:
            return [1.0, 0.0, 0.0]
        if "remind" in text:
            return [0.0, 1.0, 0.0]
        if "time" in text:
            return [0.0, 0.0, 1.0]
        return [0.0, 0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "calendar": [[1.0, 0.0, 0.0]],
        "scheduler": [[0.0, 1.0, 0.0]],
        "time": [[0.0, 0.0, 1.0]],
        "weather": [[0.0, 0.0, 0.4]],
    }

    routed = await router.route(
        "check my calendar and remind me to leave and check the time in London",
        session_id="geoff",
    )

    assert routed == {
        "calendar.list_events",
        "scheduler.create_reminder",
        "time.time_in",
    }
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.policy_name == "budget_aware_multi_intent"
    assert diagnostics.match_mode == "multi_intent"
    assert diagnostics.matched_plugins == ["calendar", "scheduler", "time"]


@pytest.mark.asyncio
async def test_previous_routed_plugins_do_not_carry_without_tool_focus(router, monkeypatch):
    monkeypatch.setattr(tool_router.embedding_service, "embed_one", lambda _text: [0.0])
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [[0.0] for _text in texts])
    monkeypatch.setattr(tool_router.embedding_service, "cosine_similarity", lambda query_vec, plugin_vec: query_vec[0] * plugin_vec[0])
    router._utterance_vectors = {
        "calendar": [[1.0]],
        "scheduler": [[1.0]],
    }
    routed = await router.route("okay, do it now", session_id="geoff")

    assert routed == set()
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "none"
    assert diagnostics.used_session_carryover is False


@pytest.mark.asyncio
async def test_tool_focus_survives_intervening_no_tool_turn(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "protocols active" in text:
            return [1.0, 0.0, 0.0]
        if "cancel" in text:
            return [0.0, 0.72, 0.0]
        return [0.0, 0.0, 1.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "protocol": [[1.0, 0.0, 0.0]],
        "scheduler": [[0.0, 1.0, 0.0]],
        "weather": [[0.0, 0.0, 0.4]],
    }

    first = await router.route("Do I have any protocols active?", session_id="geoff")
    router.record_tool_focus("geoff", tools={"protocol.delete_protocol"})
    middle = await router.route("Thanks, that's helpful.", session_id="geoff")
    routed = await router.route("Can you please cancel that?", session_id="geoff")

    assert first == {"protocol.delete_protocol"}
    assert middle == {"protocol.delete_protocol"}
    assert "protocol.delete_protocol" in routed
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "tool_focus"
    assert diagnostics.used_tool_focus is True


@pytest.mark.asyncio
async def test_tool_focus_and_delivered_route_are_both_available(router):
    router._utterance_vectors = {
        "scheduler": [[0.1]],
        "weather": [[0.2]],
    }
    router.record_tool_focus(
        "geoff",
        tools={"scheduler.create_reminder"},
    )
    router.record_route_carryover(
        "geoff",
        tools={"weather.get_weather"},
    )

    routed = await router.route(
        "A short contextual reply.",
        session_id="geoff",
    )

    assert routed == {
        "scheduler.create_reminder",
        "weather.get_weather",
    }
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.used_tool_focus is True
    assert diagnostics.used_session_carryover is True


@pytest.mark.asyncio
async def test_context_dependent_followup_without_focus_uses_semantic_route(router):
    router._utterance_vectors = {
        "protocol": [[0.92]],
        "scheduler": [[0.91]],
    }

    routed = await router.route("Delete it.", session_id="fresh")

    assert routed == {"protocol.delete_protocol", "scheduler.create_reminder"}
    diagnostics = router.get_last_diagnostics("fresh")
    assert diagnostics is not None
    assert diagnostics.match_mode == "multi_intent"


@pytest.mark.asyncio
async def test_strong_semantic_match_can_override_recent_reference(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "slack" in text:
            return [1.0, 0.0]
        if "email" in text:
            return [0.0, 1.0]
        return [0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "slack": [[1.0, 0.0]],
        "gmail": [[0.0, 1.0]],
    }

    await router.route("Send Maya a Slack saying the draft is ready.", session_id="geoff")
    routed = await router.route("Actually email it to her instead.", session_id="geoff")

    assert routed == {"gmail.send_email"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "multi_intent"


@pytest.mark.asyncio
async def test_bare_acknowledgement_does_not_trigger_carryover(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "calendar" in text or "reminder" in text:
            return [1.0, 0.0]
        return [0.0, 1.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "calendar": [[1.0, 0.0]],
        "scheduler": [[0.95, 0.0]],
        "weather": [[0.0, 0.2]],
    }

    await router.route("check my calendar and set a reminder", session_id="geoff")
    routed = await router.route("okay, hold on a second", session_id="geoff")

    assert routed == set()
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "none"

@pytest.mark.asyncio
async def test_tool_focus_adds_previous_tool_plugin_for_short_followup(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "delete" in text or "get rid" in text:
            return [0.72, 0.0]
        return [0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "calendar": [[1.0, 0.0]],
        "scheduler": [[0.0, 1.0]],
    }
    router.record_tool_focus(
        "geoff",
        tools={"scheduler.get_alerts"},
    )

    routed = await router.route("Can you please get rid of that?", session_id="geoff")

    assert "scheduler.create_reminder" in routed
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "tool_focus"
    assert diagnostics.used_tool_focus is True


@pytest.mark.asyncio
async def test_tool_focus_handles_ack_plus_followup(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "get rid" in text:
            return [0.72, 0.0]
        return [0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "calendar": [[1.0, 0.0]],
        "scheduler": [[0.0, 1.0]],
    }
    router.record_tool_focus("geoff", tools={"scheduler.get_alerts"})

    routed = await router.route("Thanks, can you get rid of that?", session_id="geoff")

    assert "scheduler.create_reminder" in routed


@pytest.mark.asyncio
async def test_tool_focus_does_not_override_strong_new_domain(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "email" in text:
            return [1.0, 0.0]
        return [0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "gmail": [[1.0, 0.0]],
        "scheduler": [[0.0, 1.0]],
    }
    router.record_tool_focus("geoff", tools={"scheduler.get_alerts"})

    routed = await router.route("Actually email it instead.", session_id="geoff")

    assert routed == {"gmail.send_email"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "multi_intent"
    assert diagnostics.used_tool_focus is False


def test_tool_focus_clears_with_session(router):
    router.record_tool_focus("geoff", tools={"scheduler.get_alerts"})

    router.clear_session("geoff")

    assert router._focused_plugins("geoff") == set()


@pytest.mark.asyncio
async def test_tool_focus_can_surface_tools_on_no_action_followup(router, monkeypatch):
    monkeypatch.setattr(tool_router.embedding_service, "embed_one", lambda _text: [0.72])
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [[0.72] for _text in texts])
    router._utterance_vectors = {
        "scheduler": [[1.0]],
    }
    router.record_tool_focus("geoff", tools={"scheduler.create_reminder"})

    routed = await router.route("Ignore that for now.", session_id="geoff")

    assert routed == {"scheduler.create_reminder"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "multi_intent"


@pytest.mark.asyncio
async def test_tool_focus_can_surface_tools_on_plain_acknowledgement(router, monkeypatch):
    monkeypatch.setattr(tool_router.embedding_service, "embed_one", lambda _text: [0.0])
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [[0.0] for _text in texts])
    monkeypatch.setattr(tool_router.embedding_service, "cosine_similarity", lambda query_vec, plugin_vec: query_vec[0] * plugin_vec[0])
    router._utterance_vectors = {
        "scheduler": [[1.0]],
    }
    router.record_tool_focus("geoff", tools={"scheduler.get_alerts"})

    routed = await router.route("Thanks, that's helpful.", session_id="geoff")

    assert routed == {"scheduler.create_reminder"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "tool_focus"


@pytest.mark.asyncio
async def test_mixed_no_action_followup_can_route_new_command(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "slack" in text:
            return [1.0, 0.0]
        if "email" in text:
            return [0.0, 1.0]
        return [0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "slack": [[1.0, 0.0]],
        "gmail": [[0.0, 1.0]],
    }

    await router.route("Send Maya a Slack saying the draft is ready.", session_id="geoff")
    routed = await router.route("Don't send it, email Sarah instead.", session_id="geoff")

    assert routed == {"gmail.send_email"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "multi_intent"


@pytest.mark.asyncio
async def test_fresh_there_question_routes_by_semantics(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "rain" in text:
            return [1.0, 0.0]
        return [0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "weather": [[1.0, 0.0]],
        "google_maps": [[0.0, 1.0]],
    }

    routed = await router.route("Is there rain tomorrow?", session_id="fresh")

    assert routed == {"weather.get_weather"}
    diagnostics = router.get_last_diagnostics("fresh")
    assert diagnostics is not None
    assert diagnostics.match_mode == "multi_intent"


@pytest.mark.asyncio
async def test_strong_fresh_semantic_match_routes_by_current_turn(router, monkeypatch):
    def embed_one(text: str):
        text = text.lower()
        if "slack" in text:
            return [1.0, 0.0]
        if "queue" in text:
            return [0.0, 1.0]
        return [0.0, 0.0]

    monkeypatch.setattr(tool_router.embedding_service, "embed_one", embed_one)
    monkeypatch.setattr(tool_router.embedding_service, "embed", lambda texts: [embed_one(t) for t in texts])
    router._utterance_vectors = {
        "slack": [[1.0, 0.0]],
        "spotify": [[0.0, 1.0]],
    }

    await router.route("Send Maya a Slack saying the draft is ready.", session_id="geoff")
    routed = await router.route("Queue it instead.", session_id="geoff")

    assert routed == {"spotify.play"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.match_mode == "multi_intent"


@pytest.mark.asyncio
async def test_system_policy_routes_current_hint(router):
    router._utterance_vectors = {
        "calendar": [[0.4]],
        "scheduler": [[0.9]],
        "weather": [[0.3]],
    }
    routed = await router.route(
        "create reminder from automation directive",
        session_id="geoff",
        policy=SYSTEM_POLICY,
    )

    assert routed == {"scheduler.create_reminder"}
    diagnostics = router.get_last_diagnostics("geoff")
    assert diagnostics is not None
    assert diagnostics.policy_name == "system_budget_aware_multi_intent"


@pytest.mark.asyncio
async def test_matched_plugins_are_not_evicted_by_schema_size(router_with_real_schema_stats):
    router_with_real_schema_stats._utterance_vectors = {
        "large": [[0.95]],
        "tiny": [[0.94]],
    }
    policy = RoutingPolicy(
        name="keep_matches",
        threshold=0.5,
        max_matched=2,
    )

    routed = await router_with_real_schema_stats.route(
        "use both tools",
        session_id="geoff",
        policy=policy,
    )

    assert routed == {"large.large_tool", "tiny.tiny_tool"}


def test_always_on_fqns_uses_explicit_allowlist(monkeypatch):
    fake = _FakeRegistry({
        "files": SimpleNamespace(
            name="files",
            description="files",
            get_tools=lambda: {"read": _tiny_tool, "open_file": _tiny_tool},
        ),
        "weather": SimpleNamespace(
            name="weather",
            description="weather",
            get_tools=lambda: {"get_weather": _tiny_tool},
        ),
        "system": SimpleNamespace(
            name="system",
            description="system",
            get_tools=lambda: {
                "search_tools": _tiny_tool,
                "set_volume": _tiny_tool,
                "approve_pending": _tiny_tool,
                "deny_pending": _tiny_tool,
            },
        ),
    })
    monkeypatch.setattr(tool_router, "registry", fake)

    assert tool_router.always_on_fqns() == {
        "files.read",
        "system.search_tools",
        "system.approve_pending",
        "system.deny_pending",
    }
    assert "files.open_file" not in tool_router.always_on_fqns()
    assert "weather.get_weather" not in tool_router.always_on_fqns()
    assert "system.set_volume" not in tool_router.always_on_fqns()


def test_always_on_excludes_routable_domain_creates():
    assert {
        "scheduler.remind",
        "automations.create_rule",
        "db.reset_conversation_window",
        "agents.inspect",
        "agents.list_tasks",
    }.isdisjoint(tool_router.ALWAYS_ON_FQNS)
    assert {
        "system.search_tools",
        "files.read",
        "display.push_content",
        "search.web",
        "profile.remember",
        "agents.dispatch",
        "agents.get_status",
        "system.exec",
        "system.approve_pending",
        "system.deny_pending",
    } <= tool_router.ALWAYS_ON_FQNS


def test_active_tool_fqns_hides_remind_on_act_turns(monkeypatch):
    monkeypatch.setattr(
        tool_router,
        "always_on_fqns",
        lambda: {"scheduler.remind", "system.search_tools"},
    )
    routed = {"scheduler.defer", "scheduler.remind", "smart_home.control_lights"}

    act = tool_router.active_tool_fqns(routed, trigger_decision="act")
    assert "scheduler.remind" not in act
    assert "scheduler.defer" in act
    assert "system.search_tools" in act
    assert "smart_home.control_lights" in act

    default = tool_router.active_tool_fqns(routed)
    assert "scheduler.remind" in default
    tell = tool_router.active_tool_fqns(routed, trigger_decision="tell")
    assert "scheduler.remind" in tell


def test_active_tool_fqns_hides_dispatch_during_background_execution(monkeypatch):
    monkeypatch.setattr(
        tool_router,
        "always_on_fqns",
        lambda: {"agents.dispatch", "agents.get_status", "system.search_tools"},
    )

    background = tool_router.active_tool_fqns(
        {"agents.dispatch", "gmail.search"},
        source="background",
    )
    assert "agents.dispatch" not in background
    assert "agents.get_status" in background
    assert "gmail.search" in background

    interactive = tool_router.active_tool_fqns(
        {"agents.dispatch", "gmail.search"},
        source="user",
    )
    assert "agents.dispatch" in interactive


def test_discovered_fqns_promotes_search_and_edit_tool(monkeypatch):
    known = {"gmail.search", "scheduler.replace_alert"}
    monkeypatch.setattr(
        tool_router.registry,
        "get_capability",
        lambda fqn: object() if fqn in known else None,
    )

    search = tool_router.discovered_fqns(
        {"tools": [{"fqn": "gmail.search", "name": "search"}]}
    )
    assert search == {"gmail.search"}

    setups = tool_router.discovered_fqns(
        {
            "setups": [
                {
                    "name": "Morning Wakeup Lights",
                    "edit_tool": "scheduler.replace_alert",
                    "series_id": "rule-morning",
                }
            ]
        }
    )
    assert setups == {"scheduler.replace_alert"}

    from_json = tool_router.discovered_fqns(
        '{"edit_tool":"scheduler.replace_alert","series_id":"rule-morning"}'
    )
    assert from_json == {"scheduler.replace_alert"}

    assert tool_router.discovered_fqns({"edit_tool": "not a tool"}) == set()
    assert tool_router.discovered_fqns({"edit_tool": "missing.tool"}) == set()
