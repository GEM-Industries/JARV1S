# Phase 7.5a: Agent Runtime Hardening

**Status:** Built  
**Superseded by:** [`docs/BACKGROUND_AGENTS.md`](../../BACKGROUND_AGENTS.md) — the current reference for config defaults (`AGENT_INPROCESS_MAX_TURNS=30`), permission modes, and fan-out controls.  
**Current deviations:** `reason()` removed; reasoning effort is per-turn via `resolve_reasoning_effort()` + LiteLLM capability detection + `LLM_TEXT_REASONING_EFFORT` / `LLM_HEADLESS_REASONING_EFFORT`. Pre-turn model routing and `LLM_REASONING_*` / `AGENT_WORKER_*` dual-tier identity were later removed.  
**Date:** 2026-03-29

Historical design for Phase 7.5a.  
**Depends on:** [SUBAGENT_ARCHITECTURE.md](../SUBAGENT_ARCHITECTURE.md) (Phase 7.0 + 7.1)

---

## Problem

Phase 7 delivered background agents, reasoning escalation, and the model tiering architecture. The implementation has known bugs and missing guardrails that will compound under sustained use. These are not architectural problems — they're hardening gaps. Fix them before building the in-process agent runtime (Phase 7.5b proposal).

1. **Three config values are wired incorrectly or incompletely.** `LLM_REASONING_EFFORT` is applied globally via `_extra_params()` on every `LLMService` instance — the voice model gets `reasoning_effort` when only the reasoning tier should. `max_budget_usd` is persisted on task documents and accepted by `dispatch()` but never passed to `_run_agent()` (the function doesn't even accept the parameter). The worker's `allowed_tools` is `["Bash", "Read", "Write", "Edit"]` — missing `Grep` and `Glob` from the Phase 7 proposal spec.

2. **No fan-out or depth limits on agent spawning.** The `dispatch_agent` automation action and Phase 7.2 PR comment triggers can both spawn background agents. A misconfigured automation rule matching a high-frequency trigger can consume the entire concurrency pool indefinitely. The global `AGENT_MAX_CONCURRENT` semaphore caps total agents but doesn't prevent one noisy source from monopolizing all slots. Additionally, Phase 7.5b's `mode="jarvis"` in-process agents run the CodeAct loop with full `jarvis.*` access — meaning they can write `await jarvis.agents.dispatch(...)` and spawn further agents. Without a depth limit, a background task can fan out unbounded sub-tasks. This guardrail is a precondition of Phase 7.5b shipping safely.

3. **First-word TTS latency is determined by first sentence length.** The orchestrator streams sentences to Cartesia as they complete, but long first sentences (common with reasoning models) delay first audio with no upper bound.

4. **Subprocess cleanup is single-phase.** The current cleanup attempts `client.disconnect()` with a timeout, then SIGKILL. No graceful SIGTERM first, no verification the process actually died.

---

## Design

### 1. Config Fixes

**a) Scope `LLM_REASONING_EFFORT` per-instance.**

```python
def __init__(self, ..., reasoning_effort: str | None = None):
    self._reasoning_effort = reasoning_effort

def _extra_params(self) -> dict:
    params = {}
    if self._reasoning_effort:
        params["reasoning_effort"] = self._reasoning_effort
    return params
```

In `main.py`, pass `reasoning_effort=settings.LLM_REASONING_EFFORT` only when constructing the reasoning `LLMService`. Phase 7.5b's second `JarvisAgent` (powerful model) also benefits — each instance gets its own config.

**b) Wire `max_budget_usd` as post-hoc enforcement.**

`_run_agent()` doesn't accept `max_budget_usd` and `_spawn_agent()` doesn't pass it — the value is stored on the task document but never enforced. The SDK's `ResultMessage.total_cost_usd` arrives only at session end (not per-turn), so real-time budget enforcement isn't possible without per-turn cost tracking from the SDK.

Fix: after `ResultMessage` arrives, compare `total_cost_usd` against the task's `max_budget_usd`. Log a warning if exceeded. Surface the overage in the task completion event so the voice model can inform the user. This is analytics-grade enforcement, not real-time — honest about what the SDK exposes.

**c) Add `Grep` and `Glob` to worker `allowed_tools`** to match the Phase 7 proposal spec.

### 2. Fan-Out and Depth Limits

| Control | Default | Purpose |
|---|---|---|
| `AGENT_MAX_DEPTH` | `1` | Background agents cannot spawn further agents. Voice model and automation engine are depth 0; tasks they spawn are depth 1. Depth-1 tasks cannot call `dispatch()`. Required before Phase 7.5b ships — `mode="jarvis"` in-process agents have full `jarvis.*` access and can write `jarvis.agents.dispatch()` via CodeAct. |
| `AGENT_MAX_PER_SOURCE` | `3` | One automation rule can't consume the entire concurrency pool. |
| `AGENT_MAX_CONCURRENT` | `2` | Existing global semaphore across all sources. |
| `AGENT_INPROCESS_MAX_TURNS` | `15` | Turn cap for `mode="jarvis"` in-process tasks. Prevents context window overflow without mid-loop trimming. |

Track active counts per source in-memory on the `AgentsPlugin` instance (a `Counter` dict — increment on dispatch, decrement on completion/failure callback, reset on startup). Check `AGENT_MAX_PER_SOURCE` before acquiring the global semaphore. No database query needed.

For depth: store `depth` on the task document at dispatch time. In `_dispatch()`, check the current task's depth (default 0 for voice/automation callers). If depth ≥ `AGENT_MAX_DEPTH`, reject with a clear error. When a background agent calls `dispatch()` via CodeAct, the executor has access to the current `task_id` via context — pass `depth=current_depth + 1` to `_dispatch()`.

```python
# In AgentsPlugin:
self._source_counts: Counter[str] = Counter()

# In _dispatch(), before self._spawn_agent():
if depth >= settings.AGENT_MAX_DEPTH:
    return f"Depth limit ({settings.AGENT_MAX_DEPTH}) reached. Background agents cannot spawn further agents."
if self._source_counts[source] >= settings.AGENT_MAX_PER_SOURCE:
    return f"Source '{source}' already has {self._source_counts[source]} active tasks (limit: {settings.AGENT_MAX_PER_SOURCE})."
self._source_counts[source] += 1

# In _spawn_agent() done callback:
task.add_done_callback(lambda _: self._source_counts.subtract([source]))
```

### 3. Subprocess Hardening

Applies to `mode="code"` background tasks only (after Phase 7.5b, `mode="jarvis"` tasks run in-process and have no subprocess concerns).

Two-phase cleanup: SIGTERM → 1s wait → `os.kill(pid, 0)` to check aliveness → SIGKILL if still alive. Log any process requiring SIGKILL. Startup scan for orphaned `opencode`/`claude` processes from previous runs (best-effort, logged only).

### 4. Early TTS Flush

Time-based early flush on the **first sentence of each turn**. If 800ms have elapsed since the first text token and the buffer has 60+ characters, flush at the nearest clause boundary (comma, semicolon, colon) even without sentence-ending punctuation. After first audio is sent, normal sentence splitting resumes for the remainder of the turn.

---

## Implementation Plan

1. Scope `LLM_REASONING_EFFORT` per-instance in `LLMService`, update `main.py` lifespan
2. Add `max_budget_usd` to `_run_agent()` signature, post-hoc comparison against `ResultMessage.total_cost_usd`, log warning + surface in completion event
3. Add `Grep`, `Glob` to `allowed_tools` in `client.py`
4. Add `AGENT_MAX_DEPTH`, `AGENT_MAX_PER_SOURCE`, and `AGENT_INPROCESS_MAX_TURNS` to `config.py`
5. Add `_source_counts` counter and depth check to `AgentsPlugin._dispatch()`, store `depth` on task documents, decrement in done callback
6. Two-phase subprocess cleanup in `_run_agent()` finally block (SIGTERM → SIGKILL → verify dead)
7. Startup orphan scan for `opencode`/`claude` processes
8. Early TTS flush with `first_text_at` tracking and clause-boundary splitting in orchestrator

---

## Tradeoffs

**Per-instance `reasoning_effort`:** The voice model no longer gets `reasoning_effort`. Today no one is intentionally sending this to Groq (which ignores it), so the behavioral change is nil.

**Post-hoc budget enforcement:** `max_budget_usd` is checked after the task completes, not during. The SDK doesn't expose per-turn cost. This means a task can exceed its budget — we log it and inform the user, but can't prevent it. Honest about the limitation.

**No depth limiting removed:** `AGENT_MAX_DEPTH` is back and is a precondition of Phase 7.5b. `mode="jarvis"` in-process agents run the full CodeAct loop with `jarvis.*` access — they can write `jarvis.agents.dispatch()` and spawn tasks. Without a depth limit, a background task could fan out unbounded sub-tasks. Default of 1 means only the voice model and automation engine (depth 0) can spawn agents. Conservative — raise when orchestrator patterns are explicitly designed.

**Early TTS flush quality:** Clause-level splits produce shorter TTS segments with less natural prosody. Limited to first sentence of each turn, with character + time thresholds. Quality impact is minimal.
