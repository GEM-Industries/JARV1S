from __future__ import annotations

from pathlib import Path

from tools.latency_probe import (
    ProbeResult,
    apply_speech_relative_metrics,
    estimate_speech_end_ms,
    extract_turn_run_summary,
    finalize_result_contract,
    iter_audio_chunks,
    load_reference,
    resolve_suite_cases,
    summarize_results,
)


def _pcm_ms(duration_ms: int, *, amplitude: int = 0) -> bytes:
    sample = amplitude.to_bytes(2, byteorder="little", signed=True)
    return sample * (16 * duration_ms)


def test_extract_turn_run_summary_normal_turn_shape() -> None:
    document = {
        "turn_id": "turn-1",
        "status": "completed",
        "stages": [
            {"key": "stt_batch", "ms": 420.0},
            {"key": "turn_detector", "ms": 50.0},
        ],
        "voice": {
            "audio_ms": 2500.0,
            "transcript_chars": 32,
            "stt_coverage_pct": 100.0,
            "stt_audio_gap_ms": 0.0,
        },
        "turn_detection": {
            "decision": "commit",
            "reason": "audio_eou",
            "endpoint_age_ms": 151.0,
        },
        "stt": {"feed_count": 12, "bytes_fed": 46080},
    }

    summary = extract_turn_run_summary(document)

    assert summary["turn_id"] == "turn-1"
    assert summary["stages"]["stt_batch"]["ms"] == 420.0
    assert summary["voice"]["audio_ms"] == 2500.0
    assert summary["voice"]["stt_audio_gap_ms"] == 0.0
    assert summary["turn_detection"]["endpoint_age_ms"] == 151.0
    assert summary["stt"]["feed_count"] == 12


def test_extract_turn_run_summary_recovery_shape() -> None:
    document = {
        "turn_id": "turn-2",
        "stages": [],
        "voice": {
            "recovered": True,
            "recovery_count": 1,
            "recovery_elapsed_ms": 450.0,
            "recovery_had_response_to_retract": True,
        },
    }

    summary = extract_turn_run_summary(document)

    assert summary["voice"]["recovered"] is True
    assert summary["voice"]["recovery_count"] == 1
    assert summary["voice"]["recovery_elapsed_ms"] == 450.0
    assert summary["voice"]["recovery_had_response_to_retract"] is True


def test_iter_audio_chunks_uses_actual_final_chunk_duration() -> None:
    chunks = list(iter_audio_chunks(_pcm_ms(100), chunk_ms=96))

    assert [duration for _, duration in chunks] == [96.0, 4.0]


def test_estimate_speech_end_ignores_trailing_silence() -> None:
    audio = _pcm_ms(100, amplitude=1200) + _pcm_ms(300)

    assert estimate_speech_end_ms(audio) == 100.0


def test_apply_speech_relative_metrics() -> None:
    result = ProbeResult(
        run=1,
        mode="audio",
        ok=True,
        speech_end_ms=1200.0,
        latest_partial_ms=1600.0,
        first_transcript_ms=1700.0,
        last_transcript_ms=1800.0,
    )

    apply_speech_relative_metrics(result)

    assert result.latest_partial_after_speech_ms == 400.0
    assert result.first_transcript_after_speech_ms == 500.0
    assert result.last_transcript_after_speech_ms == 600.0


def test_load_reference_prefers_combined_recovery_reference(tmp_path: Path) -> None:
    part1 = tmp_path / "recovery_part1.wav"
    part2 = tmp_path / "recovery_part2.wav"
    combined = tmp_path / "recovery.txt"
    part1.with_suffix(".txt").write_text("first half", encoding="utf-8")
    part2.with_suffix(".txt").write_text("second half", encoding="utf-8")
    combined.write_text("combined transcript", encoding="utf-8")

    assert load_reference(None, part1, part2) == "combined transcript"


def test_load_reference_falls_back_to_joined_part_references(tmp_path: Path) -> None:
    part1 = tmp_path / "clip_part1.wav"
    part2 = tmp_path / "clip_part2.wav"
    part1.with_suffix(".txt").write_text("first half", encoding="utf-8")
    part2.with_suffix(".txt").write_text("second half", encoding="utf-8")

    assert load_reference(None, part1, part2) == "first half second half"


def test_resolve_streaming_smoke_suite(tmp_path: Path) -> None:
    cases = resolve_suite_cases("streaming-smoke", tmp_path)

    assert [case.fixture for case in cases] == ["normal_request", "planning_comments"]
    assert cases[0].audio_path == tmp_path / "normal_request.wav"


def test_summarize_results_reports_commit_reliability() -> None:
    committed = ProbeResult(
        run=1,
        mode="audio",
        ok=True,
        fixture="normal_request",
        first_partial_ms=1000.0,
        latest_partial_ms=1600.0,
        latest_partial_after_speech_ms=300.0,
        first_transcript_ms=1800.0,
        first_transcript_after_speech_ms=500.0,
        first_response_ms=2500.0,
    )
    timed_out = ProbeResult(
        run=2,
        mode="audio",
        ok=False,
        fixture="normal_request",
        error="timed out after 30.0s",
        partials=["hello"],
    )
    finalize_result_contract(committed)
    finalize_result_contract(timed_out)

    summary = summarize_results([committed, timed_out])

    assert summary["runs"] == 2
    assert summary["ok_runs"] == 1
    assert summary["committed_runs"] == 1
    assert summary["timeout_runs"] == 1
    assert summary["commit_rate"] == 0.5
    assert summary["latest_partial_after_speech_ms_p50"] == 300.0
    assert summary["latest_partial_text"] == "hello"
