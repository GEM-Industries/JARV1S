"""Tests for proactive endpoint routing (stage 2 after trigger delivery gate)."""

from __future__ import annotations

import time

from api.websockets.presence import LocationRef
from core.triggers.endpoint_router import LiveEndpoint, resolve_proactive_endpoints
from core.triggers.models import DeliveryPlan, DeliveryTargetHint


def _endpoint(
    connection_id: str,
    node_id: str,
    *,
    last_active_at: float | None = None,
    connected_at: float = 0.0,
    location: LocationRef | None = None,
    capabilities: frozenset[str] | None = None,
) -> LiveEndpoint:
    return LiveEndpoint(
        connection_id=connection_id,
        node_id=node_id,
        capabilities=capabilities or frozenset({"mic", "speaker"}),
        location=location or LocationRef(),
        last_active_at=last_active_at,
        connected_at=connected_at,
    )


def test_target_node_wins_over_more_recent_default():
    now = time.time()
    endpoints = [
        _endpoint("conn-browser", "browser", last_active_at=now, connected_at=now),
        _endpoint("conn-bedroom", "bedroom", last_active_at=now - 100, connected_at=now - 100),
    ]
    result = resolve_proactive_endpoints(
        delivery=DeliveryPlan(target=DeliveryTargetHint(node_id="bedroom")),
        endpoints=endpoints,
    )
    assert result.target is not None
    assert result.target.connection_id == "conn-bedroom"
    assert result.reason == "target_node"


def test_last_active_speaker_wins_without_target_hint():
    now = time.time()
    endpoints = [
        _endpoint("conn-browser", "browser", last_active_at=now - 10, connected_at=now - 10),
        _endpoint("conn-bedroom", "bedroom", last_active_at=now, connected_at=now),
    ]
    result = resolve_proactive_endpoints(delivery=DeliveryPlan(), endpoints=endpoints)
    assert result.target is not None
    assert result.target.connection_id == "conn-bedroom"
    assert result.reason == "last_active_speaker"


def test_target_location_selects_matching_area():
    now = time.time()
    bedroom_loc = LocationRef(provider="manual", room_id="bedroom", ha_area_id="area-bedroom")
    endpoints = [
        _endpoint("conn-browser", "browser", last_active_at=now, location=LocationRef(room_id="office")),
        _endpoint("conn-bedroom", "bedroom", last_active_at=now - 5, location=bedroom_loc),
    ]
    result = resolve_proactive_endpoints(
        delivery=DeliveryPlan(
            target=DeliveryTargetHint(
                location_ref={"room_id": "bedroom", "ha_area_id": "area-bedroom"},
            ),
        ),
        endpoints=endpoints,
    )
    assert result.target is not None
    assert result.target.node_id == "bedroom"


def test_target_location_offline_when_no_matching_live_endpoint():
    now = time.time()
    endpoints = [
        _endpoint("conn-browser", "browser", last_active_at=now, location=LocationRef(room_id="office")),
    ]
    result = resolve_proactive_endpoints(
        delivery=DeliveryPlan(
            target=DeliveryTargetHint(
                location_ref={"room_id": "bedroom", "ha_area_id": "area-bedroom"},
            ),
        ),
        endpoints=endpoints,
    )
    assert result.target is None
    assert result.reason == "target_location_offline"


def test_target_location_offline_fallback_uses_last_active_speaker():
    now = time.time()
    endpoints = [
        _endpoint("conn-browser", "browser", last_active_at=now, location=LocationRef(room_id="office")),
    ]
    result = resolve_proactive_endpoints(
        delivery=DeliveryPlan(
            target=DeliveryTargetHint(
                location_ref={"room_id": "bedroom", "ha_area_id": "area-bedroom"},
            ),
            fallback="follow_me_if_target_unavailable",
        ),
        endpoints=endpoints,
    )
    assert result.target is not None
    assert result.target.connection_id == "conn-browser"
    assert result.reason == "target_location_offline_fallback"


def test_room_target_without_fallback_stays_offline():
    now = time.time()
    endpoints = [
        _endpoint("conn-browser", "browser", last_active_at=now, location=LocationRef(room_id="office")),
    ]
    result = resolve_proactive_endpoints(
        delivery=DeliveryPlan(
            target=DeliveryTargetHint(
                location_ref={"room_id": "bedroom", "ha_area_id": "area-bedroom"},
            ),
            fallback="none",
        ),
        endpoints=endpoints,
    )
    assert result.target is None
    assert result.reason == "target_location_offline"


def test_no_speaker_capability_returns_empty():
    endpoints = [
        LiveEndpoint(
            connection_id="conn-display",
            node_id="display",
            capabilities=frozenset({"display"}),
            location=LocationRef(),
            last_active_at=time.time(),
            connected_at=time.time(),
        )
    ]
    result = resolve_proactive_endpoints(delivery=DeliveryPlan(), endpoints=endpoints)
    assert result.target is None
    assert result.reason == "no_speaker_endpoint"
