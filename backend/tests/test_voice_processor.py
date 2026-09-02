import asyncio
import time

import pytest

from core.voice.processor import SpeechEvent, SpeechProcessor, SpeechTurnPhase, VoiceMode


class FakeVAD:
    def __init__(self, results: list[bool]) -> None:
        self.results = results

    def is_speech(self, audio_bytes: bytes) -> bool:
        return self.results.pop(0) if self.results else False


class FakeWakeWord:
    def __init__(self, *, fire_on_calls: set[int] | None = None) -> None:
        self.process_count = 0
        self.reset_count = 0
        self._fire_on_calls = fire_on_calls or {1}

    def process(self, chunk: bytes) -> bool:
        self.process_count += 1
        return self.process_count in self._fire_on_calls

    def reset(self) -> None:
        self.reset_count += 1


def test_turn_complete_is_edge_triggered_until_continued(monkeypatch):
    async def run() -> None:
        processor = SpeechProcessor(vad_service=FakeVAD([False, False, False, False]))
        processor.mode = VoiceMode.ACTIVE_IDLE
        processor.turn_phase = SpeechTurnPhase.SPEAKING
        processor.last_activity_time = 0.0
        processor.silence_threshold = 0.0

        assert await processor.add_audio(b"silence-1") == SpeechEvent.TURN_COMPLETE
        assert processor.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE
        assert await processor.add_audio(b"silence-2") is None
        assert bytes(processor.turn_buffer) == b"silence-1silence-2"

        processor.continue_turn(reason="test")
        assert processor.turn_phase == SpeechTurnPhase.SPEAKING
        processor.last_activity_time = 0.0
        assert await processor.add_audio(b"silence-3") == SpeechEvent.TURN_COMPLETE
        assert await processor.add_audio(b"silence-4") is None

    asyncio.run(run())


def test_explicit_commit_marks_speaking_turn_as_endpoint_candidate():
    processor = SpeechProcessor(vad_service=FakeVAD([]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.turn_phase = SpeechTurnPhase.SPEAKING
    processor.turn_buffer.extend(b"captured speech")

    assert processor.request_turn_commit() is True
    assert processor.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE
    assert bytes(processor.turn_buffer) == b"captured speech"


def test_explicit_commit_ignores_empty_turn():
    processor = SpeechProcessor(vad_service=FakeVAD([]))
    processor.mode = VoiceMode.ACTIVE_IDLE

    assert processor.request_turn_commit() is False
    assert processor.turn_phase == SpeechTurnPhase.IDLE


def test_endpoint_candidate_buffers_and_resumes_same_turn():
    async def run() -> None:
        processor = SpeechProcessor(vad_service=FakeVAD([False, False, True]))
        processor.mode = VoiceMode.ACTIVE_IDLE
        processor.turn_phase = SpeechTurnPhase.SPEAKING
        processor.last_activity_time = 0.0
        processor.silence_threshold = 0.0

        assert await processor.add_audio(b"before-pause") == SpeechEvent.TURN_COMPLETE
        assert await processor.add_audio(b"pause") is None
        assert await processor.add_audio(b"resumed") == SpeechEvent.TURN_RESUMED

        assert processor.turn_phase == SpeechTurnPhase.SPEAKING
        assert bytes(processor.turn_buffer) == b"before-pausepauseresumed"

    asyncio.run(run())


def test_turn_complete_preserves_last_speech_anchor():
    async def run() -> None:
        processor = SpeechProcessor(vad_service=FakeVAD([True, False]))
        processor.mode = VoiceMode.ACTIVE_IDLE
        processor.turn_phase = SpeechTurnPhase.SPEAKING

        assert await processor.add_audio(b"speech") is None
        speech_at = processor.last_speech_monotonic
        assert speech_at > 0

        processor.last_activity_time = 0.0
        processor.silence_threshold = 0.0
        assert await processor.add_audio(b"silence") == SpeechEvent.TURN_COMPLETE
        assert processor.last_speech_monotonic == speech_at

        processor.consume_turn_audio()
        assert processor.last_speech_monotonic == 0.0

    asyncio.run(run())


def test_barge_in_vad_emits_candidate_not_committed_user_start():
    async def run() -> None:
        processor = SpeechProcessor(vad_service=FakeVAD([True, True]))
        processor.mode = VoiceMode.ACTIVE_AI_TURN
        processor.barge_in_min_frames = 2

        assert await processor.add_audio(b"speech-1") is None
        assert await processor.add_audio(b"speech-2") == SpeechEvent.BARGE_IN_CANDIDATE_STARTED
        assert processor.turn_phase == SpeechTurnPhase.SPEAKING
        assert bytes(processor.turn_buffer)

        processor.suppress_barge_in_candidate(reason="test")
        assert processor.turn_phase == SpeechTurnPhase.IDLE
        assert processor.turn_buffer == bytearray()

    asyncio.run(run())


def test_passive_wake_suppressed_skips_wakeword_process():
    async def run() -> None:
        wake = FakeWakeWord()
        processor = SpeechProcessor(vad_service=FakeVAD([]), wakeword_service=wake)
        processor._suppress_wake_for(5.0, "post_tts")

        assert await processor.add_audio(b"chunk") is None
        assert wake.process_count == 0

    asyncio.run(run())


def test_committed_wake_arms_refractory_and_blocks_immediate_rewake():
    async def run() -> None:
        wake = FakeWakeWord(fire_on_calls={1, 2})
        processor = SpeechProcessor(vad_service=FakeVAD([]), wakeword_service=wake)

        assert await processor.add_audio(b"wake-1") == SpeechEvent.WAKE_WORD_DETECTED
        processor.mode = VoiceMode.PASSIVE
        processor.turn_phase = SpeechTurnPhase.IDLE
        processor.turn_buffer.clear()

        assert await processor.add_audio(b"wake-2") is None
        assert wake.process_count == 1

    asyncio.run(run())


def test_activity_timeout_releases_refractory_for_immediate_rewake():
    async def run() -> None:
        wake = FakeWakeWord(fire_on_calls={1, 2})
        processor = SpeechProcessor(vad_service=FakeVAD([]), wakeword_service=wake)
        processor.active_timeout = 0.01

        assert await processor.add_audio(b"wake-1") == SpeechEvent.WAKE_WORD_DETECTED
        processor.last_activity_time = time.time() - 1.0
        processor.turn_phase = SpeechTurnPhase.IDLE

        assert await processor.add_audio(b"silence") == SpeechEvent.SESSION_ENDED
        assert processor.mode == VoiceMode.PASSIVE

        assert await processor.add_audio(b"wake-2") == SpeechEvent.WAKE_WORD_DETECTED
        assert wake.process_count == 2

    asyncio.run(run())


def test_leaving_active_ai_turn_arms_post_tts_suppression(monkeypatch):
    monkeypatch.setattr(
        "core.voice.processor.settings.VOICE.wakeword_post_tts_suppression_seconds",
        1.5,
    )
    processor = SpeechProcessor(vad_service=FakeVAD([]))
    processor.mode = VoiceMode.ACTIVE_AI_TURN

    processor.set_mode(VoiceMode.ACTIVE_IDLE, source="test")

    assert processor._wake_suppression_reason == "post_tts"
    assert processor._is_wake_suppressed()


def test_no_audio_passive_transition_allows_immediate_rewake():
    async def run() -> None:
        wake = FakeWakeWord(fire_on_calls={1})
        processor = SpeechProcessor(vad_service=FakeVAD([]), wakeword_service=wake)
        processor.mode = VoiceMode.ACTIVE_AI_TURN
        processor._suppress_wake_for(5.0, "refractory")

        processor.force_passive(
            reason="test.no_audio",
            release_wake_refractory=True,
            arm_post_tts_suppression=False,
        )

        assert processor.mode == VoiceMode.PASSIVE
        assert await processor.add_audio(b"wake") == SpeechEvent.WAKE_WORD_DETECTED

    asyncio.run(run())


def test_suppressed_passive_audio_still_retains_preroll():
    async def run() -> None:
        processor = SpeechProcessor(vad_service=FakeVAD([]), wakeword_service=FakeWakeWord())
        processor._suppress_wake_for(5.0, "post_tts")

        await processor.add_audio(b"pre-roll-chunk", retain_preroll=True)
        assert b"pre-roll-chunk" in b"".join(processor.circular_buffer)

    asyncio.run(run())


def test_suppression_then_open_gate_does_not_process_blocked_chunks():
    async def run() -> None:
        wake = FakeWakeWord(fire_on_calls={2})
        processor = SpeechProcessor(vad_service=FakeVAD([]), wakeword_service=wake)
        processor._suppress_wake_for(0.05, "post_tts")

        for _ in range(3):
            await processor.add_audio(b"blocked")

        assert wake.process_count == 0
        await asyncio.sleep(0.06)

        assert await processor.add_audio(b"fresh-1") is None
        assert wake.process_count == 1
        assert await processor.add_audio(b"wake-2") == SpeechEvent.WAKE_WORD_DETECTED
        assert wake.process_count == 2
        assert wake.reset_count >= 1

    asyncio.run(run())


def test_suppressed_wake_logs_once_per_window(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        "core.voice.processor.perf.log",
        lambda event, **metadata: events.append(event),
    )

    async def run() -> None:
        processor = SpeechProcessor(vad_service=FakeVAD([]), wakeword_service=FakeWakeWord())
        processor._suppress_wake_for(5.0, "post_tts")

        assert await processor.add_audio(b"blocked-1") is None
        assert await processor.add_audio(b"blocked-2") is None

    asyncio.run(run())
    assert events == ["wake_gate_suppressed"]


@pytest.mark.asyncio
async def test_playback_end_path_arms_post_tts_via_set_mode():
    processor = SpeechProcessor(vad_service=FakeVAD([]))
    processor.mode = VoiceMode.ACTIVE_AI_TURN

    processor.set_mode(VoiceMode.ACTIVE_IDLE, source="ws.audio_playback_end")

    assert processor.mode == VoiceMode.ACTIVE_IDLE
    assert processor._wake_suppression_reason == "post_tts"
    assert processor._is_wake_suppressed()


@pytest.mark.asyncio
async def test_peek_turn_speech_audio_excludes_preroll():
    # Non-speech preroll (TTS bleed), then sustained speech frames that promote the candidate.
    processor = SpeechProcessor(
        vad_service=FakeVAD([False, False, True, True, True, True]),
        wakeword_service=FakeWakeWord(fire_on_calls=set()),
    )
    processor.mode = VoiceMode.ACTIVE_AI_TURN
    processor.barge_in_min_frames = 3
    processor.min_speech_frames = 3

    assert await processor.add_audio(b"pre1") is None
    assert await processor.add_audio(b"pre2") is None
    assert await processor.add_audio(b"sp1") is None
    assert await processor.add_audio(b"sp2") is None
    event = await processor.add_audio(b"sp3")
    assert event == SpeechEvent.BARGE_IN_CANDIDATE_STARTED
    assert await processor.add_audio(b"sp4") is None

    full = bytes(processor.turn_buffer)
    speech = processor.peek_turn_speech_audio()
    assert full.endswith(speech)
    assert len(speech) < len(full)
    assert speech.startswith(b"sp1")
    assert b"pre1" in full
    assert not speech.startswith(b"pre")


@pytest.mark.asyncio
async def test_peek_turn_speech_audio_keeps_candidate_gaps_after_onset():
    processor = SpeechProcessor(
        vad_service=FakeVAD([False, True, False, True, True]),
        wakeword_service=FakeWakeWord(fire_on_calls=set()),
    )
    processor.mode = VoiceMode.ACTIVE_AI_TURN
    processor.barge_in_min_frames = 3

    assert await processor.add_audio(b"pre") is None
    assert await processor.add_audio(b"sp1") is None
    assert await processor.add_audio(b"gap") is None
    assert await processor.add_audio(b"sp2") is None
    event = await processor.add_audio(b"sp3")

    assert event == SpeechEvent.BARGE_IN_CANDIDATE_STARTED
    assert processor.peek_turn_speech_audio() == b"sp1gapsp2sp3"


@pytest.mark.asyncio
async def test_followup_identity_candidate_does_not_refresh_activity():
    processor = SpeechProcessor(vad_service=FakeVAD([True, True]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.hold_activity_until_owner = True
    processor.min_speech_frames = 2
    started = time.time() - 0.5
    processor.last_activity_time = started

    assert await processor.add_audio(b"s1") is None
    assert await processor.add_audio(b"s2") == SpeechEvent.FOLLOWUP_CANDIDATE_STARTED
    assert processor.followup_identity_pending is True
    assert processor.last_activity_time == started
    assert processor.turn_phase == SpeechTurnPhase.SPEAKING


@pytest.mark.asyncio
async def test_followup_identity_drop_preserves_activity_timer():
    processor = SpeechProcessor(vad_service=FakeVAD([True, True, True]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.hold_activity_until_owner = True
    processor.min_speech_frames = 2
    started = time.time() - 0.5
    processor.last_activity_time = started

    await processor.add_audio(b"s1")
    await processor.add_audio(b"s2")
    processor.drop_followup_identity_candidate(reason="speaker_mismatch")

    assert processor.last_activity_time == started
    assert processor.turn_phase == SpeechTurnPhase.IDLE
    assert processor.followup_identity_pending is False
    assert processor.turn_buffer == bytearray()


@pytest.mark.asyncio
async def test_followup_capture_does_not_timeout_while_speaking():
    processor = SpeechProcessor(vad_service=FakeVAD([True, True]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.hold_activity_until_owner = True
    processor.min_speech_frames = 1
    processor.active_timeout = 0.01
    processor.last_activity_time = time.time() - 1.0

    assert await processor.add_audio(b"s1") == SpeechEvent.FOLLOWUP_CANDIDATE_STARTED
    assert await processor.add_audio(b"s2") is None
    assert processor.mode == VoiceMode.ACTIVE_IDLE
    assert processor.turn_phase == SpeechTurnPhase.SPEAKING
    assert processor.followup_identity_pending is True


@pytest.mark.asyncio
async def test_admitted_followup_can_finish_after_activity_deadline():
    processor = SpeechProcessor(vad_service=FakeVAD([True, True]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.hold_activity_until_owner = True
    processor.min_speech_frames = 1
    processor.active_timeout = 0.01
    processor.last_activity_time = time.time() - 1.0

    assert await processor.add_audio(b"s1") == SpeechEvent.FOLLOWUP_CANDIDATE_STARTED
    processor.admit_followup_identity()
    assert await processor.add_audio(b"s2") is None
    assert processor.mode == VoiceMode.ACTIVE_IDLE
    assert processor.turn_phase == SpeechTurnPhase.SPEAKING
    assert processor.followup_identity_pending is False


@pytest.mark.asyncio
async def test_followup_drop_then_idle_can_timeout():
    processor = SpeechProcessor(vad_service=FakeVAD([True, False]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.hold_activity_until_owner = True
    processor.min_speech_frames = 1
    processor.active_timeout = 0.01
    processor.last_activity_time = time.time() - 1.0

    assert await processor.add_audio(b"s1") == SpeechEvent.FOLLOWUP_CANDIDATE_STARTED
    processor.drop_followup_identity_candidate(reason="speaker_mismatch")
    assert await processor.add_audio(b"silence") == SpeechEvent.SESSION_ENDED
    assert processor.mode == VoiceMode.PASSIVE


@pytest.mark.asyncio
async def test_endpoint_candidate_does_not_activity_timeout():
    processor = SpeechProcessor(vad_service=FakeVAD([False]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.turn_phase = SpeechTurnPhase.ENDPOINT_CANDIDATE
    processor.active_timeout = 0.01
    processor.last_activity_time = time.time() - 1.0

    assert await processor.add_audio(b"silence") is None
    assert processor.mode == VoiceMode.ACTIVE_IDLE
    assert processor.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE


@pytest.mark.asyncio
async def test_unenrolled_followup_still_starts_user_turn():
    processor = SpeechProcessor(vad_service=FakeVAD([True, True]))
    processor.mode = VoiceMode.ACTIVE_IDLE
    processor.hold_activity_until_owner = False
    processor.min_speech_frames = 2
    started = time.time() - 0.5
    processor.last_activity_time = started

    assert await processor.add_audio(b"s1") is None
    assert await processor.add_audio(b"s2") == SpeechEvent.USER_TURN_STARTED
    assert processor.last_activity_time != started
    assert processor.followup_identity_pending is False
