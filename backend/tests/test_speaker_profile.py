"""Tests for owner speaker-profile helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from core.voice import speaker_profile as profile
from core.voice.speaker_profile import (
    REQUIRED_CLIP_COUNT,
    SpeakerProfileError,
    append_node_clip,
    delete_profile,
    get_profile_status,
    profile_path,
    validate_clips,
    write_profile,
)
from core.voice.speaker_verifier import (
    l2_normalize,
    load_speaker_profile_parts,
    mean_centroid,
    save_speaker_profile,
    speaker_model_id,
)


def _pcm(duration_s: float = 1.0, amplitude: float = 0.2) -> bytes:
    samples = int(16000 * duration_s)
    t = np.linspace(0, duration_s, samples, endpoint=False, dtype=np.float32)
    wave = (np.sin(2 * np.pi * 220 * t) * amplitude).astype(np.float32)
    return (wave * 32767.0).astype(np.int16).tobytes()


def test_mean_centroid_normalizes() -> None:
    a = np.array([3.0, 0.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 4.0, 0.0], dtype=np.float32)
    centroid = mean_centroid([a, b])
    assert centroid.dtype == np.float32
    assert np.linalg.norm(centroid) == pytest.approx(1.0)


def test_validate_clips_requires_five() -> None:
    with pytest.raises(SpeakerProfileError) as exc:
        validate_clips([_pcm()] * 4)
    assert exc.value.reason == "processing_failed"


def test_validate_clips_too_quiet() -> None:
    quiet = _pcm(amplitude=0.0001)
    with pytest.raises(SpeakerProfileError) as exc:
        validate_clips([quiet] * REQUIRED_CLIP_COUNT)
    assert exc.value.reason == "too_quiet"
    assert exc.value.clip_index == 1


def test_profile_path_is_hashed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    first = profile_path("owner-a")
    second = profile_path("owner-b")
    assert first.parent == tmp_path / "voice" / "speaker-profiles"
    assert first != second
    assert first.suffix == ".npz"


def test_write_and_delete_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(profile.settings.VOICE, "wakeword_speaker_model_path", str(tmp_path / "model.onnx"))
    (tmp_path / "model.onnx").write_bytes(b"fake")

    clips = [_pcm() for _ in range(REQUIRED_CLIP_COUNT)]
    embeddings = [
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([0.95, 0.05, 0.0, 0.0], dtype=np.float32),
        np.array([0.9, 0.1, 0.0, 0.0], dtype=np.float32),
        np.array([0.92, 0.08, 0.0, 0.0], dtype=np.float32),
        np.array([0.93, 0.07, 0.0, 0.0], dtype=np.float32),
    ]

    with (
        patch.object(profile, "load_speaker_extractor", return_value=object()),
        patch.object(profile, "embed_pcm16", side_effect=embeddings),
    ):
        status = write_profile("owner-a", clips)

    assert status.status == "enrolled"
    path = profile_path("owner-a")
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600
    with np.load(path, allow_pickle=False) as payload:
        assert payload["model_id"].item() == speaker_model_id(tmp_path / "model.onnx")
        loaded = payload["embeddings"]
    assert loaded.shape == (REQUIRED_CLIP_COUNT, 4)
    for row in loaded:
        assert np.linalg.norm(row) == pytest.approx(1.0)

    deleted = delete_profile("owner-a")
    assert deleted.status == "not_enrolled"
    assert get_profile_status("owner-a").status == "not_enrolled"
    assert not path.exists()


def test_legacy_npy_profile_is_not_enrolled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    destination = profile_path("owner-a").with_suffix(".npy")
    destination.parent.mkdir(parents=True)
    np.save(destination, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))

    status = get_profile_status("owner-a")
    assert status.status == "not_enrolled"


def test_corrupt_profile_is_not_enrolled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    destination = profile_path("owner-a")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"not-a-numpy-profile")

    status = get_profile_status("owner-a")
    assert status.status == "not_enrolled"


def test_wrong_model_profile_is_not_enrolled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    model = tmp_path / "model.onnx"
    model.write_bytes(b"new")
    monkeypatch.setattr(profile.settings.VOICE, "wakeword_speaker_model_path", str(model))
    destination = profile_path("owner-a")
    destination.parent.mkdir(parents=True)
    save_speaker_profile(
        destination,
        model_id="old-model:sha256:deadbeef",
        embeddings=np.ones((REQUIRED_CLIP_COUNT, 4), dtype=np.float32),
    )

    status = get_profile_status("owner-a")
    assert status.status == "not_enrolled"


def test_write_profile_rejects_inconsistent_samples(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(profile.settings.VOICE, "wakeword_speaker_model_path", str(tmp_path / "model.onnx"))
    (tmp_path / "model.onnx").write_bytes(b"fake")

    clips = [_pcm() for _ in range(REQUIRED_CLIP_COUNT)]
    embeddings = [
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
    ]

    with (
        patch.object(profile, "load_speaker_extractor", return_value=object()),
        patch.object(profile, "embed_pcm16", side_effect=embeddings),
        pytest.raises(SpeakerProfileError) as exc,
    ):
        write_profile("owner-a", clips)

    assert exc.value.reason == "inconsistent_samples"
    assert exc.value.clip_index == 5
    assert get_profile_status("owner-a").status == "not_enrolled"


def test_failed_replacement_preserves_existing_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(profile.settings.VOICE, "wakeword_speaker_model_path", str(tmp_path / "model.onnx"))
    (tmp_path / "model.onnx").write_bytes(b"fake")
    existing = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
    destination = profile_path("owner-a")
    destination.parent.mkdir(parents=True)
    save_speaker_profile(
        destination,
        model_id=speaker_model_id(tmp_path / "model.onnx"),
        embeddings=existing,
    )
    before = destination.read_bytes()

    embeddings = [
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
    ]
    with (
        patch.object(profile, "load_speaker_extractor", return_value=object()),
        patch.object(profile, "embed_pcm16", side_effect=embeddings),
        pytest.raises(SpeakerProfileError),
    ):
        write_profile("owner-a", [_pcm()] * REQUIRED_CLIP_COUNT)

    assert destination.read_bytes() == before


def test_l2_normalize_rejects_zero() -> None:
    with pytest.raises(ValueError):
        l2_normalize(np.zeros(4, dtype=np.float32))


def _enrolled_gallery() -> np.ndarray:
    return np.array(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.95, 0.05, 0.0, 0.0],
            [0.9, 0.1, 0.0, 0.0],
            [0.92, 0.08, 0.0, 0.0],
            [0.93, 0.07, 0.0, 0.0],
        ],
        dtype=np.float32,
    )


def test_v1_five_clip_profile_is_enrolled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    model = tmp_path / "model.onnx"
    model.write_bytes(b"fake")
    monkeypatch.setattr(profile.settings.VOICE, "wakeword_speaker_model_path", str(model))
    destination = profile_path("owner-a")
    destination.parent.mkdir(parents=True)
    save_speaker_profile(
        destination,
        model_id=speaker_model_id(model),
        embeddings=_enrolled_gallery(),
    )
    status = get_profile_status("owner-a")
    assert status.status == "enrolled"
    assert status.node_ids == ()


def test_append_node_clip_keeps_enrollment_rows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    model = tmp_path / "model.onnx"
    model.write_bytes(b"fake")
    monkeypatch.setattr(profile.settings.VOICE, "wakeword_speaker_model_path", str(model))
    destination = profile_path("owner-a")
    destination.parent.mkdir(parents=True)
    save_speaker_profile(
        destination,
        model_id=speaker_model_id(model),
        embeddings=_enrolled_gallery(),
    )
    node_embedding = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    with (
        patch.object(profile, "load_speaker_extractor", return_value=object()),
        patch.object(profile, "embed_pcm16", return_value=node_embedding),
    ):
        status = append_node_clip("owner-a", "sat-1", _pcm(duration_s=2.2))

    assert status.status == "enrolled"
    assert status.node_ids == ("sat-1",)
    with np.load(destination, allow_pickle=False) as payload:
        assert int(payload["format_version"].item()) == 2
        assert payload["embeddings"].shape[0] == REQUIRED_CLIP_COUNT
        assert list(payload["node_ids"]) == ["sat-1"]
        assert payload["node_embeddings"].shape[0] == 1

    other_embedding = np.array([0.1, 0.9, 0.0, 0.0], dtype=np.float32)
    with (
        patch.object(profile, "load_speaker_extractor", return_value=object()),
        patch.object(profile, "embed_pcm16", return_value=other_embedding),
    ):
        append_node_clip("owner-a", "sat-1", _pcm(duration_s=2.2))
    with np.load(destination, allow_pickle=False) as payload:
        assert payload["node_embeddings"].shape[0] == 1


def test_write_profile_preserves_node_embeddings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    model = tmp_path / "model.onnx"
    model.write_bytes(b"fake")
    monkeypatch.setattr(profile.settings.VOICE, "wakeword_speaker_model_path", str(model))
    destination = profile_path("owner-a")
    destination.parent.mkdir(parents=True)
    save_speaker_profile(
        destination,
        model_id=speaker_model_id(model),
        embeddings=_enrolled_gallery(),
        node_embeddings={"sat-1": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)},
    )
    clips = [_pcm() for _ in range(REQUIRED_CLIP_COUNT)]
    with (
        patch.object(profile, "load_speaker_extractor", return_value=object()),
        patch.object(profile, "embed_pcm16", side_effect=list(_enrolled_gallery())),
    ):
        status = write_profile("owner-a", clips)

    assert status.node_ids == ("sat-1",)
    enrollment, nodes = load_speaker_profile_parts(
        destination,
        model_id=speaker_model_id(model),
        embedding_dim=4,
    )
    assert enrollment.shape[0] == REQUIRED_CLIP_COUNT
    assert "sat-1" in nodes
    assert nodes["sat-1"].ndim == 1


def test_append_node_clip_requires_enrollment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profile.settings, "DATA_DIR", tmp_path)
    with pytest.raises(SpeakerProfileError) as exc:
        append_node_clip("owner-a", "sat-1", _pcm(duration_s=2.2))
    assert exc.value.reason == "not_enrolled"
