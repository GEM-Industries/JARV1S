"""Small deterministic voice controls handled before the agent loop."""

from __future__ import annotations

import re
from enum import StrEnum


class LocalVoiceCommand(StrEnum):
    NONE = "none"
    STOP_LISTENING = "stop_listening"
    SOFT_MUTE = "soft_mute"
    UNMUTE = "unmute"
    POWER_DOWN = "power_down"
    POWER_ON = "power_on"
    POWER_CHECK = "power_check"
    STOP = "stop"
    DROP = "drop"
    ACKNOWLEDGE = "acknowledge"
    SNOOZE = "snooze"


_LEADING_FILLERS = frozenset({"ah", "oh", "uh", "uhh", "um", "umm", "erm", "hey", "ok", "okay"})
_WAKE_WORDS = frozenset({"jarvis", "javis", "garvest", "javas", "java"})
_POLITE_PREFIXES = frozenset({"please"})

_STOP_LISTENING_PHRASES = frozenset({
    "sleep",
    "go to sleep",
    "never mind",
    "nevermind",
    "stop listening",
})

_SOFT_MUTE_PHRASES = frozenset({
    "mute",
    "mute yourself",
})

_UNMUTE_PHRASES = frozenset({
    "unmute",
    "resume",
    "wake up",
    "start listening",
    "listen again",
})

_POWER_DOWN_PHRASES = frozenset({
    "power down",
    "shut down",
    "shutdown",
    "shut yourself down",
    "go offline",
})

_POWER_ON_PHRASES = frozenset({
    "power on",
    "come online",
})

_POWER_CHECK_PHRASES = frozenset({
    "you in there",
    "you there",
    "are you in there",
})

_STOP_PHRASES = frozenset({
    "stop",
    "stop talking",
    "be quiet",
    "wait",
    "cancel",
    "hold on",
})

_ACKNOWLEDGE_PHRASES = frozenset({
    "dismiss",
    "acknowledge",
    "got it",
    "okay okay",
    "i hear you",
    "turn it off",
    "shut it off",
})

_SNOOZE_PHRASES = frozenset({
    "snooze",
    "snooze ten minutes",
    "snooze five minutes",
    "remind me later",
})


def normalize_local_command(text: str) -> str:
    """Normalize short room-control phrases without trying to infer intent."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    tokens = normalized.split()
    candidate = list(tokens)
    while candidate and candidate[0] in _LEADING_FILLERS:
        candidate.pop(0)
    stripped_wake_prefix = False
    if candidate and candidate[0] in _WAKE_WORDS:
        candidate = candidate[1:]
        stripped_wake_prefix = True
        # STT often splits "Java's" into "java" + "s".
        if candidate and candidate[0] == "s":
            candidate = candidate[1:]
    elif candidate != tokens:
        candidate = list(tokens)
    if candidate and candidate[0] in _POLITE_PREFIXES:
        candidate = candidate[1:]
    if stripped_wake_prefix or candidate != tokens:
        return " ".join(candidate)
    return " ".join(tokens)


def has_wake_prefix(text: str) -> bool:
    """Return true when text starts with a known wake word after leading fillers."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    tokens = normalized.split()
    while tokens and tokens[0] in _LEADING_FILLERS:
        tokens.pop(0)
    return bool(tokens and tokens[0] in _WAKE_WORDS)


def classify_local_command(text: str) -> LocalVoiceCommand:
    """Classify exact full-transcript control phrases; never match substrings."""
    normalized = normalize_local_command(text)
    if normalized in _STOP_LISTENING_PHRASES:
        return LocalVoiceCommand.STOP_LISTENING
    if normalized in _SOFT_MUTE_PHRASES:
        return LocalVoiceCommand.SOFT_MUTE
    if normalized in _UNMUTE_PHRASES:
        return LocalVoiceCommand.UNMUTE
    if normalized in _POWER_DOWN_PHRASES:
        return LocalVoiceCommand.POWER_DOWN
    if normalized in _POWER_ON_PHRASES:
        return LocalVoiceCommand.POWER_ON
    if normalized in _POWER_CHECK_PHRASES:
        return LocalVoiceCommand.POWER_CHECK
    if normalized in _STOP_PHRASES:
        return LocalVoiceCommand.STOP
    if normalized in _ACKNOWLEDGE_PHRASES:
        return LocalVoiceCommand.ACKNOWLEDGE
    if normalized in _SNOOZE_PHRASES:
        return LocalVoiceCommand.SNOOZE
    return LocalVoiceCommand.NONE


_SOFT_MUTE_PASSTHROUGH = frozenset({
    LocalVoiceCommand.UNMUTE,
    LocalVoiceCommand.POWER_DOWN,
    LocalVoiceCommand.POWER_ON,
    LocalVoiceCommand.POWER_CHECK,
    LocalVoiceCommand.STOP,
    LocalVoiceCommand.ACKNOWLEDGE,
    LocalVoiceCommand.SNOOZE,
})


def resolve_local_command(text: str, *, soft_muted: bool = False) -> LocalVoiceCommand:
    """Resolve transcript + soft-mute state to the local command handler outcome.

    Power and trigger-control commands pass through soft-mute so JARV1S can
    be resumed or silenced further without accepting normal turns.
    """
    command = classify_local_command(text)
    if soft_muted and command not in _SOFT_MUTE_PASSTHROUGH:
        return LocalVoiceCommand.DROP
    return command
