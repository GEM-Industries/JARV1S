"""Google Maps Plugin — bespoke wrapper over the Composio MCP transport.

Reuses Composio OAuth but resolves "here"/placeholder locations at the tool
boundary via resolve_current_location(). The auto-bridged MCPBridgePlugin for
"google_maps" is skipped because bespoke plugins have priority.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from pydantic import BaseModel

from core.context import (
    format_geo_coordinates,
    get_timezone,
    is_placeholder_location,
    resolve_current_location,
    resolve_search_location,
)
from core.decorators import tool
from core.integrations.mcp.bridge import _unwrap_composio_response
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.capabilities import CapabilityErrorDetail


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


_NEAR_ME_RE = re.compile(
    r"\b(near\s+me|nearby|around\s+here|close\s+by|in\s+my\s+area)\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"^(?P<seconds>\d+(?:\.\d+)?)s$")

_DEFAULT_CATEGORY_RADIUS_M = 1_500.0
_MAX_LOCAL_SEARCH_RADIUS_M = 50_000.0
# City-scale soft bias; explicit locations in the query still override it.
_DEFAULT_TEXT_SEARCH_BIAS_RADIUS_M = 15_000.0
_LOCATION_UNAVAILABLE = (
    "Current location is unavailable. Provide a concrete place or address, "
    "or share GPS from this device."
)
_PLACE_SUMMARY_FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "location",
    "rating",
    "businessStatus",
    "regularOpeningHours",
    "currentOpeningHours",
    "googleMapsUri",
)
_SEARCH_FIELD_MASK = ",".join(f"places.{field}" for field in _PLACE_SUMMARY_FIELDS)
_SUMMARY_DETAIL_FIELD_MASK = ",".join(_PLACE_SUMMARY_FIELDS)
_DETAIL_FIELD_MASK = ",".join(
    (*_PLACE_SUMMARY_FIELDS, "types", "nationalPhoneNumber", "websiteUri")
)


class PlaceSummary(BaseModel):
    place_id: str
    name: str
    route_target: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    distance_km: float | None = None
    rating: float | None = None
    business_status: str | None = None
    open_now: bool | None = None
    google_maps_uri: str | None = None


class PlaceDetails(BaseModel):
    place_id: str
    name: str
    route_target: str
    address: str | None = None
    open_now: bool | None = None
    weekday_hours: list[str] | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    google_maps_uri: str | None = None


class CurrentLocationResult(BaseModel):
    latitude: float
    longitude: float
    source: Literal["device", "home"]
    address: str | None = None


class RouteSummary(BaseModel):
    route_target: str
    travel_mode: Literal["DRIVE", "TRANSIT", "WALK", "BICYCLE", "TWO_WHEELER"]
    distance_km: float
    duration_minutes: float


def _region_code_from_timezone(tz_name: str) -> str | None:
    """CLDR region for Places regionCode soft bias. Skip unknown/world zones."""
    from babel.core import get_global

    territory = get_global("zone_territories").get((tz_name or "").strip())
    if not territory or territory in {"ZZ", "001"}:
        return None
    return territory.lower()


def _text_search_location_bias(location: dict[str, Any]) -> dict[str, Any]:
    center = {
        "latitude": float(location["latitude"]),
        "longitude": float(location["longitude"]),
    }
    return {
        "circle": {
            "center": center,
            "radius": _DEFAULT_TEXT_SEARCH_BIAS_RADIUS_M,
        }
    }


def _display_name(place: dict[str, Any]) -> str:
    display_name = place.get("displayName")
    if isinstance(display_name, dict):
        display_name = display_name.get("text")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("Google Maps returned a place without a display name")
    return display_name.strip()


def _place_route_target(place: dict[str, Any]) -> str:
    address = place.get("formattedAddress")
    if isinstance(address, str) and address.strip():
        return address.strip()
    location = place.get("location")
    if isinstance(location, dict):
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is not None and longitude is not None:
            return f"{latitude},{longitude}"
    raise ValueError("Google Maps returned a place without a routable location")


def _project_place(
    place: dict[str, Any],
    *,
    distance_meters: float | None = None,
) -> PlaceSummary:
    location = place.get("location")
    hours = place.get("currentOpeningHours") or place.get("regularOpeningHours")
    return PlaceSummary(
        place_id=place.get("id"),
        name=_display_name(place),
        route_target=_place_route_target(place),
        address=place.get("formattedAddress"),
        latitude=location.get("latitude") if isinstance(location, dict) else None,
        longitude=location.get("longitude") if isinstance(location, dict) else None,
        distance_km=round(distance_meters / 1000, 2) if distance_meters is not None else None,
        rating=place.get("rating"),
        business_status=place.get("businessStatus"),
        open_now=hours.get("openNow") if isinstance(hours, dict) else None,
        google_maps_uri=place.get("googleMapsUri"),
    )


def _project_places(result: Any) -> list[PlaceSummary] | CapabilityErrorDetail:
    """Project a provider response into stable LLM-facing place fields."""
    if isinstance(result, CapabilityErrorDetail):
        return result
    if isinstance(result, dict):
        result = result.get("places")
    if not isinstance(result, list):
        raise ValueError("Google Maps returned an unexpected places response")
    if not all(isinstance(place, dict) for place in result):
        raise ValueError("Google Maps returned an invalid place")
    return [_project_place(place) for place in result]


def _project_place_details(result: Any) -> PlaceDetails | CapabilityErrorDetail:
    if isinstance(result, CapabilityErrorDetail):
        return result
    if not isinstance(result, dict):
        raise ValueError("Google Maps returned an unexpected place details response")
    summary = _project_place(result)
    hours = result.get("regularOpeningHours")
    weekday = hours.get("weekdayDescriptions") if isinstance(hours, dict) else None
    return PlaceDetails(
        place_id=summary.place_id,
        name=summary.name,
        route_target=summary.route_target,
        address=summary.address,
        open_now=summary.open_now,
        weekday_hours=[str(item) for item in weekday] if isinstance(weekday, list) else None,
        phone=result.get("nationalPhoneNumber"),
        website=result.get("websiteUri"),
        rating=summary.rating,
        google_maps_uri=summary.google_maps_uri,
    )


def _autocomplete_predictions(
    result: Any,
) -> list[tuple[str, float | None]] | CapabilityErrorDetail:
    if isinstance(result, CapabilityErrorDetail):
        return result
    suggestions = result.get("suggestions") if isinstance(result, dict) else None
    if not isinstance(suggestions, list):
        raise ValueError("Google Maps returned an unexpected autocomplete response")

    predictions: list[tuple[str, float | None]] = []
    for suggestion in suggestions:
        prediction = suggestion.get("placePrediction") if isinstance(suggestion, dict) else None
        if not isinstance(prediction, dict):
            continue
        place_id = prediction.get("place") or prediction.get("placeId")
        if not isinstance(place_id, str) or not place_id:
            continue
        distance = prediction.get("distanceMeters")
        predictions.append(
            (
                place_id,
                float(distance) if isinstance(distance, (int, float)) else None,
            )
        )
    predictions.sort(key=lambda item: item[1] if item[1] is not None else float("inf"))
    return predictions


def _project_route(
    result: Any,
    *,
    route_target: str,
    travel_mode: Literal["DRIVE", "TRANSIT", "WALK", "BICYCLE", "TWO_WHEELER"],
) -> RouteSummary | CapabilityErrorDetail:
    """Project a provider response into stable, user-facing route units."""
    if isinstance(result, CapabilityErrorDetail):
        return result
    payload = result.get("response_data", result) if isinstance(result, dict) else None
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list):
        raise ValueError("Google Maps returned an unexpected route response")
    if not routes:
        return _fail("No route was found to the specified destination.")
    route = routes[0]
    if not isinstance(route, dict):
        raise ValueError("Google Maps returned an invalid route")

    distance_meters = route.get("distanceMeters")
    duration = route.get("duration")
    duration_match = _DURATION_RE.fullmatch(duration) if isinstance(duration, str) else None
    if not isinstance(distance_meters, (int, float)) or duration_match is None:
        raise ValueError("Google Maps returned an incomplete route")

    return RouteSummary(
        route_target=route_target,
        travel_mode=travel_mode,
        distance_km=round(float(distance_meters) / 1000, 2),
        duration_minutes=round(float(duration_match.group("seconds")) / 60, 1),
    )


def _address_from_geocode(result: Any) -> str | None:
    """Best-effort human address from a reverse-geocode MCP payload."""
    if isinstance(result, str):
        text = result.strip()
        return text or None
    if not isinstance(result, dict):
        return None
    for key in ("formattedAddress", "formatted_address", "address"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    results = result.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            for key in ("formattedAddress", "formatted_address", "address"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


class GoogleMapsPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="google_maps",
        version="1.0.0",
        description="Google Maps place search, details, geocoding, and travel times.",
        composio_app="google_maps",
        utterances=[
            "find a place nearby",
            "find a business by name",
            "look for that place near me",
            "look up a local cafe",
            "find a coffee shop near me",
            "search for a cafe or restaurant nearby",
            "is this place open",
            "is it open now",
            "business opening hours",
            "where is the nearest place",
            "where am I",
            "what's my location",
            "directions to a place",
            "travel time to a destination",
            "distance to an address",
            "how long is the drive to",
            "drive time to",
            "how far away is",
            "what's nearby",
            "check places in that suburb",
            "Google Maps place search",
        ],
    )

    async def _mcp(self, tool_name: str, **kwargs: Any) -> Any:
        from core.integrations.composio_gateway import get_composio_gateway

        gw = get_composio_gateway()
        if not gw:
            return _fail("Google Maps is not configured.")
        filtered = {k: v for k, v in kwargs.items() if v is not None}
        raw = await gw.call_mcp_tool("google_maps", tool_name, filtered)
        result = _unwrap_composio_response(raw)
        if isinstance(result, CapabilityErrorDetail):
            return result
        return result

    @staticmethod
    async def _current_location() -> dict[str, Any] | None:
        return await resolve_current_location()

    @staticmethod
    async def _search_location() -> dict[str, Any] | None:
        return await resolve_search_location()

    async def _search_named_place_nearby(
        self,
        query: str,
        location: dict[str, Any],
        max_results: int,
    ) -> list[PlaceSummary] | CapabilityErrorDetail:
        query = _NEAR_ME_RE.sub("", query).strip(" ,")
        if not query:
            return _fail("query must name a place or business.")

        center = {
            "latitude": float(location["latitude"]),
            "longitude": float(location["longitude"]),
        }
        result = await self._mcp(
            "GOOGLE_MAPS_AUTOCOMPLETE",
            input=query,
            origin=center,
            locationRestriction={
                "circle": {
                    "center": center,
                    "radius": _MAX_LOCAL_SEARCH_RADIUS_M,
                }
            },
            includeQueryPredictions=False,
        )
        predictions = _autocomplete_predictions(result)
        if isinstance(predictions, CapabilityErrorDetail):
            return predictions
        predictions = predictions[: max(1, min(max_results, 5))]

        details = await asyncio.gather(
            *(
                self._mcp(
                    "GOOGLE_MAPS_GET_PLACE_DETAILS",
                    name=place_id,
                    fieldMask=_SUMMARY_DETAIL_FIELD_MASK,
                )
                for place_id, _distance in predictions
            )
        )

        places: list[PlaceSummary] = []
        errors: list[CapabilityErrorDetail] = []
        for (place_id, distance), detail in zip(predictions, details):
            if isinstance(detail, CapabilityErrorDetail):
                errors.append(detail)
                continue
            if not isinstance(detail, dict):
                raise ValueError("Google Maps returned an unexpected place details response")
            detail.setdefault("id", place_id)
            places.append(_project_place(detail, distance_meters=distance))
        if not places and errors:
            return errors[0]
        return places

    @tool
    async def get_current_location(self) -> CurrentLocationResult | CapabilityErrorDetail:
        """
        Resolve and reverse-geocode the user's current device or eligible home location.
        Other maps tools resolve omitted location inputs automatically.
        """
        location = await self._current_location()
        if location is None:
            return _fail(_LOCATION_UNAVAILABLE)
        latitude = float(location["latitude"])
        longitude = float(location["longitude"])
        geocode = await self._mcp(
            "GOOGLE_MAPS_GEOCODE_LOCATION",
            latitude=latitude,
            longitude=longitude,
        )
        address = None
        if not isinstance(geocode, CapabilityErrorDetail):
            address = _address_from_geocode(geocode)
        return CurrentLocationResult(
            latitude=latitude,
            longitude=longitude,
            source=location.get("source") or "device",
            address=address,
        )

    @tool
    async def search_places(
        self,
        query: str,
        near_me: bool = False,
        max_results: int = 8,
    ) -> list[PlaceSummary] | CapabilityErrorDetail:
        """
        Search for a named place, business, or address.
        Biases toward the current or eligible home location when known; include a city
        or country in query for remote places. Set near_me only to hard-restrict locally.
        Pass a result's route_target to get_route; use place_id for detail lookup.
        Use search_nearby to browse categories such as cafes, gyms, or restaurants.
        """
        wants_near = near_me or bool(_NEAR_ME_RE.search(query))
        if wants_near:
            location = await self._current_location()
            if location is None:
                return _fail(_LOCATION_UNAVAILABLE)
            return await self._search_named_place_nearby(query, location, max_results)

        args: dict[str, Any] = {
            "textQuery": query,
            "maxResultCount": max(1, min(max_results, 20)),
            "fieldMask": _SEARCH_FIELD_MASK,
        }
        location = await self._search_location()
        if location is not None:
            args["locationBias"] = _text_search_location_bias(location)
        else:
            region = _region_code_from_timezone(get_timezone())
            if region:
                args["regionCode"] = region
        result = await self._mcp("GOOGLE_MAPS_TEXT_SEARCH", **args)
        return _project_places(result)

    @tool
    async def search_nearby(
        self,
        included_types: list[str] | None = None,
        radius_m: float = _DEFAULT_CATEGORY_RADIUS_M,
        max_results: int = 10,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> list[PlaceSummary] | CapabilityErrorDetail:
        """
        Browse place categories near the current location (or an explicit lat/lng).
        Use Google place types such as cafe, gym, or restaurant in included_types.
        Omit both coordinates to resolve the device or eligible home location.
        Pass a result's route_target to get_route; use place_id for detail lookup.
        """
        if (latitude is None) != (longitude is None):
            return _fail("Provide both latitude and longitude, or omit both.")
        if not 0 <= radius_m <= _MAX_LOCAL_SEARCH_RADIUS_M:
            return _fail("radius_m must be between 0 and 50000 meters.")
        if latitude is None:
            location = await self._current_location()
            if location is None:
                return _fail(_LOCATION_UNAVAILABLE)
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
        result = await self._mcp(
            "GOOGLE_MAPS_NEARBY_SEARCH",
            latitude=latitude,
            longitude=longitude,
            radius=radius_m,
            includedTypes=included_types,
            maxResultCount=max(1, min(max_results, 20)),
            fieldMask=_SEARCH_FIELD_MASK,
        )
        return _project_places(result)

    @tool
    async def get_place_details(
        self,
        place_id: str,
    ) -> PlaceDetails | CapabilityErrorDetail:
        """
        Get details for a Google Place ID.
        Use when search results lack required opening hours, contact, or website data.
        """
        name = place_id if place_id.startswith("places/") else f"places/{place_id}"
        return _project_place_details(
            await self._mcp(
                "GOOGLE_MAPS_GET_PLACE_DETAILS",
                name=name,
                fieldMask=_DETAIL_FIELD_MASK,
            )
        )

    async def geocode_address(
        self,
        address: str,
        region_code: str | None = None,
    ) -> Any | CapabilityErrorDetail:
        """Convert a concrete street address or place string into coordinates."""
        if is_placeholder_location(address):
            return _fail("address must be a concrete place or street address.")
        return await self._mcp(
            "GOOGLE_MAPS_GEOCODE_ADDRESS_WITH_QUERY",
            address_query=address,
            region_code=region_code,
        )

    async def geocode_location(
        self,
        latitude: float,
        longitude: float,
    ) -> Any | CapabilityErrorDetail:
        """Reverse-geocode coordinates into a human-readable address."""
        return await self._mcp(
            "GOOGLE_MAPS_GEOCODE_LOCATION",
            latitude=latitude,
            longitude=longitude,
        )

    @tool
    async def get_route(
        self,
        route_target: str,
        origin: str | None = None,
        travel_mode: Literal["DRIVE", "TRANSIT", "WALK", "BICYCLE", "TWO_WHEELER"] = "DRIVE",
    ) -> RouteSummary | CapabilityErrorDetail:
        """
        Get travel time and distance between two places.
        Use PlaceSummary.route_target after search; direct targets should be full addresses or coordinates.
        Omit origin to resolve the user's current location.
        """
        if is_placeholder_location(route_target):
            return _fail("route_target must be a concrete address or coordinates.")
        if is_placeholder_location(origin):
            location = await self._current_location()
            if location is None:
                return _fail(_LOCATION_UNAVAILABLE)
            origin_address = format_geo_coordinates(location)
        else:
            origin_address = origin
        route_args = {
            "origin_address": origin_address,
            "destination_address": route_target,
            "travel_mode": travel_mode,
            "units": "METRIC",
            "field_mask": "routes.distanceMeters,routes.duration",
        }
        if travel_mode == "DRIVE":
            route_args["routing_preference"] = "TRAFFIC_AWARE"
        result = await self._mcp("GOOGLE_MAPS_GET_ROUTE", **route_args)
        return _project_route(
            result,
            route_target=route_target,
            travel_mode=travel_mode,
        )

    async def compute_route_matrix(
        self,
        destinations: list[str],
        origins: list[str] | None = None,
        travel_mode: Literal["DRIVE", "TRANSIT", "WALK", "BICYCLE", "TWO_WHEELER"] = "DRIVE",
    ) -> Any | CapabilityErrorDetail:
        """
        Compare travel times from one or more origins to multiple destinations.
        Omit origins to use the current location. Destinations must be concrete.
        """
        if not destinations:
            return _fail("destinations must not be empty.")
        if any(is_placeholder_location(item) for item in destinations):
            return _fail("every destination must be a concrete place or address.")
        origin_count = len(origins) if origins else 1
        if origin_count * len(destinations) > 625:
            return _fail("route matrices support at most 625 origin-destination pairs.")

        resolved_origins: list[dict[str, str]] = []
        if not origins:
            location = await self._current_location()
            if location is None:
                return _fail(_LOCATION_UNAVAILABLE)
            resolved_origins = [{"address": format_geo_coordinates(location)}]
        else:
            for item in origins:
                if is_placeholder_location(item):
                    location = await self._current_location()
                    if location is None:
                        return _fail(_LOCATION_UNAVAILABLE)
                    item = format_geo_coordinates(location)
                resolved_origins.append({"address": item})

        resolved_destinations = [{"address": item} for item in destinations]

        return await self._mcp(
            "GOOGLE_MAPS_COMPUTE_ROUTE_MATRIX",
            origins=resolved_origins,
            destinations=resolved_destinations,
            travelMode=travel_mode,
            units="METRIC",
            fieldMask="originIndex,destinationIndex,duration,distanceMeters,status,condition",
        )
