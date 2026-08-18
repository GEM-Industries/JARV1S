"""
Smart Home Plugin for JARV1S.

Direct Home Assistant REST/WebSocket client for discovery, control, and setup.

Grid Connect / Tuya path: reload integration, reconcile registry, organize.
Tapo/Kasa second slice: drive per-device config flows via ha_client config-flow REST methods.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from core.context import get_ctx, get_node_id, get_owner_id
from core.decorators import tool
from core.plugins.consent import require_consent
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.capabilities import CapabilityErrorDetail
from plugins.db import get_tool_data, store_tool_data
from plugins.smart_home.domains import (
    ADJUST_AMOUNTS,
    COLOR_MODES,
    brightness_pct_from_ha,
    clamp_light_params,
    default_service_for_action,
    entity_domain,
    is_supported_control_action,
    live_light_state_from_ha,
    relative_kelvin_adjustment,
    requires_control_consent,
    resolve_hue_adjustment,
    supported_control_actions,
)
from plugins.smart_home.ha_client import HomeAssistantClient, HomeAssistantError
from plugins.smart_home.inventory import (
    TOOL_DATA_KEY,
    InventoryEntity,
    entities_for_config_entry,
    entity_to_device_summary,
    match_area_by_name,
    search_inventory,
)
from plugins.smart_home.models import (
    DeviceSummary,
    OrganizeDeviceResult,
    RefreshHomeAssistantResult,
)
from plugins.smart_home.node_binding import bind_node, resolve_area_from_context
from plugins.smart_home.rooms import (
    RoomMutationResponse,
    RoomsResponse,
    create_room as create_ha_room,
    list_rooms as list_ha_rooms,
    rename_room as rename_ha_room,
)
from plugins.smart_home.status import (
    check_liveness,
    check_readiness,
    force_refresh_inventory,
    inventory_cache_payload,
    load_or_refresh_inventory,
)

ROOM_REFERENCE_QUERIES = {"here", "this room", "in here", "this area"}
WARMTH_STEPS_MIRED = {
    "slight": 50,
    "normal": 100,
    "large": 150,
}


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)



def _normalize_room_name(name: str) -> str:
    return " ".join(name.strip().split())


TUYA_SETUP_HANDOFF = (
    "Home Assistant does not have the Tuya integration linked yet. "
    "In Home Assistant: Settings → Devices & Services → Add Integration → Tuya. "
    "Use the Smart Life or Tuya Smart app (not Grid Connect) to scan the QR code and authorize. "
    "After linking, add bulbs in Smart Life, then tell JARV1S to refresh again."
)

RELOAD_POLL_INTERVAL_S = 0.5
RELOAD_POLL_TIMEOUT_S = 15.0
CONTROL_VERIFY_INTERVAL_S = 0.2
CONTROL_VERIFY_TIMEOUT_S = 1.0
CONFIG_ENTRY_PENDING_STATES = {"setup_in_progress", "setup_retry", "unload_in_progress"}
BRIGHTNESS_VERIFY_TOLERANCE_PCT = 2
KELVIN_VERIFY_TOLERANCE = 100
RGB_VERIFY_TOLERANCE = 12


def _format_params(params: dict[str, Any]) -> str:
    if not params:
        return ""
    rendered = ", ".join(f"{key}={value}" for key, value in params.items())
    return f" Requested {rendered}."


def _friendly_state_name(state: dict[str, Any], fallback: str) -> str:
    return str((state.get("attributes") or {}).get("friendly_name") or fallback)


async def _wait_for_device_states(
    smart_home: HomeAssistantClient,
    entity_ids: list[str],
    expected_state: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + CONTROL_VERIFY_TIMEOUT_S
    while True:
        states = await asyncio.gather(
            *(smart_home.get_state(entity_id) for entity_id in entity_ids)
        )
        if all(
            not _state_mismatches(state, expected_state, payload) for state in states
        ):
            return states
        if time.monotonic() >= deadline:
            return states
        await asyncio.sleep(CONTROL_VERIFY_INTERVAL_S)


def _entity_from_snapshot(snapshot, entity_id: str) -> InventoryEntity | None:
    return next((e for e in snapshot.entities if e.entity_id == entity_id), None)


def _coerce_entity_ids(entity_ids: list[str] | str | None) -> list[str]:
    if entity_ids is None:
        return []
    if isinstance(entity_ids, str):
        raw_ids = [entity_ids]
    else:
        try:
            raw_ids = list(entity_ids)
        except TypeError:
            raw_ids = [entity_ids]
    return [
        entity_id
        for entity_id in dict.fromkeys(str(item).strip() for item in raw_ids)
        if entity_id
    ]


def _invalid_entity_id_reason(entity_id: str) -> str | None:
    if entity_id in {"*", "all", "any"}:
        return "wildcards are not valid entity_ids"
    if "." not in entity_id:
        return "entity_ids must look like 'domain.name'"
    return None


def _normalize_light_action(action: str) -> str | None:
    normalized = " ".join(
        str(action or "").strip().casefold().replace("-", "_").split()
    )
    normalized = normalized.replace(" ", "_")
    return {
        "on": "turn_on",
        "turn_on": "turn_on",
        "off": "turn_off",
        "turn_off": "turn_off",
        "toggle": "toggle",
    }.get(normalized)


def _clamp_params_for_entity(
    entity: InventoryEntity | None,
    entity_id: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    domain = entity_domain(entity_id)
    if domain != "light":
        return dict(params or {})
    stub = entity or InventoryEntity(
        entity_id=entity_id, name=entity_id, domain="light", state="unknown"
    )
    return clamp_light_params(stub, params)


def _format_light_state(attrs: dict[str, Any], state: str) -> str:
    parts: list[str] = []
    color_mode = attrs.get("color_mode")
    if color_mode:
        parts.append(f"Color mode: {color_mode}.")
    brightness_pct = brightness_pct_from_ha(
        int(attrs["brightness"])
        if isinstance(attrs.get("brightness"), int | float)
        else None,
        state=state,
    )
    if brightness_pct is not None:
        parts.append(f"Brightness: {brightness_pct}%.")
    kelvin = attrs.get("color_temp_kelvin")
    if isinstance(kelvin, int | float):
        parts.append(f"Color temperature: {int(kelvin)}K.")
    rgb = attrs.get("rgb_color")
    if isinstance(rgb, list | tuple) and len(rgb) == 3:
        parts.append(f"RGB color: {[int(c) for c in rgb]}.")
    return " ".join(parts)


def _state_mismatches(
    state: dict[str, Any],
    expected_state: str | None,
    payload: dict[str, Any],
) -> list[str]:
    mismatches: list[str] = []
    actual_state = str(state.get("state", "unknown"))
    if expected_state is not None and actual_state != expected_state:
        mismatches.append(f"state={actual_state}")

    attrs = state.get("attributes") or {}
    if "brightness_pct" in payload:
        brightness = attrs.get("brightness")
        actual = brightness_pct_from_ha(
            int(brightness) if isinstance(brightness, int | float) else None,
            state=actual_state,
        )
        expected = int(payload["brightness_pct"])
        if actual is None or abs(actual - expected) > BRIGHTNESS_VERIFY_TOLERANCE_PCT:
            mismatches.append(
                f"brightness={actual if actual is not None else 'unknown'}%"
            )

    if "color_temp_kelvin" in payload:
        actual = attrs.get("color_temp_kelvin")
        expected = int(payload["color_temp_kelvin"])
        if (
            not isinstance(actual, int | float)
            or abs(actual - expected) > KELVIN_VERIFY_TOLERANCE
        ):
            mismatches.append(
                f"color_temp={actual if actual is not None else 'unknown'}K"
            )

    if "rgb_color" in payload:
        actual = attrs.get("rgb_color")
        expected = payload["rgb_color"]
        color_mode = attrs.get("color_mode")
        if color_mode == "rgb":
            valid = (
                isinstance(actual, list | tuple)
                and len(actual) == 3
                and all(
                    abs(float(a) - float(e)) <= RGB_VERIFY_TOLERANCE
                    for a, e in zip(actual, expected, strict=True)
                )
            )
        else:
            valid = color_mode in COLOR_MODES
        if not valid:
            mismatches.append(f"rgb={actual if actual is not None else 'unknown'}")

    if "color_name" in payload and attrs.get("color_mode") not in COLOR_MODES:
        mismatches.append(f"color_mode={attrs.get('color_mode') or 'unknown'}")

    return mismatches


def _payload_key(payload: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(
        sorted((key, _freeze_payload_value(value)) for key, value in payload.items())
    )


def _freeze_payload_value(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(
            sorted((key, _freeze_payload_value(item)) for key, item in value.items())
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_payload_value(item) for item in value)
    return value


def _thaw_payload_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_thaw_payload_value(item) for item in value]
    return value


class SmartHomePlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="smart_home",
        version="2.0.0",
        description="Control smart home devices via Home Assistant.",
        utterances=[
            "turn on the living room lights",
            "set the thermostat to 22 degrees",
            "lock the front door",
            "dim the bedroom lights to 40 percent",
            "is the garage door open",
            "turn off all the lights",
            "turn on all the lights",
            "turn all the lights on in the house",
            "turn off the lights",
            "lights on",
            "turn the lights down",
            "make the bedroom lights warmer",
            "make the lights more orange",
        ],
    )

    async def register_integrations(self) -> None:
        from core.integrations import integrations
        from plugins.smart_home.client import create_smart_home_client

        integrations.register(
            "smart_home",
            create_smart_home_client,
        )

    async def _get_tool_data(self) -> dict:
        return await get_tool_data("smart_home")

    async def _store_inventory(self, snapshot) -> None:
        data = await self._get_tool_data()
        data[TOOL_DATA_KEY] = inventory_cache_payload(snapshot)
        await store_tool_data("smart_home", data)

    async def _invalidate_inventory_cache(self) -> None:
        data = await self._get_tool_data()
        data.pop(TOOL_DATA_KEY, None)
        await store_tool_data("smart_home", data)

    async def _inventory(self, client: HomeAssistantClient, *, force: bool = False):
        if force:
            snapshot = await force_refresh_inventory(client)
            await self._store_inventory(snapshot)
            return snapshot

        data = await self._get_tool_data()
        cached = data.get(TOOL_DATA_KEY)
        snapshot = await load_or_refresh_inventory(client, cached)
        await self._store_inventory(snapshot)
        return snapshot

    async def _wait_for_config_entry_loaded(
        self,
        client: HomeAssistantClient,
        entry_id: str,
        *,
        timeout_s: float = RELOAD_POLL_TIMEOUT_S,
    ) -> tuple[bool, str]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            entries = await client.list_config_entries()
            entry = next((e for e in entries if e.get("entry_id") == entry_id), None)
            if not entry:
                await asyncio.sleep(RELOAD_POLL_INTERVAL_S)
                continue
            state = entry.get("state")
            if state == "loaded":
                return True, "loaded"
            if state in CONFIG_ENTRY_PENDING_STATES:
                await asyncio.sleep(RELOAD_POLL_INTERVAL_S)
                continue
            return False, str(state or "unknown")
        return False, "timeout"

    @tool(inject=["smart_home"])
    async def refresh_home_assistant(
        self,
        reload_domain: str | None = "tuya",
        smart_home: HomeAssistantClient = None,
    ) -> RefreshHomeAssistantResult:
        """
        Reload a Home Assistant integration and list controllable devices it owns.
        Use after the user adds devices in Smart Life/Tuya or when HA inventory looks stale.
        Returns reload_failed, reload_ok_no_entities, reload_ok_with_entities, or integration_missing.
        """
        domain = (reload_domain or "tuya").strip().lower()
        entries = await smart_home.list_config_entries(domain=domain)
        if not entries:
            return RefreshHomeAssistantResult(
                outcome="integration_missing",
                message=TUYA_SETUP_HANDOFF
                if domain == "tuya"
                else f"No Home Assistant integration found for domain '{domain}'.",
            )

        entry_ids = [
            str(entry["entry_id"]) for entry in entries if entry.get("entry_id")
        ]
        if not entry_ids:
            return RefreshHomeAssistantResult(
                outcome="reload_failed",
                message=f"Found {domain} integration entries but none had an entry_id.",
                error="missing entry_id",
            )

        reload_errors: list[str] = []
        loaded_entry_ids: list[str] = []
        for entry_id in entry_ids:
            try:
                await smart_home.reload_config_entry(entry_id)
            except HomeAssistantError as e:
                reload_errors.append(f"{entry_id}: {e}")
                continue

            loaded, state = await self._wait_for_config_entry_loaded(
                smart_home, entry_id
            )
            if loaded:
                loaded_entry_ids.append(entry_id)
            else:
                reload_errors.append(f"{entry_id}: did not become loaded ({state})")

        if not loaded_entry_ids:
            return RefreshHomeAssistantResult(
                outcome="reload_failed",
                message=f"Home Assistant did not reload any {domain} integration entries successfully.",
                error="; ".join(reload_errors) or "reload failed",
            )

        snapshot = await self._inventory(smart_home, force=True)
        candidates = [
            entity
            for entry_id in loaded_entry_ids
            for entity in entities_for_config_entry(snapshot, entry_id, safe_only=True)
        ]
        summaries = [entity_to_device_summary(entity) for entity in candidates]

        if not summaries:
            return RefreshHomeAssistantResult(
                outcome="reload_ok_no_entities",
                message=(
                    f"Reloaded the {domain} integration but no controllable devices are visible yet. "
                    "Tuya cloud sync can take a moment — ask again shortly."
                ),
                config_entry_id=loaded_entry_ids[0],
                candidate_count=0,
                error="; ".join(reload_errors) or None,
            )

        names = ", ".join(item.name for item in summaries[:5])
        return RefreshHomeAssistantResult(
            outcome="reload_ok_with_entities",
            message=f"Reloaded {domain} and found {len(summaries)} controllable device(s): {names}.",
            config_entry_id=loaded_entry_ids[0],
            candidate_count=len(summaries),
            candidates=summaries,
            error="; ".join(reload_errors) or None,
        )

    @tool(inject=["smart_home"])
    async def organize_device(
        self,
        entity_id: str,
        name: str | None = None,
        area_name: str | None = None,
        smart_home: HomeAssistantClient = None,
    ) -> OrganizeDeviceResult:
        """
        Name a device and assign it to a Home Assistant area during setup.
        Use an entity_id returned by refresh_home_assistant or search_devices.
        Never ask the user to speak an entity_id aloud.
        """
        snapshot = await self._inventory(smart_home, force=True)
        entity = next((e for e in snapshot.entities if e.entity_id == entity_id), None)
        if not entity:
            raise RuntimeError(
                f"Entity not found in Home Assistant inventory: {entity_id}"
            )

        area_id: str | None = None
        resolved_area_name: str | None = None
        if area_name:
            areas = await smart_home.list_areas()
            match = match_area_by_name(areas, area_name)
            if match:
                area_id = match["area_id"]
                resolved_area_name = match.get("name") or area_id
            else:
                created = await smart_home.create_area(area_name.strip())
                area_id = created.get("area_id")
                resolved_area_name = created.get("name") or area_name.strip()
                if not area_id:
                    raise RuntimeError(
                        f"Home Assistant did not create area '{area_name}'."
                    )

        if entity.device_id:
            await smart_home.update_device(
                entity.device_id,
                area_id=area_id,
                name_by_user=name,
            )
        if name is not None:
            await smart_home.update_entity(
                entity_id, name=name, area_id=area_id if not entity.device_id else None
            )
        elif area_id and not entity.device_id:
            await smart_home.update_entity(entity_id, area_id=area_id)

        await self._invalidate_inventory_cache()
        snapshot = await self._inventory(smart_home, force=True)
        updated = next(
            (e for e in snapshot.entities if e.entity_id == entity_id), entity
        )
        display_name = name or updated.name

        parts = [f"Organized {display_name}."]
        if resolved_area_name:
            parts.append(f"Area: {resolved_area_name}.")
        return OrganizeDeviceResult(
            entity_id=entity_id,
            name=display_name,
            area_name=resolved_area_name or updated.area_name,
            area_id=area_id or updated.area_id,
            message=" ".join(parts),
        )

    async def validate_connection(self, smart_home: HomeAssistantClient = None) -> str:
        """
        Check whether Home Assistant is configured, reachable, and accepting the token.
        Use during setup or troubleshooting — not for routine device control.
        """
        status = await check_liveness(smart_home.base_url, smart_home.token)
        return status.message

    @tool(inject=["smart_home"])
    async def get_setup_status(self, smart_home: HomeAssistantClient = None) -> str:
        """
        Full Home Assistant readiness check: registry access and safe controllable devices.
        Use during setup or troubleshooting after credentials are saved; use search_devices for routine device lookup/control.
        """
        status = await check_readiness(smart_home)
        parts = [status.message]
        parts.append(
            f"Entities: {status.entity_count}, safe controllable: {status.safe_controllable_count}."
        )
        if status.setup_candidate:
            parts.append(f"Suggested first device: {status.setup_candidate}.")
        return " ".join(parts)

    @tool(inject=["smart_home"])
    async def list_rooms(self, smart_home: HomeAssistantClient = None) -> RoomsResponse:
        """
        List Home Assistant rooms and any JARV1S satellites bound to them.
        Use when the user asks what rooms exist, where satellites are bound, or wants room setup help. Do not use for routine device lookup/control.
        """
        return await list_ha_rooms(smart_home, owner_id=get_owner_id())

    @tool(inject=["smart_home"])
    async def create_room(
        self, name: str, smart_home: HomeAssistantClient = None
    ) -> RoomMutationResponse | CapabilityErrorDetail:
        """
        Create a Home Assistant room by name.
        Use when the user explicitly asks to add a room. If the room already exists, treat that as a successful no-op.
        """
        room_name = _normalize_room_name(name)
        if not room_name:
            return _fail("Room name is required.")

        areas = await smart_home.list_areas()
        existing = match_area_by_name(areas, room_name)
        if existing:
            label = existing.get("name") or room_name
            return f"Room '{label}' already exists."

        async def _do_create() -> RoomMutationResponse:
            return await create_ha_room(
                smart_home, owner_id=get_owner_id(), name=room_name
            )

        return await require_consent(
            f"Create Home Assistant room '{room_name}'?",
            _do_create,
            detail=f"name={room_name}",
        )

    @tool(inject=["smart_home"])
    async def rename_room(
        self,
        room_name: str,
        new_name: str,
        smart_home: HomeAssistantClient = None,
    ) -> str:
        """
        Rename an existing Home Assistant room by its current name.
        Resolve the current room name first; ask the user if multiple names are plausible rather than guessing. Do not use for deleting rooms.
        """
        current_name = _normalize_room_name(room_name)
        target_name = _normalize_room_name(new_name)
        if not current_name or not target_name:
            return _fail("Current room name and new_name are required.")
        if current_name.casefold() == target_name.casefold():
            return f"Room is already named '{target_name}'."

        areas = await smart_home.list_areas()
        match = match_area_by_name(areas, current_name)
        if not match:
            names = ", ".join(
                str(a.get("name") or a.get("area_id", "")) for a in areas[:8]
            )
            return _fail(f"No Home Assistant room matched '{current_name}'. Known rooms: {names}")

        duplicate = match_area_by_name(areas, target_name)
        if duplicate and duplicate.get("area_id") != match.get("area_id"):
            return _fail(f"A different Home Assistant room is already named '{duplicate.get('name') or target_name}'.")

        area_id = str(match["area_id"])
        resolved_name = str(match.get("name") or current_name)

        async def _do_rename() -> RoomMutationResponse:
            result = await rename_ha_room(
                smart_home,
                owner_id=get_owner_id(),
                area_id=area_id,
                name=target_name,
            )
            label = result.room.name if result.room else target_name
            if result.affected_node_ids:
                from api.websockets.connection import manager as connection_manager
                from plugins.smart_home.node_binding import ha_area_to_location

                for node_id in result.affected_node_ids:
                    connection_manager.update_node_location(
                        get_owner_id(),
                        node_id,
                        ha_area_to_location(area_id, label),
                    )
            return result

        return await require_consent(
            f"Rename Home Assistant room '{resolved_name}' to '{target_name}'?",
            _do_rename,
            detail=f"area_id={area_id}; old_name={resolved_name}; new_name={target_name}",
        )

    @tool(inject=["smart_home"])
    async def search_devices(
        self,
        query: str,
        smart_home: HomeAssistantClient = None,
    ) -> list[DeviceSummary]:
        """
        Find smart home devices matching natural phrases like "lights", "bedroom lights", "bedroom one", or "in here".
        Returns entity_id, state, area, brightness_pct, color_temp_kelvin, color_mode, rgb_color, and capabilities. Pass entity_ids to control_devices or get_device_states.
        VOICE: List matches briefly by name and state.
        """
        matches = await self._search_entities(query, smart_home)
        states = await asyncio.gather(
            *(smart_home.get_state(entity.entity_id) for entity in matches)
        )
        summaries: list[DeviceSummary] = []
        for entity, state in zip(matches, states, strict=True):
            summary = entity_to_device_summary(entity)
            updates: dict[str, Any] = {"state": str(state.get("state", "unknown"))}
            if entity.domain == "light":
                live = live_light_state_from_ha(
                    state,
                    fallback_modes=entity.supported_color_modes,
                )
                updates.update(
                    brightness_pct=live.brightness_pct,
                    color_temp_kelvin=live.color_temp_kelvin,
                    color_mode=live.color_mode,
                    rgb_color=live.rgb_color,
                )
            summaries.append(summary.model_copy(update=updates))
        return summaries

    @tool(inject=["smart_home"])
    async def control_lights(
        self,
        query: str,
        action: Literal["on", "off", "toggle", "turn_on", "turn_off"],
        brightness_pct: int | None = None,
        color_temp_kelvin: int | None = None,
        color_name: str | None = None,
        smart_home: HomeAssistantClient = None,
    ) -> str:
        """
        Turn lights by natural scope.
        Use for all lights, room lights, or "in here" — entity_ids are resolved inside the tool. Brightness-only turn_on restores the last colour; for warm/white/cool/daylight also pass color_temp_kelvin or color_name. Use adjust_lights for relative changes (dimmer/brighter, warmer/cooler white, more orange/red/blue); use control_devices only when you already have exact entity_ids from search_devices.
        """
        service_action = _normalize_light_action(action)
        if service_action is None:
            return _fail("Unsupported light action. Use one of: on, off, toggle.")

        lights = [
            entity
            for entity in await self._search_entities(query, smart_home, complete=True)
            if entity.domain == "light"
        ]
        if not lights:
            return _fail(f"No lights matched '{query}'.")

        params: dict[str, Any] = {}
        if service_action == "turn_on":
            if brightness_pct is not None:
                params["brightness_pct"] = brightness_pct
            if color_temp_kelvin is not None:
                params["color_temp_kelvin"] = color_temp_kelvin
            if color_name is not None:
                params["color_name"] = color_name

        return await self.control_devices(
            [entity.entity_id for entity in lights],
            service_action,
            params=params or None,
            smart_home=smart_home,
        )

    async def _search_entities(
        self,
        query: str,
        smart_home: HomeAssistantClient,
        *,
        complete: bool = False,
    ) -> list[InventoryEntity]:
        ctx = get_ctx()
        normalized_query = query.strip().lower()
        area_id = None
        if normalized_query in ROOM_REFERENCE_QUERIES:
            area_id = await resolve_area_from_context(
                ctx.get("location_ref"), get_node_id()
            )

        if normalized_query in ROOM_REFERENCE_QUERIES and not area_id:
            raise RuntimeError(
                "This node is not bound to a Home Assistant area yet. "
                "Use bind_node_area during setup or specify a room name."
            )

        snapshot = await self._inventory(smart_home)
        return search_inventory(
            snapshot,
            query,
            area_id=area_id,
            dedupe_devices=not complete,
            limit=None if complete else 10,
        )

    async def control_device(
        self,
        entity_id: str,
        action: str,
        params: dict[str, Any] | None = None,
        smart_home: HomeAssistantClient = None,
    ) -> str:
        """
        Internal compatibility wrapper. Prefer control_devices with a one-item entity_ids list.
        """
        return await self.control_devices(
            [entity_id],
            action,
            params=params,
            smart_home=smart_home,
        )

    @tool(inject=["smart_home"])
    async def control_devices(
        self,
        entity_ids: list[str],
        action: str,
        params: dict[str, Any] | None = None,
        smart_home: HomeAssistantClient = None,
    ) -> str:
        """
        Execute on exact Home Assistant entity_ids only.
        Do not pass wildcards or natural scopes like "all lights" — use control_lights instead. Pass entity_ids from search_devices. Valid common actions: turn_on, turn_off, toggle. For lights, use turn_on to apply brightness_pct 1-100, color_temp_kelvin, rgb_color [r,g,b], or color_name. Named colours (orange, red, blue) are hues — use adjust_lights(color=...). Warmer/cooler is white balance (Kelvin) via adjust_lights(warmth=...). Prefer adjust_lights for relative brightness, hue, or warmth requests.
        VOICE: Confirm the group outcome briefly.
        """
        targets = _coerce_entity_ids(entity_ids)
        if not targets:
            return _fail("No entity_ids provided.")

        snapshot = await self._inventory(smart_home)
        entity_by_id = {entity.entity_id: entity for entity in snapshot.entities}
        groups: dict[tuple[str, str, str, tuple[tuple[str, Any], ...]], list[str]] = {}
        for entity_id in targets:
            invalid_reason = _invalid_entity_id_reason(entity_id)
            if invalid_reason:
                return _fail(
                    f"Invalid entity_id '{entity_id}': {invalid_reason}. "
                    'Use control_lights(query="lights", action="on") for all lights, '
                    "or call search_devices first and pass the returned entity_id values."
                )
            entity = entity_by_id.get(entity_id)
            if entity is None:
                return _fail(
                    f"No Home Assistant entity matched '{entity_id}'. "
                    "Call search_devices first and pass the returned entity_id values."
                )
            domain = entity_domain(entity_id)
            if not is_supported_control_action(domain, action):
                allowed = ", ".join(sorted(supported_control_actions(domain)))
                return _fail(f"Unsupported action '{action}' for {domain}. Use one of: {allowed}.")
            svc_domain, service = default_service_for_action(domain, action)
            try:
                payload = _clamp_params_for_entity(entity, entity_id, params)
            except ValueError as exc:
                return _fail(f"{exc}")
            key = (domain, svc_domain, service, _payload_key(payload))
            groups.setdefault(key, []).append(entity_id)

        async def _do_control() -> str:
            results = []
            for (
                domain,
                svc_domain,
                service,
                payload_items,
            ), grouped_ids in groups.items():
                payload = {
                    key: _thaw_payload_value(value) for key, value in payload_items
                }
                await smart_home.call_service(
                    svc_domain, service, entity_id=grouped_ids, data=payload
                )
                expected_state = {"turn_on": "on", "turn_off": "off"}.get(service)
                if expected_state is None:
                    states = await asyncio.gather(
                        *(smart_home.get_state(entity_id) for entity_id in grouped_ids)
                    )
                else:
                    states = await _wait_for_device_states(
                        smart_home,
                        grouped_ids,
                        expected_state,
                        payload,
                    )
                names = [
                    _friendly_state_name(state, entity_id)
                    for state, entity_id in zip(states, grouped_ids, strict=True)
                ]
                mismatches = []
                for name, state in zip(names, states, strict=True):
                    reasons = _state_mismatches(state, expected_state, payload)
                    if reasons:
                        mismatches.append(f"{name} ({', '.join(reasons)})")
                if mismatches:
                    return _fail(
                        "Home Assistant did not confirm the requested state for "
                        f"{', '.join(mismatches)} after {service}."
                    )
                results.append(
                    f"Home Assistant reports {', '.join(names)} "
                    f"{expected_state or 'updated'} ({', '.join(grouped_ids)}).{_format_params(payload)}"
                )
            return " ".join(results)

        risky = [
            entity_id
            for entity_id in targets
            if requires_control_consent(entity_id, action)
        ]
        if not risky:
            return await _do_control()

        states = await asyncio.gather(
            *(smart_home.get_state(entity_id) for entity_id in targets)
        )
        names = [
            _friendly_state_name(state, entity_id)
            for state, entity_id in zip(states, targets, strict=True)
        ]
        return await require_consent(
            f"Run '{action}' on {', '.join(names)}?",
            _do_control,
            detail=f"entity_ids={targets}; action={action}; params={params or {}}",
        )

    @tool(inject=["smart_home"])
    async def adjust_lights(
        self,
        query: str,
        warmth: Literal["warmer", "cooler"] | None = None,
        amount: Literal["slight", "normal", "large"] = "slight",
        brightness_delta_pct: int | None = None,
        color: str | None = None,
        smart_home: HomeAssistantClient = None,
    ) -> str:
        """
        Adjust lights from a natural query like "bedroom lights" or "in here" without HA service details.
        Use brightness_delta_pct for brighter/dimmer (negative dims). Use warmth for warmer/cooler white balance. Use color to move toward a named hue like orange, red, or blue. Never use warmth for named colours. Returns a no-change result when lights are already at their limit.
        """
        if warmth is not None and color:
            return _fail("Choose either warmth for white balance or color for a named hue, not both.")
        if warmth is None and brightness_delta_pct is None and not color:
            return _fail("No light adjustment requested.")

        lights = [
            entity
            for entity in await self._search_entities(query, smart_home, complete=True)
            if entity.domain == "light"
        ]
        if not lights:
            return _fail(f"No lights matched '{query}'.")

        amount_key = amount if amount in ADJUST_AMOUNTS else "slight"
        ha_states = await asyncio.gather(
            *(smart_home.get_state(entity.entity_id) for entity in lights)
        )
        live_by_id = {
            entity.entity_id: live_light_state_from_ha(
                state,
                fallback_modes=entity.supported_color_modes,
            )
            for entity, state in zip(lights, ha_states, strict=True)
        }

        payload_groups: dict[tuple[tuple[str, Any], ...], list[str]] = {}
        unchanged: list[str] = []
        unsupported_temp: list[str] = []

        for entity in lights:
            live = live_by_id[entity.entity_id]
            params: dict[str, Any] = {}

            if warmth is not None:
                if "color_temp" not in live.supported_color_modes:
                    unsupported_temp.append(entity.name)
                else:
                    kelvin = relative_kelvin_adjustment(
                        live,
                        warmth,
                        amount_key,
                        WARMTH_STEPS_MIRED,
                    )
                    if kelvin is None:
                        unchanged.append(entity.name)
                    else:
                        params["color_temp_kelvin"] = kelvin

            if brightness_delta_pct is not None:
                current = (
                    live.brightness_pct
                    if live.brightness_pct is not None
                    else (1 if live.state != "on" else 50)
                )
                params["brightness_pct"] = max(
                    1, min(100, current + brightness_delta_pct)
                )

            if color:
                hue_params = resolve_hue_adjustment(
                    color, amount_key, live, relative=True
                )
                if hue_params is None:
                    unchanged.append(entity.name)
                else:
                    params.update(hue_params)

            if params:
                live_entity = entity.model_copy(
                    update={
                        "supported_color_modes": live.supported_color_modes,
                        "min_color_temp_kelvin": live.min_color_temp_kelvin,
                        "max_color_temp_kelvin": live.max_color_temp_kelvin,
                    }
                )
                try:
                    payload = clamp_light_params(live_entity, params)
                except ValueError as exc:
                    return _fail(f"{exc}")
                payload_groups.setdefault(_payload_key(payload), []).append(
                    entity.entity_id
                )

        if not payload_groups:
            if unsupported_temp:
                names = ", ".join(unsupported_temp)
                return _fail(f"{names} do not support color temperature.")
            names = ", ".join(dict.fromkeys(unchanged))
            if warmth is not None:
                direction = "warmest" if warmth == "warmer" else "coolest"
                return f"No change. {names} already at {direction} setting."
            return f"No change. {names} already at the requested setting."

        results = []
        for payload_items, entity_ids in payload_groups.items():
            payload = {key: _thaw_payload_value(value) for key, value in payload_items}
            results.append(
                await self.control_devices(
                    entity_ids,
                    "turn_on",
                    params=payload,
                    smart_home=smart_home,
                )
            )
        return " ".join(results)

    async def get_device_state(
        self,
        entity_id: str,
        smart_home: HomeAssistantClient = None,
    ) -> str:
        """
        Internal compatibility wrapper. Prefer get_device_states with a one-item entity_ids list.
        """
        return await self._format_device_state(entity_id, smart_home)

    @tool(inject=["smart_home"])
    async def get_device_states(
        self,
        entity_ids: list[str],
        smart_home: HomeAssistantClient = None,
    ) -> list[str]:
        """
        Get fresh current states for multiple smart home devices.
        Use for one or many devices, especially after control_devices to verify grouped lights/devices in one observable result.
        For lights, returns brightness_pct, color_temp_kelvin, color_mode, and rgb_color when Home Assistant reports them.
        """
        targets = [entity_id for entity_id in dict.fromkeys(entity_ids) if entity_id]
        states = await asyncio.gather(
            *(self._format_device_state(entity_id, smart_home) for entity_id in targets)
        )
        return list(states)

    async def _format_device_state(
        self, entity_id: str, smart_home: HomeAssistantClient
    ) -> str:
        state = await smart_home.get_state(entity_id)
        attrs = state.get("attributes") or {}
        name = attrs.get("friendly_name") or entity_id
        current = str(state.get("state", "unknown"))
        parts = [f"{name} is {current}."]
        if entity_domain(entity_id) == "light":
            light_detail = _format_light_state(attrs, current)
            if light_detail:
                parts.append(light_detail)
        return " ".join(parts)

    @tool(inject=["smart_home"])
    async def bind_node_area(
        self,
        area_name: str,
        smart_home: HomeAssistantClient = None,
    ) -> str:
        """
        Bind this node to a Home Assistant area so "in here" and bedroom-targeted alarms work.
        Binds only the current node — run from the satellite or client in that room after user confirmation.
        Do not call during device setup unless the user explicitly asks to bind this room.
        """
        node_id = get_node_id()
        if not node_id:
            raise RuntimeError("No node_id in context — bind from a live JARV1S node.")

        areas = await smart_home.list_areas()
        match = match_area_by_name(areas, area_name)
        if not match:
            names = ", ".join(a.get("name") or a.get("area_id", "") for a in areas[:8])
            raise RuntimeError(
                f"No Home Assistant area matched '{area_name}'. Known areas: {names}"
            )

        owner_id = get_owner_id()
        location = await bind_node(
            owner_id,
            node_id,
            match["area_id"],
            area_name=match.get("name"),
        )
        from api.websockets.connection import manager as connection_manager

        connection_manager.update_node_location(owner_id, node_id, location)
        label = location.room_name or location.ha_area_id or area_name
        return f"Bound this node to {label}."
