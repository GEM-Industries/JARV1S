"""Tests for smart_home plugin tools and status."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.auth.device_models import DeviceCredentialSummary, DeviceLocation
import plugins.smart_home as smart_home
from plugins.smart_home import SmartHomePlugin, _state_mismatches
from plugins.smart_home.domains import (
    brightness_pct_from_ha,
    capabilities_for_entity,
    clamp_light_params,
    is_safe_setup_entity,
    is_supported_control_action,
    live_light_state_from_ha,
    parse_color_phrase,
    requires_control_consent,
    resolve_hue_adjustment,
)
from plugins.smart_home.inventory import entity_to_device_summary
from plugins.smart_home.ha_client import HomeAssistantError
from plugins.smart_home.inventory import (
    InventoryEntity,
    InventorySnapshot,
    build_inventory,
    entities_for_config_entry,
    match_area_by_name,
    parse_device_query,
    search_inventory,
)
from plugins.smart_home.node_binding import bind_node, resolve_area_for_node
from plugins.smart_home.rooms import list_rooms
from plugins.smart_home.status import LivenessStatus, check_liveness, check_readiness


def _text(result) -> str:
    message = getattr(result, "message", None)
    if message is not None and getattr(result, "code", None) is not None:
        return message
    content = getattr(result, "content", None)
    if content is not None:
        return content
    return str(result)

FIXTURES = Path(__file__).parent / "fixtures" / "ha"
pytestmark = pytest.mark.usefixtures("smart_home_tool_data_store")


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def smart_home_tool_data_store(fake_tool_data_store, monkeypatch):
    fake_tool_data_store.install(monkeypatch, smart_home)
    return fake_tool_data_store


class FakeHAClient:
    base_url = "http://localhost:8123"
    token = "token"

    def __init__(self):
        self.service_calls: list[
            tuple[str, str, str | list[str] | None, dict | None]
        ] = []
        self.areas = _load("area_registry_list.json")
        self.state_overrides: dict[str, str] = {}
        self.attribute_overrides: dict[str, dict] = {}

    async def list_areas(self):
        return self.areas

    async def create_area(self, name: str):
        area_id = name.strip().lower().replace(" ", "_")
        area = {"area_id": area_id, "name": name}
        self.areas.append(area)
        return area

    async def update_area(self, area_id: str, *, name: str):
        for area in self.areas:
            if area.get("area_id") == area_id:
                area["name"] = name
                return area
        raise HomeAssistantError(f"Area not found: {area_id}")

    async def list_devices(self):
        return _load("device_registry_list.json")

    async def list_entities_registry(self):
        return _load("entity_registry_list.json")

    async def get_states(self):
        return _load("states_sample.json")

    async def get_state(self, entity_id: str):
        for state in _load("states_sample.json"):
            if state["entity_id"] == entity_id:
                attrs = {
                    **state.get("attributes", {}),
                    **self.attribute_overrides.get(entity_id, {}),
                }
                return {
                    **state,
                    "state": self.state_overrides.get(entity_id, state["state"]),
                    "attributes": attrs,
                }
        raise RuntimeError("missing")

    async def call_service(self, domain, service, *, entity_id=None, data=None):
        self.service_calls.append((domain, service, entity_id, data))
        if service in {"turn_on", "turn_off"}:
            state = "on" if service == "turn_on" else "off"
            targets = [entity_id] if isinstance(entity_id, str) else entity_id or []
            self.state_overrides.update(dict.fromkeys(targets, state))
            for target in targets:
                attrs = self.attribute_overrides.setdefault(target, {})
                if data and "brightness_pct" in data:
                    attrs["brightness"] = round(data["brightness_pct"] * 255 / 100)
                if data and "color_temp_kelvin" in data:
                    attrs["color_temp_kelvin"] = data["color_temp_kelvin"]
                    attrs["color_mode"] = "color_temp"
                if data and "rgb_color" in data:
                    attrs["rgb_color"] = data["rgb_color"]
                    attrs["color_mode"] = "rgb"
                if data and "color_name" in data:
                    attrs["color_mode"] = "rgb"
        return None


class TuyaFakeHAClient(FakeHAClient):
    async def list_config_entries(self, domain=None):
        entries = _load("config_entries_tuya.json")
        if domain:
            return [e for e in entries if e.get("domain") == domain]
        return entries

    async def list_devices(self):
        return _load("device_registry_tuya.json")

    async def list_entities_registry(self):
        return _load("entity_registry_tuya.json")

    async def get_states(self):
        return _load("states_tuya.json")

    async def get_state(self, entity_id: str):
        for state in _load("states_tuya.json"):
            if state["entity_id"] == entity_id:
                attrs = {
                    **state.get("attributes", {}),
                    **self.attribute_overrides.get(entity_id, {}),
                }
                return {
                    **state,
                    "state": self.state_overrides.get(entity_id, state["state"]),
                    "attributes": attrs,
                }
        raise RuntimeError("missing")

    async def reload_config_entry(self, entry_id: str):
        self.last_reload = entry_id

    async def list_areas(self):
        return _load("area_registry_list.json")

    async def create_area(self, name: str):
        return {"area_id": "bedroom", "name": name}

    async def update_device(self, device_id, *, area_id=None, name_by_user=None):
        self.device_updates = (device_id, area_id, name_by_user)
        return {"id": device_id, "area_id": area_id, "name_by_user": name_by_user}

    async def update_entity(
        self, entity_id, *, name=None, area_id=None, new_entity_id=None
    ):
        self.entity_updates = (entity_id, name, area_id)
        return {"entity_id": entity_id, "name": name, "area_id": area_id}


def _credential(
    *,
    node_id: str,
    ha_area_id: str,
    room_name: str,
) -> DeviceCredentialSummary:
    now = datetime.now(timezone.utc)
    return DeviceCredentialSummary(
        device_id=f"dev-{node_id}",
        owner_id="owner-1",
        node_id=node_id,
        node_label=node_id.replace("-", " ").title(),
        capabilities=["mic", "speaker"],
        location=DeviceLocation(
            provider="home_assistant",
            room_id=room_name.lower().replace(" ", "_"),
            room_name=room_name,
            ha_area_id=ha_area_id,
        ),
        kind="satellite",
        created_at=now,
        last_seen_at=now,
    )


@pytest.mark.asyncio
async def test_build_inventory_merges_registry_and_states() -> None:
    snapshot = await build_inventory(FakeHAClient())
    assert snapshot.entity_count == 3
    living = next(e for e in snapshot.entities if e.entity_id == "light.living_room")
    assert living.area_name == "Living Room"


@pytest.mark.asyncio
async def test_search_inventory_by_area() -> None:
    snapshot = await build_inventory(FakeHAClient())
    matches = search_inventory(snapshot, "living", area_id="living_room")
    assert len(matches) == 1
    assert matches[0].entity_id == "light.living_room"


def test_safe_setup_entity_excludes_locks() -> None:
    assert is_safe_setup_entity("light.living_room")
    assert not is_safe_setup_entity("lock.front_door")


def test_control_actions_restricted_only_for_closed_domains() -> None:
    # Closed on/off domains reject invented services.
    assert is_supported_control_action("light", "turn_on")
    assert not is_supported_control_action("light", "set_attributes")
    # Open-ended domains pass through any HA service — flexibility preserved.
    assert is_supported_control_action("fan", "set_percentage")
    assert is_supported_control_action("cover", "set_cover_position")
    assert is_supported_control_action("media_player", "select_source")


@pytest.mark.asyncio
async def test_rooms_read_model_derives_names_from_home_assistant() -> None:
    stale_credential = _credential(
        node_id="living-sat",
        ha_area_id="living_room",
        room_name="Old Living Name",
    )

    with patch(
        "plugins.smart_home.rooms.device_auth_service.list_devices",
        new=AsyncMock(return_value=[stale_credential]),
    ):
        response = await list_rooms(FakeHAClient(), owner_id="owner-1")

    living = next(room for room in response.rooms if room.area_id == "living_room")
    assert living.name == "Living Room"
    assert living.exists_in_ha is True
    assert living.entity_count == 1
    assert living.device_count == 1
    assert living.bound_nodes[0].node_id == "living-sat"
    assert living.bound_nodes[0].room_name == "Old Living Name"


@pytest.mark.asyncio
async def test_rooms_read_model_surfaces_dangling_area_bindings() -> None:
    dangling_credential = _credential(
        node_id="bedroom-sat",
        ha_area_id="deleted-bedroom",
        room_name="Bedroom",
    )

    with patch(
        "plugins.smart_home.rooms.device_auth_service.list_devices",
        new=AsyncMock(return_value=[dangling_credential]),
    ):
        response = await list_rooms(FakeHAClient(), owner_id="owner-1")

    dangling = next(
        room for room in response.rooms if room.area_id == "deleted-bedroom"
    )
    assert dangling.name == "Bedroom"
    assert dangling.exists_in_ha is False
    assert dangling.bound_nodes[0].node_id == "bedroom-sat"


@pytest.mark.asyncio
async def test_list_rooms_tool_returns_home_assistant_rooms(tool_context) -> None:
    plugin = SmartHomePlugin()

    with patch(
        "plugins.smart_home.rooms.device_auth_service.list_devices",
        new=AsyncMock(return_value=[]),
    ):
        response = await plugin.list_rooms(smart_home=FakeHAClient())

    assert [room.name for room in response.rooms] == ["Living Room", "Office"]


@pytest.mark.asyncio
async def test_create_room_tool_resolves_duplicates_before_consent(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()

    async def fail_consent(*_args, **_kwargs):
        raise AssertionError("approval should not be created for an existing room")

    with patch("plugins.smart_home.require_consent", new=fail_consent):
        result = await plugin.create_room("living room", smart_home=FakeHAClient())

    assert result == "Room 'Living Room' already exists."


@pytest.mark.asyncio
async def test_create_room_tool_creates_after_consent(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    async def approve(_description, action, detail=""):
        return await action()

    with (
        patch("plugins.smart_home.require_consent", new=approve),
        patch("plugins.smart_home.rooms.invalidate_inventory_cache", new=AsyncMock()),
        patch(
            "plugins.smart_home.rooms.device_auth_service.list_devices",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await plugin.create_room(" Study ", smart_home=client)

    assert result.room is not None
    assert result.room.name == "Study"
    assert any(area["area_id"] == "study" for area in client.areas)


@pytest.mark.asyncio
async def test_rename_room_tool_resolves_by_name_and_updates_area(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    async def approve(_description, action, detail=""):
        return await action()

    with (
        patch("plugins.smart_home.require_consent", new=approve),
        patch("plugins.smart_home.rooms.invalidate_inventory_cache", new=AsyncMock()),
        patch(
            "plugins.smart_home.rooms.device_auth_service.update_area_room_name",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "plugins.smart_home.rooms.device_auth_service.list_devices",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await plugin.rename_room("Living Room", "Lounge", smart_home=client)

    assert result.room is not None
    assert result.room.name == "Lounge"
    assert (
        next(area for area in client.areas if area["area_id"] == "living_room")["name"]
        == "Lounge"
    )


def test_smart_home_public_tools_are_batch_first() -> None:
    tools = SmartHomePlugin().get_tools()

    assert "control_devices" in tools
    assert "control_lights" in tools
    assert "get_device_states" in tools
    assert "adjust_lights" in tools
    assert "control_room_lights" not in tools
    assert "control_device" not in tools
    assert "get_device_state" not in tools
    assert "validate_connection" not in tools


def test_capabilities_for_color_temp_only_light_includes_brightness() -> None:
    entity = InventoryEntity(
        entity_id="light.bedroom_1",
        name="Bedroom 1",
        domain="light",
        state="on",
        supported_color_modes=["color_temp"],
    )
    assert capabilities_for_entity(entity) == ["on_off", "brightness", "color_temp"]


def test_capabilities_for_onoff_only_light() -> None:
    entity = InventoryEntity(
        entity_id="light.hall",
        name="Hall",
        domain="light",
        state="off",
        supported_color_modes=["onoff"],
    )
    assert capabilities_for_entity(entity) == ["on_off"]


def test_brightness_pct_floors_to_one_when_on() -> None:
    assert brightness_pct_from_ha(1, state="on") == 1
    assert brightness_pct_from_ha(0, state="on") == 0


def test_clamp_light_params_clamps_kelvin() -> None:
    entity = InventoryEntity(
        entity_id="light.bedroom_1",
        name="Bedroom 1",
        domain="light",
        state="on",
        supported_color_modes=["color_temp"],
        min_color_temp_kelvin=2000,
        max_color_temp_kelvin=6500,
    )
    result = clamp_light_params(
        entity,
        {"brightness_pct": 150, "color_temp_kelvin": 9000},
    )
    assert result == {"brightness_pct": 100, "color_temp_kelvin": 6500}


def test_clamp_light_params_accepts_brightness_percent_string() -> None:
    entity = InventoryEntity(
        entity_id="light.bedroom_1",
        name="Bedroom 1",
        domain="light",
        state="on",
        supported_color_modes=["brightness"],
    )
    result = clamp_light_params(entity, {"brightness_pct": "50%"})
    assert result == {"brightness_pct": 50}


def test_clamp_light_params_rejects_unsupported_capability() -> None:
    entity = InventoryEntity(
        entity_id="light.bedroom_1",
        name="Bedroom 1",
        domain="light",
        state="on",
        supported_color_modes=["color_temp"],
    )
    with pytest.raises(ValueError, match="does not support: rgb_color"):
        clamp_light_params(entity, {"rgb_color": [255, 128, 0]})


def test_clamp_light_params_prefers_rgb_on_conflict() -> None:
    entity = InventoryEntity(
        entity_id="light.rgb_bulb",
        name="RGB Bulb",
        domain="light",
        state="on",
        supported_color_modes=["color_temp", "rgb"],
        min_color_temp_kelvin=2000,
        max_color_temp_kelvin=6500,
    )
    result = clamp_light_params(
        entity,
        {"color_temp_kelvin": 2700, "rgb_color": [255, 128, 0]},
    )
    assert result == {"rgb_color": [255, 128, 0]}


def test_clamp_light_params_maps_warm_white_to_kelvin() -> None:
    entity = InventoryEntity(
        entity_id="light.rgb_bulb",
        name="RGB Bulb",
        domain="light",
        state="on",
        supported_color_modes=["color_temp", "rgb"],
        min_color_temp_kelvin=2000,
        max_color_temp_kelvin=6500,
    )
    assert clamp_light_params(entity, {"color_name": "Warm White"}) == {
        "color_temp_kelvin": 2700
    }
    assert clamp_light_params(entity, {"color_name": "orange"}) == {
        "rgb_color": [255, 146, 20]
    }
    assert clamp_light_params(entity, {"color_name": "vivid orange"}) == {
        "color_name": "orange"
    }


def test_clamp_light_params_keeps_brightness_when_hue_unsupported() -> None:
    entity = InventoryEntity(
        entity_id="light.bedroom_1",
        name="Bedroom 1",
        domain="light",
        state="on",
        supported_color_modes=["color_temp"],
    )
    assert clamp_light_params(
        entity, {"rgb_color": [255, 128, 0], "brightness_pct": 40}
    ) == {"brightness_pct": 40}


def test_clamp_light_params_accepts_color_name_and_rgb_dict() -> None:
    entity = InventoryEntity(
        entity_id="light.rgb_bulb",
        name="RGB Bulb",
        domain="light",
        state="on",
        supported_color_modes=["rgb"],
    )
    assert clamp_light_params(entity, {"color_name": "CornflowerBlue"}) == {
        "color_name": "cornflowerblue"
    }
    assert clamp_light_params(entity, {"rgb_color": {"r": 128, "g": 0, "b": 128}}) == {
        "rgb_color": [128, 0, 128]
    }


def test_clamp_light_params_rejects_legacy_light_params() -> None:
    entity = InventoryEntity(
        entity_id="light.rgb_bulb",
        name="RGB Bulb",
        domain="light",
        state="on",
        supported_color_modes=["rgb"],
    )
    with pytest.raises(ValueError, match="Unsupported light parameter"):
        clamp_light_params(entity, {"brightness": 20})


def test_clamp_light_params_parses_transition_duration() -> None:
    entity = InventoryEntity(
        entity_id="light.bedroom_1",
        name="Bedroom 1",
        domain="light",
        state="off",
        supported_color_modes=["brightness"],
    )
    assert clamp_light_params(entity, {"transition": "15 minutes"}) == {
        "transition": 900
    }
    assert clamp_light_params(entity, {"transition": 10}) == {"transition": 10}
    with pytest.raises(ValueError, match="Could not parse transition"):
        clamp_light_params(entity, {"transition": "warm"})


@pytest.mark.parametrize(
    ("state", "expected", "payload", "want"),
    [
        (
            {"state": "on", "attributes": {"brightness": 1}},
            "on",
            {"brightness_pct": 100, "transition": 900},
            [],
        ),
        (
            {"state": "on", "attributes": {}},
            "off",
            {"transition": 900},
            [],
        ),
        (
            {"state": "off", "attributes": {}},
            "on",
            {"transition": 900},
            ["state=off"],
        ),
    ],
)
def test_state_mismatches_skips_light_levels_while_fading(
    state: dict, expected: str, payload: dict, want: list[str]
) -> None:
    assert _state_mismatches(state, expected, payload) == want


def test_entity_to_device_summary_includes_capabilities_and_area() -> None:
    entity = InventoryEntity(
        entity_id="light.bedroom_1",
        name="Bedroom 1",
        domain="light",
        state="on",
        area_name="Bedroom",
        brightness=1,
        color_temp_kelvin=3000,
        color_mode="color_temp",
        supported_color_modes=["color_temp"],
    )
    summary = entity_to_device_summary(entity)
    assert summary.area_name == "Bedroom"
    assert summary.brightness_pct == 1
    assert summary.color_temp_kelvin == 3000
    assert summary.color_mode == "color_temp"
    assert "brightness" in summary.capabilities


def test_search_inventory_matches_possessives_and_prefers_primary_device_entity() -> (
    None
):
    snapshot = InventorySnapshot(
        captured_at="2026-06-04T10:00:00+00:00",
        area_count=1,
        device_count=1,
        entity_count=3,
        entities=[
            InventoryEntity(
                entity_id="binary_sensor.charlie_lamp_overheated",
                name="Charlie Lamp Overheated",
                domain="binary_sensor",
                state="off",
                area_id="bedroom",
                area_name="Bedroom",
                device_id="charlie-device",
            ),
            InventoryEntity(
                entity_id="light.charlie_lamp",
                name="Charlie Lamp",
                domain="light",
                state="off",
                area_id="bedroom",
                area_name="Bedroom",
                device_id="charlie-device",
            ),
            InventoryEntity(
                entity_id="sensor.charlie_lamp_signal_level",
                name="Charlie Lamp Signal level",
                domain="sensor",
                state="3",
                area_id="bedroom",
                area_name="Bedroom",
                device_id="charlie-device",
            ),
        ],
    )

    matches = search_inventory(snapshot, "Charlie's lamp")

    assert [m.entity_id for m in matches] == ["light.charlie_lamp"]


def test_search_inventory_matches_plural_domains_and_spoken_numbers() -> None:
    snapshot = InventorySnapshot(
        captured_at="2026-06-04T10:00:00+00:00",
        area_count=1,
        device_count=2,
        entity_count=2,
        entities=[
            InventoryEntity(
                entity_id="light.bedroom_1",
                name="Bedroom 1",
                domain="light",
                state="on",
                area_id="bedroom",
                area_name="Bedroom",
                aliases=["left lamp"],
            ),
            InventoryEntity(
                entity_id="light.bedroom_2",
                name="Bedroom 2",
                domain="light",
                state="off",
                area_id="bedroom",
                area_name="Bedroom",
                labels=["ambient"],
            ),
        ],
    )

    assert [m.entity_id for m in search_inventory(snapshot, "all my lights")] == [
        "light.bedroom_1",
        "light.bedroom_2",
    ]
    assert [m.entity_id for m in search_inventory(snapshot, "bedroom lights")] == [
        "light.bedroom_1",
        "light.bedroom_2",
    ]
    assert [m.entity_id for m in search_inventory(snapshot, "bedroom one")] == [
        "light.bedroom_1"
    ]
    assert [m.entity_id for m in search_inventory(snapshot, "bedroom two")] == [
        "light.bedroom_2"
    ]
    assert [m.entity_id for m in search_inventory(snapshot, "left lamp")] == [
        "light.bedroom_1"
    ]
    assert [m.entity_id for m in search_inventory(snapshot, "ambient light")] == [
        "light.bedroom_2"
    ]
    assert [m.entity_id for m in search_inventory(snapshot, "Bedroom 1 and Bedroom 2")] == [
        "light.bedroom_1",
        "light.bedroom_2",
    ]
    assert [m.entity_id for m in search_inventory(snapshot, "dim the bedroom lights")] == [
        "light.bedroom_1",
        "light.bedroom_2",
    ]


def _mixed_room_snapshot() -> InventorySnapshot:
    return InventorySnapshot(
        captured_at="2026-06-04T10:00:00+00:00",
        area_count=2,
        device_count=3,
        entity_count=4,
        entities=[
            InventoryEntity(
                entity_id="light.living_room",
                name="Living Room Light",
                domain="light",
                state="on",
                area_id="living_room",
                area_name="Living Room",
            ),
            InventoryEntity(
                entity_id="light.office",
                name="Office Light",
                domain="light",
                state="off",
                area_id="office",
                area_name="Office",
            ),
            InventoryEntity(
                entity_id="fan.office",
                name="Office Fan",
                domain="fan",
                state="off",
                area_id="office",
                area_name="Office",
            ),
            InventoryEntity(
                entity_id="switch.kitchen",
                name="Kitchen Switch",
                domain="switch",
                state="on",
                area_id="kitchen",
                area_name="Kitchen",
            ),
        ],
    )


def test_parse_device_query_recognizes_global_light_scope() -> None:
    parsed = parse_device_query("all lights in the house")
    assert parsed.scope_all is True
    assert parsed.domain == "light"
    assert parsed.tokens == frozenset()
    assert parsed.room_reference is False


def test_parse_device_query_lights_in_here_is_room_reference() -> None:
    parsed = parse_device_query("lights in here")
    assert parsed.room_reference is True
    assert parsed.domain == "light"
    assert parsed.tokens == frozenset()


def test_parse_device_query_strips_dim_verb_from_tokens() -> None:
    parsed = parse_device_query("dim the bedroom lights")
    assert parsed.domain == "light"
    assert parsed.brightness_direction == "dimmer"
    assert parsed.tokens == frozenset({"bedroom"})


def test_search_inventory_all_lights_in_house_matches_only_lights() -> None:
    snapshot = _mixed_room_snapshot()
    matches = search_inventory(snapshot, "all lights in the house")
    assert {m.entity_id for m in matches} == {"light.living_room", "light.office"}


def test_search_inventory_can_return_complete_control_targets() -> None:
    entities = [
        InventoryEntity(
            entity_id=f"light.channel_{index:02}",
            name=f"Channel {index:02}",
            domain="light",
            state="on",
            device_id="shared-device" if index < 2 else f"device-{index}",
        )
        for index in range(12)
    ]
    snapshot = InventorySnapshot(
        captured_at="2026-06-04T10:00:00+00:00",
        area_count=1,
        device_count=11,
        entity_count=len(entities),
        entities=entities,
    )

    matches = search_inventory(
        snapshot,
        "all lights",
        dedupe_devices=False,
        limit=None,
    )

    assert [match.entity_id for match in matches] == [
        f"light.channel_{index:02}" for index in range(12)
    ]


def test_search_inventory_living_room_lights_scopes_to_area() -> None:
    snapshot = _mixed_room_snapshot()
    matches = search_inventory(snapshot, "living room lights")
    assert [m.entity_id for m in matches] == ["light.living_room"]


def test_search_inventory_lights_in_here_uses_area_scope() -> None:
    snapshot = _mixed_room_snapshot()
    matches = search_inventory(snapshot, "lights in here", area_id="office")
    assert [m.entity_id for m in matches] == ["light.office"]


def test_search_inventory_office_fan_does_not_match_lights() -> None:
    snapshot = _mixed_room_snapshot()
    matches = search_inventory(snapshot, "office fan")
    assert [m.entity_id for m in matches] == ["fan.office"]


def test_search_inventory_empty_query_does_not_match_everything() -> None:
    snapshot = _mixed_room_snapshot()
    assert search_inventory(snapshot, "") == []
    assert search_inventory(snapshot, "in the") == []


@pytest.mark.asyncio
async def test_check_liveness_missing_config() -> None:
    status = await check_liveness(None, None)
    assert not status.configured
    assert "Smart Home panel" in status.message


@pytest.mark.asyncio
async def test_check_liveness_handles_invalid_url() -> None:
    status = await check_liveness("http://", "token")
    assert status.configured
    assert not status.reachable
    assert "Invalid HA_URL" in status.message


@pytest.mark.asyncio
async def test_search_devices_tool(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()
    client.state_overrides["light.living_room"] = "off"
    client.attribute_overrides["light.living_room"] = {
        "brightness": 255,
        "color_mode": "hs",
        "rgb_color": [180, 0, 255],
        "supported_color_modes": ["hs", "color_temp"],
    }

    with patch(
        "plugins.smart_home.resolve_area_from_context", AsyncMock(return_value=None)
    ):
        results = await plugin.search_devices("living", smart_home=client)
    assert results[0].entity_id == "light.living_room"
    assert results[0].state == "off"
    assert results[0].brightness_pct == 100
    assert results[0].color_mode == "hs"
    assert results[0].rgb_color == [180, 0, 255]


@pytest.mark.asyncio
async def test_search_devices_does_not_apply_node_area_to_explicit_room_query(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    with patch(
        "plugins.smart_home.resolve_area_from_context", AsyncMock(return_value="office")
    ):
        results = await plugin.search_devices("living", smart_home=client)
    assert [r.entity_id for r in results] == ["light.living_room"]


@pytest.mark.asyncio
async def test_search_devices_uses_node_area_for_this_room(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    with patch(
        "plugins.smart_home.resolve_area_from_context", AsyncMock(return_value="office")
    ):
        results = await plugin.search_devices("in here", smart_home=client)
    assert [r.entity_id for r in results] == ["switch.office_fan"]


@pytest.mark.asyncio
async def test_bind_node_area() -> None:
    from core.auth.device_models import DeviceLocation

    location = DeviceLocation(
        provider="home_assistant",
        room_id="office",
        room_name="Office",
        ha_area_id="office",
    )

    with (
        patch(
            "plugins.smart_home.node_binding.device_auth_service.update_node_location",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "plugins.smart_home.node_binding.device_auth_service.get_node_location",
            new=AsyncMock(return_value=location),
        ),
    ):
        stored = await bind_node("owner-1", "node-1", "office", area_name="Office")
        assert stored.ha_area_id == "office"
        assert await resolve_area_for_node("node-1", owner_id="owner-1") == "office"


@pytest.mark.asyncio
async def test_control_device_requires_consent_for_risky_actions(tool_context) -> None:
    assert requires_control_consent("lock.front_door", "unlock")
    plugin = SmartHomePlugin()

    with patch("core.plugins.consent.create_pending_input", AsyncMock()):
        result = await plugin.control_device(
            "lock.front_door",
            "unlock",
            smart_home=FakeHAClient(),
        )
    assert result.code == "approval_needed"
    assert "has not executed yet" in result.message


@pytest.mark.asyncio
async def test_control_device_clamps_light_params(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_device(
        "light.living_room",
        "turn_on",
        params={"brightness_pct": 20, "color_temp_kelvin": 2200},
        smart_home=client,
    )

    assert "Home Assistant reports Living Room on" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.living_room"],
            {"brightness_pct": 20, "color_temp_kelvin": 2200},
        )
    ]


@pytest.mark.asyncio
async def test_control_devices_batches_same_domain_targets(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.control_devices(
        ["light.grid_bulb_1", "light.grid_bulb_2"],
        "turn_on",
        params={"brightness_pct": 20, "color_temp_kelvin": 2200},
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert "Smart Bulb 1" in result
    assert "Smart Bulb 2" in result
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1", "light.grid_bulb_2"],
            {"brightness_pct": 20, "color_temp_kelvin": 2200},
        )
    ]


@pytest.mark.asyncio
async def test_control_devices_errors_when_home_assistant_state_does_not_change(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()
    client.call_service = AsyncMock(return_value=None)

    with patch("plugins.smart_home.CONTROL_VERIFY_TIMEOUT_S", 0):
        result = await plugin.control_devices(
            ["light.living_room"],
            "turn_off",
            smart_home=client,
        )

    assert _text(result) == (
        "Home Assistant did not confirm the requested state for "
        "Living Room (state=on) after turn_off."
    )


@pytest.mark.asyncio
async def test_control_devices_errors_when_light_attributes_do_not_change(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()
    client.call_service = AsyncMock(return_value=None)

    with patch("plugins.smart_home.CONTROL_VERIFY_TIMEOUT_S", 0):
        result = await plugin.control_devices(
            ["light.living_room"],
            "turn_on",
            params={"brightness_pct": 80},
            smart_home=client,
        )

    assert _text(result) == (
        "Home Assistant did not confirm the requested state for "
        "Living Room (brightness=20%) after turn_on."
    )


@pytest.mark.asyncio
async def test_control_devices_batches_rgb_color_list(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.control_devices(
        ["light.grid_bulb_1", "light.grid_bulb_2"],
        "turn_on",
        params={"rgb_color": [0, 255, 0]},
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1", "light.grid_bulb_2"],
            {"rgb_color": [0, 255, 0]},
        )
    ]


@pytest.mark.asyncio
async def test_control_devices_accepts_rgb_dict(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    await plugin.control_devices(
        ["light.grid_bulb_1", "light.grid_bulb_2"],
        "turn_on",
        params={"rgb_color": {"r": 0, "g": 255, "b": 0}},
        smart_home=client,
    )

    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1", "light.grid_bulb_2"],
            {"rgb_color": [0, 255, 0]},
        )
    ]


@pytest.mark.asyncio
async def test_control_devices_accepts_home_assistant_color_name(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    await plugin.control_devices(
        ["light.grid_bulb_1", "light.grid_bulb_2"],
        "turn_on",
        params={"color_name": "green"},
        smart_home=client,
    )

    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1", "light.grid_bulb_2"],
            {"rgb_color": [38, 255, 56]},
        )
    ]


@pytest.mark.asyncio
async def test_control_devices_rejects_legacy_light_params(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.control_devices(
        ["light.grid_bulb_1", "light.grid_bulb_2"],
        "turn_on",
        params={"brightness": 20},
        smart_home=client,
    )

    assert _text(result).startswith("Unsupported light parameter")
    assert "brightness_pct" in _text(result)
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_control_devices_rejects_unsupported_action_before_ha_call(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_devices(
        ["light.living_room"],
        "set_attributes",
        params={"color_temp_kelvin": 2700},
        smart_home=client,
    )

    assert _text(result).startswith("Unsupported action 'set_attributes' for light")
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_control_devices_rejects_wildcard_before_ha_call(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_devices("*", "turn_on", smart_home=client)

    assert _text(result).startswith("Invalid entity_id '*'")
    assert "control_lights" in _text(result)
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_control_devices_rejects_unknown_entity_before_ha_call(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_devices(
        ["light.not_real"], "turn_on", smart_home=client
    )

    assert _text(result).startswith("No Home Assistant entity matched 'light.not_real'")
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_control_lights_resolves_whole_house_scope(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_lights(
        "all the lights in the house",
        "on",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [("light", "turn_on", ["light.living_room"], {})]


@pytest.mark.asyncio
async def test_adjust_lights_warmer_lowers_kelvin(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.adjust_lights(
        "living",
        warmth="warmer",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"color_temp_kelvin": 2379})
    ]


@pytest.mark.asyncio
async def test_adjust_lights_named_color_sets_destination(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.adjust_lights(
        "lights",
        color="orange",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1", "light.grid_bulb_2"],
            {"rgb_color": [255, 146, 20]},
        )
    ]


@pytest.mark.asyncio
async def test_adjust_lights_color_wins_over_warmth(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.adjust_lights(
        "lights",
        warmth="warmer",
        color="orange",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1", "light.grid_bulb_2"],
            {"rgb_color": [255, 146, 20]},
        )
    ]


@pytest.mark.asyncio
async def test_adjust_lights_orange_on_ct_only_uses_candle(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.adjust_lights(
        "living",
        warmth="warmer",
        color="orange",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"color_temp_kelvin": 2000})
    ]


@pytest.mark.asyncio
async def test_adjust_lights_warm_white_uses_kelvin(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.adjust_lights(
        "living",
        color="warm white",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"color_temp_kelvin": 2700})
    ]


@pytest.mark.asyncio
async def test_adjust_lights_reports_no_change_at_warmest(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    async def _states_at_warm_limit():
        states = _load("states_sample.json")
        states[0]["attributes"]["color_temp_kelvin"] = 2000
        return states

    async def _state_at_warm_limit(entity_id: str):
        for state in await _states_at_warm_limit():
            if state["entity_id"] == entity_id:
                return state
        raise RuntimeError("missing")

    client.get_states = _states_at_warm_limit
    client.get_state = _state_at_warm_limit

    result = await plugin.adjust_lights(
        "living",
        warmth="warmer",
        smart_home=client,
    )

    assert result == "No change. Living Room already at warmest setting."
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_adjust_lights_normalizes_more_color_phrase(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.adjust_lights(
        "lights",
        color="more orange",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1"],
            {"rgb_color": [255, 197, 130]},
        ),
        (
            "light",
            "turn_on",
            ["light.grid_bulb_2"],
            {"rgb_color": [55, 255, 5]},
        ),
    ]


@pytest.mark.asyncio
async def test_adjust_lights_skips_targets_without_hue(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    async def _mixed_capabilities(entity_id: str):
        state = await TuyaFakeHAClient.get_state(client, entity_id)
        if entity_id != "light.grid_bulb_2":
            return state
        attrs = dict(state["attributes"])
        attrs["supported_color_modes"] = ["color_temp"]
        return {**state, "attributes": attrs}

    client.get_state = _mixed_capabilities

    result = await plugin.adjust_lights(
        "lights",
        color="more blue",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert "Skipped Smart Bulb 2" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1"],
            {"rgb_color": [255, 221, 133]},
        )
    ]


def test_parse_color_phrase_detects_relative_hue() -> None:
    assert parse_color_phrase("more orange") == ("orange", True)
    assert parse_color_phrase("orange") == ("orange", False)


def test_resolve_hue_adjustment_shifts_rgb_toward_anchor() -> None:
    live = live_light_state_from_ha(
        {
            "entity_id": "light.bedroom",
            "state": "on",
            "attributes": {
                "supported_color_modes": ["rgb", "color_temp"],
                "color_mode": "rgb",
                "rgb_color": [0, 255, 0],
            },
        }
    )
    result = resolve_hue_adjustment("more orange", "slight", live)
    assert result == {"rgb_color": [55, 255, 5]}


def test_resolve_hue_adjustment_orange_uses_rgb_even_when_ct_supported() -> None:
    live = live_light_state_from_ha(
        {
            "entity_id": "light.bedroom",
            "state": "off",
            "attributes": {
                "supported_color_modes": ["rgb", "color_temp"],
                "min_color_temp_kelvin": 2700,
                "max_color_temp_kelvin": 6500,
            },
        }
    )
    assert resolve_hue_adjustment("orange", "slight", live) == {
        "rgb_color": [255, 146, 20]
    }
    assert resolve_hue_adjustment("vivid orange", "slight", live) == {
        "color_name": "orange"
    }


def test_resolve_hue_adjustment_named_hues_are_saturated() -> None:
    live = live_light_state_from_ha(
        {
            "entity_id": "light.bedroom",
            "state": "on",
            "attributes": {"supported_color_modes": ["rgb"]},
        }
    )
    assert resolve_hue_adjustment("orange", "slight", live) == {
        "rgb_color": [255, 146, 20]
    }
    assert resolve_hue_adjustment("green", "slight", live) == {
        "rgb_color": [38, 255, 56]
    }
    assert resolve_hue_adjustment("yellow", "slight", live) == {
        "rgb_color": [255, 239, 20]
    }
    assert resolve_hue_adjustment("blue", "slight", live) == {
        "rgb_color": [31, 158, 255]
    }


def test_resolve_hue_adjustment_warm_white_uses_kelvin() -> None:
    live = live_light_state_from_ha(
        {
            "entity_id": "light.bedroom",
            "state": "on",
            "attributes": {
                "supported_color_modes": ["rgb", "color_temp"],
                "min_color_temp_kelvin": 2000,
                "max_color_temp_kelvin": 6500,
            },
        }
    )
    assert resolve_hue_adjustment("warm white", "slight", live) == {
        "color_temp_kelvin": 2700
    }


def test_resolve_hue_adjustment_skips_hue_on_color_temp_only() -> None:
    live = live_light_state_from_ha(
        {
            "entity_id": "light.bedroom",
            "state": "on",
            "attributes": {"supported_color_modes": ["color_temp"]},
        }
    )
    assert resolve_hue_adjustment("blue", "slight", live) is None
    assert resolve_hue_adjustment("orange", "slight", live) == {
        "color_temp_kelvin": 2000
    }


@pytest.mark.asyncio
async def test_adjust_lights_dimmer_and_more_orange(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.adjust_lights(
        "lights",
        color="more orange",
        brightness_delta_pct=-20,
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.grid_bulb_1"],
            {"rgb_color": [255, 197, 130], "brightness_pct": 1},
        ),
        (
            "light",
            "turn_on",
            ["light.grid_bulb_2"],
            {"rgb_color": [55, 255, 5], "brightness_pct": 1},
        ),
    ]


@pytest.mark.asyncio
async def test_adjust_lights_brightness_delta_uses_live_state(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    async def _live_state(entity_id: str):
        state = await FakeHAClient.get_state(client, entity_id)
        if entity_id != "light.living_room" or entity_id in client.attribute_overrides:
            return state
        attrs = dict(state["attributes"])
        attrs["brightness"] = 25
        return {**state, "attributes": attrs}

    client.get_state = _live_state

    result = await plugin.adjust_lights(
        "living",
        brightness_delta_pct=-5,
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"brightness_pct": 5})
    ]


@pytest.mark.asyncio
async def test_adjust_lights_direction_uses_amount_steps(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    async def _live_state(entity_id: str):
        state = await FakeHAClient.get_state(client, entity_id)
        if entity_id != "light.living_room" or entity_id in client.attribute_overrides:
            return state
        attrs = dict(state["attributes"])
        attrs["brightness"] = 128
        return {**state, "attributes": attrs}

    client.get_state = _live_state

    result = await plugin.adjust_lights(
        "living",
        direction="dimmer",
        amount="normal",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"brightness_pct": 30})
    ]


@pytest.mark.asyncio
async def test_adjust_lights_dim_query_does_not_require_direction(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    async def _live_state(entity_id: str):
        state = await FakeHAClient.get_state(client, entity_id)
        if entity_id != "light.living_room" or entity_id in client.attribute_overrides:
            return state
        attrs = dict(state["attributes"])
        attrs["brightness"] = 128
        return {**state, "attributes": attrs}

    client.get_state = _live_state

    result = await plugin.adjust_lights(
        "dim the living room lights",
        amount="normal",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"brightness_pct": 30})
    ]


@pytest.mark.asyncio
async def test_adjust_lights_amount_alone_names_recovery(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.adjust_lights("living", amount="normal", smart_home=client)

    assert "direction=" in _text(result)
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_control_lights_in_here_scopes_to_current_room(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    with patch(
        "plugins.smart_home.resolve_area_from_context",
        AsyncMock(return_value="living_room"),
    ):
        result = await plugin.control_lights("in here", "off", smart_home=client)

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [("light", "turn_off", ["light.living_room"], {})]


@pytest.mark.asyncio
async def test_control_lights_lights_in_here_scopes_to_current_room(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    with patch(
        "plugins.smart_home.resolve_area_from_context",
        AsyncMock(return_value="living_room"),
    ):
        result = await plugin.control_lights(
            "lights in here", "off", smart_home=client
        )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [("light", "turn_off", ["light.living_room"], {})]


@pytest.mark.asyncio
async def test_control_lights_unmatched_query_lists_rooms(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_lights(
        "master bedroom lights", "on", smart_home=client
    )

    message = _text(result)
    assert "No lights matched 'master bedroom lights'" in message
    assert "Rooms:" in message


@pytest.mark.asyncio
async def test_control_lights_in_here_brightness_rides_on_turn_on(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    with patch(
        "plugins.smart_home.resolve_area_from_context",
        AsyncMock(return_value="living_room"),
    ):
        result = await plugin.control_lights(
            "in here", "on", brightness_pct=30, smart_home=client
        )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"brightness_pct": 30})
    ]


@pytest.mark.asyncio
async def test_control_lights_passes_transition_seconds(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_lights(
        "living room",
        "on",
        brightness_pct=100,
        transition="15 minutes",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        (
            "light",
            "turn_on",
            ["light.living_room"],
            {"brightness_pct": 100, "transition": 900},
        )
    ]


@pytest.mark.asyncio
async def test_control_lights_warm_white_uses_kelvin(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.control_lights(
        "living room",
        "on",
        color_name="warm white",
        smart_home=client,
    )

    assert "Home Assistant reports" in _text(result)
    assert client.service_calls == [
        ("light", "turn_on", ["light.living_room"], {"color_temp_kelvin": 2700})
    ]


@pytest.mark.asyncio
async def test_control_lights_in_here_unbound_node_errors_without_ha_call(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    with (
        patch(
            "plugins.smart_home.resolve_area_from_context", AsyncMock(return_value=None)
        ),
        pytest.raises(RuntimeError, match="not bound to a Home Assistant area"),
    ):
        await plugin.control_lights("in here", "off", smart_home=client)

    assert client.service_calls == []


@pytest.mark.asyncio
async def test_get_device_state_includes_light_attributes(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()

    result = await plugin.get_device_state("light.living_room", smart_home=client)

    assert "Living Room is on." in result
    assert "Brightness: 20%." in result
    assert "Color temperature: 2700K." in result


@pytest.mark.asyncio
async def test_get_device_states_returns_batch_light_attributes(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.get_device_states(
        ["light.grid_bulb_1", "light.grid_bulb_2"],
        smart_home=client,
    )

    assert result[0] == "Smart Bulb 1 is off."
    assert "Smart Bulb 2 is on." in result[1]
    assert "Color mode: rgb." in result[1]
    assert "RGB color: [0, 255, 0]." in result[1]


@pytest.mark.asyncio
async def test_build_inventory_captures_light_attributes() -> None:
    snapshot = await build_inventory(FakeHAClient())
    living = next(e for e in snapshot.entities if e.entity_id == "light.living_room")
    assert living.supported_color_modes == ["brightness", "color_temp"]
    assert living.brightness == 51
    assert living.color_temp_kelvin == 2700
    assert living.min_color_temp_kelvin == 2000


@pytest.mark.asyncio
async def test_check_readiness_with_fake_client() -> None:
    liveness = LivenessStatus(
        configured=True,
        reachable=True,
        authenticated=True,
        message="ok",
    )
    with patch(
        "plugins.smart_home.status.check_liveness", AsyncMock(return_value=liveness)
    ):
        status = await check_readiness(FakeHAClient())
    assert status.ready is True
    assert status.setup_candidate == "light.living_room"


@pytest.mark.asyncio
async def test_build_inventory_includes_config_entry_metadata() -> None:
    snapshot = await build_inventory(TuyaFakeHAClient())
    bulb = next(e for e in snapshot.entities if e.entity_id == "light.grid_bulb_1")
    assert bulb.config_entry_ids == ["tuya-entry-1"]
    assert bulb.platform == "tuya"
    assert bulb.manufacturer == "Grid Connect"


def test_entities_for_config_entry_filters_by_membership() -> None:
    snapshot = InventorySnapshot(
        captured_at="2026-06-04T10:00:00+00:00",
        area_count=0,
        device_count=2,
        entity_count=2,
        entities=[
            InventoryEntity(
                entity_id="light.grid_bulb_1",
                name="Bulb 1",
                domain="light",
                state="off",
                config_entry_ids=["tuya-entry-1", "bridge-entry"],
            ),
            InventoryEntity(
                entity_id="switch.office_fan",
                name="Office Fan",
                domain="switch",
                state="on",
                config_entry_ids=["entry-other"],
            ),
        ],
    )
    matches = entities_for_config_entry(snapshot, "tuya-entry-1", safe_only=True)
    assert [m.entity_id for m in matches] == ["light.grid_bulb_1"]


def test_match_area_by_name_is_case_insensitive() -> None:
    areas = _load("area_registry_list.json")
    match = match_area_by_name(areas, "  living   room ")
    assert match["area_id"] == "living_room"


@pytest.mark.asyncio
async def test_refresh_home_assistant_reports_tuya_candidates(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    result = await plugin.refresh_home_assistant(smart_home=client)

    assert result.outcome == "reload_ok_with_entities"
    assert result.candidate_count == 2
    assert client.last_reload == "tuya-entry-1"


@pytest.mark.asyncio
async def test_refresh_home_assistant_reloads_all_tuya_entries(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()
    reloads: list[str] = []

    async def _entries(domain=None):
        entries = [
            {"entry_id": "tuya-entry-1", "domain": "tuya", "state": "loaded"},
            {"entry_id": "tuya-entry-2", "domain": "tuya", "state": "loaded"},
        ]
        if domain:
            return [e for e in entries if e["domain"] == domain]
        return entries

    async def _reload(entry_id: str):
        reloads.append(entry_id)

    client.list_config_entries = _entries
    client.reload_config_entry = _reload

    result = await plugin.refresh_home_assistant(smart_home=client)

    assert result.outcome == "reload_ok_with_entities"
    assert reloads == ["tuya-entry-1", "tuya-entry-2"]


@pytest.mark.asyncio
async def test_refresh_home_assistant_missing_integration(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = FakeHAClient()
    client.list_config_entries = AsyncMock(return_value=[])

    result = await plugin.refresh_home_assistant(smart_home=client)
    assert result.outcome == "integration_missing"
    assert "Tuya" in result.message


@pytest.mark.asyncio
async def test_refresh_home_assistant_reload_failed(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    async def _reload(_entry_id: str):
        raise HomeAssistantError("reload failed")

    client.reload_config_entry = _reload
    result = await plugin.refresh_home_assistant(smart_home=client)
    assert result.outcome == "reload_failed"


@pytest.mark.asyncio
async def test_refresh_home_assistant_reports_stuck_reload(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()

    with patch.object(
        plugin,
        "_wait_for_config_entry_loaded",
        AsyncMock(return_value=(False, "timeout")),
    ):
        result = await plugin.refresh_home_assistant(smart_home=client)

    assert result.outcome == "reload_failed"
    assert "did not reload" in result.message


@pytest.mark.asyncio
async def test_wait_for_config_entry_requires_loaded_state(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()
    client.list_config_entries = AsyncMock(
        return_value=[{"entry_id": "tuya-entry-1", "state": "setup_error"}]
    )

    loaded, state = await plugin._wait_for_config_entry_loaded(
        client,
        "tuya-entry-1",
    )

    assert loaded is False
    assert state == "setup_error"


@pytest.mark.asyncio
async def test_wait_for_config_entry_retries_pending_state(tool_context) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()
    client.list_config_entries = AsyncMock(
        side_effect=[
            [{"entry_id": "tuya-entry-1", "state": "setup_retry"}],
            [{"entry_id": "tuya-entry-1", "state": "loaded"}],
        ]
    )

    with patch("plugins.smart_home.RELOAD_POLL_INTERVAL_S", 0):
        loaded, state = await plugin._wait_for_config_entry_loaded(
            client,
            "tuya-entry-1",
        )

    assert loaded is True
    assert state == "loaded"


@pytest.mark.asyncio
async def test_refresh_home_assistant_reports_no_entities_after_reload(
    tool_context,
) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()
    client.get_states = AsyncMock(return_value=[])

    result = await plugin.refresh_home_assistant(smart_home=client)

    assert result.outcome == "reload_ok_no_entities"


@pytest.mark.asyncio
async def test_organize_device_updates_registry_and_invalidates_cache(
    smart_home_tool_data_store, tool_context
) -> None:
    plugin = SmartHomePlugin()
    client = TuyaFakeHAClient()
    smart_home_tool_data_store.data["smart_home"] = {
        "inventory": {
            "captured_at_epoch": 9999999999,
            "snapshot": {
                "captured_at": "old",
                "area_count": 0,
                "device_count": 0,
                "entity_count": 0,
                "entities": [],
            },
        }
    }

    result = await plugin.organize_device(
        "light.grid_bulb_1",
        name="Bedroom Lamp",
        area_name="Living Room",
        smart_home=client,
    )

    assert result.name == "Bedroom Lamp"
    assert result.area_name == "Living Room"
    assert client.device_updates[0] == "grid-bulb-1"
    store = smart_home_tool_data_store.data["smart_home"]
    assert "inventory" in store
    assert store["inventory"]["snapshot"]["entity_count"] == 2
