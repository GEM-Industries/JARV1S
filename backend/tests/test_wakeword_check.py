"""Unit tests for bounded wake-phrase check helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.voice.wakeword.check import WakeCheckError, check_wake_phrase, validate_wake_check_pcm
from core.voice.wakeword.types import WakeDecision


def test_validate_rejects_empty_and_odd_length() -> None:
    with pytest.raises(WakeCheckError) as empty:
        validate_wake_check_pcm(b"")
    assert empty.value.reason == "too_short"

    with pytest.raises(WakeCheckError) as odd:
        validate_wake_check_pcm(b"\x00")
    assert odd.value.reason == "processing_failed"


def test_check_wake_phrase_recognized() -> None:
    service = MagicMock()
    service.model_loaded = True
    service.process.return_value = True
    service.last_had_candidate = True

    with patch("core.voice.wakeword.check.WakeWordService") as cls:
        cls.return_value = service
        cls.INFERENCE_WINDOW_BYTES = 4
        result = check_wake_phrase(b"\x01\x00\x02\x00\x03\x00\x04\x00", owner_id="owner-a")

    assert result.status == "recognized"
    service.reset.assert_called_once()


def test_check_wake_phrase_speaker_mismatch() -> None:
    service = MagicMock()
    service.model_loaded = True
    service.process.return_value = False
    service.last_had_candidate = True
    service.last_decision = WakeDecision(
        accept=False,
        reason="speaker_mismatch",
        scores={"speaker_cosine": 0.2},
    )

    with patch("core.voice.wakeword.check.WakeWordService") as cls:
        cls.return_value = service
        cls.INFERENCE_WINDOW_BYTES = 4
        result = check_wake_phrase(b"\x01\x00\x02\x00", owner_id="owner-a")

    assert result.status == "speaker_mismatch"


def test_check_wake_phrase_not_detected() -> None:
    service = MagicMock()
    service.model_loaded = True
    service.process.return_value = False
    service.last_had_candidate = False
    service.last_decision = None

    with patch("core.voice.wakeword.check.WakeWordService") as cls:
        cls.return_value = service
        cls.INFERENCE_WINDOW_BYTES = 4
        result = check_wake_phrase(b"\x00\x00\x00\x00", owner_id=None)

    assert result.status == "not_detected"
    assert service.process.call_count >= 1


def test_check_wake_phrase_requires_model() -> None:
    service = MagicMock()
    service.model_loaded = False

    with patch("core.voice.wakeword.check.WakeWordService", return_value=service):
        with pytest.raises(WakeCheckError) as exc:
            check_wake_phrase(b"\x00\x00", owner_id="owner-a")
    assert exc.value.reason == "processing_failed"
