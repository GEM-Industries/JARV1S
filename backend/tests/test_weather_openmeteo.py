import httpx
import pytest

from plugins.weather.client import (
    ATTRIBUTION_LABEL,
    ATTRIBUTION_URL,
    CurrentWeather,
    ForecastDay,
    OpenMeteoClient,
    WeatherData,
    create_weather_client,
    wmo_code_to_condition,
)
from plugins.weather import WeatherPlugin, _weather_envelope


def test_create_weather_client_requires_no_config():
    client = create_weather_client({})
    assert isinstance(client, OpenMeteoClient)


def test_wmo_code_mapping():
    assert wmo_code_to_condition(0) == "Clear sky"
    assert wmo_code_to_condition(63) == "Moderate rain"
    assert wmo_code_to_condition(999) == "Unknown"


@pytest.mark.asyncio
async def test_geocode_resolves_city():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "geocoding-api.open-meteo.com" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Sydney",
                        "latitude": -33.87,
                        "longitude": 151.21,
                        "country": "Australia",
                        "admin1": "New South Wales",
                    }
                ]
            },
        )

    client = OpenMeteoClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    lat, lon, name = await client.geocode("Sydney")
    assert lat == pytest.approx(-33.87)
    assert lon == pytest.approx(151.21)
    assert "Sydney" in name
    await client.aclose()


@pytest.mark.asyncio
async def test_fetch_forecast_maps_current_and_daily():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "api.open-meteo.com" in str(request.url)
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 22.4,
                    "apparent_temperature": 21.0,
                    "relative_humidity_2m": 55,
                    "weather_code": 2,
                    "wind_speed_10m": 12.3,
                    "is_day": 1,
                },
                "hourly": {"temperature_2m": [22.0, 21.5, 21.0, 20.5, 20.0, 19.5]},
                "daily": {
                    "time": ["2026-06-24", "2026-06-25"],
                    "temperature_2m_max": [24.0, 23.0],
                    "temperature_2m_min": [16.0, 15.0],
                    "weather_code": [2, 61],
                },
            },
        )

    client = OpenMeteoClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    parsed = await client.fetch_forecast(-33.87, 151.21, location_name="Sydney")
    assert parsed.location == "Sydney"
    assert parsed.current.temp == 22
    assert parsed.current.condition == "Partly cloudy"
    assert parsed.current.wind_speed == 12
    assert parsed.hourly_trend == [22, 22, 21, 20, 20, 20]
    assert len(parsed.forecast) == 2
    assert parsed.forecast[1].condition == "Slight rain"
    assert parsed.attribution_url == ATTRIBUTION_URL
    assert parsed.attribution_label == ATTRIBUTION_LABEL
    await client.aclose()


class _FakeWeatherClient:
    async def geocode(self, city: str):
        return -33.87, 151.21, city

    async def fetch_forecast(self, lat: float, lon: float, *, location_name: str | None = None):
        return WeatherData(
            location=location_name,
            current=CurrentWeather(
                temp=22,
                feels_like=21,
                condition="Partly cloudy",
                humidity=55,
                wind_speed=12,
                is_daytime=True,
            ),
            hourly_trend=[22, 21, 20],
            daily_high=24,
            daily_low=16,
            forecast=[
                ForecastDay(day="Wed", high=24, low=16, condition="Partly cloudy"),
            ],
            alerts=[],
        )


@pytest.mark.asyncio
async def test_weather_placeholder_uses_runtime_location(monkeypatch):
    async def fake_location():
        return {"latitude": -33.86, "longitude": 151.2, "city": "Sydney"}

    monkeypatch.setattr("plugins.weather.resolve_current_location", fake_location)
    result = await WeatherPlugin()._resolve_coordinates("current location", _FakeWeatherClient())
    assert result == (-33.86, 151.2, None)


@pytest.mark.asyncio
async def test_omitted_city_uses_device_source_without_place_name(monkeypatch):
    async def fake_location():
        return {"latitude": -34.83, "longitude": 138.64, "city": "Glen Osmond"}

    monkeypatch.setattr("plugins.weather.resolve_current_location", fake_location)
    plugin = WeatherPlugin()
    weather = _FakeWeatherClient()

    result = await plugin.get_weather(weather=weather)
    data = await plugin._fetch_weather_data(None, weather)
    envelope = _weather_envelope(data, None)

    assert result.source == "device"
    assert result.location is None
    assert "location" not in result.model_dump(exclude_none=True)
    assert result.daily
    assert envelope.title == "Weather"
    assert "location" not in envelope.data


@pytest.mark.asyncio
async def test_weather_plugin_returns_attribution_in_text_models():
    plugin = WeatherPlugin()
    weather = _FakeWeatherClient()

    result = await plugin.get_weather(city="Sydney", weather=weather)

    assert result.attribution_label == ATTRIBUTION_LABEL
    assert result.attribution_url == ATTRIBUTION_URL
    assert result.source == "named"
    assert result.location == "Sydney"
    assert result.daily
    assert result.daily[0].day == "Wed"
    assert ATTRIBUTION_LABEL in str(result)


@pytest.mark.asyncio
async def test_weather_widget_payload_keeps_stable_keys_and_attribution():
    plugin = WeatherPlugin()
    weather = _FakeWeatherClient()
    data = await plugin._fetch_weather_data("Sydney", weather)
    envelope = _weather_envelope(data, "Sydney")

    payload = envelope.data
    assert payload["temp"] == 22
    assert payload["feelsLike"] == 21
    assert payload["dailyHigh"] == 24
    assert payload["dailyLow"] == 16
    assert payload["forecast"] == [
        {"day": "Wed", "high": 24, "low": 16, "condition": "Partly cloudy"}
    ]
    assert payload["attributionUrl"] == ATTRIBUTION_URL
    assert payload["attributionLabel"] == ATTRIBUTION_LABEL
    assert payload["location"] == "Sydney"
    assert envelope.title == "Weather: Sydney"
