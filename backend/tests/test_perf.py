from services.perf import PerfLogger


def _enable(perf: PerfLogger) -> None:
    perf._enabled = True


def _turn(perf: PerfLogger, **metadata):
    return perf.context(owner_id="u1", connection_id="u1", **metadata)


def test_perf_context_adds_turn_metadata_to_summary():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-1", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        elapsed = perf.end("llm", "u1", iteration=0, model="fast-model")
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    assert elapsed >= 0
    summary = perf.latest_turn_summary()
    assert summary is not None
    assert summary["turn_id"] == "turn-1"
    assert summary["modality"] == "voice"
    assert summary["stages"][0]["key"] == "llm"
    assert summary["stages"][0]["iteration"] == 0
    assert summary["model"] == "fast-model"


def test_tool_routing_event_is_added_to_turn_summary():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-routing", source="user", scenario="voice"):
        perf.log(
            "tool_routing_complete",
            session="u1",
            policy_name="budget_aware_multi_intent",
            match_mode="multi_intent",
            matched_plugins=["calendar", "scheduler"],
            routed_tool_count=4,
            ranked_plugins=[{"plugin": "calendar", "raw": 0.9, "adjusted": 0.9}],
            schema_chars=1200,
            schema_tokens=300,
            route_latency_ms=24.5,
            segment_matches={"check calendar": ["calendar"]},
            used_session_carryover=False,
        )
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["tool_routing"] == {
        "policy_name": "budget_aware_multi_intent",
        "match_mode": "multi_intent",
        "matched_plugins": ["calendar", "scheduler"],
        "routed_tool_count": 4,
        "schema_tokens": 300,
        "route_latency_ms": 24.5,
        "used_session_carryover": False,
    }


def test_latest_turn_summary_retains_multi_iteration_stages():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-1", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.end("llm", "u1", iteration=0, model="fast-model")
        perf.start("tts_first_chunk", "u1")
        perf.end("tts_first_chunk", "u1")
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")
        perf.start("turn_latency", "u1")
        perf.end("turn_latency", "u1")
        perf.start("code_exec", "u1", iteration=0)
        perf.end("code_exec", "u1", iteration=0)
        perf.start("llm", "u1", iteration=1, model="fast-model")
        perf.end("llm", "u1", iteration=1, model="fast-model")
        perf.log("process_turn_finally", session="u1", outcome="audio_sent")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["turn_id"] == "turn-1"
    assert summary["source"] == "user"
    assert summary["modality"] == "voice"
    assert summary["response_ms"] is not None
    assert summary["total_ms"] is not None
    assert [s["iteration"] for s in summary["stages"] if s["key"] == "llm"] == [0, 1]
    post_audio = [s for s in summary["stages"] if s["group"] == "post_first_audio"]
    assert {s["key"] for s in post_audio} == {"code_exec", "llm"}
    completed = perf.completed_turn_summaries()
    assert completed[0]["owner_id"] == "u1"
    assert completed[0]["expires_at"]


def test_connection_scoped_turn_summary_uses_owner_id_and_keeps_model_stage():
    perf = PerfLogger()
    _enable(perf)

    with perf.context(
        turn_id="turn-presence",
        source="user",
        scenario="voice",
        owner_id="geoff",
        connection_id="conn-browser",
    ):
        perf.start("stt_finalize_wait", "conn-browser")
        perf.end("stt_finalize_wait", "conn-browser")
        perf.start("llm", "conn-browser", iteration=0, model="fast-model")
        perf.end("llm", "conn-browser", iteration=0, model="fast-model")
        perf.start("response_latency", "conn-browser")
        perf.end("response_latency", "conn-browser")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["owner_id"] == "geoff"
    assert summary["connection_id"] == "conn-browser"
    assert summary["model"] == "fast-model"
    assert [stage["key"] for stage in summary["stages"]] == ["stt_finalize_wait", "llm"]


def test_latest_turn_summary_exposes_visible_running_llm_retry_state():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-running", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.log(
            "llm_stream_event",
            session="u1",
            iteration=0,
            model="fast-model",
            status="timeout",
            attempt=1,
            retry_count=0,
            timeout_ms=12000,
        )

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["turn_id"] == "turn-running"
    assert summary["status"] == "running"
    assert summary["active_stage"]["key"] == "llm"
    assert summary["active_stage"]["status"] == "timeout"
    assert summary["active_stage"]["timeout_ms"] == 12000


def test_latest_turn_summary_prefers_completed_turn_over_older_orphaned_running_turn():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-orphaned", source="user", scenario="voice"):
        perf.start("tool_route", "u1")
        perf.end("tool_route", "u1")

    with _turn(perf, turn_id="turn-completed", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.end("llm", "u1", iteration=0, model="fast-model")
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["turn_id"] == "turn-completed"
    assert summary["status"] == "completed"


def test_latest_turn_summary_keeps_newer_running_turn_over_completed_turn():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-completed", source="user", scenario="voice"):
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    with _turn(perf, turn_id="turn-running", source="user", scenario="voice"):
        perf.start("tool_route", "u1")
        perf.end("tool_route", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["turn_id"] == "turn-running"
    assert summary["status"] == "running"


def test_latest_turn_summary_keeps_hidden_running_turns_out_of_user_diagnostics():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-hidden", source="system", scenario="system", delivery="silent"):
        perf.log(
            "llm_stream_event",
            session="u1",
            iteration=0,
            model="fast-model",
            status="waiting",
            attempt=1,
            retry_count=0,
            timeout_ms=12000,
        )

    assert perf.latest_turn_summary() is None


def test_latest_turn_summary_keeps_sessionless_running_turns_out_of_user_diagnostics():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-sessionless", source="user", scenario="voice"):
        perf.start("prompt_build", "")
        perf.end("prompt_build", "")

    assert perf.latest_turn_summary() is None
    assert perf._turn_summaries == {}


def test_sessionless_running_turn_does_not_hide_latest_completed_turn():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-sessionless", source="user", scenario="voice"):
        perf.start("prompt_build", "")
        perf.end("prompt_build", "")

    with _turn(perf, turn_id="turn-completed", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.end("llm", "u1", iteration=0, model="fast-model")
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["turn_id"] == "turn-completed"


def test_llm_stage_records_retry_metadata():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-retry", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.end(
            "llm",
            "u1",
            iteration=0,
            model="fast-model",
            attempt=2,
            retry_count=1,
            timeout_ms=12000,
            status="retry_ok",
        )
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    stage = summary["stages"][0]
    assert stage["key"] == "llm"
    assert stage["attempt"] == 2
    assert stage["retry_count"] == 1
    assert stage["timeout_ms"] == 12000
    assert stage["status"] == "retry_ok"


def test_llm_stage_end_handles_cleared_active_stage():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-active-clear", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.log(
            "llm_stream_event",
            session="u1",
            iteration=0,
            model="fast-model",
            status="waiting",
            attempt=1,
            retry_count=0,
            timeout_ms=12000,
        )
        perf.log(
            "llm_stream_event",
            session="u1",
            iteration=0,
            model="fast-model",
            status="first_token",
            attempt=1,
            retry_count=0,
            timeout_ms=12000,
        )
        perf.end("llm", "u1", iteration=0, model="fast-model", status="ok")
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["active_stage"] is None
    assert summary["stages"][0]["key"] == "llm"


def test_latest_turn_summary_ignores_fast_recovery_handoff():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-handoff", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.end("llm", "u1", iteration=0, model="fast-model")
        perf.log("process_turn_finally", session="u1", outcome="fast_recovery_handoff")

    assert perf.latest_turn_summary() is None


def test_latest_turn_summary_keeps_no_audio_completed_turn_visible():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-no-audio", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.end("llm", "u1", iteration=0, model="fast-model")
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1", status="no_audio_sent")
        perf.log("process_turn_finally", session="u1", outcome="no_audio_sent")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["turn_id"] == "turn-no-audio"
    assert summary["status"] == "completed"


def test_latest_turn_summary_ignores_cancelled_endpoint_candidate():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-endpoint-candidate", source="user", scenario="voice"):
        perf.start("stt_stream_start", "u1", stream_id="stt-1")
        perf.end("stt_stream_start", "u1", stream_id="stt-1")
        perf.log(
            "turn_detector_decision",
            session="u1",
            decision="continue",
            reason="awaiting_stt_text",
        )
        perf.log("endpoint_candidate_cancelled", session="u1")

    assert perf.latest_turn_summary() is None


def test_stale_cancelled_endpoint_does_not_hide_latest_completed_turn():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-cancelled-endpoint", source="user", scenario="voice"):
        perf.start("stt_stream_start", "u1", stream_id="stt-1")
        perf.end("stt_stream_start", "u1", stream_id="stt-1")
        perf.log("endpoint_candidate_cancelled", session="u1")

    with _turn(perf, turn_id="turn-completed", source="user", scenario="voice"):
        perf.start("llm", "u1", iteration=0, model="fast-model")
        perf.end("llm", "u1", iteration=0, model="fast-model")
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["turn_id"] == "turn-completed"
    assert summary["status"] == "completed"


def test_latest_turn_summary_maps_prefetched_to_system_modality():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-prefetched", source="system", scenario="prefetched"):
        perf.start("tts_first_chunk", "u1")
        perf.end("tts_first_chunk", "u1")
        perf.start("turn_latency", "u1")
        perf.end("turn_latency", "u1")
        perf.log("process_turn_finally", session="u1", outcome="audio_sent")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["source"] == "system"
    assert summary["modality"] == "system"
    assert summary["delivery"] == "prefetched"


def test_turn_summary_captures_compact_voice_and_stt_metadata():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-voice", source="user", scenario="voice"):
        perf.log(
            "stt_stream_summary",
            session="u1",
            stream_id="stt-1",
            status="finished",
            finish_status="ok",
            transcript_chars=42,
            feed_count=7,
            bytes_fed=1234,
            provider_protocol="apple_speech",
            provider_events=3,
            provider_finals=1,
            provider_turn_ends=1,
            finalize_chars=42,
            finalize_used_latest=False,
        )
        perf.log(
            "turn_detector_decision",
            session="u1",
            decision="commit",
            reason="audio_eou",
            confidence=0.92,
            text_chars=42,
        )
        perf.log(
            "voice_turn_committed",
            session="u1",
            audio_ms=1536.0,
            transcript_chars=42,
            reason="audio_eou",
            admission_source="followup",
            admission_reason="owner_followup",
        )
        perf.log(
            "fast_recovery_triggered",
            session="u1",
            elapsed_ms=320.0,
            continuation_audio_ms=640.0,
            transcript_chars=18,
            active_stream_id="stt-2",
        )
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()

    assert summary is not None
    assert summary["stt"] == {
        "stream_id": "stt-1",
        "status": "finished",
        "finish_status": "ok",
        "transcript_chars": 42,
        "feed_count": 7,
        "bytes_fed": 1234,
        "provider_protocol": "apple_speech",
        "provider_events": 3,
        "provider_finals": 1,
        "provider_turn_ends": 1,
        "finalize_chars": 42,
        "finalize_used_latest": False,
    }
    assert summary["turn_detection"]["reason"] == "audio_eou"
    assert summary["turn_detection"]["confidence"] == 0.92
    assert summary["voice"]["audio_ms"] == 1536.0
    assert summary["voice"]["admission_source"] == "followup"
    assert summary["voice"]["admission_reason"] == "owner_followup"
    assert summary["voice"]["recovered"] is True
    assert summary["voice"]["recovery_count"] == 1


def test_turn_detector_decision_merges_eou_visibility_fields():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-eou", source="user", scenario="voice"):
        perf.log(
            "turn_detector_decision",
            session="u1",
            decision="continue",
            reason="awaiting_stt_text",
            text_chars=0,
            endpoint_age_ms=40.0,
        )
        perf.log(
            "turn_detector_decision",
            session="u1",
            decision="commit",
            reason="audio_eou",
            confidence=0.71,
            text_chars=18,
            endpoint_age_ms=160.0,
            end_of_turn_delay_ms=420.5,
            transcription_delay_ms=0.0,
            continue_count=1,
            awaiting_stt_count=1,
            vad_endpoint_count=2,
            endpointing_profile="min=0.15,apple_max=0.45,stable=0.20,stt_wait=0.50",
        )
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    summary = perf.latest_turn_summary()
    assert summary is not None
    assert summary["turn_detection"] == {
        "decision": "commit",
        "reason": "audio_eou",
        "confidence": 0.71,
        "text_chars": 18,
        "endpoint_age_ms": 160.0,
        "end_of_turn_delay_ms": 420.5,
        "transcription_delay_ms": 0.0,
        "continue_count": 1,
        "awaiting_stt_count": 1,
        "vad_endpoint_count": 2,
        "endpointing_profile": "min=0.15,apple_max=0.45,stable=0.20,stt_wait=0.50",
    }


def test_playback_end_received_annotates_existing_voice_summary_only():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-playback", source="user", scenario="voice"):
        perf.log(
            "voice_turn_committed",
            session="u1",
            audio_ms=800.0,
            transcript_chars=10,
            reason="audio_eou",
        )
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")

    perf.log(
        "playback_end_received",
        session="u1",
        turn_id="turn-playback",
        stale=False,
        turn_active=False,
    )
    # Unknown turns must not invent a summary.
    perf.log(
        "playback_end_received",
        session="u1",
        turn_id="turn-missing",
        stale=True,
    )

    summary = perf.latest_turn_summary()
    assert summary is not None
    assert summary["turn_id"] == "turn-playback"
    assert summary["voice"]["playback_end_received"] is True
    assert summary["voice"]["playback_end_stale"] is False
    assert summary["voice"]["playback_end_turn_active"] is False
    assert len(perf.completed_turn_summaries()) == 1


def test_fast_recovery_after_first_audio_keeps_recovered_on_completed_summary():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-late-recovery", source="user", scenario="voice"):
        perf.log(
            "voice_turn_committed",
            session="u1",
            audio_ms=800.0,
            transcript_chars=20,
            reason="audio_eou",
        )
        perf.start("response_latency", "u1")
        perf.end("response_latency", "u1")
        perf.log(
            "fast_recovery_triggered",
            session="u1",
            elapsed_ms=400.0,
            continuation_audio_ms=200.0,
        )

    summary = perf.latest_turn_summary()
    assert summary is not None
    assert summary["status"] == "completed"
    assert summary["voice"]["recovered"] is True
    assert summary["voice"]["recovery_count"] == 1


def test_process_turn_cancelled_fast_recovery_stamps_recovered():
    perf = PerfLogger()
    _enable(perf)

    with _turn(perf, turn_id="turn-handoff-recovery", source="user", scenario="voice"):
        perf.log(
            "voice_turn_committed",
            session="u1",
            audio_ms=800.0,
            transcript_chars=20,
            reason="audio_eou",
        )
        perf.log("process_turn_cancelled", session="u1", fast_recovery=True)

    summary = perf._turn_summaries["u1:turn-handoff-recovery"]
    assert summary["status"] == "handoff"
    assert summary["voice"]["recovered"] is True
    assert perf.latest_turn_summary() is None

