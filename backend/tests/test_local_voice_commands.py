import pytest

from core.voice.local_commands import (
    LocalVoiceCommand,
    classify_local_command,
    has_wake_prefix,
    normalize_local_command,
    resolve_local_command,
)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("Jarvis mute.", "mute"),
        ("uhh Javis, mute.", "mute"),
        ("Garvest please mute.", "mute"),
        ("Java's shut down.", "shut down"),
        ("Javas shutdown.", "shutdown"),
        ("hey JARVIS, never mind", "never mind"),
        ("Okay Jarvis resume", "resume"),
        ("uh mute", "uh mute"),
        ("please mute", "mute"),
        ("turn on the lights", "turn on the lights"),
    ],
)
def test_normalize_local_command_strips_wake_prefix_and_punctuation(raw: str, normalized: str) -> None:
    assert normalize_local_command(raw) == normalized


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jarvis stop", True),
        ("okay Jarvis stop", True),
        ("uhh Javis, stop", True),
        ("please stop", False),
        ("okay okay I will check", False),
    ],
)
def test_has_wake_prefix_reuses_local_command_wake_words(text: str, expected: bool) -> None:
    assert has_wake_prefix(text) is expected


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("Jarvis mute", LocalVoiceCommand.SOFT_MUTE),
        ("please mute", LocalVoiceCommand.SOFT_MUTE),
        ("Garvest please mute", LocalVoiceCommand.SOFT_MUTE),
        ("Jarvis unmute", LocalVoiceCommand.UNMUTE),
        ("Jarvis power down", LocalVoiceCommand.POWER_DOWN),
        ("Jarvis power on", LocalVoiceCommand.POWER_ON),
        ("Jarvis go offline", LocalVoiceCommand.POWER_DOWN),
        ("Java's shut down.", LocalVoiceCommand.POWER_DOWN),
        ("Javas shutdown.", LocalVoiceCommand.POWER_DOWN),
        ("Jarvis come online", LocalVoiceCommand.POWER_ON),
        ("Jarvis, you in there?", LocalVoiceCommand.POWER_CHECK),
        ("Jarvis are you in there", LocalVoiceCommand.POWER_CHECK),
        ("never mind", LocalVoiceCommand.STOP_LISTENING),
        ("stop listening", LocalVoiceCommand.STOP_LISTENING),
        ("stop", LocalVoiceCommand.STOP),
        ("wait", LocalVoiceCommand.STOP),
        ("cancel", LocalVoiceCommand.STOP),
        ("hold on", LocalVoiceCommand.STOP),
        ("Jarvis go quiet", LocalVoiceCommand.NONE),
        ("turn on the office lights", LocalVoiceCommand.NONE),
        ("hey jarvis mute this person on insta", LocalVoiceCommand.NONE),
        ("jarvis stop talking about instagram", LocalVoiceCommand.NONE),
        ("jarvis unmute my laptop", LocalVoiceCommand.NONE),
    ],
)
def test_classify_local_command_uses_exact_short_phrases(text: str, command: LocalVoiceCommand) -> None:
    assert classify_local_command(text) is command


def test_resolve_local_command_drops_non_unmute_while_soft_muted() -> None:
    assert resolve_local_command("turn on the lights", soft_muted=True) is LocalVoiceCommand.DROP
    assert resolve_local_command("Jarvis mute", soft_muted=True) is LocalVoiceCommand.DROP
    assert resolve_local_command("Jarvis unmute", soft_muted=True) is LocalVoiceCommand.UNMUTE
    assert resolve_local_command("Jarvis power down", soft_muted=True) is LocalVoiceCommand.POWER_DOWN
    assert resolve_local_command("Jarvis power on", soft_muted=True) is LocalVoiceCommand.POWER_ON
    assert resolve_local_command("Jarvis, you in there?", soft_muted=True) is LocalVoiceCommand.POWER_CHECK
    assert resolve_local_command("stop", soft_muted=True) is LocalVoiceCommand.STOP
    assert resolve_local_command("dismiss", soft_muted=True) is LocalVoiceCommand.ACKNOWLEDGE
    assert resolve_local_command("snooze ten minutes", soft_muted=True) is LocalVoiceCommand.SNOOZE
