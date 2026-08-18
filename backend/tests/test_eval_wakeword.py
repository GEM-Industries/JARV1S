"""Smoke tests for tools/eval_wakeword.py."""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = BACKEND_DIR / "tools"


def test_eval_wakeword_help() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "eval_wakeword.py"), "--help"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--grid" in proc.stdout
    assert "--failures" in proc.stdout
    assert "--ambient" in proc.stdout
    assert "--ambient-manifest" in proc.stdout
    assert "--max-fa-per-hour" in proc.stdout
    assert "--diagnose-feedback" in proc.stdout
    assert "--speaker-verifier" in proc.stdout
    assert "--speaker-profile" in proc.stdout
    assert "--speaker-only" in proc.stdout
    assert "--tts-echo" in proc.stdout
    assert "--speaker-enrollment-manifest" in proc.stdout
    assert "--speaker-threshold-grid" in proc.stdout
    assert "--ambient-grid" in proc.stdout
    assert "--ambient-max-hours" in proc.stdout
    assert "--ambient-source-regex" in proc.stdout
    assert "--sensitivity-grid" in proc.stdout


def test_collect_wavs_missing_dir() -> None:
    from tools.eval_wakeword import _collect_wavs

    assert _collect_wavs(None) == []
    assert _collect_wavs(Path("/nonexistent/wakeword_eval_dir")) == []


def test_default_config_matches_settings() -> None:
    from core.config import settings
    from tools.eval_wakeword import _default_config

    cfg = _default_config()
    assert cfg.sensitivity == settings.VOICE.wakeword_sensitivity
    assert cfg.patience == settings.VOICE.wakeword_patience
    assert cfg.vad == settings.VOICE.wakeword_vad_threshold


def test_pcm_duration_seconds() -> None:
    from tools.eval_wakeword import BYTES_PER_SECOND, pcm_duration_seconds

    assert pcm_duration_seconds(b"\x00" * BYTES_PER_SECOND) == pytest.approx(1.0)
    assert pcm_duration_seconds(b"") == 0.0


def test_false_accepts_per_hour() -> None:
    from tools.eval_wakeword import false_accepts_per_hour

    assert false_accepts_per_hour(0, 3600.0) == 0.0
    assert false_accepts_per_hour(2, 3600.0) == pytest.approx(2.0)
    assert false_accepts_per_hour(1, 1800.0) == pytest.approx(2.0)
    assert false_accepts_per_hour(5, 0.0) == 0.0


def test_format_timestamp() -> None:
    from tools.eval_wakeword import format_timestamp

    assert format_timestamp(0.0) == "00:00.000"
    assert format_timestamp(65.5) == "01:05.500"


def _write_silent_wav(path: Path, *, duration_s: float = 0.1) -> None:
    nframes = int(16000 * duration_s)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * nframes)


def test_collect_ambient_paths_file_and_dir(tmp_path: Path) -> None:
    from tools.eval_wakeword import collect_ambient_paths

    wav = tmp_path / "room.wav"
    _write_silent_wav(wav)
    assert collect_ambient_paths(wav) == [wav]

    sub = tmp_path / "eval"
    sub.mkdir()
    a = sub / "a.wav"
    b = sub / "b.wav"
    _write_silent_wav(a)
    _write_silent_wav(b)
    assert collect_ambient_paths(sub) == [a, b]


def test_collect_ambient_paths_errors(tmp_path: Path) -> None:
    from tools.eval_wakeword import collect_ambient_paths

    with pytest.raises(FileNotFoundError):
        collect_ambient_paths(tmp_path / "missing.wav")

    not_wav = tmp_path / "notes.txt"
    not_wav.write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="\\.wav"):
        collect_ambient_paths(not_wav)

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(ValueError, match="No .wav"):
        collect_ambient_paths(empty_dir)


def test_load_ambient_manifest_relative_paths(tmp_path: Path) -> None:
    from tools.eval_wakeword import load_ambient_manifest

    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()
    data_dir = tmp_path / "data" / "public_eval" / "dipco" / "conversation"
    data_dir.mkdir(parents=True)
    wav = data_dir / "session.wav"
    _write_silent_wav(wav)

    manifest = manifest_dir / "public_fa_eval.jsonl"
    manifest.write_text(
        (
            '{"path":"../data/public_eval/dipco/conversation/session.wav",'
            '"source":"dipco","category":"conversation_farfield","contains_wakeword":false}\n'
        ),
        encoding="utf-8",
    )

    clips = load_ambient_manifest(manifest)
    assert clips[0].path == wav
    assert clips[0].source == "dipco/conversation_farfield/session.wav"


def test_load_ambient_manifest_rejects_wakeword_positive(tmp_path: Path) -> None:
    from tools.eval_wakeword import load_ambient_manifest

    wav = tmp_path / "wake.wav"
    _write_silent_wav(wav)
    manifest = tmp_path / "public_fa_eval.jsonl"
    manifest.write_text(
        '{"path":"wake.wav","source":"test","contains_wakeword":true}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="wakeword-positive"):
        load_ambient_manifest(manifest)


class _FakeDetector:
    def __init__(self, fire_at_chunks: set[int]) -> None:
        self._fire_at_chunks = fire_at_chunks
        self._chunk_idx = 0
        self.reset_count = 0

    def process(self, chunk: bytes) -> bool:
        fire = self._chunk_idx in self._fire_at_chunks
        self._chunk_idx += 1
        return fire

    def reset(self) -> None:
        self.reset_count += 1
        self._chunk_idx = 0


def test_stream_false_accepts_resets_after_fire() -> None:
    from tools.eval_wakeword import CHUNK_BYTES, stream_false_accepts

    pcm = b"\x00" * (CHUNK_BYTES * 5)
    detector = _FakeDetector(fire_at_chunks={1, 3})
    fires = stream_false_accepts(detector, pcm, source="test.wav")
    assert len(fires) == 2
    assert fires[0].timestamp_s == pytest.approx(CHUNK_BYTES / (16000 * 2))
    assert fires[0].source == "test.wav"
    assert detector.reset_count == 2


def test_ambient_requires_path() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "eval_wakeword.py"), "--ambient", "/nonexistent/ambient.wav"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "not found" in proc.stderr.lower() or "not found" in proc.stdout.lower()


def test_max_fa_per_hour_requires_ambient() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "eval_wakeword.py"), "--max-fa-per-hour", "1.0"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "--max-fa-per-hour requires --ambient or --ambient-manifest" in proc.stderr


def test_ambient_and_manifest_are_mutually_exclusive(tmp_path: Path) -> None:
    wav = tmp_path / "room.wav"
    _write_silent_wav(wav)
    manifest = tmp_path / "public_fa_eval.jsonl"
    manifest.write_text('{"path":"room.wav"}\n', encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(TOOLS_DIR / "eval_wakeword.py"),
            "--ambient",
            str(wav),
            "--ambient-manifest",
            str(manifest),
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "cannot be combined" in proc.stderr


def test_diagnose_feedback_duration_stats(tmp_path: Path) -> None:
    from tools.eval_wakeword import _duration_stats

    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    _write_silent_wav(a, duration_s=1.0)
    _write_silent_wav(b, duration_s=2.0)
    stats = _duration_stats([a, b])
    assert stats["count"] == 2
    assert stats["min_s"] == pytest.approx(1.0)
    assert stats["max_s"] == pytest.approx(2.0)


def test_stream_false_accepts_with_attribution_counts_rejects() -> None:
    from core.voice.wakeword.types import WakeCandidate, WakeDecision
    from tools.eval_wakeword import CHUNK_BYTES, stream_false_accepts_with_attribution

    class _RejectingWake:
        def __init__(self) -> None:
            self._calls = 0
            self._stats = {"candidates": 0, "verifier_rejects": 0, "commits": 0}

        def process(self, chunk: bytes) -> bool:
            self._calls += 1
            self._last_had_candidate = self._calls == 1
            if self._calls == 1:
                self._stats["candidates"] += 1
                self._last_decision = WakeDecision(
                    accept=False,
                    reason="speaker_mismatch",
                    scores={"speaker_cosine": 0.1},
                )
                self._stats["verifier_rejects"] += 1
                return False
            return False

        def reset(self) -> None:
            self._calls = 0

        @property
        def last_had_candidate(self) -> bool:
            return self._last_had_candidate

        @property
        def last_decision(self):
            return self._last_decision

        @property
        def pipeline_stats(self) -> dict[str, int]:
            return dict(self._stats)

    pcm = b"\x00" * (CHUNK_BYTES * 2)
    ww = _RejectingWake()
    fires, reasons = stream_false_accepts_with_attribution(ww, pcm)  # type: ignore[arg-type]
    assert fires == []
    assert reasons == {"speaker_mismatch": 1}


def test_speaker_config_from_args_defaults() -> None:
    import argparse

    from tools.eval_wakeword import DEFAULT_SPEAKER_ENROLLMENT, _speaker_config_from_args

    args = argparse.Namespace(
        speaker_verifier=True,
        speaker_model="resources/models/speaker/model.onnx",
        speaker_enrollment_manifest=DEFAULT_SPEAKER_ENROLLMENT,
        speaker_threshold=0.55,
        speaker_id="enrolled_user",
    )
    cfg = _speaker_config_from_args(args)
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.threshold == 0.55


def test_speaker_only_requires_owner_clips(tmp_path: Path, capsys) -> None:
    import argparse

    from tools.eval_wakeword import _run_speaker_only

    tts_echo = tmp_path / "tts"
    tts_echo.mkdir()
    _write_silent_wav(tts_echo / "echo.wav")
    args = argparse.Namespace(
        positives=tmp_path / "missing-owner",
        negatives=tmp_path / "missing-other",
        tts_echo=tts_echo,
    )

    assert _run_speaker_only(args) == 1
    assert "Owner clips are required" in capsys.readouterr().err


def test_speaker_only_requires_tts_echo_clips(tmp_path: Path, capsys) -> None:
    import argparse

    from tools.eval_wakeword import _run_speaker_only

    owner = tmp_path / "owner"
    owner.mkdir()
    _write_silent_wav(owner / "owner.wav")
    args = argparse.Namespace(
        positives=owner,
        negatives=tmp_path / "missing-other",
        tts_echo=None,
    )

    assert _run_speaker_only(args) == 1
    assert "Jarvis TTS echo clips are required" in capsys.readouterr().err


def test_parse_threshold_grid_defaults_and_csv() -> None:
    from tools.eval_wakeword import SPEAKER_THRESHOLD_GRID, _parse_threshold_grid

    assert _parse_threshold_grid("") == list(SPEAKER_THRESHOLD_GRID)
    assert _parse_threshold_grid("0.45, 0.6") == [0.45, 0.6]


def test_parse_threshold_grid_rejects_out_of_range() -> None:
    from tools.eval_wakeword import _parse_threshold_grid

    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        _parse_threshold_grid("1.2")


def test_parse_stage1_grids() -> None:
    from tools.eval_wakeword import _parse_float_grid, _parse_int_grid

    assert _parse_float_grid("0.9, 0.93") == [0.9, 0.93]
    assert _parse_int_grid("3, 4") == [3, 4]
    with pytest.raises(ValueError, match="empty"):
        _parse_float_grid("")


def test_ambient_result_from_candidates_applies_speaker_threshold() -> None:
    from tools.eval_wakeword import (
        AmbientCandidate,
        WakewordConfig,
        _ambient_result_from_candidates,
    )

    candidates = [
        AmbientCandidate(timestamp_s=1.0, source="librispeech/a.wav", speaker_score=0.55),
        AmbientCandidate(timestamp_s=2.0, source="speech_commands/b.wav", speaker_score=0.75),
    ]
    result = _ambient_result_from_candidates(
        WakewordConfig(sensitivity=0.93, patience=4, vad=0.4),
        duration_s=3600.0,
        vad_effective=0.4,
        candidates=candidates,
        speaker_threshold=0.7,
    )

    assert result.false_accepts == 1
    assert result.false_accepts_per_hour == pytest.approx(1.0)
    assert result.attribution.stage1_candidates == 2
    assert result.attribution.verifier_rejects == 1
    assert result.fires[0].source == "speech_commands/b.wav"


def test_filter_ambient_clips_by_source() -> None:
    from tools.eval_wakeword import AmbientClip, _filter_ambient_clips_by_source

    clips = [
        AmbientClip(path=Path("a.wav"), source="librispeech/read/a.wav"),
        AmbientClip(path=Path("b.wav"), source="demand/noise/b.wav"),
    ]

    assert _filter_ambient_clips_by_source(clips, source_regex="libri") == [clips[0]]
    with pytest.raises(ValueError, match="matched no clips"):
        _filter_ambient_clips_by_source(clips, source_regex="dipco")


def test_clip_fires_with_preroll_uses_preroll_before_clip() -> None:
    from tools.eval_wakeword import CHUNK_BYTES, _clip_fires_with_preroll

    class _SeqDetector:
        def __init__(self) -> None:
            self.reset_count = 0
            self.process_count = 0

        def reset(self) -> None:
            self.reset_count += 1

        def process(self, chunk: bytes) -> bool:
            self.process_count += 1
            return self.process_count == 5

    det = _SeqDetector()
    pcm = b"\x00" * CHUNK_BYTES
    assert _clip_fires_with_preroll(det, pcm, preroll_s=0.5) is True  # type: ignore[arg-type]


@pytest.mark.skipif(
    not (BACKEND_DIR.parent / "training/wakeword/data/positives_real").is_dir(),
    reason="no local positive clips",
)
def test_eval_wakeword_runs_on_real_clips() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "eval_wakeword.py")],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}"
    assert "recall" in proc.stdout.lower()
