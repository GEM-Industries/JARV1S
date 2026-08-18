"""Attention priority vocabulary and gate semantics for trigger delivery."""

from __future__ import annotations

from typing import Literal

AttentionLevel = Literal["normal", "urgent", "critical"]

# Event priority vs owner attention mode: an event passes when its rank meets
# the mode floor. paused=None means nothing pierces it today; a future
# safety-class override must come from TriggerOrigin, never a model-chosen level.
LEVEL_RANK: dict[str, int] = {"normal": 0, "urgent": 1, "critical": 2}
MODE_FLOOR: dict[str, int | None] = {"active": 0, "quiet": 1, "paused": None}

SOUND_BY_LEVEL: dict[AttentionLevel, str] = {
    "normal": "chime",
    "urgent": "timer",
    "critical": "alarm",
}


def breaks_through(attention_mode: str, level: str) -> bool:
    """True when a trigger's declared level may present under the owner's mode."""
    floor = MODE_FLOOR.get(attention_mode, 0)
    return floor is not None and LEVEL_RANK.get(level, 0) >= floor


INTERRUPTIVE_ATTENTION_LEVELS: tuple[AttentionLevel, ...] = tuple(
    level for level in ("normal", "urgent", "critical") if breaks_through("quiet", level)
)


def commitment_attention_mongo_filter() -> dict:
    """Mongo filter for interruptive attention snapshots."""
    return {
        "$or": [
            {"attention_snapshot.requires_ack": True},
            {"attention_snapshot.level": {"$in": list(INTERRUPTIVE_ATTENTION_LEVELS)}},
        ],
    }


def attention_policy_fields(
    importance: AttentionLevel,
    *,
    requires_ack: bool = False,
) -> dict[str, str | bool]:
    """Derive AttentionPolicy fields from declared importance."""
    if requires_ack:
        return {"level": "critical", "requires_ack": True, "sound": "alarm"}
    return {
        "level": importance,
        "requires_ack": False,
        "sound": SOUND_BY_LEVEL[importance],
    }
