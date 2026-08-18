"""Pydantic models for the attention subsystem.

The effective attention mode is *derived*, never stored authoritatively. The
only persisted decision is the user's `ManualOverride`; the effective
`AttentionState` is computed from that override plus active `QuietWindow`s.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

AttentionMode = Literal["active", "quiet", "paused"]

# Sources that represent an explicit user choice (vs. derived state).
ManualSource = Literal["manual", "tool", "local_command"]
MANUAL_SOURCES: frozenset[str] = frozenset({"manual", "tool", "local_command"})

# Where the *effective* state came from: a manual source, a quiet window, or
# the default (nothing active).
AttentionSource = Literal["manual", "tool", "local_command", "schedule", "default"]


class ManualOverride(BaseModel):
    """The user's explicit attention choice — the only authoritative input.

    `expires_at=None` means "until explicitly changed". For a manual `active`
    taken during a quiet window, `expires_at` is the window boundary so the
    schedule resumes afterwards instead of being permanently suppressed.
    """

    mode: AttentionMode
    source: ManualSource = "tool"
    set_at: datetime
    expires_at: datetime | None = None


class AttentionState(BaseModel):
    """Derived, read-only view of an owner's current attention."""

    owner_id: str
    mode: AttentionMode = "active"
    # When the current mode ends (override expiry or window boundary). None = indefinite.
    expires_at: datetime | None = None
    updated_at: datetime | None = None
    source: AttentionSource = "default"
    # Quiet windows responsible for the current mode (only when source == "schedule").
    active_window_ids: tuple[str, ...] = ()


class QuietWindow(BaseModel):
    """A recurring local-wall-clock window during which attention is quiet."""

    id: str
    owner_id: str
    name: str
    start_time: str
    end_time: str
    timezone: str = "UTC"
    days: list[str] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
