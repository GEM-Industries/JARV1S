"""Temporary spoken fallback while the active model does not own preambles.

Voice-agent stacks keep this in the session, not in tool definitions:
Deepgram injects filler during function execution; LiveKit plays it through
``session.say`` / ``with_filler``. OpenAI Realtime can own preambles natively.

Decision (VoiceDelivery, first tool batch of the turn):
1. Model already spoke this round → use that. Do not add a second phrase.
2. Capability is in ``_SILENT_CAPABILITIES`` → stay quiet (instant control).
3. Otherwise queue one TTS phrase and return immediately so dispatch is not
   blocked. The agent yields all TEXT from the round before TOOL_CALL, so a
   fast model cannot race native speech vs this fallback.

When a provider emits mixed speech + tool calls reliably, delete this module
and the VoiceDelivery call site. Do not add plugin decorator flags for it.
"""

from __future__ import annotations

import random

# Spoken like JARVIS, not a generic assistant: short, contracted, dry.
# Vary sentence shape. "sir" is sparse. No filler, no enthusiasm.
PHRASES: tuple[str, ...] = (
    "On it.",
    "Just a moment.",
    "Checking now.",
    "I'll have a look.",
    "One second.",
    "Won't be long.",
    "Give me a moment.",
    "Just checking.",
    "Right away.",
    "A moment.",
    "Bear with me.",
    "Let's see.",
    "I'll look.",
    "I'll check.",
    "Just a second.",
    "This won't take long.",
    "Of course.",
    "On it, sir.",
    "Just a moment, sir.",
)

_unused: list[str] = []

# Instantaneous controls have no latency to mask. Lives here so plugins stay
# unaware of voice delivery. Delete with this module.
_SILENT_CAPABILITIES: frozenset[str] = frozenset({
    "smart_home.adjust_lights",
    "smart_home.control_devices",
    "smart_home.control_lights",
    "spotify.pause",
    "spotify.play",
    "spotify.repeat",
    "spotify.set_volume",
    "spotify.shuffle",
    "spotify.skip",
    "spotify.transfer_playback",
    "system.set_volume",
})


def phrase_for(capability: str | None) -> str | None:
    """Return one spoken fallback, or None to stay quiet.

    Draws without replacement so consecutive turns do not repeat a stock line.
    """
    global _unused
    if not capability or capability in _SILENT_CAPABILITIES:
        return None
    if not _unused:
        _unused = list(PHRASES)
        random.shuffle(_unused)
    return _unused.pop()
