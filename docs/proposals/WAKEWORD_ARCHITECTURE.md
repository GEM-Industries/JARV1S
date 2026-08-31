# Wakeword Architecture

**Status:** Partial (V1 backend cascade default-on; local hard-negative finetune promoted; enrolled free-speech gate nearly cleared; packaged runtime artifacts shipped)
**Date:** 2026-07-01
**Related:** `backend/core/voice/wakeword_service.py`, `backend/core/voice/processor.py`, `backend/core/config.py`, `backend/tools/eval_wakeword.py`, `training/wakeword/`, `docs/proposals/partial/VOICE_SATELLITE_EDGE.md`, `docs/proposals/built/BARGE_IN_RELIABILITY.md`

---

## Progress

| Item | Status |
|------|--------|
| Architecture proposal (cascade detector, stopping rules, arbitrary-wakeword north star) | Done |
| Clip eval (`eval_wakeword.py` recall / false-rate / `--failures` / `--grid`) | Pre-existing |
| **Ambient FA/hr eval** (`--ambient`, `--ambient-manifest`, `--max-fa-per-hour`, per-fire timestamps) | **Done** |
| `task be:eval-wakeword` | Done |
| Public FA/hr manifest support (`training/wakeword/manifests/`, `data/public_eval/`) | Done |
| Feedback data cleanup (mislabeled negatives → positives / removed) | Done |
| Public corpus acquisition/prep | Done locally (gitignored); scripted via `prepare_public_eval.py` |
| Feedback-positive recall diagnosis | Done — not a valid recall gate (see baseline) |
| `WakeStage`/`WakeVerifier` pipeline refactor | **Done** — `backend/core/voice/wakeword/`; OWW Stage 1 + Sherpa speaker verifier |
| Packaged runtime artifacts | **Done** — wakeword ONNX + speaker model validated in `build-host-runtime.sh`; app must not depend on `training/wakeword/` |
| Commit/suppression gate | **Done** (`SpeechProcessor` refractory + post-TTS suppression) |
| Enrolled-user free-speech FA/hr manifest | Scripted via `prepare_enrolled_eval.py`; audio local |
| Sherpa-ONNX Stage 1 spike doc | `training/wakeword/docs/SHERPA_KWS_SPIKE.md` |
| Speaker verifier (Stage 2b) | **Done** — Sherpa-ONNX speaker embedding verifier behind `WakeVerifier`; runtime uses packaged model + owner embedding gallery |
| Stage 2b eval tuning | **Done** — ambient grid/source filters, speaker threshold grid, per-stage attribution |
| Enrolled free-speech measurement | **Done** — exposed self-speech as the true always-on bottleneck |
| Local hard-negative Stage 1 finetune | **Promoted** — `Jarvis.onnx` replaced; defaults now `thr=0.70`, `N=3`, `vad=0.5`, `speaker=0.6` |
| ACAV-backed finetune experiment | Done — useful coverage, but current training/selection produced worse enrolled FA/hr than local hard negatives |
| Wire into `task be:eval` | Done — `task be:eval` / `release:candidate`; default is clip eval (ambient FA/hr still opt-in) |
| Auto-capture negatives on timeout | Not started |

**Run the harness (from `backend/`):**

```bash
task be:eval-wakeword -- --ambient-manifest ../training/wakeword/manifests/public_fa_eval.jsonl
task be:eval-wakeword -- --ambient-manifest ../training/wakeword/manifests/public_fa_eval.jsonl --max-fa-per-hour 1.0
task be:eval-wakeword -- --diagnose-feedback
task be:eval-wakeword -- --speaker-verifier --speaker-threshold-grid
task be:eval-wakeword -- --ambient-manifest ../training/wakeword/manifests/public_fa_eval.jsonl --ambient-grid --speaker-verifier
task be:eval-wakeword -- --ambient-manifest ../training/wakeword/manifests/enrolled_free_speech.jsonl --speaker-verifier --max-fa-per-hour 1.0
```

Speaker verifier is **enabled when an owner profile exists** (`wakeword_speaker_verifier_enabled=true`). Clean installs boot with `AcceptAllWakeVerifier` until the owner enrolls during first-run setup (optional) or later in Settings → Voice & Audio. Eval passes `--speaker-verifier` with an explicit `--speaker-profile` or `--speaker-enrollment-manifest`; omit it only when testing Stage 1 in isolation.

**Runtime artifacts:**

| Artifact | Path | Role |
|----------|------|------|
| Stage 1 keyword head | `backend/resources/models/wakeword/Jarvis.onnx` | openWakeWord embedding + finetuned MLP |
| Stage 2b speaker model | `backend/resources/models/speaker/nemo_en_titanet_small.onnx` | Sherpa-ONNX TitaNet-small speaker embedding extractor |
| Stage 2b owner profile | `{DATA_DIR}/voice/speaker-profiles/<sha256(owner_id)>.npz` | Versioned gallery bound to the speaker-model SHA-256 |

Offline eval profiles can still be built from training manifests:

```bash
cd training/wakeword && python scripts/prepare_speaker_enrollment.py
cd ../../backend && uv run python tools/export_speaker_profile.py --output /tmp/eval_profile.npz
```

Packaged desktop builds require the Stage 1 ONNX and speaker embedding model via `apps/desktop/scripts/build-host-runtime.sh`. They must **not** ship a personal profile; `build_default_wake_verifiers()` boots in accept-all mode until enrollment.
**Dev environment note:** Backend `uv sync` does not run on Windows (MLX dependency). Wakeword **training/finetuning** uses the separate `training/wakeword/` venv (works on Windows). Running `eval_wakeword.py` with the real ONNX model needs Linux or macOS backend venv, or WSL with a native Linux clone — not required to prepare public manifests; only to score them.

---

## Problem

The original detector was a **single-stage, overconfident classifier** (`Jarvis.onnx`: a frozen-embedding MLP) thresholded at `0.93` with an in-app sustain filter (`wakeword_patience=4`). The sigmoid saturates to ~1.0 for both true wakes and speech-like confusers, so the score carries little information — only *sustain duration* discriminates. That failed in continuous conversation for structural reasons:

- **VAD is disabled when it matters.** `wakeword_vad_threshold=0.4` only suppresses non-speech. While anyone talks, the gate is fully open, leaving sustain as the only filter.
- **FAR scales with talk time.** A false accept needs a confuser to hold ≥0.93 for ~320 ms. Over minutes of speech the probability of at least one such run grows roughly linearly — so "constant conversation" is the worst case.
- **No personalization, no second stage.** The system fired for any voice and any phonetic neighbor of "Jarvis" (javascript, harvest, service, names).
- **The metric that matters was unmeasured.** Eval was clip-rate, not false-accepts/hour on continuous audio. We tuned blind.

The V1 backend cascade now addresses the biggest architecture gap with an explicit candidate/verifier pipeline and a speaker verifier. New enrolled-user free-speech measurement showed the earlier public FA/hr gate was necessary but not sufficient: non-enrolled public speech mostly exercises speaker rejection, while always-on reliability is dominated by **the enrolled user saying near-words**. The current blocker is Stage 1 keyword discrimination on self-speech ("JavaScript" style confusers), not speaker verification.

---

## Design principles

1. **Operating-point separation.** No single model should be both high-recall and high-precision. Split the job into a permissive **candidate** stage and a strict **verifier** stage, each tuned for one objective.
2. **Measurement is the contract.** A continuous **false-accepts/hour** (FA/hr) + **false-reject-rate** (FRR) harness on multi-condition audio is the foundation, built *first*. Every model/gate change is judged against it. Nothing ships that regresses FA/hr.
3. **Personalization is a first-class stage,** not an afterthought. The strongest, cheapest FAR lever (per openWakeWord's own guidance) is verifying the activation came from a known speaker.
4. **Train on the deployment distribution.** Augment in the waveform/mel domain (reverb, room noise, TV, distance, SNR) and mine real false activations. The model must see what it fails on.
5. **Edge-portable by construction.** The candidate stage must run on a Pi Zero 2 W so PASSIVE detection can move to the satellite later (see `VOICE_SATELLITE_EDGE.md`) without re-architecting.
6. **Phrase is a runtime choice, not an architecture constant.** V1 may ship with "Jarvis", but the long-term system should support user-chosen wake words without baking a single phrase into the pipeline.
7. **Stop when the gate clears.** The cascade is a menu of precision filters, not a commitment to build all of them. Add the cheapest stage, measure FA/hr and FRR, and stop at the first configuration that meets the gate.

---

## Target architecture: a cascade detector

```
audio (16 kHz mono PCM, 80 ms frames)
  │
  ▼
┌──────────────────────────────────────────────────────────────────┐
│ Stage 0 — VAD gate (Silero)              cheap, always-on          │
│   purpose: drop non-speech energy only                             │
└──────────────────────────────────────────────────────────────────┘
  │ speech frames
  ▼
┌──────────────────────────────────────────────────────────────────┐
│ Stage 1 — Candidate detector (embedding + small keyword model)     │
│   objective: HIGH RECALL, permissive (catch every real wake;       │
│              tolerate higher FAR here)                              │
│   edge-portable: runs on satellite or backend                      │
│   emits: WakeCandidate{audio_window, score, t_start, t_end}        │
└──────────────────────────────────────────────────────────────────┘
  │ candidate (only on trip — rare)
  ▼
┌──────────────────────────────────────────────────────────────────┐
│ Stage 2 — Verifier (runs only on a candidate; not always-on)       │
│   2a Keyword re-scorer: stronger model re-scores the buffered      │
│      window (posterior-smoothed, not raw threshold)                │
│   2b Speaker verifier: is this the enrolled user's voice?          │
│   2c (fallback) STT phrase confirm for low-confidence candidates   │
│   objective: HIGH PRECISION; collapses FA/hr                       │
│   emits: WakeDecision{accept: bool, reason, speaker_id?}           │
└──────────────────────────────────────────────────────────────────┘
  │ accept
  ▼
┌──────────────────────────────────────────────────────────────────┐
│ Stage 3 — Commit gate (state machine)                              │
│   refractory window, post-TTS suppression, PASSIVE→ACTIVE_IDLE     │
└──────────────────────────────────────────────────────────────────┘
```

Stage 1 trips often enough to catch every real wake; Stage 2 runs only on those trips (cheap on average) and is where false accepts die. This is the structure Alexa/Siri/Google use (tiny permissive on-device stage → stronger verifier/speaker re-scorer) and exactly what openWakeWord recommends (base model + custom verifier).

### Stage summary

| Stage | Model | Objective | When it runs | Where |
|-------|-------|-----------|--------------|-------|
| 0 VAD | Silero (existing) | drop non-speech | every frame | edge or backend |
| 1 Candidate | OWW embedding + small keyword head | **recall** (permissive) | every speech frame | **edge-portable** |
| 2a Keyword re-score | stronger keyword model, posterior smoothing | precision | on candidate only | optional |
| 2b Speaker verify | Stage-1-independent speaker-embedding verifier (enrolled user) | precision | on candidate only | backend first, edge later |
| 2c STT confirm | existing STT, fuzzy phrase match | precision (fallback) | low-confidence candidate | optional |
| 3 Commit | state machine | stability | on accept | backend |

---

## Interfaces / contracts

The cascade is now an explicit pipeline so stages are swappable and testable in isolation. Current abstraction in `backend/core/voice/wakeword/`:

```python
@dataclass(frozen=True)
class WakeCandidate:
    audio: bytes          # buffered window incl. pre-roll (the captured wake)
    score: float          # stage-1 score
    t_start: float
    t_end: float

@dataclass(frozen=True)
class WakeDecision:
    accept: bool
    reason: str           # "verified" | "speaker_mismatch" | "keyword_reject" | "stt_miss"
    speaker_id: str | None = None
    scores: dict[str, float] = field(default_factory=dict)  # per-stage, for traces/eval

class WakeStage(Protocol):
    def process(self, frame: bytes) -> WakeCandidate | None: ...   # stage 1 (streaming)

class WakeVerifier(Protocol):
    def verify(self, candidate: WakeCandidate) -> WakeDecision: ...  # stage 2 (on candidate)
```

`WakeWordService` is the **orchestrator** of `[CandidateDetector]` + `[Verifier...]`, preserving its existing `process()/reset()` surface so `processor.py` and `handlers.py` stay minimally affected. Each `WakeDecision` should carry per-stage scores for traces and eval attribution.

---

## The models & training methodology

### Stage 1 — candidate detector
- Current production is still the frozen OWW embedding + small head, now finetuned on local hard negatives and promoted to `backend/resources/models/wakeword/Jarvis.onnx`. Current operating point: `thr=0.70`, `N=3`, `vad=0.5`.
- Keep this V1 baseline only while it is measured. The self-speech gate showed its real weakness: phrase discrimination against the enrolled user's near-words. Speaker verification cannot reject these by design.
- Training lessons so far:
  - Skip too-short positives (`<1.0s`); many one-second clips can truncate the trailing "is" in "Jarvis" and teach partial wake windows.
  - Include real enrolled-user false accepts as hard negatives. This produced the biggest improvement (`5.36` → `1.07` enrolled FA/hr).
  - Downloaded ACAV embeddings provide broad background coverage, but the first ACAV-backed finetunes were worse on the enrolled-speech gate than the local hard-negative finetune. Treat ACAV as supporting coverage, not a substitute for deployment-distribution hard negatives.
  - Model selection must account for score calibration drift. The finetuned models moved away from the old `0.95` threshold, so checkpoint selection now sweeps validation thresholds.
- Next training improvement should be **real acoustic augmentation in the waveform/mel domain** (RIR reverb convolution, additive room/TV/conversation noise at varied SNR, distance/gain, light SpecAugment). Embedding-scalar "gain" is not enough. Multi-speaker, multi-accent synthetic positives are still needed.
- Calibrate with label smoothing / focal loss so the score is not collapsed to {0,1}; keep usable dynamic range so posterior smoothing in Stage 2a has signal.
- Treat this per-phrase model as a replaceable baseline, not the long-term architecture. The north-star Stage 1 is an enrollment-embedding keyword spotter (GE2E-KWS/open-vocabulary KWS style): the user enrolls a wake word with a few clips, the system stores a centroid, and runtime detection is cosine similarity against that enrollment without retraining a model per phrase.

### Stage 2a — keyword re-scorer
- Optional. A stronger keyword classifier (larger window/context, or an ensemble) that re-scores only the buffered candidate. Decisioning via **posterior smoothing / streaming decode**, not a single-frame threshold.
- Do not build this until the FA/hr harness shows that Stage 2b plus the commit gate still leak keyword-like false accepts.

### Stage 2b — speaker verifier (owner profile, shipped)
- Built as a Stage-1-independent speaker embedding verifier, not an openWakeWord feature/logistic-regression verifier. Runtime math lives in `backend/core/voice/speaker_verifier.py` (`EnrolledSpeakerVerifier`): one process-shared immutable Sherpa extractor plus a session-scoped, atomically replaceable owner gallery scored by **max cosine** (enrollment rows, plus at most one extra vector per room-speaker `node_id`). Empty or sub-0.4s PCM is unscorable, not a mismatch. Barge-in scores the first 0.8s of onset speech.
- Wake Stage 2b (`SpeakerEmbeddingWakeVerifier`) is a thin `WakeVerifier` adapter over that shared verifier. The same instance scores barge-in candidates with `barge_in_speaker_threshold` (calibrate via `eval_barge_in_speaker.py`, not live `host.log`).
- **Runtime contract:** production loads the bundled speaker model plus a versioned **owner profile** from `{DATA_DIR}/voice/speaker-profiles/<sha256(owner_id)>.npz` when present. Profiles store the exact model artifact SHA-256 and must match runtime. Older `.npy` profiles are ignored and users re-enroll. If no current profile exists, the factory returns `AcceptAllWakeVerifier` so Stage 1 still wakes anyone until enrollment. Explicit `wakeword_speaker_profile_path` remains a developer/eval override only.
- **Multi-person path:** keep embedding extraction and identity policy separate. A future open-set identifier can score the same model-bound gallery format for several profile IDs, return `unknown` unless the top score and top-two margin pass, and stamp the selected `speaker_id` on the existing voice turn. Owner authorization remains a policy decision, not a property of the embedding model.
- **Enrollment workflow:** First-run setup offers optional enrollment after AI is ready; Settings → Voice & Audio remains the manage/re-record/delete surface. Both record five PCM clips (three short “Jarvis”, then two natural requests) through the existing AudioWorklet → `PUT /api/v1/voice/speaker-profile` → normalized gallery write → `WakeWordService.reload_verifiers()` on live owner sessions. After a room speaker is online, one “Jarvis” from that mic is stored as a per-node vector (`POST /api/v1/voice/speaker-profile/nodes/{node_id}/sample`); capturing again replaces that vector. Raw PCM is never persisted.
- **Offline eval/export:** `prepare_speaker_enrollment.py` + `export_speaker_profile.py` remain training tools and must not be referenced by packaged runtime.
- Tune from data we already have plus enrolled-user free-speech negatives:
  - threshold tuning positives: `positives_real` + `feedback/positives` (manifest used only at export time),
  - negatives: enrolled user's non-wake speech (`manifests/enrolled_free_speech.jsonl`) **and** real false activations (`feedback/negatives`, 222).
- Tradeoff (accepted): less likely to wake for unfamiliar voices once enrolled. For a personal assistant this is the right default; multi-user enrollment is a later extension.
- This is durable even if Stage 1 later becomes sherpa/open-vocabulary KWS: the product invariant is "the enrolled user addressed the assistant", not just "some audio sounded like the phrase."
- Keep it behind the existing `WakeVerifier` chain in `WakeWordService`; do not add verifier policy to `SpeechProcessor` or the turn orchestrator.

### Stage 2c — STT phrase confirm (fallback)
- Optional. For candidates that pass keyword but are borderline on speaker, run the existing STT on the captured pre-roll and fuzzy-match the configured wake phrase. Reuses infrastructure already present.
- This adds latency to the wake path, so keep it as a fallback only if measured FA/hr still fails after the speaker verifier.

### Data & feedback loop
- **Auto-capture deployment negatives**: when Stage 1 trips but the turn is abandoned (no speech → timeout), auto-save to `feedback/negatives`. Stop losing the most valuable data (currently only saved on a manual "NOT ME" within 5s).
- **Use all hard negatives, over-weighted** — remove the 200-cap and per-epoch downsampling in `train.py`.
- Use a repeatable **public FA/hr corpus** as the canonical negative eval reference. Local room captures are optional personal tuning data, not the project gate.
- Recommended public sources:
  - **DiPCo Dinner Party Corpus** — primary continuous far-field conversation eval.
  - **MISP home-TV wake-word/free-talk data** — hard TV + multi-speaker stress eval; use negative/free-talk segments only.
  - **Real-World FAR Benchmark** — broad one-second negatives (TV, podcasts, people, living room) concatenated or manifest-listed by category.
  - **DEMAND domestic/public subsets** — non-speech and ambient-noise sanity checks.
  - **MUSAN / DNS Challenge** — training augmentation and broad hard negatives, not the primary FA/hr gate.

---

## Measurement foundation

Shipped in `tools/eval_wakeword.py`:

| Metric | Definition | Gate |
|--------|------------|------|
| **FA/hr** | false accepts per hour over public negative audio manifests (far-field conversation, TV/media, domestic noise) | **< 1/hr** (V1 public gate) |
| **Enrolled free-speech FA/hr** | false accepts per hour over the enrolled user's non-wake speech, with noisy/mixed rooms separate by source | **< 1/hr** (always-on local gate) |
| **FRR / recall** | missed wakes over `positives_real`; broader live recall corpus still needed | target recall ≥ 90%; temporary activation floor ≥ 85% |
| per-stage attribution | where each FA/FR is caught or leaks | regression triage |

`task be:eval-wakeword` is available and included in `task be:eval` / `release:candidate` (default: clip eval). Ambient eval supports manifest/source filtering and grids (`--ambient-grid`, `--ambient-source-regex`, `--ambient-max-hours`, `--sensitivity-grid`, `--patience-grid`, `--vad-grid`, `--speaker-threshold-grid`) so tuning can focus on hard sources before the full gate. With `--speaker-verifier`, pass an explicit `--speaker-profile` or `--speaker-enrollment-manifest`; there is no packaged personal profile default.

**Corpus prep:**
- Public negatives: `training/wakeword/data/public_eval/` → `prepare_public_eval.py` → `manifests/public_fa_eval.jsonl`
- Enrolled free-speech: `training/wakeword/data/enrolled_eval/free_speech/` (16 kHz mono PCM) → `prepare_enrolled_eval.py` → `manifests/enrolled_free_speech.jsonl`. Excludes `false_accepts/` helper clips; labels `quiet_self`, `noisy_self_others`, `non_self` as manifest categories.

### Current local baseline and V1 cascade result

Public FA/hr audio now includes DiPCo far-field conversation, LibriSpeech `test-clean` read speech, expanded DEMAND noise, and bounded Speech Commands near-word confusers.

| Eval | Result | Interpretation |
|------|--------|----------------|
| Clip recall (`positives_real`) before finetune | 59/65 = **91%** | Barely cleared the recall gate before the cascade/finetune changes. |
| Clip false rate (`feedback/negatives`) before finetune | 39/222 = **18%** | Too high for always-on use. |
| Feedback-positive replay | 81/591 = **14%** | **Not a recall gate.** Clips are ~1.32s detection-window captures; cold replay from `reset()` lacks live streaming mel context. Use for verifier/training data; gate recall on `positives_real`. |
| Hard-negative continuous replay | 72 fires / 290.6s = **892 FA/hr** | Not a public baseline; confirms hard negatives still leak badly. |
| Public FA/hr v0 (`librispeech` + `demand`) | 10 fires / 5.653h = **1.77 FA/hr** | Fails the `<1/hr` gate; all fires came from read speech, none from DEMAND noise. |
| Public FA/hr v1 (`librispeech` + expanded `demand` + bounded `speech_commands`) | 18 fires / 7.047h = **2.55 FA/hr** | Fails the gate; failures are speech/near-word confusers (`librispeech`, `sheila`, `visual`), not ambient noise. |
| Public FA/hr v2 (`dipco` + `librispeech` + expanded `demand` + bounded `speech_commands`) | 23 fires / 12.382h = **1.86 FA/hr** | Fails the gate; DiPCo adds 5 far-field conversation fires, while DEMAND still contributes none. |
| Best grid row | `thr=0.93`, `N=4`, `vad=0.4` | Runtime knobs alone do not fix FAR. `vad=0.5` cuts false clips to 16% but drops recall to 88%. |
| Stage 2b at old Stage 1 config | 14 fires / 12.382h = **1.13 FA/hr** | Speaker verifier rejects 9/23 Stage 1 candidates but narrowly misses the public gate. |
| Public-tuned V1 cascade (`thr=0.95`, `N=4`, `vad=0.5`, `speaker=0.6`) | 10 fires / 12.382h = **0.81 FA/hr**; recall 55/65 = **85%** | Passed the public gate, but the enrolled-speech set later showed this was not sufficient for always-on reliability. |
| Enrolled free-speech gate at public-tuned config | 5 fires / 0.933h = **5.36 FA/hr** | Failed badly. 4/5 fires came from `quiet_self`; speaker verifier rejected none, as expected for the enrolled user's voice. |
| Local hard-negative finetune (`thr=0.70`, `N=3`, `vad=0.5`, `speaker=0.6`) on first enrolled set (0.933h) | recall 57/65 = **88%**; feedback false 3/222 = **1%**; enrolled free-speech 1 fire / 0.933h = **1.07 FA/hr** | First enrolled measurement; near-word leak on `quiet_self`. |
| Local hard-negative finetune on expanded enrolled set (1.810h, 4 continuous clips incl. 52 min TV/conversation) | **0.55 FA/hr** (1 fire / 1.810h); `noisy_self_others` 52 min clip had **0** fires | **Current enrolled gate.** Clears `<1/hr` on expanded corpus; still low sample count — one extra fire would move to ~1.1 FA/hr. |
| Local hard-negative finetune + ACAV experiment | best recall/FA tradeoff did not beat the local-only finetune on enrolled free-speech | ACAV is useful broad coverage, but the current training recipe needs better balancing/augmentation before ACAV improves the shipped model. |

Conclusion: the V1 backend cascade is the production path — Stage 2b is default-on, not an experimental toggle. Public FA/hr was necessary but not sufficient; enrolled free-speech is the always-on gate. The promoted finetune improved enrolled FA/hr from `5.36` → `0.55` on the expanded set while keeping recall at **88%**. Do not build another speaker stage for near-word leaks: the remaining failures are Stage 1 keyword discrimination on the enrolled user's speech. Next work is auto-capturing hard negatives, waveform augmentation/retraining, more enrolled free-speech hours for statistical confidence, and the open-vocabulary Stage 1 spike.

---

## Deployment topology

- **V1 (today):** whole cascade runs on backend; satellite streams raw PCM (`VOICE_SATELLITE_EDGE.md`).
- **V2 (edge wake):** Stage 0+1 move to the satellite (Pi Zero 2 W), which only streams audio to the backend *after* a candidate. Stage 2 (verifier) runs on the backend. Because Stage 1 is defined as edge-portable and emits a `WakeCandidate`, this move requires no re-architecture — only relocating the candidate stage and sending the candidate window over the existing WebSocket contract.
- **North star:** Stage 0+1 plus lightweight enrolled-speaker/phrase verification run at the edge, so no room audio leaves the satellite until the enrolled user addresses the assistant. Backend verification can remain as a second opinion when the edge is uncertain.

---

## Migration map (build correct, minimal churn)

1. **Eval harness + public FA/hr corpus** — harness, manifest support, and local corpus prep **shipped**. No model change.
2. **Introduce the `WakeStage`/`WakeVerifier` pipeline** inside `WakeWordService`, with current model as Stage 1 (behavior-preserving refactor). **Shipped** — `backend/core/voice/wakeword/`.
3. **Commit/suppression gate** — **shipped** in `backend/core/voice/processor.py`: global wake refractory, post-TTS suppression, reason-tagged telemetry, and early refractory release for failed/timeout turns. Keep it as voice-state policy; do not merge it with `core/voice/turn_admission.py`, which handles barge-in / follow-up pre-agent admission.
4. **Stage 2b speaker verifier** wired as a `WakeVerifier` in `WakeWordService` — **shipped** via `SpeakerEmbeddingWakeVerifier` adapter over session-scoped `EnrolledSpeakerVerifier`; activates when an owner profile exists (accept-all until first-run or Settings → Voice & Audio enrollment). The same verifier gates barge-in commits.
5. **Measure against public and enrolled-user gates.** Public gate cleared; enrolled free-speech exposed the real always-on bottleneck. Expanded enrolled corpus (1.81h) now clears `<1/hr` at current operating point; continue growing hours for confidence.
6. **Auto-capture negatives** in `handlers.py` for the continuous feedback loop. This is now the highest-leverage next implementation step.
7. **Retrain Stage 1 with real augmentation** before adding more verifier stages. Preserve the current hard-negative finetune as production, but build the next candidate with waveform-domain augmentation, better positive quality control, uncapped hard negatives, and ACAV/background coverage balanced against deployment hard negatives.
8. **Research spike: open-vocabulary Stage 1.** Evaluate sherpa-onnx / GE2E-KWS style candidates against the same public + enrolled harness. This is the arbitrary-wake-word path; treat it as an A/B measurement, not a rewrite commitment.
9. **(Optional) edge wake (V2)** once V1 cascade meets the FA/hr gate.

Each step is independently shippable and judged against the FA/hr gate from step 1.

---

## Current decisions

1. **V1 phrase:** keep bare "Jarvis" for now. Do not switch to "Hey Jarvis".
2. **Speaker model:** Stage-1-independent `sherpa-onnx` speaker embedding verifier (`EnrolledSpeakerVerifier`, with wake adapter `SpeakerEmbeddingWakeVerifier`). Runtime loads `wakeword_speaker_model_path` plus an owner profile from `DATA_DIR` (or an explicit eval override via `wakeword_speaker_profile_path`). Do not point production at `training/wakeword/`. Do not try to fix enrolled-user near-word leaks by tightening speaker threshold; it destroys recall and does not solve keyword discrimination. Barge-in uses `barge_in_speaker_threshold`, calibrated separately.
3. **Single vs multi-user enrollment:** single enrolled user for V1; multi-user enrollment is a later extension.
4. **Current V1 operating point:** promoted local hard-negative finetune with `wakeword_sensitivity=0.70`, `wakeword_patience=3`, `wakeword_vad_threshold=0.5`, provisional TitaNet `wakeword_speaker_threshold=0.21`, `wakeword_speaker_verifier_enabled=true`.
5. **Packaged app contract:** `desktop:build` bundles and validates wakeword + speaker artifacts; missing speaker model fails the build rather than breaking WebSocket connect at runtime.
6. **Arbitrary wakeword path:** time-box GE2E-KWS/open-vocabulary KWS research next; do not build a model factory unless this spike proves one is required.
7. **Edge timeline:** clear the enrolled free-speech gate with sufficient hours before attempting V2 edge wake.
