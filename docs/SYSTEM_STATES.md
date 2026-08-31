# JARV1S State Reference

## 0. Voice Pipeline — End to End

This section traces a single voice interaction from mic to speaker so you can reason about any point in the pipeline.

```
Browser mic
  │  WebSocket handshake: timezone + stable node_id + capabilities + optional location refs
  │  Optional later: context.update location = ephemeral GPS {lat,lng,source,accuracy,captured_at}
  │  16kHz mono PCM, 1536-sample (~96ms) chunks, base64 over the connection_id socket
  ▼
handle_audio_stream (handlers.py)
  │  Decodes audio, calls processor.add_audio() on every chunk
  ▼
SpeechProcessor.add_audio() (processor.py)   ← stateful hot path
  │
  ├─ PASSIVE mode → WakeWordService (ONNX model)
  │    • Circular buffer holds recent wake audio for pre-roll into the active turn
  │    • Echo cooldown suppresses detection for 0.5s after AI finishes speaking
  │    • On detection: dumps circular buffer → turn_buffer, fires WAKE_WORD_DETECTED
  │
  └─ ACTIVE_IDLE / ACTIVE_AI_TURN → TEN VAD (is_speech: bool per chunk)
       │
       ├─ SpeechTurnPhase.IDLE (waiting for speech to start)
       │    • Speech frames go into vad_buffer
       │    • Requires min_speech_frames (2) or barge_in_min_frames (4) consecutive hits
       │    • On confirmed: pre-roll + vad_buffer → turn_buffer, phase → SPEAKING, fires USER_TURN_STARTED
       │
       ├─ SpeechTurnPhase.SPEAKING (recording)
            • Every chunk appended to turn_buffer
            • Flat VAD silence threshold proposes an endpoint
            • On silence exceeded: phase → ENDPOINT_CANDIDATE, fires one TURN_COMPLETE edge
       │
       └─ SpeechTurnPhase.ENDPOINT_CANDIDATE
            • Processor suppresses duplicate TURN_COMPLETE events while async STT/model work runs
            • New chunks are still appended to turn_buffer and fed to the same STT stream
            • If VAD sees speech resume: phase → SPEAKING, fires TURN_RESUMED
            • If the model says "not done": continue_turn() moves back to SPEAKING
            • If committed: consume_turn_audio() moves back to IDLE

       (between turns, phase = IDLE, mode = ACTIVE_IDLE)
            • On 4s idle with no speech: fires SESSION_ENDED

handler receives SpeechEvent
  │
  ├─ WAKE_WORD_DETECTED  → send speech.start (wake_word=True) to frontend; stamp admission_source=wake
  │
  ├─ USER_TURN_STARTED
  │    ├─ Late-continuation check (see below)
  │    ├─ If soft-muted and not a local command passthrough → drop before VOICE_USER_START
  │    └─ Normal path → send speech.start + publish VOICE_USER_START
  │         └─ orchestrator._handle_interruption: cancels in-flight AI turn if ACTIVE_AI_TURN
  │
  ├─ TURN_COMPLETE
  │    → if barge-in candidate: resolve admission (enforced speaker/controls), then maybe handoff
  │    → else schedule session.endpoint_decision_task and return immediately
  │       • websocket continues ingesting audio during turn_detector_min_delay (0.05s)
  │       • Streaming STT (Cartesia or Apple Speech): StreamingSTTCoordinator receives each new PCM chunk
  │       • audio EOU (LiveKit v1-mini) is the main commit signal (unless Cartesia native turn detection commits on turn.end)
  │       • done: consume_turn_audio(), register endpoint task as accepted_input_task,
  │         finalize streaming STT, merge continuation_prefix, run local commands,
  │         turn admission (reuse wake/barge/PTT stamp, else owner-gated follow-up),
  │         start response_latency, schedule process_turn with transcript text
  │       • not done: processor.continue_turn(), keep the same VoiceInputTurn/buffer/stream
  │       • max delay: commit anyway to avoid hanging
  │
  ├─ TURN_RESUMED
  │    → cancel endpoint_decision_task
  │    → keep the same VoiceInputTurn and turn_buffer (and active streaming STT session when open)
  │    → feed resumed audio into the streaming session when streaming is active
  │
  └─ SESSION_ENDED → publish VOICE_TIMEOUT → orchestrator force_passives + notifies frontend

orchestrator.process_turn()   ← thin caller: ingest + lifecycle
  │
  ├─ set mode → ACTIVE_AI_TURN
  ├─ snapshots presence metadata from the Session (owner_id, connection_id, node_id, location_ref)
  ├─ uses session.voice_turn.turn_id when WebSocket voice already started latency
  ├─ STT: handlers pass final transcript text when already known (streaming finalize at commit)
  │         └─ orchestrator does not re-transcribe when text= is set
  │         └─ send conversation.transcript keyed by the stable voice_turn.turn_id
  │
  ├─ construct VoiceDelivery(session, manager, tts, turn_id, produce_audio)
  ├─ await delivery.start()
  │         └─ assigns session.tts_sentence_queue;
  │             spawns TTS worker if produce_audio
  │
  ├─ execution.execute_turn(...)   ← delivery-agnostic agent loop (orchestrator thin wrapper)
  │    ├─ history.load_turn_history(...) (prior user turns + delivered assistant system tail; first-reply grounding/capability handoff)
  │    ├─ Build context (modality, local_time via build_turn_time_context, user_profile, routed_tools)
  │    Geographic Position (ephemeral GPS / HA home fallback) is separate from
  │    location_ref (durable room binding). Prompt shows availability + room label,
  │    not raw coordinate dumps. Maps/weather resolve "here" at the tool boundary.
  │    ├─ _route_model() → fast or powerful JarvisAgent
  │    └─ async for event in agent.process_stream(...):
  │         ├─ accumulates result.full_response / turn_trace / tools_called
  │         └─ await delivery.on_stream(StreamEvent(tag, content, tool_call_id))
  │                VoiceDelivery dispatches on tag:
  │                • text        → sentence buffer + clause flush + RESPONSE(turn_id + response_id) + TTS queue
  │                • tool_call   → speech gate + pre-tool flush + CODE + rotate response_id
  │                • tool_output → CODE_OUTPUT + STATUS=thinking
  │                • ui_*        → forward envelope to WS
  │                • final_text  → flush remaining buffer, emit final is_partial=false
  │
  ├─ await delivery.aclose(cancelled=...)
  │         └─ None sentinel to queue, join worker,
  │             snapshot session.last_turn_audio_sent / last_turn_audio_completed,
  │             reset session.tts_sentence_queue / first_audio_sent
  │
  ├─ accepted user transcript is upserted to MongoDB under owner_id as `turn_status=pending`
  ├─ _persist_trace(owner_id, source, result.turn_trace, presence_metadata=...) → MongoDB assistant/tool rows
  ├─ user row marked `turn_status=completed`
  ├─ session.voice_turn = None  (on success)
  │
  └─ finally (outer):
       ├─ If no audio sent → `ACTIVE_IDLE`, or `PASSIVE` when soft-muted / user `NO_REPLY`
      ├─ If audio sent → defer mode reset to playback client's matching audio.playback_end
       └─ Clear session.current_run_task, publish VOICE_TURN_END

VoiceDelivery._tts_worker (concurrent task spawned by delivery.start)
  │  generate_audio_stream() → send jarvis_audio chunks with turn_id
  ├─ On first chunk: perf.end("response_latency") + perf.end("turn_latency"), session.first_audio_sent = True, session.active_audio_turn_id = turn_id
  ├─ PerfLogger persists a compact `turn_runs` summary (stage timings, STT counters, turn detection, voice metadata)
  ├─ On normal None sentinel after audio was sent: mark audio.tts_end ready
  └─ Stops on None sentinel, cancelled task, or `VoiceDelivery`'s turn-scoped cancellation signal

Spoken delivery finally (`process_turn` and `_deliver_text`)
  └─ Clears session.current_run_task when this task owns it, then publishes ready audio.tts_end with turn_id
     so an immediate satellite audio.playback_end is treated as final playback, not a mid-turn gap

playback client audio.playback_end
  → backend ignores stale turn_id, otherwise sets mode ACTIVE_IDLE (or stays PASSIVE if `soft_muted`), arms echo cooldown when leaving an AI turn into listening

Satellite playback completion uses two boundaries: `audio.tts_end` means the backend has finished producing the delivery stream for `turn_id`; `audio.playback_end` means the local speaker has actually drained and echoes that `turn_id`. If satellite playback drains but `audio.tts_end` never arrives, the satellite sends a recovery `audio.playback_end` after its per-turn missing-marker timeout so `ACTIVE_AI_TURN` cannot hang indefinitely.
```

**Geographic context vs room presence:** These are separate contracts. Ephemeral GPS (`session.context["location"]` via `context.update`: lat/lng, `source=gps`, optional accuracy/captured_at) is the user's geographic position for weather/maps “here” queries. Durable `location_ref` on the device credential / presence identity is the room binding for smart-home “in here” and proactive delivery routing. `resolve_current_location()` prefers fresh device GPS (`device_kind` is bound into tool context so the policy applies during capability calls); Home Assistant home coordinates are a fallback only for fixed/room-bound endpoints (satellites) and non-interactive turns — not for phone/web/desktop without GPS. Interactive clients send GPS on connect/foreground (desktop via Host Core Location; phone/browser via Geolocation API) and clear session location when GPS is unavailable. Location-aware tools (weather, `plugins/google_maps.py`) resolve omitted location inputs at the tool boundary.

Local voice controls are handled just before turn admission and `process_turn()` scheduling. After final STT, `handlers.py` classifies only exact full-transcript control phrases (for example `Jarvis mute`, `Jarvis power down`, `Jarvis power on`, `Jarvis, you in there?`, `stop listening`, `stop`, `acknowledge`, `dismiss`, `snooze ten minutes`). These commands do not enter the LLM/tool/history path. **Note:** `go quiet` is *not* a local command — it is handled as a normal user turn (typically `attention.set_mode("quiet")`), which updates owner attention only and does **not** set `session.soft_muted`. Exact `mute` / `mute yourself` phrases map to local soft mute plus quiet attention; `power down` enters session-local soft mute and paused attention mode; `power on` exits both and speaks a fixed local acknowledgement through the normal `VoiceDelivery`/TTS path; `Jarvis, you in there?` only uses the special local resume path when attention is currently `paused`; `stop listening` returns to `PASSIVE`; `stop`/acknowledge/dismiss/snooze can act on the latest ackable `TriggerInstance`; non-passthrough turns while soft-muted are dropped silently.

### Turn Vocabulary

| Name | Meaning |
| :--- | :--- |
| `VoiceInputTurn` | The current logical user voice utterance. It owns VAD/STT transcript text, endpoint candidate metadata, stable transcript message id, late-continuation prefix, and optional `admission_source` / `admission_reason` (`wake` \| `barge_in` \| `followup` \| `push_to_talk`). It is cleared when the merged voice input is accepted, dropped, or replaced by a fresh barge-in. |
| `accepted_input_task` | Voice-only post-commit handoff task. Active between endpointing commit and assistant run start: runs STT finalize (Cartesia or Apple Speech stream `finish()`), local-command check, turn admission, and schedules `process_turn()`. Fast recovery **awaits** this task (never cancels mid-handoff — consumed PCM would be lost); then cancels `current_run_task` if the assistant run already started. Cleared by `_resolve_endpoint_candidate`'s finally block. |
| `current_run_task` | The active assistant execution/delivery task. Set by `_commit_voice_turn` (voice), `handle_text_input` (text), and trigger/protocol/prefetch paths. Barge-in and interruption cancel this. Cleared when spoken delivery finishes (`process_turn` / `_deliver_text`). |
| `process_turn()` | The accepted input execution lifecycle: session lookup, STT/text/system ingest, turn lock, agent execution, delivery, persistence, and mode cleanup. |
| `VoiceDelivery` / `HeadlessDelivery` | A delivery attempt for a run. Delivery owns TTS and audio cancellation; it does not own `VoiceInputTurn`. |
| `turn_id` | Correlation key across `VoiceInputTurn`, transcript rows, assistant trace rows, `turn_runs`, and protocol logs. |

### Pending Approval State

Pending approvals are not assistant turns and do not take the session turn lock. A destructive tool call creates a `pending_inputs` row and pushes `PendingInputWidget`; the live Python callback/waiter remains process-local. Foreground approvals are resolved through `approve_pending()` / `deny_pending()` for voice yes/no follow-ups or `resolve_pending_input(input_id, decision)` from the widget. `mode="jarvis"` background tasks use the same contract, but store the visible pointer on `background_tasks.pending_input` and set `attention="approval"` while the task remains `status="running"`. On backend restart, unresolved runtime-bound pending inputs are cancelled because their executable callbacks no longer exist.

### Background Task Terminal States

Delegated work rows in `background_tasks` settle independently of the Home turn lock. `status=completed` and `status=failed` enqueue a title-first `TRIGGER_DUE` follow-up. User `cancel_task` records `status=cancelled` and does not chime. Interrupted local work after a backend restart is `failed` with `interrupted_reason=backend_restart` and stays open/resumable; it is never auto-resumed. `mode="code"` pins `worker_kind` (`cursor_local` or `claude_code`) on the lineage; Conductor-style mid-run questions/approvals remain deferred for code workers.

### What happens to audio, conversation, and UI in each scenario

#### Normal completed turn

| Stage | Audio | Conversation history | Frontend transcript |
| :--- | :--- | :--- | :--- |
| User speaks | Accumulates in `turn_buffer` (seeded from rolling pre-roll on turn start) | — | Cartesia/Apple Speech: blue partials via `conversation.partial` |
| VAD endpoint candidate | `SpeechTurnPhase.ENDPOINT_CANDIDATE`; buffer remains owned by processor while the async endpoint task runs | — | — |
| Endpoint window | New PCM continues into `turn_buffer`; streaming STT fed when active; resumed speech cancels the endpoint task | — | Partials can continue updating |
| Turn detector commits | `consume_turn_audio()` extracts PCM; endpoint task registered as `accepted_input_task` and runs streaming STT finalize | — | — |
| STT finalizes | Streaming STT: `finish()` + merged transcript, websocket closed in background | User row upserted as `turn_status=pending` after admission | User message added (`conversation.transcript`) |
| LLM streams | — | — | Assistant text streams in (`conversation.response` with whole-turn `turn_id` and segment `response_id`). On text/screen turns, provider reasoning streams separately on `conversation.reasoning` (collapsible transcript row; never spoken on audio-bound voice turns). |
| TTS plays | Audio chunks sent to playback client with `turn_id`; `audio.tts_end` marks backend producer completion | — | — |
| Assistant completes | — | Assistant/tool rows stored; user row marked `completed` | — |
| `playback_end` | — | — | Matching `turn_id` mode → ACTIVE_IDLE (or `PASSIVE` if `soft_muted`, so soft mute persists after an AI turn); stale ids are ignored |

---

#### Late continuation (user resumes speaking within 2s after commit)

Trigger: `USER_TURN_STARTED` fires while the same `VoiceInputTurn` is active and `elapsed < VOICE.fast_recovery_window` (default 2s). If post-commit handoff is still running (`accepted_input_task`), fast recovery awaits it to capture finalized segment text, then cancels `current_run_task` if the assistant run already started. If handoff finished, it cancels `current_run_task` directly.

The in-flight accepted turn work runs concurrently during the window — STT finalize, DB load, LLM streaming, and TTS can all progress before the user resumes. If the user resumes inside the window, speculative assistant work is cancelled, but the user turn remains the same logical `VoiceInputTurn`.

| What | Outcome |
| :--- | :--- |
| In-flight handoff / run | Handoff still active: **await** `accepted_input_task` (streaming STT finalize — PCM already consumed). Then `current_run_task.cancel("fast_recovery")` if the assistant run started. Handoff done: cancel `current_run_task` only |
| Partial LLM response on frontend | Retracted via `conversation.retract` using `voice_turn.turn_id` plus the captured `current_delivery.response_id` when one exists; the browser removes assistant text rows for that turn only |
| `turn_trace` at cancellation | Empty or partial |
| DB writes | Same pending user row updated on merge because `turn_id` is preserved |
| Frontend status | `speech.start` sent immediately → UI snaps to `listening` |
| `voice_turn.continuation_prefix` | Set to transcript text from the first segment |
| MLX segment UI | First segment re-shown as blue `conversation.partial` (interim), not a committed final |
| STT (Cartesia) | Fresh stream for continuation audio in `turn_buffer` |
| STT (Apple Speech) | Next commit finalizes only the new segment; prefix merge preserves prior words |
| Transcript merge | `continuation_prefix` + new segment text, with word-overlap dedupe |
| Frontend transcript | Reuses `voice_turn.turn_id` — one row updates in place |
| LLM context | One merged user message — no trace of the cancelled assistant attempt |

---

#### Barge-in candidate (user speaks while AI is talking — ACTIVE_AI_TURN)

Trigger: sustained VAD emits `BARGE_IN_CANDIDATE_STARTED`. The backend starts STT and a short candidate window without publishing `VOICE_USER_START`. When an owner speaker profile is enrolled, the shared `EnrolledSpeakerVerifier` scores the first 0.8s of onset speech (max cosine vs enrollment plus the optional per-node room vector). Empty or sub-0.4s PCM is unscorable, not a mismatch. Short VAD endpoints do not terminal-suppress speaker mismatch or unscorable audio before `barge_in_candidate_max_wait_s`; a first negative may be rescored once over accumulated PCM at max-wait (at most two inferences).

| Candidate outcome | Outcome |
| :--- | :--- |
| Suppress | Candidate STT is closed, candidate audio is discarded, provisional candidate transcript is retracted via `conversation.retract {message_id}`, mode remains `ACTIVE_AI_TURN`, and the active delivery continues. Enrolled sessions suppress speaker mismatch / verifier failure only after max-wait (optionally after one rescore). Without enrollment, proactive alerts suppress arbitrary endpointed side speech unless it is wake-prefixed or an exact local control. |
| Commit | `VOICE_USER_START` is published and `_handle_interruption` runs. Wake-prefix and exact local controls always commit. Enrolled owner match commits only with meaningful text on endpoint or max wait (including proactive). Matched-but-tiny transcripts wait for STT until max-wait, then suppress as `empty_or_tiny` (including soft-wait resolves with `endpointed=False`). Without enrollment, normal answers still commit on endpointed text / max wait. Matched owner identity is stamped on `VoiceInputTurn` (`speaker_id`, `speaker_confidence`, `speaker_source="barge_in"`) along with `admission_source="barge_in"` and the policy reason. When commit lands while already `ENDPOINT_CANDIDATE` (direct VAD, soft-wait, or endpointed max-wait), the same path publishes `VOICE_USER_END` and schedules exactly one normal endpoint decision so STT finalizes; mid-speech commits (`SPEAKING`) keep listening until a silence edge; push-to-talk bypasses the scheduler and finalizes directly. |

Admission after final STT: wake / barge-in / push-to-talk stamps are reused and not re-evaluated. Ordinary `ACTIVE_IDLE` follow-ups call `decide_followup_admission`: enrolled owner match commits (including short `yes`/`no`); mismatch suppresses and retracts; too-short/unscorable clips fail-open (`followup_unscorable`); no owner profile stays fail-open. Wake-prefix is not an identity bypass on follow-up. Future DDSD populates `Directedness` for owner side-speech; it must not replace the owner allow-list.

#### Committed barge-in

| What | Outcome |
| :--- | :--- |
| TTS sentence queue | Drained immediately (`drain_sentence_queue`, from `core/turns/delivery.py`) — queued sentences discarded |
| In-flight `process_turn` task | Cancelled via `task.cancel()` |
| `system.stop` | Sent to frontend — browser stops audio playback immediately; may include `turn_id` and `response_id` so streamed assistant text for the interrupted turn is finalized visually |
| Mode | ACTIVE_AI_TURN → ACTIVE_IDLE (set immediately, not deferred, to avoid the playback_end race) |
| Echo cooldown | Armed (0.5s) |
| `turn_trace` at cancellation | May contain the user message + partial assistant response (whatever LLM had streamed) |
| DB writes | **Partial save**: user message stored; assistant partial saved with `interrupted: True` metadata |
| Frontend transcript | Assistant response stays visible (whatever had streamed); if `turn_id` is present, the frontend marks assistant text rows for that turn non-partial so they do not remain in the blue streaming state |
| `voice_turn.turn_id` | Preserved only for fast recovery. A committed barge-in starts a fresh `VoiceInputTurn`, so the next independent user turn creates a fresh transcript bubble |
| LLM context | Next turn loads from DB — sees the user message and the partial assistant response |
| New user turn | Starts fresh from the new speech; no audio prepending |
| Endpoint handoff | If the processor is already `ENDPOINT_CANDIDATE` at admission, publish `VOICE_USER_END` and schedule the normal `endpoint_decision_task` once (same helper as non-barge VAD endpoints). Soft-wait / max-wait commits must not leave an orphaned candidate stream open. |

---

#### Stop button / mute mid-response

Trigger: `VOICE_INTERRUPT` or `audio.mute` WS message → `_handle_interruption` or `force_passive`.

| What | Outcome |
| :--- | :--- |
| TTS sentence queue | Drained |
| Task | Cancelled |
| `system.stop` | Sent to frontend |
| Mode | → PASSIVE (force_passive), echo cooldown armed |
| DB writes | Same partial save as barge-in |
| `voice_turn` | Discarded by the WebSocket voice layer before interruption is published |
| Reactivation | Wake word required to re-enter conversation |

---

#### Local voice controls

Trigger: final STT transcript exactly matches a local command before `process_turn()` is scheduled.

| Phrase | Outcome |
| :--- | :--- |
| `Jarvis mute` / `mute yourself` (exact phrase after normalization) | Stops any current frontend playback, sets owner attention to `quiet`, enters session-local soft mute; no LLM turn, no spoken reply, processor → `PASSIVE` |
| `Jarvis unmute` / `Jarvis resume` | Exits soft mute; speaks fixed local acknowledgement `Online.` via `VoiceDelivery`; no LLM/history turn |
| `Jarvis power down` / `Jarvis shut down` | Stops current frontend playback, enters session-local soft mute, sets global attention to `paused`; no LLM turn, no spoken reply, processor → `PASSIVE` |
| `Jarvis power on` / `Jarvis come online` | Sets global attention to `active`, exits soft mute, speaks fixed local acknowledgement `Online.` via `VoiceDelivery`; no LLM/history turn |
| `Jarvis, you in there?` | If global attention is `paused`, sets attention to `active`, exits soft mute, and says `For you sir, always.`; otherwise falls through as a normal turn |
| `Jarvis sleep` / `Jarvis stop listening` | Processor → `PASSIVE`; wake word required for the next normal turn |
| `stop` / `dismiss` / `acknowledge` | Stops frontend playback and acknowledges the latest ackable trigger instance, if one exists |
| `snooze ...` | Snoozes the latest ackable trigger instance by the requested duration, if one exists |
| Any non-passthrough phrase while soft-muted | Dropped silently; no LLM, TTS, or history write |

Local command matching is full-transcript only after normalization and optional wake-prefix stripping. It does not match substrings, so `Jarvis mute this person on Instagram` remains a normal user turn instead of muting JARV1S.

---

#### Cancelled turn with no content (STT returned empty)

| What | Outcome |
| :--- | :--- |
| `turn_trace` | Empty |
| `cumulative_response` | Empty |
| DB writes | Nothing — the guard `if partial and not already_saved` prevents empty saves |
| Frontend | Status → `listening`, no transcript change |

---

### Session State Fields (connection.py)

| Field | Purpose |
| :--- | :--- |
| `processor` | Per-session `SpeechProcessor` — owns all audio state |
| `voice_turn` | Current `VoiceInputTurn` (`turn_id`, latest transcript text, continuation prefix, monotonic endpoint timing, endpoint candidate text length, stable transcript message id, optional `admission_source` / `admission_reason`). This survives late continuation and is cleared only when the merged voice input is done. |
| `endpoint_decision_task` | Cancellable task that resolves a VAD endpoint candidate after `turn_detector_min_delay`. It owns the pre-submit endpoint window and is cancelled when speech resumes before commit. |
| `accepted_input_task` | Voice-only post-commit handoff task. Active in the narrow window between endpointing commit and assistant run start: runs final STT flush, local-command check, turn admission, and schedules `process_turn()`. Late continuation awaits this (does not cancel mid-handoff), then cancels the assistant run if needed. |
| `current_run_task` | asyncio Task for the active assistant execution/delivery run. Set by voice commit, text input, and trigger/protocol/prefetch paths. Barge-in and interruption cancel this. Cleared when spoken delivery finishes (`process_turn` / `_deliver_text`). |
| `stt_stream` | Active per-`VoiceInputTurn` `StreamingSTTCoordinator` for Apple Speech or Cartesia. Voice turns require this stream; batch transcription is reserved for offline eval tooling. |
| `soft_muted` | Session-local voice input cache. When true, normal post-wake transcripts are dropped before the orchestrator and before `VOICE_USER_START`; local unmute/resume plus stop/ack/snooze passthrough commands still run. Does **not** control proactive system output — use `AttentionMode` (persisted to MongoDB via `attention_service`) for that. Reset to `False` on session reconnect. |
| `tts_sentence_queue` | Pipe between LLM streamer and TTS worker; drained on interruption |
| `first_audio_sent` | True once TTS audio reaches the frontend during the in-flight turn. Reset by `aclose`. |
| `last_turn_audio_sent` | Snapshot of `first_audio_sent` written by `aclose` before the in-flight reset. Survives turn boundaries so post-turn consumers (trigger delivery finalization) can determine whether audio actually reached the user. |
| `active_audio_turn_id` | `turn_id` for the latest TTS audio stream awaiting `audio.playback_end`; used to ignore stale playback completion from older turns |
| `current_delivery.response_id` | UUID of the active `conversation.response` stream segment. Captured with `current_delivery.turn_id` before `task.cancel()`; `turn_id` groups turn-scoped client cleanup, while `response_id` preserves segment identity after tool-call rotations |

### Turn Telemetry

`services.perf.PerfLogger` is an in-process collector, not a file logger. Existing `perf.start()` / `perf.end()` calls build:

- the live `diagnostics.turn` payload used by the frontend performance menu;
- compact `turn_runs` MongoDB documents keyed by `(owner_id, turn_id)`.

`turn_runs` stores operational telemetry only: stage timings, response/turn latency, source/modality/delivery, STT counters, turn-detector reason/confidence, committed audio duration, transcript length, late-continuation recovery metadata, and small presence identifiers. It does **not** store prompt text, transcript text, tool output, or audio. User-visible content remains in `conversations`, linked by `metadata.turn_id`.

### Operations Visibility

The visible Operations surface uses three read paths:

- `/api/v1/activity/` and `jarvis.activity.recent` return a cheap timeline over headless turns, background tasks, and trigger/automation run envelopes. User-initiated conversation turns are excluded by default (`include_user=false`); they remain an opt-in facet. These rows are summary pointers and intentionally do not copy trace blobs.
- `/api/v1/activity/page` is the Operations workspace timeline. **All** merges reminders, automations, tasks, and system runs only; pass `category=conversation` for the Conversations facet. The Operations overlay filters this list (`all`, `reminder`, `automation`, `system`, `conversation`).
- `/api/v1/operations/runs/{instance_id}` returns the drill-down read model for trigger/automation runs. It loads `trigger_instances.id`, follows `trigger_instances.turn_ids`, and joins `conversations` (`metadata.turn_id`), `turn_runs` (`turn_id`), and `protocol_runs` (`turn_id`) into grouped attempts.
- `/api/v1/schedules/`, `/api/v1/automations/`, and `/api/v1/protocols/` return definition summaries for the Operations **Definitions** tab (one category visible at a time: Schedules, Automations, or Protocols). Schedules default to enabled rules only; `?include_disabled=true` includes disabled definitions.

When scheduler or automation tools change definitions, the backend publishes `operations.changed` (`scope`: `schedules` | `automations` | `protocols`). Connected clients bump a scoped version counter and refetch the open definition list — no transcript parsing on the frontend.

The durable stores remain separate by responsibility: `trigger_rules` owns scheduled rules and external automation definitions, `trigger_instances` owns lifecycle, `conversations` owns user-visible trace content, `turn_runs` owns performance telemetry, and `protocol_runs` owns protocol lifecycle. The old `alerts`, `schedules`, and standalone `automations` Mongo collections are not part of the active schema.



## 1. Host and Connection (Infrastructure)

Host reachability and UI transport are separate state axes:

| Axis | Source | States | UI meaning |
| :--- | :--- | :--- | :--- |
| Host | Tauri `host-launch-update` / `get_launch_state`; browser `/api/v1/health` probe | `unknown`, `online`, `degraded`, `offline` | Trust pill shows `Host offline` when the backend is unavailable |
| UI WebSocket | `WebSocket.readyState` + reconnect attempts | `disconnected`, `connecting`, `connected`, `error`, `reconnecting` | Trust pill shows `Disconnected` / `Reconnecting…` when the Host may still be running |

Uvicorn runs the `websockets` implementation with protocol ping interval/timeout set to 20 seconds. Protocol ping/pong is authoritative while a page is hidden because it does not depend on throttled JavaScript timers. The application `system.ping` heartbeat runs only while visible, closes a zombie socket after three consecutive missed pongs, and supplies latency/diagnostic data. `visibilitychange`, focus, `pageshow`, and network `online` resume the visible heartbeat and immediately reconnect a stale socket.

Every successful reconnect receives a fresh `system.connect` payload (`attention`, session state, preferences) and `ui.snapshot`. The client discards transient listening/speaking/playback state from the disconnected session before applying this server truth.

## 2. Agent (Cognition)
**Source:** `WSMessageType.STATUS` (Backend: `stage`) + frontend audio playback events

| State | Label | Visual | Notes |
| :--- | :--- | :--- | :--- |
| **`idle`** | Ready / Voice active / Mic stalled | Steady dim green (warning when stalled) | **Wake Word Only.** (Processor: `PASSIVE`). Trust pill shows `Ready` until the mic pipeline is live, then `Voice active`. If the mic is claimed but PCM is not flowing, trust shows `Mic stalled` instead of lying with `Voice active` / `Listening`. Privacy/attention can replace this with `Mic muted`, `Voice muted`, `Quiet mode`, or `Paused`. |
| **`waking`** | Detected | Rapid pulse blue | Wake word detected. Transitioning to `ACTIVE_IDLE`. |
| **`listening`** | Listening | Steady blue glow | **VAD Active.** Times out after 8s of silence. |
| **`transcribing`** | Transcribing | Pulsing blue | STT processing voice to text. |
| **`thinking`** | Thinking | Pulsing blue | Delivery progress while the LLM is working (not provider reasoning content). Summarized reasoning streams on `conversation.reasoning` for text clients only. |
| **`composing_tool`** | Thinking | Pulsing blue | Tool-call composition subphase of thinking. Frontend live-stage and trust pill collapse this into `Thinking`. |
| **`running_tool`** | Working | Pulsing blue | Tool code executing. |
| **`speaking`** | Speaking | Pulsing green | TTS audio streaming. The backend emits this when the first sentence enters the TTS queue; local `isSpeaking` reflects actual client playback. Rendered audio is authoritative for the live stage and trust pill, so both remain `Speaking` if backend work advances or a provisional barge-in candidate appears before playback stops. Candidates do not become `Listening` until committed. |

**Visual rule:** Pulse = system is actively doing work. Steady = system is waiting.

## 3. Microphone (Privacy)
**Source:** `AudioContext.state` + `store.isMuted`

| State | Condition | UI Button (Zone 3) |
| :--- | :--- | :--- |
| **`dormant`** | `!AudioContext` | **WAKE** (Teal Outline) |
| **`active`** | `!isMuted` | **MUTE** (Teal Solid) |
| **`muted`** | `isMuted` | **RESUME** (Red Solid) |

**Hard mute behaviour:** UI/hardware mute sends `audio.mute` to the backend, releases/stops the browser mic, and calls `force_passive()` — transitioning the processor to `PASSIVE` and arming the echo cooldown. The per-node hard-mute flag is persisted locally, restored before audio initialization, and re-sent after reconnect; a reload or hidden-window reconnect cannot silently reacquire the microphone. On hard unmute, the wake word is required to re-enter conversation mode.

**Always-listen contracts (house-first):** Satellites are the always-on room ears/mouth. Desktop WebView capture is focused best-effort only — not always-listen. Phone/browser are companions (PTT / text / short interactive capture). “Accessible anytime in the house” requires a live satellite, not a backgrounded WebView. Desktop mic honesty: on visibility/focus the client resumes the capture `AudioContext` and reacquires the mic when the track ended or PCM stalled; the trust pill must not show `Voice active` or `Listening` while `audioDevices.captureStalled` is true. Desktop mute does not disable satellite delivery or presence routing.

**Desktop / WKWebView playback session:** On Tauri (macOS WKWebView), prolonged background/idle or some OS audio-session interruptions can tear down the native CoreAudio output under the webview. WebAudio may still report `state=running` and fire buffer `onended` (`playback_summary` `outcome=render_completed`) while no sound reaches the speakers — and recreating `AudioContext` / a JS reload does not heal it. The desktop app mounts a process-lifetime silent looping HTMLAudioElement (`runtime/audioSessionKeepAlive`) at App start to hold the session open across mute/stop and background/focus; it is independent of mic capture. A full app relaunch remains the last-resort recovery if the session was already dead before keep-alive started.

**Soft mute behaviour:** Saying `Jarvis mute` (or the exact `mute yourself` phrase) keeps the mic/wake pipeline available but marks the live session as soft-muted (`session.soft_muted = True`) and sets the owner's global attention mode to **quiet**. Saying `Jarvis power down` uses the same input gate but sets global attention to **paused**, meaning proactive user-facing presentation is deferred until resume. JARV1S ignores every post-wake transcript except `Jarvis unmute` / `Jarvis resume`, `Jarvis power on`, `Jarvis, you in there?`, `Jarvis power down`, and trigger-control passthrough commands such as stop, acknowledge, dismiss, or snooze. While soft-muted, the rolling wake pre-roll buffer is not retained (`retain_preroll=False` in the audio handler), so ambient conversation is not seeded into the next wake turn. `session.soft_muted` is a session-local input gate only and does not survive reconnection; the global attention mode is persisted to MongoDB.

**LLM tools:** `attention.set_mode("quiet")` changes owner attention only (DND for proactive delivery) and does **not** soft-mute the session. For an explicit “mute yourself” intent from the agent, use `attention.mute()` (quiet + same session soft mute as local mute). `set_mode("active")` and `resume()` clear soft mute on the live connection when present.

**Attention modes (global, owner-level):** Three modes control proactive output independently of input:

| Mode | Effect |
| :--- | :--- |
| `active` | Default — proactive execution and presentation run normally |
| `quiet` | DND — `urgent`/`critical` user-facing presentation breaks through; `normal` is deferred |
| `paused` | Full pause — all proactive user-facing presentation is deferred until explicitly resumed; manual user turns still work |

**Priority is one axis of four.** A trigger carries (1) **priority** — `AttentionPolicy.level ∈ {normal, urgent, critical}` — the *only* input to the quiet/paused gate; (2) **decision** — `TriggerAction.decision ∈ {tell, offer, act}` — whether the user should hear from JARV1S; (3) **routing** — `DeliveryPlan` (`channel`, `target`) for physical endpoints; and (4) **presentation** — `sound` / `requires_ack`, derived from presets. Shared priority semantics live in `core/triggers/priority.py`; `resolve_trigger_delivery` maps attention mode to a floor: `active` admits everything, `quiet` admits `urgent`+, `paused` admits nothing.

Two growth paths are designed but deliberately *not* stubbed in code (re-surfacing of deferred items, and fire-time channel/presence resolution for multi-room + iOS push). See [proposals/built/ATTENTION_GATE_AXES.md](proposals/built/ATTENTION_GATE_AXES.md).

Set via `jarvis.attention.set_mode()` / `resume()` (LLM tools), `jarvis.attention.mute()` when the user wants the assistant to stop listening on this node, or fast-tracked from local commands (`Jarvis mute` → quiet + soft mute, `Jarvis power down` → paused + soft mute, `Jarvis power on` / `Jarvis unmute` → active + clear soft mute). Proactive trigger routing is resolved by `core.triggers.delivery_policy.resolve_trigger_delivery()`, which returns channel-neutral execution/presentation semantics:

**Scheduled quiet windows:** Recurring DND windows live in MongoDB `attention_schedules` as `QuietWindow`s. The effective mode is *derived* on read by `resolve_effective_attention()` (a live `ManualOverride` wins, else an active quiet window, else active) — it is never stored authoritatively. `AttentionReconcileService` (startup, window CRUD, 60s poll) only recomputes and emits `ATTENTION_CHANGED` on a mode transition. Windows use local wall-clock start/end times with optional day filters; overlapping enabled windows coalesce into one continuous quiet span. `jarvis.attention.set_quiet_window` / `list_quiet_windows` / `clear_quiet_window` manage them. A manual `set_mode("active")` or `resume()` during a window writes an override bounded to the current window end, so the user can temporarily unmute without deleting the window. Timed `set_mode("quiet")` / `set_mode("paused")` expire via the override's own `expires_at`. Background mechanical services such as `SystemPulse` keep running; quiet/paused only affects whether proactive user-facing presentation is allowed at trigger delivery time.

| `TriggerAction.decision` | Agent execution | Presentation | Successful settlement |
| :--- | :--- | :--- | :--- |
| `tell` | `user_facing` | `always` | `delivered` after audio starts and delivery closes without TTS failure; no-audio/TTS-failed/offline stays `awaiting_delivery` |
| `act` | `headless` | `never` | `completed`; no user-facing delivery was requested |
| `offer` | `headless` | `if_content` | `suppressed` on `NO_REPLY`; `delivered` after `_deliver_text()` completes; `awaiting_delivery` on `DEFER`, blocked, interrupted, or failed presentation |

After a live-routed `tell` or spoken `offer` settles as delivered, its routed plugins become one-shot candidates for the next routed user turn on the same `connection_id`. The user transcript still routes normally, manifest budgets still apply, and the agent resolves meaning from delivered conversation history. A newer delivered proactive turn replaces an older pending handoff. System turns and local control commands do not consume it. Silent, suppressed, deferred, no-audio, TTS-failed, fixed local, and prefetched protocol deliveries do not create one. `mark_delivered()` settlement is authoritative, so a local stop/dismiss that settles the instance first also prevents handoff.

`decision="offer"` runs headless with `presentation=if_content`, settles as `suppressed` on `NO_REPLY` with an auditable `failure_reason`, marks `awaiting_delivery` on `DEFER` or `DEFER_UNTIL` (validated `next_retry_at`), marks `delivered` if content presentation completes, and stays `awaiting_delivery` if presentation is blocked, interrupted, or sends no audio.

`FreshnessPolicy` is checked before both first delivery and `awaiting_delivery` replay. Explicit `expires_at`, `expire_after_due_s`, or `stale_if_source_event_started` policies classify stale work before attention/presentation routing runs; `on_expiry="expire"` drops it as `expired`, while `on_expiry="force_deliver"` prevents final `NO_REPLY`/`DEFER` and speaks the trigger message as the deadline backstop. `calendar_event_started` is the exception: stale pre-start calendar content expires rather than being force-delivered. Missing freshness is invalid trigger data.

`awaiting_delivery` is for system-side non-delivery and retryable offer deferral: no session, attention deferral, offline/reconnect, audio never reaching the user, or semantic `DEFER`/`DEFER_UNTIL`. Availability blockers retry on `SESSION_CONNECTED` / `ATTENTION_CHANGED -> active`; semantic defers retry when `SystemPulse` wakes rows whose `next_retry_at` is due. Explicit user STOP during an active announce trigger dismisses that trigger instead of leaving it retryable (`acknowledged` for ackable timer/alarm-style instances, otherwise `cancelled`). Hard mic mute remains a privacy/input state, not a trigger acknowledgment.

The first gate runs in `AssistantOrchestrator._handle_trigger_due` before execution begins. `quiet` defers normal `tell` presentation and lets `urgent`/`critical` through; `paused` defers all proactive presentation. `act` triggers still run headless so background work can complete without interrupting. If an `offer` run produces speakable content, final presentation is re-checked against the same priority rules before `_deliver_text()`.

## 4. Backend Voice Processor (`VoiceMode`)

The processor has two explicit state layers:

- `VoiceMode` owns the session-level conversation mode.
- `SpeechTurnPhase` owns the current user audio-turn lifecycle inside active modes.

```python
class VoiceMode(Enum):
    PASSIVE        = auto()  # Waiting for wake word
    ACTIVE_IDLE    = auto()  # Conversation window open, waiting for user speech
    ACTIVE_AI_TURN = auto()  # AI speaking/processing — barge-in threshold raised

class SpeechTurnPhase(Enum):
    IDLE               = auto()  # No user audio turn is being captured
    SPEAKING           = auto()  # Capturing audio for current user turn
    ENDPOINT_CANDIDATE = auto()  # VAD fired; detector/commit decision pending
```

| Mode | Wake Word | VAD Threshold | Activity Timeout | Echo Cooldown |
| :--- | :--- | :--- | :--- | :--- |
| **`PASSIVE`** | Active | — | — | Applied (post-speech) |
| **`ACTIVE_IDLE`** | Off | `min_speech_frames` (2) | Active (8s) | — |
| **`ACTIVE_AI_TURN`** | Off | `barge_in_min_frames` (4) starts a barge-in candidate | Suspended | Armed on exit |

### Mode Transitions

| Trigger | From | To |
| :--- | :--- | :--- |
| Wake word detected | `PASSIVE` | `ACTIVE_IDLE` |
| Turn begins (audio/system) | `ACTIVE_IDLE` | `ACTIVE_AI_TURN` |
| Turn ends (playback_end, no audio sent) | `ACTIVE_AI_TURN` | `ACTIVE_IDLE` (or `PASSIVE` if `soft_muted`) |
| User turn `NO_REPLY` (no audio sent) | `ACTIVE_AI_TURN` | `PASSIVE` (skip listening window; wake word required) |
| Activity timeout (8s silence) | `ACTIVE_IDLE` | `PASSIVE` |
| Mute / power down / stop / session end | any | `PASSIVE` |
| Committed interruption (barge-in / cancel) | `ACTIVE_AI_TURN` | `ACTIVE_IDLE` |
| Local soft mute | any | `PASSIVE` |
| Local unmute | `PASSIVE` | `ACTIVE_IDLE` + fixed acknowledgement |

**Echo cooldown:** When leaving `ACTIVE_AI_TURN` after assistant audio, a short cooldown is armed to prevent residual speaker audio from triggering the wake word detector. A user-turn `NO_REPLY` skips it because no TTS was played.

**Endpointing rule:** VAD silence creates an endpoint candidate, not a final turn. `SpeechTurnPhase` makes this edge-triggered so repeated silent chunks cannot double-submit the same user turn while STT finalization or model EOU detection is running. While `endpoint_decision_task` is pending, the processor keeps appending audio; Cartesia streaming also receives each chunk. Resumed speech emits `TURN_RESUMED`, cancels the endpoint task, and returns to `SPEAKING`; `TurnDetector` can reject the candidate via `continue_turn()`. Wake-opened turns (`VoiceInputTurn.from_wake`) additionally refuse commit while the transcript has no request content after wake/filler normalize (`wake_followon_pending`); after `turn_detector_max_delay` they idle-settle in `ACTIVE_IDLE` without an LLM turn so follow-on speech does not need a second wake.

### Key Flows

**Normal voice turn:**
`PASSIVE` → wake word → `ACTIVE_IDLE` + `SPEAKING` → VAD endpoint candidate + async endpoint decision window → TurnDetector commit → STT final → `ACTIVE_AI_TURN` → LLM → TTS → playback_end → `ACTIVE_IDLE` + `IDLE` → (8s) → `PASSIVE`

**Wake then pause before command:**
`PASSIVE` → wake → `from_wake` turn with vocative-only / empty request text → endpoint → `wake_followon_pending` (same turn/buffer/STT) → user resumes → `TURN_RESUMED` → commit when non-wake content exists. If no follow-on before max delay → idle-settle (`wake_followon_timeout`), stay `ACTIVE_IDLE` + `IDLE`.

**Pause resumes before commit:**
`SPEAKING` → VAD endpoint candidate → `endpoint_decision_task` pending while audio continues (Cartesia stream when active) → resumed speech → `TURN_RESUMED` → cancel endpoint task → same `VoiceInputTurn` / same `turn_buffer` → `SPEAKING`

**Mute during speech:**
`ACTIVE_AI_TURN` → mute → `force_passive()` → `PASSIVE` + echo cooldown armed → wake word detection active on unmute

**Barge-in candidate (user may be interrupting AI):**
`ACTIVE_AI_TURN` → sustained VAD (4 frames) → `BARGE_IN_CANDIDATE_STARTED` → candidate STT + frontend `speech.start {barge_candidate: true}` → deterministic policy commits or suppresses

**Committed barge-in:**
candidate commit → `VOICE_USER_START` → cancel turn → `ACTIVE_IDLE` → if already `ENDPOINT_CANDIDATE`, `VOICE_USER_END` + schedule endpoint decision → otherwise keep listening until silence → finalize STT as the new user turn

**Suppressed barge-in:**
candidate suppress → close candidate STT → discard candidate audio → stay in `ACTIVE_AI_TURN` → delivery continues

**Late continuation (user resumes within 2s after endpoint commit):**
`ACTIVE_IDLE` → endpoint accepted → streaming STT finalize and/or LLM/TTS run concurrently → user speaks again within window → `USER_TURN_STARTED` → fast recovery (await handoff finalize, blue partial for prior segment when applicable) → retract partial assistant if needed → `listening` → `continuation_prefix` → new audio transcribed/merged into same user turn

**Stop button:**
`ACTIVE_AI_TURN` → stop → `VOICE_INTERRUPT` + `force_passive()` → `PASSIVE`

**Local soft mute / power down / resume:**
`PASSIVE` → wake word → `ACTIVE_IDLE` + speech → STT final `Jarvis mute` (or exact `mute yourself`) or `Jarvis power down` → soft-muted + `PASSIVE` (`quiet` for mute, `paused` for power down) → wake word → STT final `Jarvis unmute`, `Jarvis power on`, or paused-only `Jarvis, you in there?` → soft-muted cleared + fixed acknowledgement → `ACTIVE_IDLE`

**Soft-muted trigger control:**
`ACTIVE_AI_TURN` proactive delivery → user says `stop` / `dismiss` / `snooze` → local command passthrough runs without a normal LLM turn → playback stops and the latest ackable `TriggerInstance` is acknowledged or snoozed

**Proactive trigger delivery:**
`TriggerInstance.pending` → atomically claimed → freshness check → `resolve_trigger_delivery()` (combines owner `AttentionMode`, `AttentionPolicy.level`, and `TriggerAction.decision`) → blocked user-facing presentation moves to `awaiting_delivery` → allowed work moves to `executing` → settlement follows the resolved presentation mode. Local `stop` / `dismiss` / `snooze` may settle the instance first and delivery finalization will not overwrite it.

`TriggerAction.decision` maps to execution: `act` → `headless`, `presentation=never`; `tell` → `user_facing`, `presentation=always`; `offer` → `headless`, `presentation=if_content`. `DeliveryPlan` holds routing hints for future channel resolution. A future channel router can map user-facing presentation to voice, Telegram, in-app, etc. without changing trigger policy.

Prompt assembly keeps trigger data on separate surfaces:
`action.reply_grounding` renders as scalar data at fire time and beside the
settled assistant turn for its first user reply (scalars only; no separate
item/character cap); selected `instance.source_event`
data renders as source/resource context; internal `management` ownership is never
shown; and offer-only live state renders as `CURRENT_STATE`. Grounding never
enters routing text. Persisted routed-tool metadata separately restores the
one-reply capability handoff after a same-node reconnect.

For authoring guidance across scheduler, automations, habits, offers, and the low-level rules facade, see [`TRIGGER_AUTHORING.md`](./TRIGGER_AUTHORING.md).

**Evaluative trigger:**
`TriggerInstance.pending` → atomically claimed → freshness check → headless evaluation → `NO_REPLY` marks `suppressed` with reason `offer_no_reply` or `evaluate_no_reply`; `DEFER`/`DEFER_UNTIL` marks `awaiting_delivery` with reason `offer_deferred` and validated `next_retry_at`; non-sentinel content is checked by `resolve_proactive_speech_delivery()` before presentation; presented output marks `delivered` only after `_deliver_text()` completes; blocked/no-audio/interrupted presentation marks `awaiting_delivery`; evaluation crash marks `failed`. Offer triggers (`decision="offer"`) also receive live alarm/timer commitment context from `offer_context.py`; conversation history comes from the normal headless turn path, not a separate offer history block.

## State Matrix (Valid Combinations)

| Context | Connection | Agent | Mic | Processor Mode | Zone 1 Output | Zone 3 Action |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Offline** | `disconnected` | `idle` | `dormant` | — | Disconnected / Host offline | **Connect** |
| **Privacy Ready** | `connected` | `idle` | `dormant` | `PASSIVE` | Ready · time secondary | **Start voice** |
| **Active Ready** | `connected` | `idle` | `active` | `PASSIVE` | Voice active · time secondary | **Mute mic** |
| **Listening** | `connected` | `listening` | `active` | `ACTIVE_IDLE` | Listening · time secondary | **Mute mic** |
| **Processing** | `connected` | `thinking/speaking` | `active` | `ACTIVE_AI_TURN` | Thinking / Working / Speaking · time secondary | **Mute mic** |
| **Hard Mute** | `connected` | `idle` | `muted` | `PASSIVE` | Mic muted · time secondary | **Unmute mic** |
