import time
import logging
import asyncio
from collections import deque
from enum import Enum, auto
from typing import Literal, Optional
from core.voice.vad_service import TenVADService
from core.voice.wakeword_service import WakeWordService
from core import settings
from services.perf import perf

logger = logging.getLogger(__name__)

WakeSuppressionReason = Literal["refractory", "post_tts"]


class SpeechEvent(Enum):
    USER_TURN_STARTED = auto()
    BARGE_IN_CANDIDATE_STARTED = auto()
    FOLLOWUP_CANDIDATE_STARTED = auto()
    TURN_RESUMED = auto()
    TURN_COMPLETE = auto()
    WAKE_WORD_DETECTED = auto()
    SESSION_ENDED = auto()


class VoiceMode(Enum):
    PASSIVE        = auto()  # Waiting for wake word
    ACTIVE_IDLE    = auto()  # Conversation window open, waiting for user speech
    ACTIVE_AI_TURN = auto()  # AI speaking/processing — barge-in threshold raised


class SpeechTurnPhase(Enum):
    IDLE = auto()                # No user audio turn is being captured
    SPEAKING = auto()            # Capturing audio for the current user turn
    ENDPOINT_CANDIDATE = auto()  # VAD fired; waiting for detector/commit decision


class SpeechProcessor:
    """
    Stateful audio processor managing the voice interaction lifecycle.

    A single VoiceMode enum encodes all valid states — invalid combinations
    (e.g. "passive but AI speaking") are structurally unrepresentable.
    """

    def __init__(self, vad_service: TenVADService, wakeword_service: Optional[WakeWordService] = None):
        self.vad_service = vad_service
        self.wakeword_service = wakeword_service

        # Configuration
        self.min_speech_frames = settings.VOICE.min_speech_frames
        self.barge_in_min_frames = settings.VOICE.barge_in_min_frames
        self.silence_threshold = settings.VOICE.silence_threshold
        self.active_timeout = settings.VOICE.active_timeout

        # State
        self.mode = VoiceMode.PASSIVE

        # Circular pre-roll buffer for "Wake-and-Append". Trimmed by duration (not chunk
        # count) so it stays correct regardless of the frontend's audio chunk size.
        self._preroll_max_bytes = int(
            settings.VOICE.sample_rate * settings.VOICE.channels * 2 * settings.VOICE.wake_preroll_seconds
        )
        self.circular_buffer: deque[bytes] = deque()
        self._circular_bytes = 0

        self.turn_buffer = bytearray()
        self.vad_positive_count = 0
        self.vad_negative_count = 0
        self.turn_phase = SpeechTurnPhase.IDLE
        self._wake_suppressed_until = 0.0
        self._wake_suppression_reason: WakeSuppressionReason | None = None
        self._wake_suppression_logged = False
        self.last_activity_time = time.time()
        # VAD silence anchor. Distinct from last_activity_time (listen-window clock).
        self.last_speech_monotonic = 0.0
        self.max_negative_frames = 2
        self._turn_start_time: float = 0.0
        # Byte offset into turn_buffer where estimated speech onset begins.
        # Pre-roll before this may contain TTS playback and must not enter speaker scoring.
        self._speech_onset_offset = 0
        self._candidate_span_bytes = 0
        # When enrolled, ACTIVE_IDLE VAD captures must score as the owner before they
        # count as activity or become a user turn.
        self.hold_activity_until_owner = False
        self.followup_identity_pending = False

    def refresh_activity(self, *, source: str = "unspecified"):
        """Reset the listen-window clock. Not used for VAD silence."""
        self.last_activity_time = time.time()

    def _speech_silence_age_s(self, current_time: float) -> float:
        """Endpointing uses last speech, never the 4s listen-window clock."""
        if self.last_speech_monotonic > 0:
            return time.monotonic() - self.last_speech_monotonic
        return current_time - self.last_activity_time

    def admit_followup_identity(self, *, source: str = "followup_identity") -> None:
        """Owner (or fail-open) onset: this capture may reset the listening window."""
        self.followup_identity_pending = False
        self.refresh_activity(source=source)

    def drop_followup_identity_candidate(self, *, reason: str = "unspecified") -> None:
        """Discard an una-admitted follow-up capture without resetting the activity timer."""
        logger.debug("Dropping follow-up identity candidate | reason=%s", reason)
        self.reset_state(refresh_activity=False)
        self.clear_preroll()

    def _suppress_wake_for(self, seconds: float, reason: WakeSuppressionReason) -> None:
        if seconds <= 0:
            return
        until = time.monotonic() + seconds
        if until > self._wake_suppressed_until:
            self._wake_suppressed_until = until
            self._wake_suppression_reason = reason
            self._wake_suppression_logged = False
            logger.info("Wake gate armed | reason=%s duration_s=%.2f", reason, seconds)

    def _wake_suppression_remaining_ms(self) -> float:
        return max(0.0, (self._wake_suppressed_until - time.monotonic()) * 1000.0)

    def _is_wake_suppressed(self) -> bool:
        if time.monotonic() < self._wake_suppressed_until:
            return True
        self._wake_suppressed_until = 0.0
        self._wake_suppression_reason = None
        self._wake_suppression_logged = False
        return False

    def _log_wake_gate_suppressed(self) -> None:
        if self._wake_suppression_logged:
            return
        self._wake_suppression_logged = True
        reason = self._wake_suppression_reason or "unknown"
        remaining_ms = round(self._wake_suppression_remaining_ms(), 1)
        logger.debug("Wake gate suppressed | reason=%s remaining_ms=%.1f", reason, remaining_ms)
        perf.log(
            "wake_gate_suppressed",
            reason=reason,
            remaining_ms=remaining_ms,
        )

    def release_wake_refractory(self, *, source: str = "unspecified") -> None:
        """Drop refractory suppression so the user can immediately re-wake after a failed turn."""
        if self._wake_suppression_reason != "refractory" or not self._is_wake_suppressed():
            return
        self._wake_suppressed_until = 0.0
        self._wake_suppression_reason = None
        self._wake_suppression_logged = False
        logger.info("Wake refractory released | source=%s", source)
        perf.log("wake_gate_released", reason="refractory", source=source)

    def set_mode(
        self,
        new_mode: VoiceMode,
        *,
        source: str = "unspecified",
        arm_post_tts_suppression: bool = True,
    ):
        """
        Transition to a new VoiceMode.
        By default, leaving ACTIVE_AI_TURN arms suppression against residual TTS.
        Callers that know no assistant audio played can disable it.
        """
        old = self.mode
        if old == new_mode:
            logger.debug("set_mode no-op | mode=%s source=%s", new_mode.name, source)
            return
        self.mode = new_mode
        logger.info("Mode | source=%s from=%s to=%s", source, old.name, new_mode.name)
        if old == VoiceMode.ACTIVE_AI_TURN and arm_post_tts_suppression:
            self._suppress_wake_for(
                settings.VOICE.wakeword_post_tts_suppression_seconds,
                "post_tts",
            )

    def _preroll_max_bytes_for(self, preroll_seconds: float | None) -> int:
        seconds = settings.VOICE.wake_preroll_seconds if preroll_seconds is None else preroll_seconds
        return int(settings.VOICE.sample_rate * settings.VOICE.channels * 2 * seconds)

    def _append_preroll(self, audio_bytes: bytes, *, max_bytes: int | None = None) -> None:
        """Append to the pre-roll buffer, evicting oldest chunks beyond the duration target."""
        limit = self._preroll_max_bytes if max_bytes is None else max_bytes
        self.circular_buffer.append(audio_bytes)
        self._circular_bytes += len(audio_bytes)
        while self._circular_bytes > limit and len(self.circular_buffer) > 1:
            self._circular_bytes -= len(self.circular_buffer.popleft())

    def clear_preroll(self) -> None:
        self.circular_buffer.clear()
        self._circular_bytes = 0

    async def add_audio(
        self,
        audio_bytes: bytes,
        *,
        retain_preroll: bool = True,
        preroll_seconds: float | None = None,
    ) -> Optional[SpeechEvent]:
        """
        Process incoming audio bytes. Routes to wake word detection (PASSIVE)
        or VAD turn detection (ACTIVE_*) based on current mode.
        """
        current_time = time.time()
        if retain_preroll:
            self._append_preroll(
                audio_bytes,
                max_bytes=self._preroll_max_bytes_for(preroll_seconds),
            )

        if self.mode == VoiceMode.PASSIVE:
            if self._is_wake_suppressed():
                self._log_wake_gate_suppressed()
                return None

            if not self.wakeword_service:
                return None

            is_detected = await asyncio.to_thread(self.wakeword_service.process, audio_bytes)
            if is_detected:
                context_audio = b''.join(self.circular_buffer)
                bytes_per_second = settings.VOICE.sample_rate * settings.VOICE.channels * 2
                buffer_duration = len(context_audio) / bytes_per_second
                logger.info(
                    "Wake Word detected! buffer=%d chunks (%.2fs)",
                    len(self.circular_buffer),
                    buffer_duration,
                )

                self._suppress_wake_for(settings.VOICE.wakeword_refractory_seconds, "refractory")
                perf.log("wake_gate_committed", reason="refractory")

                self.set_mode(VoiceMode.ACTIVE_IDLE, source="wake_word_detected")
                self.wakeword_service.reset()

                self.clear_preroll()
                self.turn_buffer.extend(context_audio)
                self._speech_onset_offset = 0
                self._candidate_span_bytes = 0
                self.turn_phase = SpeechTurnPhase.SPEAKING
                self._turn_start_time = current_time
                self.last_speech_monotonic = time.monotonic()
                self.followup_identity_pending = False
                self.refresh_activity(source="wake_word_detected")

                return SpeechEvent.WAKE_WORD_DETECTED

            return None

        # ACTIVE mode: standard VAD turn detection
        is_speech = await asyncio.to_thread(self.vad_service.is_speech, audio_bytes)

        if self.turn_phase == SpeechTurnPhase.SPEAKING:
            self.turn_buffer.extend(audio_bytes)
            if is_speech:
                if not self.followup_identity_pending:
                    self.last_activity_time = current_time
                self.last_speech_monotonic = time.monotonic()
            elif self._speech_silence_age_s(current_time) > self.silence_threshold:
                self.turn_phase = SpeechTurnPhase.ENDPOINT_CANDIDATE
                return SpeechEvent.TURN_COMPLETE
        elif self.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE:
            self.turn_buffer.extend(audio_bytes)
            if is_speech:
                self.turn_phase = SpeechTurnPhase.SPEAKING
                self.last_speech_monotonic = time.monotonic()
                if not self.followup_identity_pending:
                    self.refresh_activity(source="endpoint_candidate_resumed")
                return SpeechEvent.TURN_RESUMED
            return None
        else:
            if is_speech:
                if self.vad_positive_count == 0:
                    self._candidate_span_bytes = 0
                self.vad_positive_count += 1
                self.vad_negative_count = 0
                self._candidate_span_bytes += len(audio_bytes)

                is_barge_in = self.mode == VoiceMode.ACTIVE_AI_TURN
                required = self.barge_in_min_frames if is_barge_in else self.min_speech_frames
                if self.vad_positive_count >= required:
                    gate_followup = (
                        self.hold_activity_until_owner
                        and self.mode == VoiceMode.ACTIVE_IDLE
                    )
                    label = "BARGE-IN" if is_barge_in else "VAD"
                    logger.info("%s: Sustained speech detected (%d frames). Starting turn.", label, self.vad_positive_count)
                    self.turn_phase = SpeechTurnPhase.SPEAKING
                    self._turn_start_time = current_time
                    self.last_speech_monotonic = time.monotonic()
                    # Seed from the rolling pre-roll (the same buffer the wake word uses) so
                    # the onset isn't clipped by VAD ramp-up or false-start resets.
                    preroll = b"".join(self.circular_buffer)
                    self.turn_buffer.extend(preroll)
                    self._speech_onset_offset = max(0, len(preroll) - self._candidate_span_bytes)
                    self._candidate_span_bytes = 0
                    self.clear_preroll()
                    if gate_followup:
                        self.followup_identity_pending = True
                        return SpeechEvent.FOLLOWUP_CANDIDATE_STARTED
                    self.followup_identity_pending = False
                    self.refresh_activity(source=f"{label.lower()}_speech_start")
                    if is_barge_in:
                        return SpeechEvent.BARGE_IN_CANDIDATE_STARTED
                    return SpeechEvent.USER_TURN_STARTED
            elif self.vad_positive_count > 0:
                self.vad_negative_count += 1
                self._candidate_span_bytes += len(audio_bytes)
                if self.vad_negative_count > self.max_negative_frames:
                    self.vad_positive_count = 0
                    self.vad_negative_count = 0
                    self._candidate_span_bytes = 0

        # Listen-window timeout only while expecting speech. SPEAKING and
        # ENDPOINT_CANDIDATE are recognizing — an utterance that started in the
        # window must be allowed to finish and commit.
        if (
            self.mode == VoiceMode.ACTIVE_IDLE
            and self.turn_phase == SpeechTurnPhase.IDLE
            and (current_time - self.last_activity_time) > self.active_timeout
        ):
            logger.info(
                "Activity timeout -> SESSION_ENDED | silence_age_s=%.3f threshold_s=%.3f",
                current_time - self.last_activity_time,
                self.active_timeout,
            )
            self.force_passive(reason="activity_timeout", release_wake_refractory=True)
            return SpeechEvent.SESSION_ENDED

        return None

    def continue_turn(self, *, reason: str = "unspecified") -> None:
        """Re-arm endpoint detection after a candidate endpoint was rejected."""
        if self.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE:
            logger.debug("Continuing user turn | reason=%s", reason)
            self.turn_phase = SpeechTurnPhase.SPEAKING
        if not self.followup_identity_pending:
            self.refresh_activity(source=f"continue_turn:{reason}")

    def request_turn_commit(self) -> bool:
        """Mark an explicitly-ended push-to-talk turn ready for commitment."""
        if not self.turn_buffer:
            return False
        if self.turn_phase == SpeechTurnPhase.SPEAKING:
            self.turn_phase = SpeechTurnPhase.ENDPOINT_CANDIDATE
            return True
        return self.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE

    def peek_turn_speech_audio(self) -> bytes:
        """Return onset-forward PCM for speaker scoring without consuming the STT buffer."""
        offset = min(self._speech_onset_offset, len(self.turn_buffer))
        return bytes(self.turn_buffer[offset:])

    def suppress_barge_in_candidate(self, *, reason: str = "unspecified") -> None:
        """Discard a rejected barge-in candidate while keeping the assistant turn alive."""
        logger.debug("Suppressing barge-in candidate | reason=%s", reason)
        self.turn_buffer.clear()
        self.clear_preroll()
        self.turn_phase = SpeechTurnPhase.IDLE
        self.vad_positive_count = 0
        self.vad_negative_count = 0
        self._turn_start_time = 0.0
        self.last_speech_monotonic = 0.0
        self._speech_onset_offset = 0
        self._candidate_span_bytes = 0
        self.followup_identity_pending = False
        self.refresh_activity(source=f"suppress_barge_in_candidate:{reason}")

    def consume_turn_audio(self) -> bytes:
        """Extract the accumulated audio buffer and reset turn state."""
        audio = bytes(self.turn_buffer)
        self.turn_buffer.clear()
        # Drop pre-roll so the next turn's onset can't replay this turn's tail.
        self.clear_preroll()
        self.turn_phase = SpeechTurnPhase.IDLE
        self.vad_positive_count = 0
        self.vad_negative_count = 0
        self.last_speech_monotonic = 0.0
        self._speech_onset_offset = 0
        self._candidate_span_bytes = 0
        self.followup_identity_pending = False
        self.refresh_activity(source="consume_turn_audio")
        return audio

    def reset_state(self, *, refresh_activity: bool = True):
        """Reset turn-specific state without changing mode."""
        self.turn_buffer.clear()
        self.turn_phase = SpeechTurnPhase.IDLE
        self.vad_positive_count = 0
        self.vad_negative_count = 0
        self._turn_start_time = 0.0
        self.last_speech_monotonic = 0.0
        self._speech_onset_offset = 0
        self._candidate_span_bytes = 0
        self.followup_identity_pending = False
        if refresh_activity:
            self.refresh_activity(source="reset_state")

    def force_passive(
        self,
        *,
        reason: str = "unspecified",
        release_wake_refractory: bool = False,
        arm_post_tts_suppression: bool = True,
    ):
        """Return to PASSIVE mode and clear all buffers and wake-word state."""
        if self.mode == VoiceMode.PASSIVE:
            logger.debug("force_passive skipped (already PASSIVE) | reason=%s", reason)
            if release_wake_refractory:
                self.release_wake_refractory(source=f"force_passive:{reason}")
            return
        self.set_mode(
            VoiceMode.PASSIVE,
            source=f"force_passive:{reason}",
            arm_post_tts_suppression=arm_post_tts_suppression,
        )
        self.reset_state()
        self.clear_preroll()
        if self.wakeword_service:
            self.wakeword_service.reset()
        if release_wake_refractory:
            self.release_wake_refractory(source=f"force_passive:{reason}")

    def force_active(self, *, reason: str = "unspecified"):
        """Open the active listening window without treating wake audio as user speech."""
        if self.mode != VoiceMode.ACTIVE_IDLE:
            self.set_mode(VoiceMode.ACTIVE_IDLE, source=f"force_active:{reason}")
        self.reset_state()
        self.clear_preroll()
        if self.wakeword_service:
            self.wakeword_service.reset()
