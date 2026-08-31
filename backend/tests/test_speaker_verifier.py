"""Tests for enrolled-speaker verification and the wake Stage 2b adapter."""

from __future__ import annotations

import json
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.voice.speaker_verifier import (
    EnrolledSpeakerVerifier,
    SpeakerMatchStatus,
    cosine_similarity,
    load_enrollment_paths,
    load_speaker_profile,
    load_speaker_profile_parts,
    max_cosine_score,
    mean_cosine_score,
    pcm16_bytes_to_float32,
    pcm_onset_window,
    save_speaker_profile,
    speaker_model_id,
)
from core.voice.wakeword.speaker_verifier import SpeakerEmbeddingWakeVerifier
from core.voice.wakeword.types import WakeCandidate


def test_pcm16_bytes_to_float32() -> None:
    pcm = (np.array([0, 16384, -16384], dtype=np.int16)).tobytes()
    samples = pcm16_bytes_to_float32(pcm)
    assert samples.dtype == np.float32
    assert samples[1] == pytest.approx(0.5)
    assert samples[2] == pytest.approx(-0.5)


def test_cosine_similarity_identical() -> None:
    a = np.array([1.0, 0.0], dtype=np.float32)
    assert cosine_similarity(a, a) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal() -> None:
    a = np.array([1.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0], dtype=np.float32)
    assert cosine_similarity(a, b) == pytest.approx(0.0)


def test_mean_cosine_score_averages_gallery_rows() -> None:
    query = np.array([1.0, 0.0], dtype=np.float32)
    gallery = np.array(
        [
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        dtype=np.float32,
    )
    assert mean_cosine_score(query, gallery) == pytest.approx(0.5)


def test_max_cosine_score_picks_best_row() -> None:
    query = np.array([1.0, 0.0], dtype=np.float32)
    gallery = np.array(
        [
            [1.0, 0.0],
            [0.0, 1.0],
        ],
        dtype=np.float32,
    )
    assert max_cosine_score(query, gallery) == pytest.approx(1.0)


def test_pcm_onset_window_truncates_to_max_seconds() -> None:
    pcm = b"\x00\x01" * 16000
    window = pcm_onset_window(pcm, max_seconds=0.8)
    assert len(window) == int(0.8 * 16000) * 2
    assert pcm_onset_window(pcm, max_seconds=0.0) == pcm
    assert pcm_onset_window(b"", max_seconds=0.8) == b""


def test_load_speaker_profile_accepts_model_bound_gallery(tmp_path: Path) -> None:
    path = tmp_path / "gallery.npz"
    save_speaker_profile(
        path,
        model_id="test-model",
        embeddings=np.array(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
    )
    gallery = load_speaker_profile(path, model_id="test-model", embedding_dim=4)
    assert gallery.shape == (2, 4)
    assert np.linalg.norm(gallery[0]) == pytest.approx(1.0)
    assert np.linalg.norm(gallery[1]) == pytest.approx(1.0)


def test_load_speaker_profile_rejects_other_model(tmp_path: Path) -> None:
    path = tmp_path / "gallery.npz"
    save_speaker_profile(
        path,
        model_id="old-model",
        embeddings=np.ones((2, 4), dtype=np.float32),
    )
    with pytest.raises(ValueError, match="model mismatch"):
        load_speaker_profile(path, model_id="new-model")


def _write_wav(path: Path, *, duration_s: float = 0.2) -> None:
    nframes = int(16000 * duration_s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x01" * nframes)


def test_load_enrollment_paths_filters_split(tmp_path: Path) -> None:
    enroll = tmp_path / "enroll.wav"
    dev = tmp_path / "dev.wav"
    _write_wav(enroll)
    _write_wav(dev)
    manifest = tmp_path / "speaker_enrollment.jsonl"
    manifest.write_text(
        "\n".join(
            [
                json.dumps({"path": "enroll.wav", "split": "enroll"}),
                json.dumps({"path": "dev.wav", "split": "dev"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    paths = load_enrollment_paths(manifest, split="enroll")
    assert paths == [enroll.resolve()]


def _profile_and_model(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "speaker.onnx"
    model.write_bytes(b"fake")
    profile = tmp_path / "profile.npz"
    save_speaker_profile(
        profile,
        model_id=speaker_model_id(model),
        embeddings=np.array(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.95, 0.05, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
    )
    return profile, model


@patch("core.voice.speaker_verifier.load_speaker_extractor")
@patch("core.voice.speaker_verifier.embed_waveform")
def test_enrolled_verifier_matches_above_threshold(
    mock_embed_waveform,
    mock_load_extractor,
    tmp_path: Path,
) -> None:
    mock_load_extractor.return_value = MagicMock()
    mock_embed_waveform.return_value = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    profile, model = _profile_and_model(tmp_path)

    verifier = EnrolledSpeakerVerifier(
        owner_id="user_a",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    evidence = verifier.verify_pcm(b"\x00\x01" * 8000, threshold=0.5)

    assert evidence.status is SpeakerMatchStatus.MATCHED
    assert evidence.speaker_id == "user_a"
    assert evidence.cosine is not None
    assert evidence.cosine > 0.9


@patch("core.voice.speaker_verifier.load_speaker_extractor")
@patch("core.voice.speaker_verifier.embed_waveform")
def test_enrolled_verifier_mismatches_below_threshold(
    mock_embed_waveform,
    mock_load_extractor,
    tmp_path: Path,
) -> None:
    mock_load_extractor.return_value = MagicMock()
    mock_embed_waveform.return_value = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    profile, model = _profile_and_model(tmp_path)

    verifier = EnrolledSpeakerVerifier(
        owner_id="user_a",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    evidence = verifier.verify_pcm(b"\x00\x01" * 8000, threshold=0.9)

    assert evidence.status is SpeakerMatchStatus.MISMATCH
    assert evidence.cosine == pytest.approx(0.0)


@patch("core.voice.speaker_verifier.load_speaker_extractor")
def test_enrolled_verifier_rejects_empty_audio(mock_load_extractor, tmp_path: Path) -> None:
    mock_load_extractor.return_value = MagicMock()
    profile, model = _profile_and_model(tmp_path)

    verifier = EnrolledSpeakerVerifier(
        owner_id="user_a",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    evidence = verifier.verify_pcm(b"", threshold=0.5)

    assert evidence.status is SpeakerMatchStatus.UNAVAILABLE
    assert evidence.cosine is None


@patch("core.voice.speaker_verifier.load_speaker_extractor")
def test_enrolled_verifier_too_short_is_unavailable(mock_load_extractor, tmp_path: Path) -> None:
    mock_load_extractor.return_value = MagicMock()
    profile, model = _profile_and_model(tmp_path)

    verifier = EnrolledSpeakerVerifier(
        owner_id="user_a",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    evidence = verifier.verify_pcm(b"\x00\x01" * 1600, threshold=0.5)

    assert evidence.status is SpeakerMatchStatus.UNAVAILABLE
    assert evidence.cosine is None


def test_enrolled_verifier_not_enrolled_without_profile(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "core.voice.speaker_verifier.settings.VOICE.wakeword_speaker_verifier_enabled",
        True,
    )
    monkeypatch.setattr(
        "core.voice.speaker_verifier.settings.VOICE.wakeword_speaker_model_path",
        str(tmp_path / "missing.onnx"),
    )
    monkeypatch.setattr(
        "core.voice.speaker_profile.resolve_owner_profile_path",
        lambda _owner_id: None,
    )

    verifier = EnrolledSpeakerVerifier(owner_id="owner-a", enabled=True)
    assert verifier.enrolled is False
    evidence = verifier.verify_pcm(b"\x00\x01" * 8000, threshold=0.6)
    assert evidence.status is SpeakerMatchStatus.NOT_ENROLLED


@patch("core.voice.speaker_verifier.load_speaker_extractor")
@patch("core.voice.speaker_verifier.embed_waveform")
def test_reload_profile_swaps_gallery_without_reloading_extractor(
    mock_embed_waveform,
    mock_load_extractor,
    monkeypatch,
    tmp_path: Path,
) -> None:
    model = tmp_path / "speaker.onnx"
    model.write_bytes(b"fake")
    first = tmp_path / "first.npz"
    second = tmp_path / "second.npz"
    save_speaker_profile(
        first,
        model_id=speaker_model_id(model),
        embeddings=np.array([[1.0, 0.0]], dtype=np.float32),
    )
    save_speaker_profile(
        second,
        model_id=speaker_model_id(model),
        embeddings=np.array([[0.0, 1.0]], dtype=np.float32),
    )
    active_profile = {"path": first}
    monkeypatch.setattr(
        "core.voice.speaker_profile.resolve_owner_profile_path",
        lambda _owner_id: active_profile["path"],
    )
    mock_load_extractor.return_value = MagicMock()
    mock_embed_waveform.return_value = np.array([0.0, 1.0], dtype=np.float32)

    verifier = EnrolledSpeakerVerifier(
        owner_id="owner-a",
        model_path=model,
        enabled=True,
    )
    assert verifier.verify_pcm(b"\x00\x01" * 8000, threshold=0.5).status is SpeakerMatchStatus.MISMATCH

    active_profile["path"] = second
    assert verifier.reload_profile() is True

    assert verifier.verify_pcm(b"\x00\x01" * 8000, threshold=0.5).status is SpeakerMatchStatus.MATCHED
    mock_load_extractor.assert_called_once()


@patch("core.voice.speaker_verifier.load_speaker_extractor")
@patch("core.voice.speaker_verifier.embed_waveform")
def test_wake_adapter_maps_match(
    mock_embed_waveform,
    mock_load_extractor,
    tmp_path: Path,
) -> None:
    mock_load_extractor.return_value = MagicMock()
    mock_embed_waveform.return_value = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    profile, model = _profile_and_model(tmp_path)
    shared = EnrolledSpeakerVerifier(
        owner_id="user_a",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    adapter = SpeakerEmbeddingWakeVerifier(
        speaker_id="user_a",
        threshold=0.5,
        verifier=shared,
    )
    decision = adapter.verify(WakeCandidate(audio=b"\x00\x01" * 8000, score=0.99))

    assert decision.accept is True
    assert decision.reason == "speaker_verified"
    assert decision.scores["speaker_cosine"] > 0.9


@patch("core.voice.speaker_verifier.load_speaker_extractor")
@patch("core.voice.speaker_verifier.embed_waveform")
def test_verify_pcm_scores_onset_window_not_full_clip(
    mock_embed_waveform,
    mock_load_extractor,
    tmp_path: Path,
) -> None:
    mock_load_extractor.return_value = MagicMock()
    mock_embed_waveform.return_value = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    profile, model = _profile_and_model(tmp_path)
    verifier = EnrolledSpeakerVerifier(
        owner_id="user_a",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    pcm = b"\x00\x01" * 24000
    verifier.verify_pcm(pcm, threshold=0.5, max_seconds=0.8)
    waveform = mock_embed_waveform.call_args.args[1]
    assert waveform.size == int(0.8 * 16000)


@patch("core.voice.speaker_verifier.load_speaker_extractor")
@patch("core.voice.speaker_verifier.embed_waveform")
def test_node_clip_beats_enrollment_only(
    mock_embed_waveform,
    mock_load_extractor,
    tmp_path: Path,
) -> None:
    mock_load_extractor.return_value = MagicMock()
    mock_embed_waveform.return_value = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    model = tmp_path / "speaker.onnx"
    model.write_bytes(b"fake")
    profile = tmp_path / "profile.npz"
    save_speaker_profile(
        profile,
        model_id=speaker_model_id(model),
        embeddings=np.array(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.95, 0.05, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
        node_embeddings={
            "sat-1": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        },
    )
    pcm = b"\x00\x01" * 8000
    enrollment_only = EnrolledSpeakerVerifier(
        owner_id="user_a",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    with_node = EnrolledSpeakerVerifier(
        owner_id="user_a",
        node_id="sat-1",
        model_path=model,
        profile_path=profile,
        enabled=True,
    )
    assert enrollment_only.verify_pcm(pcm, threshold=0.5).status is SpeakerMatchStatus.MISMATCH
    evidence = with_node.verify_pcm(pcm, threshold=0.5)
    assert evidence.status is SpeakerMatchStatus.MATCHED
    assert evidence.cosine == pytest.approx(1.0)


def test_v1_profile_loads_without_node_embeddings(tmp_path: Path) -> None:
    path = tmp_path / "gallery.npz"
    save_speaker_profile(
        path,
        model_id="test-model",
        embeddings=np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32),
    )
    with np.load(path, allow_pickle=False) as payload:
        assert int(payload["format_version"].item()) == 1
        assert "node_ids" not in payload.files
    enrollment, nodes = load_speaker_profile_parts(path, model_id="test-model", embedding_dim=4)
    assert enrollment.shape == (1, 4)
    assert nodes == {}
