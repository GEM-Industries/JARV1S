"""
Weather Plugin for Jarvis AI Assistant.

Uses Open-Meteo for keyless weather forecasts.
"""

from typing import Literal, Optional, List

from pydantic import BaseModel

from core.plugins.types import JarvisPlugin, PluginMetadata, UIEnvelope, WidgetLayout, WidgetSize
from core.plugins.ui import push_ui
from core.context import is_placeholder_location, resolve_current_location
from core.decorators import tool
from core.plugins.capabilities import CapabilityErrorDetail
from plugins.weather.client import OpenMeteoClient, WeatherData


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


def _place(location_name: str | None) -> tuple[str | None, Literal["device", "named"]]:
    if location_name:
        return location_name, "named"
    return None, "device"


class WeatherImpact(BaseModel):
    """Contextual implications of current weather."""
    summary: str
    clothing: str
    alerts: List[str] = []


class DailyForecast(BaseModel):
    day: str
    high: int
    low: int
    condition: str


class WeatherReport(BaseModel):
    """Current weather plus the 7-day forecast."""
    source: Literal["device", "named"]
    temperature: float
    feels_like: float
    condition: str
    humidity: int
    wind_speed: float
    is_daytime: bool = True
    location: str | None = None
    daily: List[DailyForecast] = []

    impact: Optional[WeatherImpact] = None

    hourly_trend: List[float] = []
    daily_high: Optional[float] = None
    daily_low: Optional[float] = None
    attribution_label: Optional[str] = None
    attribution_url: Optional[str] = None

    def __str__(self) -> str:
        temp_str = f"{int(self.temperature)}°C"
        feels_str = (
            f" (feels like {int(self.feels_like)}°C)"
            if abs(self.feels_like - self.temperature) > 3
            else ""
        )
        place = f" in {self.location}" if self.location else ""
        base = (
            f"Current weather{place}: "
            f"{temp_str}{feels_str}, {self.condition}. "
            f"Humidity: {self.humidity}%, Wind: {int(self.wind_speed)} km/h."
        )
        if self.impact and self.impact.alerts:
            base = f"⚠️ {', '.join(self.impact.alerts)}. " + base
        if self.daily:
            trend = " ".join(
                f"{day.day}: {day.condition}, high of {day.high}."
                for day in self.daily[:3]
            )
            base = f"{base} Forecast: {trend}"
        if self.attribution_label:
            base = f"{base} {self.attribution_label}."
        return base


def _derive_impact(temp: float, feels_like: float, condition: str, alerts: List[str]) -> WeatherImpact:
    condition_lower = condition.lower()

    if feels_like < 5:
        clothing = "Heavy coat and layers"
    elif feels_like < 12:
        clothing = "Warm jacket"
    elif feels_like < 18:
        clothing = "Light jacket or sweater"
    elif feels_like < 25:
        clothing = "T-shirt weather"
    else:
        clothing = "Light, breathable clothing"

    if "rain" in condition_lower or "drizzle" in condition_lower:
        clothing += ", bring an umbrella"

    if "rain" in condition_lower:
        summary = "Wet conditions expected"
    elif "snow" in condition_lower:
        summary = "Snowy conditions"
    elif "clear" in condition_lower and feels_like > 25:
        summary = "Hot and sunny"
    elif "cloud" in condition_lower:
        summary = "Overcast skies"
    else:
        summary = "Fair conditions"

    return WeatherImpact(summary=summary, clothing=clothing, alerts=alerts)


def _to_daily_forecast(day) -> DailyForecast:
    return DailyForecast(
        day=day.day,
        high=day.high,
        low=day.low,
        condition=day.condition,
    )


def _weather_envelope(data: WeatherData, city: str | None) -> UIEnvelope:
    location, _source = _place(data.location)
    location_slug = (city or "current").lower().replace(" ", "-")
    if not location_slug or location_slug == "-":
        location_slug = "current"
    current = data.current
    payload = {
        "temp": current.temp,
        "feelsLike": current.feels_like,
        "condition": current.condition,
        "humidity": f"{current.humidity}%",
        "wind": f"{current.wind_speed} km/h",
        "hourlyTrend": data.hourly_trend,
        "dailyHigh": data.daily_high,
        "dailyLow": data.daily_low,
        "isDaytime": current.is_daytime,
        "alerts": data.alerts,
        "clothing": _derive_impact(
            current.temp,
            current.feels_like,
            current.condition,
            data.alerts,
        ).clothing,
        "forecast": [_to_daily_forecast(day).model_dump() for day in data.forecast],
        "attributionUrl": data.attribution_url,
        "attributionLabel": data.attribution_label,
    }
    if location:
        payload["location"] = location
    return UIEnvelope(
        widget_id=f"weather-{location_slug}",
        component="WeatherWidget",
        title=f"Weather: {location}" if location else "Weather",
        layout=WidgetLayout(size=WidgetSize.WIDE, priority=5),
        data=payload,
    )


class WeatherPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="weather",
        version="2.1.0",
        description="Get real-time weather with context and forecasts.",
        dependencies=["httpx"],
        utterances=[
            "what's the weather like today",
            "will it rain tomorrow",
            "how hot is it outside",
            "do I need an umbrella",
            "what's the forecast for this weekend",
        ],
    )

    async def register_integrations(self) -> None:
        from core.integrations import integrations
        from plugins.weather.client import create_weather_client

        integrations.register("weather", create_weather_client, config_keys=[])

    async def _resolve_coordinates(
        self,
        city: Optional[str],
        client: OpenMeteoClient,
    ) -> tuple[float, float, str | None] | CapabilityErrorDetail:
        if city and not is_placeholder_location(city):
            return await client.geocode(city)

        location = await resolve_current_location()
        if location:
            return (location["latitude"], location["longitude"], None)

        return _fail(
            "Current location is unavailable. Provide a city, or share GPS from this device."
        )

    async def _fetch_weather_data(
        self,
        city: Optional[str],
        weather: OpenMeteoClient,
    ) -> WeatherData | CapabilityErrorDetail:
        resolved = await self._resolve_coordinates(city, weather)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        lat, lon, location_name = resolved
        return await weather.fetch_forecast(lat, lon, location_name=location_name)

    @tool(inject=["weather"])
    async def get_weather(
        self,
        city: Optional[str] = None,
        weather: OpenMeteoClient = None,
    ) -> WeatherReport | CapabilityErrorDetail:
        """
        Get current weather and the 7-day forecast. Uses current location if city omitted.
        Wind: "light breeze" (<15 km/h), "moderate" (15-30), "strong" (>30) — never exact km/h.
        Lead with impact.alerts if present, then impact.summary and impact.clothing.
        Summarize the forecast trend — don't read every day unless asked.
        """
        data = await self._fetch_weather_data(city, weather)
        if isinstance(data, CapabilityErrorDetail):
            return data
        current = data.current
        location, source = _place(data.location)
        push_ui(_weather_envelope(data, city))
        return WeatherReport(
            source=source,
            location=location,
            temperature=current.temp,
            feels_like=current.feels_like,
            condition=current.condition,
            humidity=current.humidity,
            wind_speed=current.wind_speed,
            is_daytime=current.is_daytime,
            daily=[_to_daily_forecast(day) for day in data.forecast],
            impact=_derive_impact(
                current.temp,
                current.feels_like,
                current.condition,
                data.alerts,
            ),
            hourly_trend=data.hourly_trend,
            daily_high=data.daily_high,
            daily_low=data.daily_low,
            attribution_label=data.attribution_label,
            attribution_url=data.attribution_url,
        )
