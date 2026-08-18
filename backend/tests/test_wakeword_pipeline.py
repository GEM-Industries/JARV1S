"""Tests for WakeWordService pipeline refactor."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.voice.wakeword.types import WakeCandidate, WakeDecision
from core.voice.wakeword.verifiers import AcceptAllWakeVerifier
from core.voice.wakeword_service import WakeWordService


class _FakeVerifier:
    def __init__(self, accept: bool, reason: str = "test") -> None:
        self.accept = accept
        self.reason = reason
        self.calls: list[WakeCandidate] = []

    def verify(self, candidate: WakeCandidate) -> WakeDecision:
        self.calls.append(candidate)
        return WakeDecision(accept=self.accept, reason=self.reason, scores={"fake": candidate.score})


class _RejectAllWakeVerifier:
    def verify(self, candidate: WakeCandidate) -> WakeDecision:
        return WakeDecision(
            accept=False,
            reason="reject_all",
            scores={candidate.stage: candidate.score},
        )


def _candidate(audio: bytes = b"wake", score: float = 0.99) -> WakeCandidate:
    return WakeCandidate(audio=audio, score=score, stage="oww")


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_process_tracks_pipeline_stats(mock_stage_cls: MagicMock) -> None:
    stage = MagicMock()
    stage.model_loaded = True
    stage.process.return_value = _candidate()
    mock_stage_cls.return_value = stage

    ww = WakeWordService(verifiers=[AcceptAllWakeVerifier()])
    ww.reset_stats()
    assert ww.process(b"chunk") is True
    stats = ww.pipeline_stats
    assert stats["candidates"] == 1
    assert stats["commits"] == 1
    assert stats["verifier_rejects"] == 0


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_process_counts_verifier_rejects(mock_stage_cls: MagicMock) -> None:
    stage = MagicMock()
    stage.model_loaded = True
    stage.process.return_value = _candidate()
    mock_stage_cls.return_value = stage

    ww = WakeWordService(verifiers=[_RejectAllWakeVerifier()])
    ww.reset_stats()
    assert ww.process(b"chunk") is False
    stats = ww.pipeline_stats
    assert stats["candidates"] == 1
    assert stats["verifier_rejects"] == 1
    assert stats["commits"] == 0
    stage.reset.assert_called_once()


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_process_commits_on_accepted_candidate(mock_stage_cls: MagicMock) -> None:
    stage = MagicMock()
    stage.model_loaded = True
    stage.process.return_value = _candidate(b"pcm-bytes")
    mock_stage_cls.return_value = stage

    ww = WakeWordService(verifiers=[AcceptAllWakeVerifier()])
    assert ww.process(b"chunk") is True
    assert ww.consume_detection_audio() == b"pcm-bytes"
    assert ww.consume_detection_audio() is None
    assert ww.last_decision is not None
    assert ww.last_decision.accept is True


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_process_rejects_when_verifier_rejects(mock_stage_cls: MagicMock) -> None:
    stage = MagicMock()
    stage.model_loaded = True
    stage.process.return_value = _candidate()
    mock_stage_cls.return_value = stage

    ww = WakeWordService(verifiers=[_RejectAllWakeVerifier()])
    assert ww.process(b"chunk") is False
    assert ww.consume_detection_audio() is None
    assert ww.last_decision is not None
    assert ww.last_decision.accept is False


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_process_returns_false_when_no_candidate(mock_stage_cls: MagicMock) -> None:
    stage = MagicMock()
    stage.model_loaded = True
    stage.process.return_value = None
    mock_stage_cls.return_value = stage

    ww = WakeWordService(verifiers=[AcceptAllWakeVerifier()])
    assert ww.process(b"chunk") is False


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_reset_delegates_to_stage(mock_stage_cls: MagicMock) -> None:
    stage = MagicMock()
    stage.model_loaded = True
    mock_stage_cls.return_value = stage

    ww = WakeWordService(verifiers=[AcceptAllWakeVerifier()])
    ww.reset()
    stage.reset.assert_called_once()


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_verifier_chain_stops_on_first_reject(mock_stage_cls: MagicMock) -> None:
    stage = MagicMock()
    stage.model_loaded = True
    stage.process.return_value = _candidate()
    mock_stage_cls.return_value = stage

    first = _FakeVerifier(accept=True, reason="pass")
    second = _FakeVerifier(accept=False, reason="fail")
    ww = WakeWordService(verifiers=[first, second])
    assert ww.process(b"chunk") is False
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_inference_window_constants_match_oww_stage() -> None:
    from core.voice.wakeword.oww_candidate import INFERENCE_WINDOW_BYTES, INFERENCE_WINDOW_SAMPLES

    assert WakeWordService.INFERENCE_WINDOW_BYTES == INFERENCE_WINDOW_BYTES
    assert WakeWordService.INFERENCE_WINDOW_SAMPLES == INFERENCE_WINDOW_SAMPLES


@patch("core.voice.wakeword_service.OWWCandidateStage")
@patch("core.voice.wakeword_service.build_default_wake_verifiers")
def test_reload_verifiers_rebuilds_default_chain(
    mock_build: MagicMock,
    mock_stage_cls: MagicMock,
) -> None:
    mock_stage_cls.return_value.model_loaded = False
    mock_stage_cls.return_value.vad_threshold = 0.5
    mock_stage_cls.return_value._model_name = None
    first = [AcceptAllWakeVerifier()]
    second = [MagicMock(name="speaker")]
    mock_build.side_effect = [first, second]

    service = WakeWordService(owner_id="owner-a")
    service.reload_verifiers()

    assert service._verifiers is second
    assert mock_build.call_count == 2
    mock_build.assert_called_with(
        "owner-a",
        speaker_verifier=service.speaker_verifier,
    )


@patch("core.voice.wakeword_service.OWWCandidateStage")
def test_reload_verifiers_preserves_injected_chain(mock_stage_cls: MagicMock) -> None:
    mock_stage_cls.return_value.model_loaded = False
    mock_stage_cls.return_value.vad_threshold = 0.5
    mock_stage_cls.return_value._model_name = None
    injected = [AcceptAllWakeVerifier()]
    service = WakeWordService(owner_id="owner-a", verifiers=injected)

    with patch("core.voice.wakeword_service.build_default_wake_verifiers") as mock_build:
        service.reload_verifiers()

    mock_build.assert_not_called()
    assert service._verifiers is injected


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "resources/models/wakeword/Jarvis.onnx").exists(),
    reason="no local ONNX model",
)
def test_real_model_process_does_not_crash() -> None:
    ww = WakeWordService(verifiers=[AcceptAllWakeVerifier()])
    if not ww._model_loaded:
        pytest.skip("model not loaded")
    silence = b"\x00\x00" * 2560
    assert ww.process(silence) is False
