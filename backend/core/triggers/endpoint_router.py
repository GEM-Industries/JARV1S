"""Proactive endpoint routing.

Selects one speaker-capable WebSocket endpoint from live presence, using an
optional target hint on ``DeliveryPlan``: ``node_id`` → ``location_ref`` →
last-active speaker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

from api.websockets.presence import LocationRef

if TYPE_CHECKING:
    from core.triggers.models import DeliveryPlan


@dataclass(frozen=True, slots=True)
class LiveEndpoint:
    connection_id: str
    node_id: str
    capabilities: frozenset[str]
    location: LocationRef
    last_active_at: float | None
    connected_at: float


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    connection_id: str
    node_id: str


@dataclass(frozen=True, slots=True)
class EndpointRoutingResult:
    target: ResolvedTarget | None
    reason: str


def _has_speaker(endpoint: LiveEndpoint) -> bool:
    return "speaker" in endpoint.capabilities


def _location_refs_match(target: Mapping[str, object], session_loc: LocationRef) -> bool:
    target_ha = target.get("ha_area_id")
    if target_ha and session_loc.ha_area_id:
        return target_ha == session_loc.ha_area_id
    target_room = target.get("room_id")
    if target_room and session_loc.room_id:
        return target_room == session_loc.room_id
    target_room_name = target.get("room_name")
    if target_room_name and session_loc.room_name:
        return str(target_room_name).casefold() == str(session_loc.room_name).casefold()
    return False


def _rank_key(endpoint: LiveEndpoint) -> tuple[float, float, str]:
    return (
        endpoint.last_active_at or 0.0,
        endpoint.connected_at,
        endpoint.connection_id,
    )


def _pick_best(endpoints: Sequence[LiveEndpoint]) -> LiveEndpoint | None:
    speakers = [ep for ep in endpoints if _has_speaker(ep)]
    if not speakers:
        return None
    return max(speakers, key=_rank_key)


def resolve_proactive_endpoints(
    *,
    delivery: "DeliveryPlan",
    endpoints: Sequence[LiveEndpoint],
) -> EndpointRoutingResult:
    """Resolve a speaker target for a proactive voice delivery attempt."""
    hint = delivery.target

    if hint and hint.node_id:
        for ep in endpoints:
            if ep.node_id == hint.node_id and _has_speaker(ep):
                return EndpointRoutingResult(
                    target=ResolvedTarget(ep.connection_id, ep.node_id),
                    reason="target_node",
                )
        if delivery.fallback == "follow_me_if_target_unavailable":
            chosen = _pick_best(endpoints)
            if chosen:
                return EndpointRoutingResult(
                    target=ResolvedTarget(chosen.connection_id, chosen.node_id),
                    reason="target_node_offline_fallback",
                )
        return EndpointRoutingResult(target=None, reason="target_node_offline")

    if hint and hint.location_ref:
        loc = hint.location_ref
        in_area = [ep for ep in endpoints if _has_speaker(ep) and _location_refs_match(loc, ep.location)]
        chosen = _pick_best(in_area)
        if chosen:
            return EndpointRoutingResult(
                target=ResolvedTarget(chosen.connection_id, chosen.node_id),
                reason="target_location",
            )
        if delivery.fallback == "follow_me_if_target_unavailable":
            chosen = _pick_best(endpoints)
            if chosen:
                return EndpointRoutingResult(
                    target=ResolvedTarget(chosen.connection_id, chosen.node_id),
                    reason="target_location_offline_fallback",
                )
        return EndpointRoutingResult(target=None, reason="target_location_offline")

    chosen = _pick_best(endpoints)
    if chosen:
        return EndpointRoutingResult(
            target=ResolvedTarget(chosen.connection_id, chosen.node_id),
            reason="last_active_speaker",
        )
    return EndpointRoutingResult(target=None, reason="no_speaker_endpoint")
