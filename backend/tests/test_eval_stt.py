"""Tests for tools/eval_stt.py."""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
TOOLS_DIR = BACKEND_DIR / "tools"


def _write_wav(path: Path, frames: bytes = b"\x00\x00" * 1600) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(frames)


def test_eval_stt_help() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOLS_DIR / "eval_stt.py"), "--help"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--backend" in proc.stdout
    assert "--fixtures" in proc.stdout
    assert "--failures" in proc.stdout


def test_normalize_text() -> None:
    from tools.eval_stt import _normalize_text

    assert _normalize_text("Uh, yeah -- don't stop.") == ["uh", "yeah", "don't", "stop"]


def test_word_error_rate() -> None:
    from tools.eval_stt import _word_error_rate

    assert _word_error_rate(["hello", "world"], ["hello", "world"]) == 0
    assert _word_error_rate(["hello", "world"], ["hello"]) == 0.5
    assert _word_error_rate([], ["hello"]) is None


def test_repeated_sequence_detects_hallucination() -> None:
    from tools.eval_stt import _repeated_sequence

    repeated, count = _repeated_sequence("line line line line jq style json".split())
    assert repeated == "line"
    assert count >= 3


def test_score_text_flags_large_deletion() -> None:
    from tools.eval_stt import _score_text

    metrics = _score_text(
        "please summarize the planning comments and post them in the channel",
        "please summarize",
        audio_ms=3000,
    )
    assert metrics.large_deletion is True
    assert metrics.flagged is True
    assert metrics.length_ratio is not None and metrics.length_ratio < 0.7


def test_score_text_flags_empty_speech() -> None:
    from tools.eval_stt import _score_text

    metrics = _score_text("hello there", "", audio_ms=1000)
    assert metrics.empty_on_speech is True
    assert metrics.flagged is True


def test_score_text_accepts_expected_silence() -> None:
    from tools.eval_stt import _score_text

    metrics = _score_text("", "", audio_ms=1000)
    assert metrics.empty_on_speech is False
    assert metrics.flagged is False


def test_score_text_flags_tail_missing_prefix_only() -> None:
    from tools.eval_stt import _score_text

    metrics = _score_text(
        "the planning docs are ready if I get approval",
        "the planning docs are ready if I get",
        audio_ms=2500,
    )

    assert metrics.tail_missing is True
    assert metrics.prefix_only is True
    assert metrics.flagged is True


def test_score_text_ignores_case_and_punctuation() -> None:
    from tools.eval_stt import _score_text

    metrics = _score_text(
        "jarvis can you check my calendar",
        "Jarvis, can you... check my calendar?",
        audio_ms=1800,
    )
    assert metrics.wer == 0
    assert metrics.length_ratio == 1
    assert metrics.flagged is False


def test_collect_fixtures(tmp_path: Path) -> None:
    from tools.eval_stt import _collect_fixtures

    wav_path = tmp_path / "sample.wav"
    txt_path = tmp_path / "sample.txt"
    _write_wav(wav_path)
    txt_path.write_text("hello world", encoding="utf-8")

    fixtures = _collect_fixtures(tmp_path)

    assert len(fixtures) == 1
    assert fixtures[0].audio_path == wav_path
    assert fixtures[0].reference_path == txt_path
    assert fixtures[0].reference_text == "hello world"
    assert fixtures[0].audio_ms == 100.0


def test_collect_fixtures_missing_dir() -> None:
    from tools.eval_stt import _collect_fixtures

    assert _collect_fixtures(Path("/nonexistent/stt_eval_dir")) == []
