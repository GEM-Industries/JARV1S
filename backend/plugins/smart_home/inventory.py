"""Home Assistant inventory snapshot and search."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from plugins.smart_home.domains import (
    brightness_pct_from_ha,
    capabilities_for_entity,
    entity_domain,
    is_safe_setup_entity,
    search_priority,
)

TOOL_DATA_KEY = "inventory"
CACHE_TTL_S = 300
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:\band\b|&)\s*", re.IGNORECASE)
_ROOM_REFERENCE_PHRASES = ("in here", "this room", "this area", "here")
_GLOBAL_SCOPE_PHRASES = (
    "in the house",
    "in the home",
    "whole house",
    "the house",
    "the home",
    "everywhere",
)
_SCOPE_FILLER_WORDS = frozenset({"my", "please", "the", "turn", "make"})
_BRIGHTNESS_DIRECTION_WORDS = {
    "dimmer": "dimmer",
    "darker": "dimmer",
    "dim": "dimmer",
    "down": "dimmer",
    "brighter": "brighter",
    "bright": "brighter",
    "up": "brighter",
}
_RELATIVE_STRIP_WORDS = frozenset({*_BRIGHTNESS_DIRECTION_WORDS, "warmer", "cooler"})
_DOMAIN_QUERY_ALIASES = (
    ("light", ("light", "lights")),
    ("switch", ("switch", "switches")),
    ("fan", ("fan", "fans")),
    ("lock", ("lock", "locks")),
    ("cover", ("cover", "covers")),
    ("sensor", ("sensor", "sensors")),
)
_NUMBER_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)
_ORDINAL_WORDS = (
    "zeroth",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
)
_TOKEN_ALIASES = {
    **{alias: domain for domain, aliases in _DOMAIN_QUERY_ALIASES for alias in aliases},
    **{word: str(number) for number, word in enumerate(_NUMBER_WORDS)},
    **{word: str(number) for number, word in enumerate(_ORDINAL_WORDS)},
}


@dataclass(frozen=True)
class DeviceQuery:
    """Parsed natural-language device search scope."""

    domain: str | None = None
    scope_all: bool = False
    room_reference: bool = False
    tokens: frozenset[str] = frozenset()
    brightness_direction: Literal["dimmer", "brighter"] | None = None


class InventoryEntity(BaseModel):
    entity_id: str
    name: str
    domain: str
    state: str
    area_id: str | None = None
    area_name: str | None = None
    device_id: str | None = None
    platform: str | None = None
    config_entry_ids: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    manufacturer: str | None = None
    model: str | None = None
    brightness: int | None = None
    color_mode: str | None = None
    supported_color_modes: list[str] = Field(default_factory=list)
    color_temp_kelvin: int | None = None
    min_color_temp_kelvin: int | None = None
    max_color_temp_kelvin: int | None = None


class InventorySnapshot(BaseModel):
    captured_at: str
    area_count: int
    device_count: int
    entity_count: int
    entities: list[InventoryEntity] = Field(default_factory=list)


def parse_device_query(query: str) -> DeviceQuery:
    """Parse a natural device query into domain, scope, and remaining match tokens."""
    raw = (query or "").strip().casefold()
    if not raw:
        return DeviceQuery()

    text = f" {raw} "
    room_reference = False
    for phrase in _ROOM_REFERENCE_PHRASES:
        pattern = rf"\b{re.escape(phrase)}\b"
        if re.search(pattern, text):
            text = re.sub(pattern, " ", text)
            room_reference = True

    brightness_direction: Literal["dimmer", "brighter"] | None = None
    for word, direction in sorted(
        _BRIGHTNESS_DIRECTION_WORDS.items(), key=lambda item: len(item[0]), reverse=True
    ):
        pattern = rf"\b{re.escape(word)}\b"
        if re.search(pattern, text):
            if brightness_direction is None:
                brightness_direction = direction
            text = re.sub(pattern, " ", text)

    for word in _RELATIVE_STRIP_WORDS - _BRIGHTNESS_DIRECTION_WORDS.keys():
        text = re.sub(rf"\b{re.escape(word)}\b", " ", text)

    scope_all = False
    for phrase in _GLOBAL_SCOPE_PHRASES:
        wrapped = f" {phrase} "
        if wrapped in text:
            text = text.replace(wrapped, " ")
            scope_all = True

    if re.search(r"\ball\b", text):
        text = re.sub(r"\ball\b", " ", text)
        scope_all = True

    for filler in _SCOPE_FILLER_WORDS:
        text = re.sub(rf"\b{re.escape(filler)}\b", " ", text)

    domain: str | None = None
    for canonical, aliases in _DOMAIN_QUERY_ALIASES:
        for alias in sorted(aliases, key=len, reverse=True):
            if re.search(rf"\b{re.escape(alias)}\b", text):
                domain = canonical
                text = re.sub(rf"\b{re.escape(alias)}\b", " ", text)
                break
        if domain:
            break

    tokens = _tokenize_search_remainder(text)
    return DeviceQuery(
        domain=domain,
        scope_all=scope_all,
        room_reference=room_reference,
        tokens=tokens,
        brightness_direction=brightness_direction,
    )


def _friendly_name(state: dict[str, Any], registry: dict[str, Any] | None) -> str:
    attrs = state.get("attributes") or {}
    if attrs.get("friendly_name"):
        return str(attrs["friendly_name"])
    if registry and registry.get("name"):
        return str(registry["name"])
    return str(state.get("entity_id", ""))


def _tokenize_search_remainder(text: str) -> frozenset[str]:
    normalized = re.sub(r"\b([a-z0-9]+)'s\b", r"\1", text.casefold())
    normalized = normalized.replace("_", " ")
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(normalized):
        token = _TOKEN_ALIASES.get(token, token)
        tokens.add(token)
    return frozenset(tokens)


def _search_tokens(text: str) -> set[str]:
    return set(_tokenize_search_remainder(text))


def _search_text(entity: InventoryEntity) -> str:
    return " ".join(
        filter(
            None,
            [
                entity.entity_id,
                entity.name,
                entity.domain,
                entity.area_name or "",
                *entity.aliases,
                *entity.labels,
            ],
        )
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int | float):
        return int(round(value))
    return None


def _light_fields_from_state(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes") or {}
    brightness = attrs.get("brightness")
    return {
        "brightness": int(brightness) if isinstance(brightness, int | float) else None,
        "color_mode": str(attrs["color_mode"]) if attrs.get("color_mode") else None,
        "supported_color_modes": _string_list(attrs.get("supported_color_modes")),
        "color_temp_kelvin": _optional_int(attrs.get("color_temp_kelvin")),
        "min_color_temp_kelvin": _optional_int(attrs.get("min_color_temp_kelvin")),
        "max_color_temp_kelvin": _optional_int(attrs.get("max_color_temp_kelvin")),
    }


def _matches_query(entity: InventoryEntity, query_tokens: set[str]) -> bool:
    if not query_tokens:
        return False
    return query_tokens <= _search_tokens(_search_text(entity))


def _result_sort_key(entity: InventoryEntity) -> tuple[int, str]:
    return search_priority(entity.entity_id), entity.name.casefold()


def _dedupe_by_device(entities: list[InventoryEntity]) -> list[InventoryEntity]:
    results: list[InventoryEntity] = []
    seen: set[str] = set()
    for entity in sorted(entities, key=_result_sort_key):
        key = entity.device_id or entity.entity_id
        if key in seen:
            continue
        seen.add(key)
        results.append(entity)
    return results


def _normalize_area_name(name: str) -> str:
    return " ".join(name.strip().casefold().split())


def match_area_by_name(
    areas: list[dict[str, Any]], area_name: str
) -> dict[str, Any] | None:
    needle = _normalize_area_name(area_name)
    if not needle:
        return None
    for area in areas:
        candidate = area.get("name") or area.get("area_id") or ""
        if _normalize_area_name(str(candidate)) == needle:
            return area
        area_id = area.get("area_id")
        if area_id and _normalize_area_name(str(area_id)) == needle:
            return area
    return None


async def build_inventory(client) -> InventorySnapshot:
    areas, devices, registry_entities, states = await _fetch_raw(client)
    area_meta = {
        a["area_id"]: {
            "name": a.get("name") or a["area_id"],
            "aliases": _string_list(a.get("aliases")),
            "labels": _string_list(a.get("labels")),
        }
        for a in areas
        if a.get("area_id")
    }
    device_areas = {d["id"]: d.get("area_id") for d in devices if d.get("id")}
    device_meta = {
        d["id"]: {
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
            "config_entry_ids": d.get("config_entries") or [],
            "aliases": _string_list(d.get("aliases")),
            "labels": _string_list(d.get("labels")),
        }
        for d in devices
        if d.get("id")
    }

    registry_by_id = {
        e["entity_id"]: e for e in registry_entities if e.get("entity_id")
    }
    state_by_id = {s["entity_id"]: s for s in states if s.get("entity_id")}

    entities: list[InventoryEntity] = []
    for entity_id, state in state_by_id.items():
        reg = registry_by_id.get(entity_id, {})
        device_id = reg.get("device_id")
        meta = device_meta.get(device_id, {}) if device_id else {}
        registry_config_entry_id = reg.get("config_entry_id")
        config_entry_ids = list(meta.get("config_entry_ids") or [])
        if (
            registry_config_entry_id
            and registry_config_entry_id not in config_entry_ids
        ):
            config_entry_ids.insert(0, registry_config_entry_id)
        area_id = reg.get("area_id") or device_areas.get(device_id)
        area = area_meta.get(area_id, {}) if area_id else {}
        aliases = [
            *_string_list(reg.get("aliases")),
            *meta.get("aliases", []),
            *area.get("aliases", []),
        ]
        labels = [
            *_string_list(reg.get("labels")),
            *meta.get("labels", []),
            *area.get("labels", []),
        ]
        domain = entity_domain(entity_id)
        light_fields = _light_fields_from_state(state) if domain == "light" else {}
        entities.append(
            InventoryEntity(
                entity_id=entity_id,
                name=_friendly_name(state, reg),
                domain=domain,
                state=str(state.get("state", "unknown")),
                area_id=area_id,
                area_name=area.get("name") if area_id else None,
                device_id=device_id,
                platform=reg.get("platform"),
                config_entry_ids=config_entry_ids,
                aliases=aliases,
                labels=labels,
                manufacturer=meta.get("manufacturer"),
                model=meta.get("model"),
                **light_fields,
            )
        )

    return InventorySnapshot(
        captured_at=datetime.now(timezone.utc).isoformat(),
        area_count=len(areas),
        device_count=len(devices),
        entity_count=len(entities),
        entities=entities,
    )


async def _fetch_raw(client) -> tuple[list, list, list, list]:
    areas, devices, registry_entities, states = await _parallel(
        client.list_areas(),
        client.list_devices(),
        client.list_entities_registry(),
        client.get_states(),
    )
    return areas, devices, registry_entities, states


async def _parallel(*coros):
    import asyncio

    return await asyncio.gather(*coros)


def _query_clauses(query: str) -> list[str]:
    parts = [part.strip() for part in _CLAUSE_SPLIT_RE.split(query or "") if part.strip()]
    return parts if len(parts) > 1 else [query]


def unmatched_lights_message(query: str, snapshot: InventorySnapshot) -> str:
    rooms = sorted(
        {entity.area_name for entity in snapshot.entities if entity.area_name}
    )
    if rooms:
        return f"No lights matched '{query}'. Rooms: {', '.join(rooms)}."
    return f"No lights matched '{query}'."


def search_inventory(
    snapshot: InventorySnapshot,
    query: str,
    *,
    area_id: str | None = None,
    domain: str | None = None,
    dedupe_devices: bool = True,
    limit: int | None = 10,
) -> list[InventoryEntity]:
    def finalize(matches: list[InventoryEntity]) -> list[InventoryEntity]:
        results = (
            _dedupe_by_device(matches)
            if dedupe_devices
            else sorted(matches, key=_result_sort_key)
        )
        return results if limit is None else results[:limit]

    clauses = _query_clauses(query)
    if len(clauses) > 1:
        full = parse_device_query(query)
        seen: set[str] = set()
        union: list[InventoryEntity] = []
        for clause in clauses:
            for entity in search_inventory(
                snapshot,
                clause,
                area_id=area_id,
                domain=domain or full.domain,
                dedupe_devices=False,
                limit=None,
            ):
                if entity.entity_id in seen:
                    continue
                seen.add(entity.entity_id)
                union.append(entity)
        return finalize(union)

    return finalize(
        _search_inventory_clause(
            snapshot,
            query,
            area_id=area_id,
            domain=domain,
        )
    )


def _search_inventory_clause(
    snapshot: InventorySnapshot,
    query: str,
    *,
    area_id: str | None,
    domain: str | None,
) -> list[InventoryEntity]:
    parsed = parse_device_query(query)
    effective_domain = domain or parsed.domain
    if parsed.room_reference:
        if not area_id:
            return []
        return [
            entity
            for entity in snapshot.entities
            if entity.area_id == area_id
            and (effective_domain is None or entity.domain == effective_domain)
            and (not parsed.tokens or _matches_query(entity, set(parsed.tokens)))
        ]

    if parsed.scope_all and not parsed.tokens:
        return [
            entity
            for entity in snapshot.entities
            if (effective_domain is None or entity.domain == effective_domain)
            and (area_id is None or entity.area_id == area_id)
        ]

    if effective_domain and not parsed.tokens and not parsed.scope_all:
        return [
            entity
            for entity in snapshot.entities
            if entity.domain == effective_domain
            and (area_id is None or entity.area_id == area_id)
        ]

    query_tokens = set(parsed.tokens)
    matches: list[InventoryEntity] = []
    for entity in snapshot.entities:
        if area_id and entity.area_id != area_id:
            continue
        if effective_domain and entity.domain != effective_domain:
            continue
        if _matches_query(entity, query_tokens):
            matches.append(entity)
    return matches


def find_safe_setup_candidate(snapshot: InventorySnapshot) -> InventoryEntity | None:
    for entity in snapshot.entities:
        if is_safe_setup_entity(entity.entity_id):
            return entity
    return None


def entities_for_config_entry(
    snapshot: InventorySnapshot,
    config_entry_id: str,
    *,
    safe_only: bool = False,
) -> list[InventoryEntity]:
    """Return entities belonging to a HA config entry by registry membership."""
    matches: list[InventoryEntity] = []
    for entity in snapshot.entities:
        if config_entry_id not in entity.config_entry_ids:
            continue
        if safe_only and not is_safe_setup_entity(entity.entity_id):
            continue
        matches.append(entity)
    return sorted(matches, key=_result_sort_key)


def entity_to_device_summary(entity: InventoryEntity):
    from plugins.smart_home.models import DeviceSummary

    brightness_pct = None
    if entity.domain == "light":
        brightness_pct = brightness_pct_from_ha(entity.brightness, state=entity.state)

    return DeviceSummary(
        entity_id=entity.entity_id,
        name=entity.name,
        domain=entity.domain,
        state=entity.state,
        area_name=entity.area_name,
        brightness_pct=brightness_pct,
        color_temp_kelvin=entity.color_temp_kelvin,
        color_mode=entity.color_mode if entity.domain == "light" else None,
        capabilities=capabilities_for_entity(entity),
    )
