"""Open-Meteo weather client — keyless forecast and geocoding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
ATTRIBUTION_URL = "https://open-meteo.com/"
ATTRIBUTION_LABEL = "Weather data by Open-Meteo.com"

# WMO weather interpretation codes (Open-Meteo)
_WMO_DESCRIPTIONS: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def wmo_code_to_condition(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return _WMO_DESCRIPTIONS.get(int(code), "Unknown")


@dataclass(frozen=True, slots=True)
class CurrentWeather:
    temp: int
    feels_like: int
    condition: str
    humidity: int
    wind_speed: int
    is_daytime: bool


@dataclass(frozen=True, slots=True)
class ForecastDay:
    day: str
    high: int
    low: int
    condition: str


@dataclass(frozen=True, slots=True)
class WeatherData:
    location: str | None
    current: CurrentWeather
    hourly_trend: list[int]
    daily_high: int
    daily_low: int
    forecast: list[ForecastDay]
    alerts: list[str]
    attribution_url: str = ATTRIBUTION_URL
    attribution_label: str = ATTRIBUTION_LABEL


class OpenMeteoClient:
    """Async client for Open-Meteo forecast and geocoding APIs."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def geocode(self, city: str) -> tuple[float, float, str]:
        resp = await self._http.get(
            GEOCODING_URL,
            params={"name": city, "count": 1, "language": "en", "format": "json"},
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            raise ValueError(f"City not found: {city}")
        place = results[0]
        name = str(place.get("name") or city)
        admin = place.get("admin1")
        country = place.get("country")
        if admin and country:
            name = f"{name}, {admin}, {country}"
        elif country:
            name = f"{name}, {country}"
        return float(place["latitude"]), float(place["longitude"]), name

    async def fetch_forecast(self, lat: float, lon: float, *, location_name: str | None = None) -> WeatherData:
        resp = await self._http.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "timezone": "auto",
                "forecast_days": 7,
                "wind_speed_unit": "kmh",
                "current": (
                    "temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "weather_code,wind_speed_10m,is_day"
                ),
                "hourly": "temperature_2m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        current_raw = data.get("current") or {}
        temp = round(float(current_raw.get("temperature_2m", 0)))
        feels_like = round(float(current_raw.get("apparent_temperature", temp)))
        condition = wmo_code_to_condition(current_raw.get("weather_code"))
        humidity = int(current_raw.get("relative_humidity_2m") or 0)
        wind_kmh = round(float(current_raw.get("wind_speed_10m") or 0))
        is_daytime = bool(current_raw.get("is_day", 1))

        hourly = data.get("hourly") or {}
        hourly_temps = hourly.get("temperature_2m") or []
        hourly_trend = [round(float(t)) for t in hourly_temps[:6]]

        daily = data.get("daily") or {}
        daily_max = daily.get("temperature_2m_max") or []
        daily_min = daily.get("temperature_2m_min") or []
        daily_codes = daily.get("weather_code") or []
        daily_times = daily.get("time") or []

        forecast: list[ForecastDay] = []
        for i, day_time in enumerate(daily_times[:7]):
            try:
                dt = datetime.fromisoformat(str(day_time))
            except ValueError:
                dt = datetime.now(timezone.utc)
            high = round(float(daily_max[i])) if i < len(daily_max) else temp
            low = round(float(daily_min[i])) if i < len(daily_min) else temp
            code = daily_codes[i] if i < len(daily_codes) else None
            forecast.append(
                ForecastDay(
                    day=dt.strftime("%a"),
                    high=high,
                    low=low,
                    condition=wmo_code_to_condition(code),
                )
            )

        daily_high = forecast[0].high if forecast else temp
        daily_low = forecast[0].low if forecast else temp

        return WeatherData(
            location=location_name,
            current=CurrentWeather(
                temp=temp,
                feels_like=feels_like,
                condition=condition,
                humidity=humidity,
                wind_speed=wind_kmh,
                is_daytime=is_daytime,
            ),
            hourly_trend=hourly_trend,
            daily_high=daily_high,
            daily_low=daily_low,
            forecast=forecast,
            alerts=[],
        )


def create_weather_client(config: dict) -> OpenMeteoClient:
    """Factory for the keyless Open-Meteo weather client."""
    _ = config
    return OpenMeteoClient()
