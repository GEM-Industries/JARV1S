# Local Model Lane

**Status:** Built (folded into [KEYLESS_ONBOARDING_LANES](./built/KEYLESS_ONBOARDING_LANES.md) Phase 4)  
**Date:** 2026-06-11  
**Updated:** 2026-06  
**Superseded by:** `docs/proposals/built/KEYLESS_ONBOARDING_LANES.md` and `backend/core/setup/llm_config.py` for normative behavior.  
**Related:** `docs/research/ONBOARDING_FRICTION_LOG.md` (#7–#10), `backend/core/setup/`, `backend/core/llm/providers.py`

---

## Problem

JARV1S claims local-first, but the only path to a working assistant is a cloud LLM API key. The architecture is already compatible with local inference — `LLMService` speaks any OpenAI-compatible endpoint and a `custom` preset exists — yet the product flow excludes it:

1. `credential_store.list_provider_options()` filters out `custom`, so the setup wizard only offers cloud providers.
2. The wizard has no base-URL field; `custom` requires `.env` edits and a restart.
3. `validate_llm_credentials()` assumes cloud semantics: a real Bearer key, `GET {base}/models` with auth, and provider-style error taxonomy. Local servers use dummy keys, sometimes lack `/models`, and fail differently (server not running, model not pulled, context too small).
4. `readiness.require_llm_ready()` gates on `resolve_llm_api_key()` — a *key* is the readiness invariant, when the actual invariant is *a working chat endpoint*.
5. `configure_llm()` persists only the API key. Provider/model/base_url are mutated in-memory and lost on restart — a real bug today, fatal for a keyless local lane (there would be nothing persisted at all).

Separately, "local model works" is not binary. JARV1S is a CodeAct agent: the model must emit `<tool_call>`-delimited Python reliably. Current evidence (Docker's tool-calling evals, Berkeley FCL) shows sub-7B models collapse on tool selection and 7–14B models vary widely by family. A user who connects `llama3.2:3b` and gets garbled tool calls will conclude JARV1S is broken, not that the model is too small.

---

## Design

### 1. Two-lane setup fork

The wizard's first real choice becomes:

```
How should Jarvis think?
  [ Run locally — private, free, needs a capable machine ]
  [ Use a cloud provider — fastest setup, needs an API key ]
```

Cloud lane is unchanged. The local lane is new.

### 2. Local runtime detection

On entering the local lane, the backend scans known OpenAI-compatible endpoints (parallel, ~1s timeout each):

| Runtime | Default endpoint | Detection |
|---|---|---|
| Ollama | `http://localhost:11434/v1` | `GET /api/tags` (richer than `/v1/models`) |
| LM Studio | `http://localhost:1234/v1` | `GET /v1/models` |
| llama.cpp / llamafile | `http://localhost:8080/v1` | `GET /v1/models` |
| MLX (`mlx_lm.server`) | `http://localhost:8081/v1` | `GET /v1/models` |
| Custom | user-entered URL | `GET /v1/models`, fall back to chat probe |

New endpoint: `GET /setup/llm/local/discover` → `[{runtime, base_url, models[], reachable}]`. The wizard shows found runtimes with their installed models; "nothing found" shows install guidance (deep link to Ollama download + the one `ollama pull` command) rather than a dead end.

This mirrors the existing sidecar philosophy: Apple Speech STT runs in a supervised localhost helper. The LLM gets the same treatment — JARV1S does **not** embed an inference engine in-process.

### 3. Provider presets for local runtimes

Add to `LLM_PROVIDER_PRESETS`:

```python
"ollama": LLMProviderPreset(
    name="ollama",
    base_url="http://localhost:11434/v1",
    model="",                       # chosen from discovery
    api_key_env_names=(),           # keyless
    requires_api_key=False,
),
"lmstudio": ...,
"llamacpp": ...,
```

`requires_api_key: bool = True` is a new preset field. `resolve_llm_api_key()` and `require_llm_ready()` treat keyless providers as configured when provider+model+base_url are persisted and the last health probe succeeded. `LLMService` passes a dummy key (`"local"`) for the OpenAI client when none is set.

### 4. Persist the full LLM config (fixes existing bug)

`configure_llm()` writes `{provider, model, base_url}` to `system_config.llm_config` (MongoDB). Resolution order: persisted `system_config.llm_config`, otherwise a non-attemptable default shell for UI/readiness. Cloud secrets stay in `CredentialStore`; this document holds no secrets. Main LLM env vars do not participate in setup readiness.

### 5. Local-aware validation

`validate_llm_credentials()` gains a local branch (keyed off `requires_api_key=False`):

- Skip placeholder/key checks; skip Bearer header on `/models`.
- New failure codes with actionable next steps:
  - `SERVER_NOT_RUNNING` → "Start Ollama (it's installed but not running)" / install link
  - `MODEL_NOT_AVAILABLE` → "Run `ollama pull <model>` — Jarvis can watch and continue when it finishes"
  - `CONTEXT_TOO_SMALL` → detected from probe metadata where available (Ollama default 2048 ctx is unusable for the CodeAct manifest; recommend `OLLAMA_CONTEXT_LENGTH=32768`)
- Chat probe stays — it is the one check that works on every runtime.

### 6. CodeAct compatibility gate (the innovative bit)

Connectivity ≠ capability. After the chat probe passes, the local lane runs a **compatibility check**: 5–8 canned single-turn prompts with a miniature tool manifest, scored mechanically (no LLM judge):

- Emits a well-formed `<tool_call>` block when a tool is clearly needed
- Calls the right tool with right argument names
- Does *not* call a tool on a plain conversational turn
- Output parses as valid Python

Score → tier shown in the wizard:

| Score | Verdict | UX |
|---|---|---|
| ≥ 90% | **Jarvis-ready** | proceed |
| 60–90% | **Limited** | proceed with warning: "expect occasional tool mistakes; consider `qwen3:14b`" |
| < 60% | **Not recommended** | block with model recommendations sized to the machine |

Implementation: a fixture file (`backend/evals/codeact_compat.yaml`) + a small runner in `core/setup/`, reusing the existing eval-fixture pattern (`evals/tool_routing.yaml`). Runs in ~15–30s on a 14B local model. Result is cached per `(base_url, model)` in `system_config` and surfaced in Settings so users can re-run after pulling a new model.

### 7. Hardware-aware model recommendations

`psutil` is already a dependency (system diagnostics plugin). Map total RAM / Apple Silicon detection to a recommendation table rendered in the wizard:

| Machine | Recommended | Notes |
|---|---|---|
| ≤ 16GB | `qwen3:8b` (Q4) | usable, tight |
| 16–32GB | `qwen3:14b` (Q4) | sweet spot — near-GPT-4 tool selection |
| ≥ 32GB / M-series Max | `qwen3:32b` or MoE variants | best local CodeAct fidelity |

The table lives in one data structure, not scattered logic, and is expected to churn as models improve — keep it trivially editable.

### 8. Managed sidecar (Phase 3 — shipping)

The desktop Host owns an isolated Ollama sidecar:

- Bundled, version-pinned Ollama payload under `Contents/Helpers/Ollama/` (see `apps/desktop/managed-llm/manifest.json`).
- Loopback only on `127.0.0.1:11435` with `OLLAMA_MODELS` under the JARV1S data directory — never adopt, stop, or delete from the user’s `:11434` server.
- Baseline model: `gemma4:e4b-mlx` (16 GB+ Apple Silicon; latency-first edge dense). Lower-memory machines use Cloud or Advanced external runtimes.
- Baseline changes ship with the app release via that single manifest (`model_id`, optional `model_digest`, `supersedes` for purging prior weights). Do not auto-follow registry `latest`.
- Setup consent downloads the model with resumable progress; Settings can activate, switch away (runtime stops, files stay), pause/cancel an in-flight pull, or remove the download.
- Active provider remains `system_config.llm_config`; managed install state is derived from `/api/tags` plus a supervisor ready marker.

---

## Readiness semantics change

`require_llm_ready()` becomes endpoint-truth, not key-truth:

```
READY  = runtime initialized AND last chat probe ok
NEEDS_SETUP = no persisted llm_config and no resolvable key
DEGRADED (new, already defined in models.py, currently unused) =
    configured but endpoint unreachable (e.g. user quit Ollama)
```

`DEGRADED` surfaces in the UI as "Jarvis's local model server isn't running" with a retry — distinct from "never set up." A lightweight re-probe on `SetupNotReadyError` paths handles the laptop-reboot case without user action when the server comes back.

---

## What NOT to build

| Idea | Why skip |
|---|---|
| In-process inference (llama-cpp-python, MLX in the backend) | Couples backend lifecycle to GPU/memory pressure; supervised helper isolation is the existing pattern |
| Per-model prompt-format shims | OpenAI-compatible servers own chat templating; if a runtime needs nonstandard handling, that's a preset `request_policies` entry, not a framework |
| Model download manager in Phase 1 | Ollama/LM Studio already do this well with progress UI; duplicate later only inside the packaged-app sidecar |
| LLM-as-judge for the compatibility gate | Mechanical parsing of `<tool_call>` output is deterministic, free, and offline |
| Automatic model switching / fallback chains | Adds failure-mode opacity; one configured model, clearly tiered, with explicit user choice |

---

## Phases

**Phase 1 — Local lane end-to-end (the unblock)**
- `requires_api_key` preset field + `ollama`/`lmstudio`/`llamacpp` presets
- `llm_config` persistence in `system_config` (also fixes cloud-lane restart bug)
- Discovery endpoint + wizard fork + local validation branch
- Keyless readiness semantics + `DEGRADED` phase

**Phase 2 — Trust**
- CodeAct compatibility gate + fixtures + Settings re-run
- Hardware-aware recommendation table
- `CONTEXT_TOO_SMALL` detection and Ollama context guidance

**Phase 3 — Packaged managed local brain**
- [x] Supervised isolated Ollama sidecar (`:11435`, JARV1S model directory)
- [x] Consent + resumable model install in setup; Settings activate/remove
- [ ] **Release gate (manual, before declaring baseline):** CodeAct/tool-routing evals + voice-turn smoke on 16 GB and 24/32 GB Macs — measure first-token latency, tokens/sec, peak memory, 32K-context stability. Packaged lifecycle: install → interrupt/resume pull → activate → chat/tool → switch Cloud (process stopped, files retained) → remove (only JARV1S model dir changes).
- [x] Local TTS lane (Kokoro) — supervised `kokoro-onnx` helper on CPU; Settings Spoken replies: Off / Cartesia / On this Mac (see [LOCAL_TTS.md](../research/LOCAL_TTS.md))

---

## Tradeoffs

- **Local quality variance becomes Jarvis's reputation.** The compatibility gate is the mitigation: set expectations *before* first turn, not after a garbled one.
- **Two lanes to test.** Validation gains a branch; the failure taxonomy grows by three codes. Bounded — the chat probe and runtime init path are shared.
- **Voice can be fully local on the Host.** Apple Speech + Kokoro cover on-device input/output; Cartesia remains the optional cloud path. Room satellites still play audio from the central Host.
