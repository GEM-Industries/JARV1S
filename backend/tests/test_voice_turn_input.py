"""Tests for pure voice-input helpers extracted from the WebSocket handler layer."""

from __future__ import annotations

from core.voice import turn_input


def test_merge_continuation_text_normalizes_boundary_punctuation() -> None:
    assert turn_input.merge_continuation_text("Can we do it tomorrow?", "tomorrow morning") == (
        "Can we do it tomorrow? morning"
    )


def test_merge_continuation_text_handles_overlap() -> None:
    assert turn_input.merge_continuation_text("hey jarvis", "jarvis check the lights") == (
        "hey jarvis check the lights"
    )


def test_merge_continuation_text_returns_update_when_base_empty() -> None:
    assert turn_input.merge_continuation_text("", "hello world") == "hello world"


def test_merge_continuation_text_returns_base_when_update_is_prefix() -> None:
    assert turn_input.merge_continuation_text("hey jarvis", "hey jarvis") == "hey jarvis"


def test_overlap_words_strips_punctuation() -> None:
    assert turn_input.overlap_words("Hey, Jarvis's lights!") == ["hey", "jarvis's", "lights"]


def test_text_tail_collapses_whitespace_and_truncates() -> None:
    assert turn_input.text_tail("a b c", limit=3) == "b c"
    assert turn_input.text_tail("hello   world", limit=100) == "hello world"


def test_pcm_duration_ms_reads_int_or_bytes() -> None:
    # 16kHz mono 16-bit: 32000 bytes/s -> 1s = 1000ms
    assert turn_input.pcm_duration_ms(32000) == 1000.0
    assert turn_input.pcm_duration_ms(b"x" * 16000) == 500.0


def test_stt_coverage_fields_reports_gap_and_pct() -> None:
    fields = turn_input.stt_coverage_fields(32000, 16000)
    assert fields["turn_audio_ms"] == 1000.0
    assert fields["stt_bytes_fed_ms"] == 500.0
    assert fields["stt_audio_gap_ms"] == 500.0
    assert fields["stt_coverage_pct"] == 50.0


def test_stt_coverage_fields_handles_empty_turn_audio() -> None:
    fields = turn_input.stt_coverage_fields(0, 0)
    assert fields["stt_coverage_pct"] is None
