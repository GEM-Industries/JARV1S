"""Tests for the first-party Google Maps location normalization wrapper."""

import pytest

from core.context import (
    RuntimeIdentity,
    ToolRuntimeContext,
    bind_tool_context,
    reset_tool_context,
)
from core.plugins.capabilities import CapabilityErrorDetail
from plugins.google_maps import GoogleMapsPlugin


@pytest.mark.asyncio
async def test_maps_delegates_mcp_calls_to_composio_gateway(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    class FakeGateway:
        async def call_mcp_tool(self, app_name: str, tool_name: str, arguments: dict):
            calls.append((app_name, tool_name, arguments))
            return {"successfull": True, "data": {"data": {"ok": True}}}

    monkeypatch.setattr(
        "core.integrations.composio_gateway.get_composio_gateway",
        lambda: FakeGateway(),
    )

    result = await GoogleMapsPlugin()._mcp("GOOGLE_MAPS_TEST", q="hello", empty=None)

    assert calls == [("google_maps", "GOOGLE_MAPS_TEST", {"q": "hello"})]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_maps_reports_missing_composio_gateway(monkeypatch):
    monkeypatch.setattr(
        "core.integrations.composio_gateway.get_composio_gateway",
        lambda: None,
    )
    result = await GoogleMapsPlugin()._mcp("GOOGLE_MAPS_TEST")
    assert isinstance(result, CapabilityErrorDetail)
    assert result.message == "Google Maps is not configured."


@pytest.mark.asyncio
async def test_maps_unsuccessful_composio_envelope_is_a_failure(monkeypatch):
    class FakeGateway:
        async def call_mcp_tool(self, app_name: str, tool_name: str, arguments: dict):
            return {"successfull": False, "error": "quota exceeded"}

    monkeypatch.setattr(
        "core.integrations.composio_gateway.get_composio_gateway",
        lambda: FakeGateway(),
    )
    result = await GoogleMapsPlugin()._mcp("GOOGLE_MAPS_TEST")
    assert isinstance(result, CapabilityErrorDetail)
    assert result.code == "tool_error"
    assert result.message == "quota exceeded"


def _bind(
    location: dict | None,
    *,
    device_kind: str = "phone",
    location_ref: dict | None = None,
    timezone: str = "UTC",
):
    return bind_tool_context(
        ToolRuntimeContext(
            identity=RuntimeIdentity(
                owner_id="geoff",
                connection_id="conn-1",
                location_ref=location_ref,
                device_kind=device_kind,
            ),
            timezone=timezone,
            location=location,
        )
    )


@pytest.mark.asyncio
async def test_get_route_replaces_current_location_placeholder(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append((tool_name, kwargs))
        return {
            "response_data": {
                "routes": [{"distanceMeters": 12_340, "duration": "1234s"}]
            }
        }

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    token = _bind({"latitude": -33.86, "longitude": 151.2, "source": "gps"})
    try:
        result = await GoogleMapsPlugin().get_route(
            route_target="Airtree Ventures, Surry Hills",
            origin="current location",
            travel_mode="TRANSIT",
        )
    finally:
        reset_tool_context(token)

    assert result.model_dump() == {
        "route_target": "Airtree Ventures, Surry Hills",
        "travel_mode": "TRANSIT",
        "distance_km": 12.34,
        "duration_minutes": 20.6,
    }
    assert calls == [
        (
            "GOOGLE_MAPS_GET_ROUTE",
            {
                "origin_address": "-33.86,151.2",
                "destination_address": "Airtree Ventures, Surry Hills",
                "travel_mode": "TRANSIT",
                "units": "METRIC",
                "field_mask": "routes.distanceMeters,routes.duration",
            },
        )
    ]


@pytest.mark.asyncio
async def test_get_route_preserves_explicit_origin(monkeypatch):
    calls: list[dict] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append(kwargs)
        return {"routes": [{"distanceMeters": 1000, "duration": "600s"}]}

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    token = _bind({"latitude": -33.86, "longitude": 151.2, "source": "gps"})
    try:
        await GoogleMapsPlugin().get_route(
            route_target="Testville Airport",
            origin="100 Example Street, Testville",
        )
    finally:
        reset_tool_context(token)

    assert calls[0]["origin_address"] == "100 Example Street, Testville"
    assert calls[0]["routing_preference"] == "TRAFFIC_AWARE"


@pytest.mark.asyncio
async def test_search_places_restricts_and_sorts_local_name_search(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append((tool_name, kwargs))
        if tool_name == "GOOGLE_MAPS_AUTOCOMPLETE":
            return {
                "suggestions": [
                    {
                        "placePrediction": {
                            "place": "places/farther",
                            "distanceMeters": 2500,
                        }
                    },
                    {
                        "placePrediction": {
                            "place": "places/nearest",
                            "distanceMeters": 600,
                        }
                    },
                ]
            }
        place_id = kwargs["name"]
        return {
            "id": place_id,
            "displayName": {"text": place_id.removeprefix("places/").title()},
            "formattedAddress": f"{place_id} Example Street, Testville",
            "location": {"latitude": 40.71, "longitude": -74.0},
        }

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    token = _bind({"latitude": 40.7128, "longitude": -74.006, "source": "gps"})
    try:
        result = await GoogleMapsPlugin().search_places("The Brew Spot near me", max_results=2)
    finally:
        reset_tool_context(token)

    tool_name, autocomplete = calls[0]
    assert tool_name == "GOOGLE_MAPS_AUTOCOMPLETE"
    assert autocomplete["input"] == "The Brew Spot"
    assert autocomplete["origin"] == {"latitude": 40.7128, "longitude": -74.006}
    assert autocomplete["locationRestriction"] == {
        "circle": {
            "center": {"latitude": 40.7128, "longitude": -74.006},
            "radius": 50_000.0,
        }
    }
    assert autocomplete["includeQueryPredictions"] is False
    assert [call[1]["name"] for call in calls[1:]] == [
        "places/nearest",
        "places/farther",
    ]
    assert [place.name for place in result] == ["Nearest", "Farther"]
    assert [place.distance_km for place in result] == [0.6, 2.5]


@pytest.mark.asyncio
async def test_search_places_returns_places_list(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append((tool_name, kwargs))
        return {
            "places": [
                {
                    "id": "places/cafe-1",
                    "displayName": {"text": "Cafe"},
                    "formattedAddress": "1 Main St",
                    "location": {"latitude": -33.86, "longitude": 151.2},
                    "currentOpeningHours": {"openNow": True},
                },
            ]
        }

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    token = _bind({"latitude": -33.86, "longitude": 151.2, "source": "gps"})
    try:
        result = await GoogleMapsPlugin().search_places("cafe")
    finally:
        reset_tool_context(token)

    assert calls == [
        (
            "GOOGLE_MAPS_TEXT_SEARCH",
            {
                "textQuery": "cafe",
                "maxResultCount": 8,
                "fieldMask": calls[0][1]["fieldMask"],
                "locationBias": {
                    "circle": {
                        "center": {"latitude": -33.86, "longitude": 151.2},
                        "radius": 15_000.0,
                    }
                },
            },
        )
    ]
    assert isinstance(result, list)
    assert result[0].model_dump() == {
        "place_id": "places/cafe-1",
        "name": "Cafe",
        "route_target": "1 Main St",
        "address": "1 Main St",
        "latitude": -33.86,
        "longitude": 151.2,
        "distance_km": None,
        "rating": None,
        "business_status": None,
        "open_now": True,
        "google_maps_uri": None,
    }


@pytest.mark.asyncio
async def test_search_places_uses_timezone_region_when_location_unavailable(monkeypatch):
    calls: list[dict] = []

    async def fake_resolve():
        return None

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append(kwargs)
        return {"places": []}

    monkeypatch.setattr("plugins.google_maps.resolve_search_location", fake_resolve)
    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    token = _bind(None, device_kind="browser", timezone="Australia/Sydney")
    try:
        await GoogleMapsPlugin().search_places("August Coffee")
    finally:
        reset_tool_context(token)

    assert calls[0]["textQuery"] == "August Coffee"
    assert calls[0]["regionCode"] == "au"
    assert "locationBias" not in calls[0]


@pytest.mark.asyncio
async def test_search_nearby_injects_resolved_coordinates(monkeypatch):
    calls: list[dict] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append(kwargs)
        return {
            "places": [
                {
                    "id": "p1",
                    "displayName": {"text": "Cafe"},
                    "formattedAddress": "1 Main St",
                }
            ]
        }

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    token = _bind({"latitude": -33.86, "longitude": 151.2, "source": "gps"})
    try:
        result = await GoogleMapsPlugin().search_nearby(included_types=["cafe"])
    finally:
        reset_tool_context(token)

    assert calls[0]["latitude"] == -33.86
    assert calls[0]["longitude"] == 151.2
    assert calls[0]["includedTypes"] == ["cafe"]
    assert result[0].place_id == "p1"
    assert result[0].name == "Cafe"
    assert result[0].route_target == "1 Main St"


@pytest.mark.asyncio
async def test_search_nearby_uses_home_for_satellite(monkeypatch):
    async def fake_resolve():
        return {
            "latitude": -33.85,
            "longitude": 151.08,
            "source": "home",
        }

    calls: list[dict] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append(kwargs)
        return {"places": []}

    monkeypatch.setattr(
        "plugins.google_maps.resolve_current_location",
        fake_resolve,
    )
    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)

    token = _bind(
        None,
        device_kind="satellite",
        location_ref={"ha_area_id": "area-bedroom", "room_name": "Bedroom"},
    )
    try:
        result = await GoogleMapsPlugin().search_nearby()
    finally:
        reset_tool_context(token)

    assert calls[0]["latitude"] == -33.85
    assert calls[0]["longitude"] == 151.08
    assert result == []


@pytest.mark.asyncio
async def test_get_current_location_resolves_and_reverse_geocodes(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append((tool_name, kwargs))
        return {
            "results": [
                {"formattedAddress": "100 Example Street, Testville"},
            ]
        }

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    token = _bind({"latitude": 40.7128, "longitude": -74.006, "source": "gps"})
    try:
        result = await GoogleMapsPlugin().get_current_location()
    finally:
        reset_tool_context(token)

    assert calls == [
        (
            "GOOGLE_MAPS_GEOCODE_LOCATION",
            {"latitude": 40.7128, "longitude": -74.006},
        )
    ]
    assert result.model_dump() == {
        "latitude": 40.7128,
        "longitude": -74.006,
        "source": "device",
        "address": "100 Example Street, Testville",
    }


@pytest.mark.asyncio
async def test_get_current_location_unavailable(monkeypatch):
    async def fake_resolve():
        return None

    monkeypatch.setattr(
        "plugins.google_maps.resolve_current_location",
        fake_resolve,
    )
    token = _bind(None, device_kind="phone")
    try:
        result = await GoogleMapsPlugin().get_current_location()
    finally:
        reset_tool_context(token)
    assert isinstance(result, CapabilityErrorDetail)
    assert result.message.startswith("Current location is unavailable")


@pytest.mark.asyncio
async def test_near_me_without_location_returns_actionable_error(monkeypatch):
    async def fake_resolve():
        return None

    monkeypatch.setattr(
        "plugins.google_maps.resolve_current_location",
        fake_resolve,
    )
    token = _bind(None, device_kind="phone")
    try:
        result = await GoogleMapsPlugin().search_places("cafe near me")
    finally:
        reset_tool_context(token)
    assert isinstance(result, CapabilityErrorDetail)
    assert result.message.startswith("Current location is unavailable")


@pytest.mark.asyncio
async def test_get_place_details_prefixes_place_id(monkeypatch):
    calls: list[dict] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append(kwargs)
        return {
            "id": "ChIJabc",
            "displayName": {"text": "Local Roast"},
            "formattedAddress": "1 George St",
            "regularOpeningHours": {
                "openNow": True,
                "weekdayDescriptions": ["Monday: 7:00 AM – 4:00 PM"],
            },
            "nationalPhoneNumber": "+61 2 0000",
            "websiteUri": "https://example.com",
        }

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    result = await GoogleMapsPlugin().get_place_details("ChIJabc")
    assert calls[0]["name"] == "places/ChIJabc"
    assert "regularOpeningHours" in calls[0]["fieldMask"]
    assert result.name == "Local Roast"
    assert result.open_now is True
    assert result.phone == "+61 2 0000"
    assert result.website == "https://example.com"


@pytest.mark.asyncio
async def test_get_route_rejects_placeholder_destination(monkeypatch):
    async def fail_mcp(self, tool_name: str, **kwargs):
        pytest.fail("provider must not be called")

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fail_mcp)
    result = await GoogleMapsPlugin().get_route(route_target="near me", origin="Sydney")
    assert isinstance(result, CapabilityErrorDetail)
    assert result.message == "route_target must be a concrete address or coordinates."


@pytest.mark.asyncio
async def test_search_nearby_rejects_partial_coordinates(monkeypatch):
    async def fail_mcp(self, tool_name: str, **kwargs):
        pytest.fail("provider must not be called")

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fail_mcp)
    result = await GoogleMapsPlugin().search_nearby(latitude=-33.86)
    assert isinstance(result, CapabilityErrorDetail)
    assert result.message == "Provide both latitude and longitude, or omit both."


@pytest.mark.asyncio
async def test_route_matrix_includes_route_condition(monkeypatch):
    calls: list[dict] = []

    async def fake_mcp(self, tool_name: str, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr(GoogleMapsPlugin, "_mcp", fake_mcp)
    await GoogleMapsPlugin().compute_route_matrix(
        origins=["Sydney"],
        destinations=["Newcastle"],
    )
    assert "condition" in calls[0]["fieldMask"]
