# JARV1S Voice Evals

Use this as the active voice-quality and latency workflow. Keep it small: each eval should answer one question quickly, using fixed inputs and repeatable output.

For the on-device streaming STT architecture and evaluation constraints, read [LOCAL_STREAMING_STT.md](./LOCAL_STREAMING_STT.md) first.

## Eval Ladder

### 1. Wakeword

Purpose: catch wake reliability regressions before live use.

Run from `backend/`:

```bash
uv run python tools/eval_wakeword.py
uv run python tools/eval_wakeword.py --grid
uv run python tools/eval_wakeword.py --failures
task be:eval-wakeword -- --ambient-manifest ../training/wakeword/manifests/enrolled_free_speech.jsonl --speaker-verifier --max-fa-per-hour 1.0
```

Inputs:

- Positive clips: `training/wakeword/data/positives_real/` (primary recall gate)
- Confirmed live positives: `training/wakeword/data/feedback/positives/` (not a reliable recall gate — short detection windows)
- Confirmed false triggers: `training/wakeword/data/feedback/negatives/`
- Enrolled free-speech ambient: `training/wakeword/manifests/enrolled_free_speech.jsonl` (always-on FA/hr gate after Stage 2b)
- Public ambient: `training/wakeword/manifests/public_fa_eval.jsonl`

Primary metrics:

- Recall on `positives_real`.
- False trigger rate on feedback negatives.
- **Enrolled free-speech FA/hr** with `--speaker-verifier` (current gate: **0.55 FA/hr** on 1.81h at `thr=0.70`, `N=3`, `vad=0.5`, `speaker=0.6`).
- Failure list for clips that still fire.

Runtime cascade: Stage 1 `Jarvis.onnx` → Stage 2b Sherpa speaker verifier when an owner profile exists (accept-all until enrollment). See [WAKEWORD_ARCHITECTURE.md](../proposals/WAKEWORD_ARCHITECTURE.md).

### 1b. Barge-in speaker (duplex scoreboard)

Purpose: pick `barge_in_speaker_threshold` from owner vs other cosines under TTS bleed — not from live dogfood or `host.log`.

Clip contract: [`training/voice/README.md`](../../training/voice/README.md). Record near-end (`clips/owner`, `clips/other`) and far-end (`clips/tts`) separately; the tool mixes SER levels. Nested `short/` and `satellite/` folders (and clip tags) group rows as `label/length/channel`. Phrase clips also get a derived 0.5s prefix so short-reply behavior shows up without extra recordings.

Runtime scoring on this board is **max cosine** against enrollment plus the optional per-node room vector, on the **full clip**. Live barge-in additionally caps at 0.8s of *VAD-onset-forward* PCM so the 1.5s STT wait does not embed later room TTS; that is `--onset-seconds 0.8`, not the default. TTS on the board is mixed in at SER levels, not recorded bleed. Audio shorter than 0.4s is `UNAVAILABLE` (`unsc` on the board), not a mismatch.

```bash
task be:eval-barge-in-speaker -- --owner-id geoff
task be:eval-barge-in-speaker -- --owner-id geoff --node-clip ../training/voice/clips/owner/satellite/jarvis.wav
```

`--score-mode mean` and `--onset-seconds 0.8` are ablations against the current scorer. Only move `barge_in_speaker_threshold` (currently **0.21**) when this board shows owner still under and other-FA still safe. Wakeword trees stay under `training/wakeword/`.

### 2. STT (offline / batch)

Purpose: compare raw transcription quality (MLX-Whisper or Cartesia batch) without the streaming helper, VAD, or turn detector. **Live voice** uses `apple_speech` or `cartesia`; the batch harness does not evaluate Apple Speech.

Run from `backend/`:

```bash
uv run python tools/eval_stt.py --fixtures logs/fixtures/stt
uv run python tools/eval_stt.py --backend mlx --model mlx-community/whisper-small.en-mlx-4bit
uv run python tools/eval_stt.py --backend cartesia
uv run python tools/eval_stt.py --suite raw-stt --label mlx-medium
```

Inputs:

- `*.wav`: 16 kHz mono 16-bit PCM.
- Matching `*.txt`: rough reference transcript.
- References can be lowercase and punctuation-free; scoring normalizes case, trailing punctuation, commas, and ellipses before comparison.
- A small fixed set: short command, normal sentence, long monologue, pause-and-continue, noisy room, and captured or re-recorded known failures.

Primary metrics:

- Transcribe time and real-time factor.
- Length ratio and large-deletion flags against the rough reference transcript.
- Tail-missing and prefix-only flags for clipped endings.
- Repetition / hallucination flags, especially repeated n-grams.
- Empty or near-empty transcript on non-empty speech.
- Coarse normalized WER as a secondary cross-backend signal.

Keep STT eval separate from endpointing. If `eval_stt.py` fails on the raw WAV, fix model/prompting/segmentation before tuning VAD or turn detection.

### 3. Voice Turn Latency

Purpose: measure whether the full voice path feels snappy after component evals pass.

Run from repo root with the backend already running:

```bash
task be:latency -- --text "How are you?" --runs 5
task be:latency -- --audio logs/fixtures/stt/how_are_you.wav --activate-audio --chunk-ms 96 --runs 5
task be:latency -- --suite voice-core --label mlx-gate1 --activate-audio --runs 3
task be:latency -- --suite streaming-smoke --label apple-speech --activate-audio --runs 3
```

Primary metrics:

- `first_partial_ms`: first streaming partial seen.
- `latest_partial_after_speech_ms`: last partial minus estimated speech end. Use this to see STT engine tail delay.
- `commit_ms`: first accepted `conversation.transcript`.
- `commit_after_speech_ms`: accepted transcript minus estimated speech end. This is the main "how long after I stopped talking did it submit?" number.
- Gap `commit_after_speech_ms - latest_partial_after_speech_ms`: small gap means commit policy is not the main bottleneck; large gap means JARV1S endpointing/finalize timing needs attention.
- `response_ms`: first assistant text.
- `first_audio_ms`: first backend audio packet.
- `turn_latency`: accepted turn to voice response telemetry.
- `stt_stream_total` / `stt_finalize_wait` for runtime streaming (`apple_speech` or `cartesia`).
- `stt_batch` only appears in offline `eval_stt.py` runs (MLX/Cartesia batch), not the live voice path.
- `llm` and `tts_first_chunk` when STT is not the bottleneck.

Use this only for before/after checks on a known scenario. Do not use it as the first debugger for STT quality.

When `--label` or `--suite` is set, the probe writes a self-contained run under `backend/logs/evals/<timestamp>_<label>/`:

- `manifest.json`: command, suite, URL, fixture directory, and relevant `VOICE__` environment.
- `results.jsonl`: compact per-run rows. Failed or flagged rows include the latest/last partial evidence by default.
- `summary.json`: machine-readable success, commit, timeout, flagged, tail-missing, and timing aggregates (`latest_partial_after_speech_ms_p50/p90`, `commit_after_speech_ms_p50/p90`).
- `summary.md`: the first file to read when comparing experiments.

Use `--include-evidence` only when you need every partial/transcript event in `results.jsonl`.

`be:latency` replays audio at realtime mic cadence by sleeping for each chunk's actual PCM duration, including the final partial chunk. Synthetic gaps and trailing silence are exact millisecond PCM durations. Keep realtime pacing on for endpointing and fast-recovery tests; fast replay is only valid for raw STT, where timing is irrelevant.

For clean end-of-speech delay numbers, trim obvious dead air from fixtures. The probe estimates speech end from trailing non-silent PCM, but tightly recorded clips are still easier to reason about.

For fast recovery, prefer a controlled gap sweep over one fixed gap:

```bash
task be:latency -- \
  --suite fast-recovery \
  --label recovery-baseline \
  --gap-runs 3 \
  --activate-audio \
  --chunk-ms 96
```

If `recovery_part1.wav`, `recovery_part2.wav`, and `recovery.txt` exist together, the probe uses `recovery.txt` as the default combined reference. Without that combined file, it joins the two part `.txt` references.

### 4. Tool Routing

Purpose: catch plugin-selection regressions before live LLM evals.

Run from repo root:

```bash
task be:eval-routing
```

Inputs:

- Cases: `backend/evals/tool_routing.yaml`
- Policy: `voice_default` (production voice routing)

Primary metrics:

- Per-category recall and precision.
- Hard-negative clean rate (wrong plugin not routed).
- Tail token budget for routed manifest.

Keep plugin `metadata.utterances` intent-shaped (e.g. "remind me later to do something"), not copies of eval case phrasing. Eval utterances should use different wording so a pass measures routing, not seed overlap.

### 5. Agent / LLM Behavior

Purpose: catch in-turn decision regressions — tool choice, consent, `NO_REPLY`, false completion claims, and silent-control probes.

Run from repo root or `backend/`:

```bash
task be:eval-agent
cd backend && uv run python tools/evaluate_agent_behavior.py --live --priority P0 --label agent-live-p0
cd backend && uv run python tools/evaluate_agent_behavior.py --probe --live --label agent-probes
```

Inputs:

- Cases: `backend/evals/agent_behavior.yaml`
- Scorers: `backend/evals/agent_scorers.py`
- Harness: reuses `_execute_turn` + `TurnResult`; plugin bootstrap via `backend/evals/bootstrap.py`

Tiers:

- **mock** (default): scripted events; validates harness plumbing.
- **live** (`--live`): real LLM; production routing by default; executor stubbed (no external side effects). Live P0 canaries require `3/3` pass.
- **probe** (`--probe --live`): opt-in prompt-tuning probes. Compare before/after; not part of the regression gate.

After live canaries pass, run a deliberate-break check: damage the surface one case depends on, confirm it goes red, revert.

With `--label`, runs write to `backend/logs/evals/<timestamp>_<label>/` (`results.jsonl`, `summary.json`).

### 6. Live Debug

Purpose: investigate behavior that only appears in interactive use.

Useful knobs:

- `VOICE__trace_voice_events=true` for batch segments, continuation merges, and fast recovery traces.
- Select Local or Cartesia under **Settings → Voice & Audio → Transcription** to isolate runtime backend behavior.
- `task be:eval-stt -- --backend mlx --model ...` to compare raw MLX model size and quality outside the runtime voice path.

### Apple Speech streaming experiments

The desktop supervisor launches the helper automatically. For backend-only development, build and launch it through `task be:dev`, then run:

```bash
task be:latency -- --suite streaming-smoke --label apple-speech --activate-audio --include-evidence
```

Document results in `backend/logs/evals/`; compare `latest_partial_after_speech_ms` vs `commit_after_speech_ms` in `summary.md`.

Useful telemetry:

- `turn_runs.stages[]` for per-stage timings.
- `turn_runs.turn_detection` for semantic/VAD endpoint decisions.
- STT coverage logs for captured-vs-fed audio gaps.

## Decision Rules

- Start with the narrowest failing layer: wakeword -> STT -> full voice turn -> tool routing -> agent behavior -> live debug.
- Change one variable at a time.
- Keep fixtures small and real. Add every painful live failure as a future regression clip.
- Prefer deleting stale benchmark notes over preserving old numbers. Baselines belong in run outputs, not long-lived docs.
- Do not chase small WER deltas from rough transcripts. Treat repeated n-grams, empty output, tail clipping, prefix-only commits, obvious deletion spans, and Cartesia-vs-MLX disagreement as higher-signal failures.
- `turn_runs` does not store audio. True live-failure fixtures need an explicit local opt-in capture hook; otherwise re-record the scenario.
