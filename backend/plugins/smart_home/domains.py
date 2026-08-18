"""Home Assistant domain helpers for smart_home."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

# Domains safe for setup milestone toggles (explicit allowlist).
SAFE_SETUP_DOMAINS = frozenset({"light", "switch", "input_boolean", "fan"})

# Never use for automated setup proof.
UNSAFE_SETUP_DOMAINS = frozenset(
    {
        "lock",
        "cover",
        "alarm_control_panel",
        "camera",
        "climate",
        "vacuum",
        "water_heater",
        "humidifier",
        "valve",
        "button",
        "scene",
        "script",
        "automation",
    }
)

# Actions that should never execute from a model-authored tool call without
# the shared approval flow.
CONSENT_REQUIRED_DOMAINS = frozenset(
    {
        "lock",
        "alarm_control_panel",
        "cover",
        "valve",
    }
)

CONSENT_REQUIRED_ACTIONS = frozenset(
    {
        "unlock",
        "alarm_disarm",
        "open_cover",
        "close_cover",
        "set_cover_position",
    }
)

COLOR_MODES = frozenset({"rgb", "rgbw", "rgbww", "hs", "xy"})

ADJUST_AMOUNTS = ("slight", "normal", "large")


# HA color_name for absolute requests; hue/sat anchors for relative steps.
@dataclass(frozen=True)
class HueAnchor:
    hue_deg: float
    saturation: float


HUE_ANCHORS: dict[str, HueAnchor] = {
    "orange": HueAnchor(27, 0.90),
    "red": HueAnchor(4, 0.88),
    "blue": HueAnchor(220, 0.72),
    "green": HueAnchor(125, 0.68),
    "yellow": HueAnchor(52, 0.82),
    "purple": HueAnchor(275, 0.62),
    "pink": HueAnchor(330, 0.52),
}

HUE_STEP_DEGREES = {
    "slight": 12,
    "normal": 25,
    "large": 45,
}

# Neutral warm-white starting point when the bulb is in CT mode or has no RGB state.
_WARM_WHITE_RGB = (255, 214, 170)

_COLOR_RELATIVE_PREFIXES = (
    "a bit more ",
    "even more ",
    "slightly more ",
    "more ",
    "a bit ",
    "slightly ",
)


@dataclass(frozen=True)
class LiveLightState:
    state: str
    brightness_pct: int | None
    color_mode: str | None
    rgb_color: list[int] | None
    color_temp_kelvin: int | None
    supported_color_modes: list[str]
    min_color_temp_kelvin: int | None
    max_color_temp_kelvin: int | None


DOMAIN_CAPABILITIES: dict[str, list[str]] = {
    "switch": ["on_off"],
    "fan": ["on_off"],
    "input_boolean": ["on_off"],
    "cover": ["open", "close", "position"],
    "climate": ["temperature", "hvac_mode"],
    "lock": ["lock", "unlock"],
    "alarm_control_panel": ["arm", "disarm"],
    "media_player": ["play", "pause", "volume"],
}

# Domains whose full set of control services is closed and small. Restricting
# only these catches invented services (e.g. light.set_attributes) at zero
# flexibility cost — brightness/color flow through turn_on params. Open-ended
# domains (fan speed, cover position, climate, media, vacuum, ...) are NOT listed
# so the model can call any valid HA service for them.
CLOSED_CONTROL_DOMAINS: dict[str, frozenset[str]] = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
    "input_boolean": frozenset({"turn_on", "turn_off", "toggle"}),
}

SEARCH_DOMAIN_PRIORITY: dict[str, int] = {
    "light": 0,
    "switch": 1,
    "fan": 2,
    "input_boolean": 3,
    "cover": 10,
    "climate": 11,
    "lock": 12,
    "alarm_control_panel": 13,
    "media_player": 14,
    "select": 40,
    "number": 41,
    "binary_sensor": 50,
    "sensor": 51,
}

if TYPE_CHECKING:
    from plugins.smart_home.inventory import InventoryEntity


def entity_domain(entity_id: str) -> str:
    if "." not in entity_id:
        return "unknown"
    return entity_id.split(".", 1)[0]


def live_light_state_from_ha(
    state: dict[str, Any],
    *,
    fallback_modes: list[str] | None = None,
) -> LiveLightState:
    attrs = state.get("attributes") or {}
    entity_state = str(state.get("state", "unknown"))
    brightness_raw = attrs.get("brightness")
    brightness = (
        int(brightness_raw) if isinstance(brightness_raw, int | float) else None
    )
    rgb_raw = attrs.get("rgb_color")
    rgb_color = _coerce_rgb(rgb_raw) if rgb_raw is not None else None
    kelvin_raw = attrs.get("color_temp_kelvin")
    kelvin = int(round(kelvin_raw)) if isinstance(kelvin_raw, int | float) else None
    modes = _string_list(attrs.get("supported_color_modes")) or list(
        fallback_modes or []
    )
    min_k = attrs.get("min_color_temp_kelvin")
    max_k = attrs.get("max_color_temp_kelvin")
    return LiveLightState(
        state=entity_state,
        brightness_pct=brightness_pct_from_ha(brightness, state=entity_state),
        color_mode=str(attrs["color_mode"]) if attrs.get("color_mode") else None,
        rgb_color=rgb_color,
        color_temp_kelvin=kelvin,
        supported_color_modes=modes,
        min_color_temp_kelvin=int(round(min_k))
        if isinstance(min_k, int | float)
        else None,
        max_color_temp_kelvin=int(round(max_k))
        if isinstance(max_k, int | float)
        else None,
    )


def parse_color_phrase(color: str) -> tuple[str, bool]:
    normalized = " ".join(color.strip().casefold().split())
    for prefix in _COLOR_RELATIVE_PREFIXES:
        if normalized.startswith(prefix):
            return normalized.removeprefix(prefix), True
    return normalized, False


def relative_kelvin_adjustment(
    live: LiveLightState,
    warmth: Literal["warmer", "cooler"],
    amount: str,
    steps_mired: dict[str, int],
) -> int | None:
    if "color_temp" not in live.supported_color_modes:
        return None

    step_mired = steps_mired.get(amount, steps_mired["slight"])
    lo = live.min_color_temp_kelvin
    hi = live.max_color_temp_kelvin
    current = live.color_temp_kelvin
    if current is None:
        fallback = 2700 if warmth == "warmer" else 4000
        return (
            max(lo, min(hi, fallback))
            if lo is not None and hi is not None
            else fallback
        )

    current_mired = 1_000_000 / current
    target_mired = (
        current_mired + step_mired if warmth == "warmer" else current_mired - step_mired
    )
    if target_mired <= 0:
        target = hi or current
    else:
        target = round(1_000_000 / target_mired)
    if lo is not None and hi is not None:
        target = max(lo, min(hi, target))
    if target == current:
        return None
    return target


def resolve_hue_adjustment(
    color: str,
    amount: str,
    live: LiveLightState,
    *,
    relative: bool | None = None,
) -> dict[str, Any] | None:
    hue_name, phrase_is_relative = parse_color_phrase(color)
    is_relative = phrase_is_relative if relative is None else relative
    anchor = HUE_ANCHORS.get(hue_name)
    modes = set(live.supported_color_modes or [])
    supports_hue = bool(modes & COLOR_MODES)

    if anchor is not None and supports_hue:
        if is_relative:
            current_h, current_s, current_v = _current_hsv(live)
            step = HUE_STEP_DEGREES.get(amount, HUE_STEP_DEGREES["slight"])
            new_h = _step_hue_toward(current_h, anchor.hue_deg, step)
            blend = min(1.0, step / HUE_STEP_DEGREES["large"])
            new_s = current_s + (anchor.saturation - current_s) * blend
            rgb = _hsv_to_rgb(new_h, new_s, current_v)
            if live.rgb_color is not None and rgb == live.rgb_color:
                return None
            return {"rgb_color": rgb}
        return {"color_name": hue_name}

    if hue_name:
        return {"color_name": hue_name}
    return None


def _current_hsv(live: LiveLightState) -> tuple[float, float, float]:
    if live.rgb_color is not None:
        return _rgb_to_hsv(*live.rgb_color)
    return _rgb_to_hsv(*_WARM_WHITE_RGB)


def _step_hue_toward(current_h: float, target_h: float, degrees: float) -> float:
    delta = ((target_h - current_h + 180) % 360) - 180
    if abs(delta) <= degrees:
        return target_h % 360
    return (current_h + degrees * (1 if delta > 0 else -1)) % 360


def _rgb_to_hsv(red: int, green: int, blue: int) -> tuple[float, float, float]:
    r, g, b = red / 255, green / 255, blue / 255
    max_c = max(r, g, b)
    min_c = min(r, g, b)
    delta = max_c - min_c
    if delta == 0:
        hue = 0.0
    elif max_c == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif max_c == g:
        hue = 60 * (((b - r) / delta) + 2)
    else:
        hue = 60 * (((r - g) / delta) + 4)
    saturation = 0.0 if max_c == 0 else delta / max_c
    return hue % 360, saturation, max_c


def _hsv_to_rgb(hue: float, saturation: float, value: float) -> list[int]:
    if saturation <= 0:
        channel = int(round(value * 255))
        return [channel, channel, channel]
    hue_sector = (hue % 360) / 60
    sector = int(hue_sector)
    fraction = hue_sector - sector
    p = value * (1 - saturation)
    q = value * (1 - saturation * fraction)
    t = value * (1 - saturation * (1 - fraction))
    red, green, blue = (
        (value, t, p),
        (q, value, p),
        (p, value, t),
        (p, q, value),
        (t, p, value),
        (value, p, q),
    )[sector % 6]
    return [
        max(0, min(255, int(round(channel * 255)))) for channel in (red, green, blue)
    ]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def brightness_pct_from_ha(brightness: int | None, *, state: str) -> int | None:
    if brightness is None:
        return None
    pct = round(brightness * 100 / 255)
    if state == "on" and brightness > 0 and pct < 1:
        return 1
    return max(0, min(100, pct))


def capabilities_for_entity(entity: InventoryEntity) -> list[str]:
    if entity.domain != "light":
        return list(DOMAIN_CAPABILITIES.get(entity.domain, []))

    modes = set(entity.supported_color_modes or [])
    caps = ["on_off"]
    if modes - {"onoff"}:
        caps.append("brightness")
    if "color_temp" in modes:
        caps.append("color_temp")
    if modes & COLOR_MODES:
        caps.append("color")
    return caps


def clamp_light_params(
    entity: InventoryEntity, params: dict[str, Any] | None
) -> dict[str, Any]:
    """Normalize model-facing light params to the HA service payload."""
    raw = dict(params or {})
    caps = set(capabilities_for_entity(entity))
    allowed_keys = {"brightness_pct", "color_temp_kelvin", "rgb_color", "color_name"}
    unknown = sorted(set(raw) - allowed_keys)
    if unknown:
        raise ValueError(
            "Unsupported light parameter(s): "
            + ", ".join(unknown)
            + ". Use brightness_pct, color_temp_kelvin, rgb_color, or color_name."
        )

    allowed = {k: v for k, v in raw.items() if k in allowed_keys}
    if "color_name" in allowed:
        name = str(allowed["color_name"]).strip().casefold()
        if not name:
            raise ValueError(
                "color_name must be a non-empty Home Assistant color name."
            )
        allowed["color_name"] = name

    unsupported: list[str] = []
    if "brightness_pct" in allowed and "brightness" not in caps:
        unsupported.append("brightness_pct")
    if "color_temp_kelvin" in allowed and "color_temp" not in caps:
        unsupported.append("color_temp_kelvin")
    if ("rgb_color" in allowed or "color_name" in allowed) and "color" not in caps:
        unsupported.append("rgb_color/color_name")
    if unsupported:
        raise ValueError(
            f"{entity.entity_id} does not support: {', '.join(unsupported)}. "
            f"Capabilities: {', '.join(capabilities_for_entity(entity)) or 'none'}."
        )

    wants_kelvin = "color_temp_kelvin" in allowed and "color_temp" in caps
    wants_rgb = "rgb_color" in allowed and "color" in caps
    wants_color_name = "color_name" in allowed and "color" in caps
    if wants_rgb and wants_color_name:
        allowed.pop("color_name")
        wants_color_name = False
    if wants_kelvin and (wants_rgb or wants_color_name):
        allowed.pop("color_temp_kelvin")
        wants_kelvin = False

    result: dict[str, Any] = {}

    if "brightness_pct" in allowed and "brightness" in caps:
        value = _coerce_percent(allowed["brightness_pct"])
        if value is not None:
            result["brightness_pct"] = max(1, min(100, value))

    if wants_kelvin:
        value = allowed["color_temp_kelvin"]
        if isinstance(value, int | float):
            kelvin = int(round(value))
            lo, hi = entity.min_color_temp_kelvin, entity.max_color_temp_kelvin
            if lo is not None and hi is not None:
                kelvin = max(lo, min(hi, kelvin))
            result["color_temp_kelvin"] = kelvin

    if wants_rgb:
        rgb = _coerce_rgb(allowed["rgb_color"])
        if rgb is None:
            raise ValueError("rgb_color must be [r, g, b] or {r, g, b}.")
        result["rgb_color"] = rgb
    if wants_color_name:
        result["color_name"] = allowed["color_name"]

    return result


def _coerce_rgb(value: Any) -> list[int] | None:
    if isinstance(value, dict):
        if not {"r", "g", "b"} <= set(value):
            return None
        value = [value["r"], value["g"], value["b"]]
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    if not all(isinstance(c, int | float) for c in value):
        return None
    return [max(0, min(255, int(round(c)))) for c in value]


def _coerce_percent(value: Any) -> int | None:
    if isinstance(value, int | float):
        return int(round(value))
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            return int(round(float(text)))
        except ValueError:
            return None
    return None


def search_priority(entity_id: str) -> int:
    return SEARCH_DOMAIN_PRIORITY.get(entity_domain(entity_id), 99)


def is_safe_setup_entity(entity_id: str) -> bool:
    domain = entity_domain(entity_id)
    return domain in SAFE_SETUP_DOMAINS and domain not in UNSAFE_SETUP_DOMAINS


def requires_control_consent(entity_id: str, action: str) -> bool:
    domain = entity_domain(entity_id)
    return domain in CONSENT_REQUIRED_DOMAINS or action in CONSENT_REQUIRED_ACTIONS


def supported_control_actions(domain: str) -> frozenset[str] | None:
    """Closed action set for the domain, or None when HA services are open-ended."""
    return CLOSED_CONTROL_DOMAINS.get(domain)


def is_supported_control_action(domain: str, action: str) -> bool:
    allowed = CLOSED_CONTROL_DOMAINS.get(domain)
    return allowed is None or action in allowed


def default_service_for_action(domain: str, action: str) -> tuple[str, str]:
    """Map action name to (domain, service) for HA REST API."""
    if action in {"turn_on", "turn_off", "toggle"}:
        return domain, action
    if domain == "cover":
        return "cover", action
    if domain == "lock":
        return "lock", action
    if domain == "climate":
        return "climate", action
    if domain == "media_player":
        return "media_player", action
    if domain == "alarm_control_panel":
        return "alarm_control_panel", action
    return domain, action
