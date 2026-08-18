# Barge-In Reliability

**Status:** Built — candidate window + enrolled-owner speaker gate  
**Date:** 2026-06-02 (speaker gate: 2026-08-04)  
**Priority:** High — false barge-ins break timer/reminder trust and cause duplicate proactive delivery.

---

## Problem

JARV1S used to treat any sustained VAD during `ACTIVE_AI_TURN` as an immediate barge-in. In practice, background conversation, TV, or someone else in the room could clear the low bar (`barge_in_min_frames = 4`) while JARV1S was speaking a proactive alert such as a timer, reminder, or automation announce.

The user never heard the alert. The trigger could be marked `awaiting_delivery` (`no_audio_sent`), then retried later, which felt like a duplicate or random reminder.

After the candidate window landed, conversational answers still committed on any endpointed meaningful text or bare max-wait. Nearby talkers could therefore cancel normal answers even though proactive alerts were already gated.

### Evidence

Diagnostic snapshot `20260602_054637` (`reason`: “Timer alert got barged in and then finally came through much later”):

1. `05:45:51` — orchestrator retries `awaiting_delivery` trigger for owner.
2. `05:45:52` — proactive `process_turn` starts (`orchestrator.turn_start` → `ACTIVE_AI_TURN`).
3. `05:45:54` — `BARGE-IN: Sustained speech detected (4 frames)` → `VOICE_USER_START` → interruption cancels run, drains TTS queue.
4. STT captures ambient text: `"okay okay i will check"` — not a directed command to JARV1S.
5. Trigger instance `trg-zocB9Gw1` remains `awaiting_delivery` with `failure_reason: no_audio_sent`.

---

## Built Behavior

```
SpeechProcessor (ACTIVE_AI_TURN)
  └─ VAD: vad_positive_count >= barge_in_min_frames (4)
       └─ SpeechEvent.BARGE_IN_CANDIDATE_STARTED
       └─ turn_buffer seeded with pre-roll; speech-onset offset tracked for speaker scoring

handlers.py
  └─ starts candidate STT without publishing VOICE_USER_START
  └─ sends speech.start {barge_candidate: true}; frontend does not stop playback
  └─ EnrolledSpeakerVerifier.verify_pcm() on onset-forward PCM (at most two attempts)
  └─ short endpoint mismatch waits until max-wait; first negative may rescore once
  └─ barge_in policy decides wait / commit / suppress

commit
  └─ stamps matched owner speaker_id/confidence on VoiceInputTurn
  └─ publishes EventType.VOICE_USER_START (unless session.soft_muted)
  └─ restores candidate VoiceInputTurn after interruption cleanup
  └─ normal endpoint/STT/local-command flow continues

suppress
  └─ closes candidate STT
  └─ discards candidate audio
  └─ retracts provisional candidate transcript (conversation.retract {message_id})
  └─ keeps ACTIVE_AI_TURN and lets delivery continue
```

The orchestrator remains the only hard-cancel path. `VoiceDelivery` drains/cancels only after a committed interruption.

Relevant settings: `VoiceConfig.barge_in_min_frames`, `barge_in_candidate_min_delay_s`, `barge_in_candidate_max_wait_s`, `barge_in_candidate_min_text_chars`, and `barge_in_speaker_threshold` (separate from `wakeword_speaker_threshold`).

---

## Policy

| Situation | Result |
| :--- | :--- |
| Wake-prefixed speech | Commit interruption |
| Exact local control (`stop`, `wait`, `cancel`, `hold on`, `dismiss`, `snooze`) | Commit interruption |
| **Enrolled owner** + endpointed meaningful text | Commit interruption |
| **Enrolled owner** + candidate max wait (match + meaningful text) | Commit interruption |
| **Enrolled** + speaker mismatch / unavailable before max-wait | Wait (keep candidate; avoid short-endpoint churn) |
| **Enrolled** + speaker mismatch / unavailable at/after max-wait | Suppress (after optional one rescore) |
| **Enrolled** + matched but tiny transcript before max-wait | Wait for STT |
| **Enrolled** + matched but tiny transcript at/after max-wait | Suppress (`empty_or_tiny`) |
| **Enrolled** + speaker score still pending | Wait |
| **Not enrolled** + normal answer + endpointed meaningful text | Commit interruption (legacy) |
| **Not enrolled** + normal answer + candidate max wait | Commit interruption (legacy) |
| **Not enrolled** + proactive alert + arbitrary side speech | Suppress |
| Empty/tiny endpoint (after max-wait / non-enrolled) | Suppress |
| Manual STOP | Immediate interruption; bypasses candidate policy |

When an owner profile exists, ambient barge-in requires a positive speaker match (or wake/control). Identity is not addressivity: DDSD remains a later gate for “talking to Jarvis vs talking to someone else.”

Echo caveat: standard TTS usually fails owner match, which helps self-cutoff. Voice-cloned TTS of the owner can still match; do not treat speaker score as an AEC replacement. Speaker scoring uses onset-forward PCM so TTS-contaminated pre-roll is excluded. Short VAD endpoints during duplex audio must not permanently discard the candidate before max-wait.

---

## Files

- `backend/core/voice/processor.py` — emits `BARGE_IN_CANDIDATE_STARTED`; speech-onset peek for speaker scoring.
- `backend/core/voice/speaker_verifier.py` — session-scoped `EnrolledSpeakerVerifier` shared by wake and barge-in, backed by a process-shared immutable extractor.
- `backend/core/voice/wakeword/speaker_verifier.py` — thin wake Stage 2b adapter.
- `backend/core/voice/turn_admission.py` — shared pre-agent admission policy (`decide_barge_in_admission`, fail-open `decide_followup_admission`, `Directedness` seam for future DDSD).
- `backend/api/websockets/handlers.py` — candidate lifecycle, age-gated speaker score + one rescore, commit/suppress telemetry + retract; stamps `admission_source`/`admission_reason` on wake/barge-in/PTT/follow-up.
- `backend/api/websockets/connection.py` — session candidate + shared verifier state; `VoiceInputTurn.admission_*` fields.
- `backend/core/voice/local_commands.py` — exact local controls and wake-prefix normalization.
- `frontend/src/client/JarvisClient.ts` — candidate `speech.start` does not hard-stop playback; `conversation.retract {message_id}` removes provisional candidate rows.

---

## Tests

- `backend/tests/test_turn_admission.py` — pure barge-in + follow-up policy cases
- `backend/tests/test_barge_in_candidate.py` — candidate lifecycle / handoff
- `backend/tests/test_speaker_verifier.py`
- `backend/tests/test_voice_processor.py`
- `backend/tests/test_local_voice_commands.py`
- Existing voice-turn coverage in `backend/tests/test_voice_turn_state.py`
- Speaker-only calibration: `uv run python tools/eval_wakeword.py --speaker-only --tts-echo ...`

---

## Follow-Ups

- Acoustic hygiene / AEC validation — resolved for first-room V1 by routing satellite TTS as 2-channel playback so XVF3800 channel 0 carries the AEC reference.
- Shared turn-admission seam — **shipped**: barge-in is the enforced admission context; `ACTIVE_IDLE` follow-up routes through the same module fail-open (no owner allow-list). `Directedness` is the plug-in point for DDSD.
- DDSD directedness gate — classify whether speech is meant for JARV1S vs side conversation and populate `Directedness` for follow-up (then barge-in when latency allows). Identity ≠ addressivity; multi-user enrollment is separate.
- Voice/agent eval harness — add repeatable regression coverage for barge-in, wakeword tuning, routing, and provider swaps.

---

## Related Docs

- [SYSTEM_STATES.md § Voice Processor](../../SYSTEM_STATES.md) — `ACTIVE_AI_TURN`, candidate/commit/suppress flow.
- [WAKEWORD_ARCHITECTURE.md](../WAKEWORD_ARCHITECTURE.md) — shared enrolled-speaker verifier; wake Stage 2b is an adapter.
- [ROADMAP.md Phase 10](../../ROADMAP.md#phase-10--trusted-daily-use) — remaining Voice Trust work.
- Snapshot API: `POST /api/v1/snapshots/` — captures `trigger_health` + logs for post-incident review.
