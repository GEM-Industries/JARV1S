"""Tests for runtime location resolution (device coords vs. home fallback)."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import core.context as context
from core.context import (
    GeoPosition,
    RuntimeIdentity,
    ToolRuntimeContext,
    allows_home_location_fallback,
    bind_tool_context,
    is_placeholder_location,
    parse_geo_position,
    reset_tool_context,
    resolve_current_location,
    resolve_search_location,
)


class _FakeHAClient:
    def __init__(self, config: dict | Exception):
        self._config = config

    async def get_config(self) -> dict:
        if isinstance(self._config, Exception):
            raise self._config
        return self._config


@pytest.fixture(autouse=True)
def _reset_home_cache():
    context._HOME_LOCATION_CACHE = None
    context._HOME_LOCATION_TS = 0.0
    yield
    context._HOME_LOCATION_CACHE = None
    context._HOME_LOCATION_TS = 0.0


def _bind(
    location: dict | None,
    *,
    device_kind: str | None = None,
    location_ref: dict | None = None,
):
    return bind_tool_context(
        ToolRuntimeContext(
            identity=RuntimeIdentity(
                owner_id="geoff",
                connection_id="conn-1",
                location_ref=location_ref,
                device_kind=device_kind,
            ),
            location=location,
        )
    )


def _patch_ha(monkeypatch, config: dict | Exception):
    from core.integrations import integrations

    async def fake_get(name: str):
        assert name == "smart_home"
        return _FakeHAClient(config)

    monkeypatch.setattr(integrations, "get", fake_get)


def test_parse_geo_position_validates_bounds_and_metadata():
    geo = parse_geo_position(
        {
            "latitude": -33.86,
            "longitude": 151.2,
            "source": "gps",
            "accuracy_m": 12.5,
            "captured_at": "2026-07-21T04:00:00Z",
        }
    )
    assert geo is not None
    assert geo.latitude == -33.86
    assert geo.source == "gps"
    assert geo.accuracy_m == 12.5
    assert geo.captured_at == datetime(2026, 7, 21, 4, 0, tzinfo=timezone.utc)


def test_parse_geo_position_rejects_invalid_latitude():
    with pytest.raises(ValueError):
        parse_geo_position({"latitude": 200, "longitude": 0})


def test_parse_geo_position_rejects_negative_accuracy():
    with pytest.raises(ValueError):
        parse_geo_position({"latitude": 0, "longitude": 0, "accuracy_m": -1})


def test_is_placeholder_location():
    assert is_placeholder_location(None)
    assert is_placeholder_location("current location")
    assert is_placeholder_location(" Near Me ")
    assert not is_placeholder_location("100 Example Street, Testville")


def test_device_coordinates_take_precedence(monkeypatch):
    _patch_ha(monkeypatch, Exception("HA should not be consulted"))
    token = _bind(
        {"latitude": -33.86, "longitude": 151.2, "city": "Sydney", "source": "gps"},
        device_kind="phone",
    )
    try:
        result = asyncio.run(resolve_current_location())
    finally:
        reset_tool_context(token)
    assert result is not None
    assert result["latitude"] == -33.86
    assert result["longitude"] == 151.2
    assert result["city"] == "Sydney"
    assert result["source"] == "device"


def test_stale_device_coordinates_are_ignored_for_interactive_device(monkeypatch):
    _patch_ha(monkeypatch, {"latitude": -37.81, "longitude": 144.96, "location_name": "Home"})
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    token = _bind(
        {"latitude": -33.86, "longitude": 151.2, "captured_at": stale, "source": "gps"},
        device_kind="phone",
    )
    try:
        result = asyncio.run(resolve_current_location())
    finally:
        reset_tool_context(token)
    assert result is None


def test_falls_back_to_home_location_for_room_bound_endpoint(monkeypatch):
    _patch_ha(monkeypatch, {"latitude": -37.81, "longitude": 144.96, "location_name": "Home"})
    token = _bind(
        None,
        device_kind="phone",
        location_ref={"provider": "home_assistant", "ha_area_id": "area-bedroom", "room_name": "Bedroom"},
    )
    try:
        result = asyncio.run(resolve_current_location())
    finally:
        reset_tool_context(token)
    assert result == {
        "latitude": -37.81,
        "longitude": 144.96,
        "source": "home",
    }


def test_falls_back_to_home_location_for_satellite(monkeypatch):
    _patch_ha(monkeypatch, {"latitude": -37.81, "longitude": 144.96, "location_name": "Jarvis"})
    token = _bind(None, device_kind="satellite")
    try:
        result = asyncio.run(resolve_current_location())
    finally:
        reset_tool_context(token)
    assert result == {
        "latitude": -37.81,
        "longitude": 144.96,
        "source": "home",
    }
    assert "city" not in result


def test_interactive_device_without_gps_does_not_use_home(monkeypatch):
    _patch_ha(monkeypatch, {"latitude": -37.81, "longitude": 144.96, "location_name": "Home"})
    token = _bind(None, device_kind="phone")
    try:
        result = asyncio.run(resolve_current_location())
    finally:
        reset_tool_context(token)
    assert result is None


def test_search_location_uses_home_as_soft_bias_for_interactive_device(monkeypatch):
    _patch_ha(monkeypatch, {"latitude": -33.86, "longitude": 151.08, "location_name": "Home"})
    token = _bind(None, device_kind="desktop")
    try:
        result = asyncio.run(resolve_search_location())
    finally:
        reset_tool_context(token)
    assert result == {
        "latitude": -33.86,
        "longitude": 151.08,
        "source": "home",
    }


def test_returns_none_when_no_device_coords_and_ha_unavailable(monkeypatch):
    _patch_ha(monkeypatch, RuntimeError("HA unreachable"))
    token = _bind(None)
    try:
        result = asyncio.run(resolve_current_location())
    finally:
        reset_tool_context(token)
    assert result is None


def test_allows_home_location_fallback_matrix():
    assert allows_home_location_fallback({"device_kind": "satellite"})
    assert allows_home_location_fallback({})
    assert allows_home_location_fallback(
        {"device_kind": "phone", "location_ref": {"ha_area_id": "area-1"}}
    )
    assert not allows_home_location_fallback({"device_kind": "phone"})
    assert not allows_home_location_fallback({"device_kind": "browser"})
    assert not allows_home_location_fallback({"device_kind": "desktop"})


def test_geo_position_freshness():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    fresh = GeoPosition(
        latitude=0,
        longitude=0,
        captured_at=now - timedelta(minutes=10),
    )
    stale = GeoPosition(
        latitude=0,
        longitude=0,
        captured_at=now - timedelta(hours=2),
    )
    future = GeoPosition(
        latitude=0,
        longitude=0,
        captured_at=now + timedelta(hours=2),
    )
    assert fresh.is_fresh(now=now)
    assert not stale.is_fresh(now=now)
    assert not future.is_fresh(now=now)
