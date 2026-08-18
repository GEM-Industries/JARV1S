# Module Graph Cleanup

**Status:** Built
**Date:** 2026-04-24
**Priority:** Medium. Structural hygiene. Independent of the other plugin-system proposals but benefits from them landing first.
**Depends on:** Nothing strictly, but [`PLUGIN_CONTRACT_CLEANUP.md`](./PLUGIN_CONTRACT_CLEANUP.md) and [`PLUGIN_REPOSITORY.md`](./PLUGIN_REPOSITORY.md) simplify the surface this touches.

---

## Problem

Three separate smells, all pointing at the same underlying issue (the module graph was never cleaned up after the codebase grew):

### 1. Pervasive function-local imports

In `backend/plugins` + `backend/core` alone there are **~119 function-local imports** (87 at 8-space method-body indent, 32 at 4-space function-body indent). `plugins/todo.py` imports `from core.plugins.ui import push_ui` **6 times** across 4 methods (lines 89, 108, 129, 151, 168, 194).

```python
async def add_task(self, text: str, group_id: str = "inbox") -> str:
    ...
    from core.plugins.ui import push_ui   # local import
    ui = await self.render_todo_widget(group_id=group["id"])
    push_ui(ui)
```

**This is cargo-culted, not cycle-driven.** Verified: `services.events` does not import `core.plugins.*`; `core.plugins.types` does not import `core.plugins.ui` or `services.events`. No cycle exists through `core.plugins.ui`. The `core.plugins.registry → services.database.mongodb` edge is one-way (mongodb only imports `core.config`). Plugin authors see local imports in every existing plugin and copy the pattern.

Effects:
- Static analysis and linters can't see the real graph.
- Cold-path cost: every call pays for a module-lookup cache hit.
- Onboarding friction: "why are imports inside functions here?" has no documented answer and no actual reason.

### 2. Hotspot files exceed the 700 LOC guideline

| File | LOC | Notes |
| --- | --- | --- |
| `backend/core/turns/orchestrator.py` | 1,191 | Turn lifecycle + headless pool + routing + globals |
| `backend/plugins/scheduler.py` | 626 | Tool surface + time parsing + recurrence validation |
| `backend/core/integrations/lifecycle.py` | 625 | Composio reconciliation + bespoke integration lifecycle |

Each is a single, coherent module but mixes concerns that split cleanly along existing seams.

### 3. Duplicated scheduler/recurrence logic

`backend/services/scheduler.py` (249 LOC) owns the runtime fire loop and has its own `next_occurrence` recurrence parser. `backend/plugins/scheduler.py` has overlapping recurrence validation (`_is_valid_recurrence`, `_describe_recurrence`) and time parsing (`_parse_time`, `_parse_date` — **instance methods**, not module-level) and then *imports* from `services.scheduler` at line 460 to call `next_occurrence`, `build_occurrence_doc`. Two modules, one concept, partial overlap.

### 4. `VOICE_CONFIG: Dict[str, Any]`

```python
# backend/core/config.py
VOICE_CONFIG: Dict[str, Any] = {
    "sample_rate": 16000,
    "vad_threshold": 0.8,
    ...
}
```

Every other field in `Settings` is typed. `VOICE_CONFIG` is accessed as `settings.VOICE_CONFIG["vad_threshold"]` in `backend/core/voice/`, `backend/api/websockets/`, losing autocomplete, misspelling protection, and schema validation.

---

## Design

Five independently shippable changes. Each stands alone.

### 1. Hoist the function-local imports

Since no cycle actually exists through `core.plugins.ui` / `services.events`, the fix is mechanical:

- Add `from __future__ import annotations` at the top of files where type hints might later introduce a cycle.
- Promote every function-local `from core.plugins.ui import ...`, `from plugins import db`, `from core.plugins.types import ...`, and similar **safe** local imports to file-top imports.
- For the type-only cases that *would* cycle at runtime, use `if TYPE_CHECKING:` guards with string annotations — not function-local imports.

**Specifically in `core/plugins/ui.py`:** the deferred `UIEnvelope` import (lines 37, 71) is pointless — `core.plugins.types` is already imported at module top for `WidgetSize`. Hoist it. The `services.events` import in `_publish_ui_event` (line 98) can also be hoisted; verified to not cycle.

Run tests. Any file that breaks has a real cycle — resolve by extracting the shared symbol to a neutral module, **not** by re-localizing the import.

**Acceptance:** Check with `pydeps backend --show-cycles` → zero cycles. `tach check` passes (after adding `tach.toml`, see §6). Each remaining local import has a comment explaining *why* (e.g. "deferred to break CLI cold-start cost"). Expected remaining count: single-digit.

### 2. Split the three hotspot files along existing seams

**`core/turns/orchestrator.py` (1,191 → ~3×400)**

Three cohesive chunks already exist in the file:

```
backend/core/turns/
├── orchestrator.py       # public AssistantOrchestrator; lifecycle
├── routing.py            # _COMPLEX_EXEMPLARS, complexity classification
backend/services/
└── headless_pool.py      # HeadlessTurnPool (moved out of core/turns/)
```

The module-level globals (`_HEADLESS_TURN_SEMAPHORE`, `_HEADLESS_TASKS`) move into a `HeadlessTurnPool` class. It's a resource pool, not a turn concept — park it directly in `services/` (skip the `core/turns/headless.py` waypoint; no second consumer justifies the extra layer). Orchestrator owns one as `self.headless_pool`.

**`core/integrations/lifecycle.py` (625 → ~2×280 + ~70 shared)**

```
backend/core/integrations/lifecycle/
├── __init__.py           # public re-exports + __all__
├── _shared.py            # IntegrationView, DisconnectResult, ReconcileResult, errors
├── composio.py           # reconcile_integration, reconcile_composio_startup
└── bespoke.py            # teardown_local_integration, refresh_non_composio_integrations
```

Shared types live in `_shared.py` because both `composio.py` and `bespoke.py` reference them; putting them in `__init__.py` creates an import cycle through the package root. Follow the CPython `asyncio.__init__` pattern:

```python
# backend/core/integrations/lifecycle/__init__.py
from .composio import reconcile_integration, reconcile_composio_startup, create_connect_link, list_integrations, disconnect_integration
from .bespoke import teardown_local_integration, refresh_non_composio_integrations
from ._shared import IntegrationView, DisconnectResult, ReconcileResult, IntegrationLifecycleError, IntegrationUnavailableError, IntegrationOperationError, IntegrationConflictError

__all__ = [
    "reconcile_integration", "reconcile_composio_startup",
    "create_connect_link", "list_integrations", "disconnect_integration",
    "teardown_local_integration", "refresh_non_composio_integrations",
    "IntegrationView", "DisconnectResult", "ReconcileResult",
    "IntegrationLifecycleError", "IntegrationUnavailableError",
    "IntegrationOperationError", "IntegrationConflictError",
]
```

All 5 existing callers (`main.py`, `api/routes/{webhooks,integrations,auth}.py`, `plugins/system.py`) keep working unchanged.

### 3. Consolidate scheduler + recurrence (supersedes the naive scheduler split)

The previous plan to split `plugins/scheduler.py` into `plugins/scheduler/{time_parsing,recurrence}.py` was wrong — it leaves the duplicated `next_occurrence` logic in `services/scheduler.py`. Instead, extract both:

```
backend/core/scheduling/
├── __init__.py           # public API
├── recurrence.py         # next_occurrence, VALID_RECURRENCE_PRESETS, validate, describe
├── time_parsing.py       # parse_time, parse_date (free functions, take tz explicitly)
└── occurrence.py         # build_occurrence_doc
```

Then:
- `plugins/scheduler.py` shrinks to the `SchedulerPlugin` tool surface (~300 LOC). `_parse_time`, `_parse_date`, `_is_valid_recurrence`, `_describe_recurrence` become method-thin wrappers around `core.scheduling.*` free functions.
- `services/scheduler.py` loses its `next_occurrence` copy, calls `core.scheduling.recurrence.next_occurrence`.
- The line-460 local import `from services.scheduler import next_occurrence, build_occurrence_doc` in `resume_series` moves to `core.scheduling` at module top — no cross-dependency between plugin and service.

**Audit before starting:** `backend/plugins/time_utils.py` was renamed to `backend/plugins/time.py` as part of PLUGIN_CONTRACT_CLEANUP. Verify whether its helpers overlap with `core/scheduling/time_parsing.py` or are distinct. If overlapping, consolidate in the same PR — don't create a third home.

### 4. Promote `VOICE_CONFIG` to a typed Pydantic model

```python
# backend/core/config.py
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

class VoiceConfig(BaseModel):
    sample_rate: int = 16000
    channels: int = 1
    vad_threshold: float = 0.8
    min_speech_frames: int = 2
    barge_in_min_frames: int = 4
    silence_threshold: float = 0.4
    fast_recovery_window: float = 1.5
    wakeword_sensitivity: float = 0.9
    stt_backend: Literal["cartesia", "local_streaming"] = "local_streaming"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,  # required so VOICE__VAD_THRESHOLD overrides one field, keeps others at default
    )
    ...
    VOICE: VoiceConfig = VoiceConfig()
```

Four pydantic-settings 2.x gotchas worth flagging (documented in the official settings concepts page):

1. **`VoiceConfig` must extend `BaseModel`, not `BaseSettings`** — nested `BaseSettings` collect values separately and produce surprising results.
2. **`nested_model_default_partial_update=True` is mandatory** here. Without it, setting any `VOICE__*` env var wipes all other defaults.
3. **Migrate `class Config:` → `model_config = SettingsConfigDict(...)`** in the same change — mixing v1 and v2 config styles is a known footgun.
4. **Env-var contract change:** `VOICE_CONFIG` (JSON blob) → `VOICE__VAD_THRESHOLD` etc. Mandatory Phase 0 audit: grep `.env*`, `frontend/.env*`, any deployment manifest, Docker compose files, CI secrets. If the proposal says "expected: none", the PR needs the grep output proving it.

Find-and-replace `settings.VOICE_CONFIG["key"]` → `settings.VOICE.key` across 5 files: `core/voice/processor.py`, `core/voice/wakeword_service.py`, `api/websockets/connection.py`, `api/websockets/handlers.py`.

### 5. Enforce the outcome with tooling, not a grep check

A grep count as an acceptance gate is a snapshot; it regresses the next time someone copies the pattern. Add two tools:

```bash
uv add --dev tach pydeps
```

- **`pydeps backend --show-cycles`** during Phase 0 audit (ground truth on real cycles) and after each phase.
- **`tach`** for ongoing enforcement. `tach init` once to declare module boundaries; `tach check` in pre-commit. It enforces: no cycles, imports only from declared deps, public interfaces via `__init__.py`. Zero runtime cost.

If adopting `tach` feels like overhead, `import-linter` with an `AcyclicContract` is a lighter alternative — it has `nominate_cycle_breakers` to tell you exactly which edges to cut.

---

## What this does NOT do

- **No PluginStateStore abstraction.** The registry's ~5-line MongoDB call stays inline. Injecting a store interface just so two methods can swap backends is overengineering for a stable, single-implementation surface.
- **No `services/ui_bus.py` extraction.** Earlier drafts proposed moving `push_ui` to a new module to "break a cycle" — but the cycle doesn't exist. Hoisting the imports in place is the whole fix.
- **No `core/plugins/ui.py` re-export shim.** No external callers (verified: 10 internal, 0 notebooks/external). If anything, the module may stay or shrink, not redirect.
- **No prompt-policy extraction.** Plugin docstrings mixing runtime contract and voice-narration policy (e.g. `calendar.get_events`) is real debt but needs a policy-loading system that doesn't exist. Defer until a second reason appears.
- **No generalized DI framework.** `IntegrationManager` works. Don't replace it.
- **No plugin scaffold / generator script.** Cosmetic; can be added anytime later.
- **No co-located tests migration.** `backend/tests/` stays.

---

## Risks

- **File split breaks imports for external callers.** Mitigation: `__init__.py` re-exports with explicit `__all__` preserve the public API. `from core.integrations.lifecycle import ...` keeps working.
- **Hoisting imports surfaces a real cycle.** Mitigation: expected for 0–2 files (to be confirmed by Phase 0 `pydeps` output). Fix by extracting the cyclic symbol to a neutral module or guarding with `if TYPE_CHECKING:`, not by re-localizing the import.
- **`VoiceConfig` env-var contract change.** Mitigation: Phase 0 audits all env sources and documents the result inline. If a `VOICE_CONFIG` JSON env var exists anywhere, add a one-release compatibility shim that reads it and maps to the new fields.
- **Consolidating scheduler/recurrence touches two production call sites** (`services/scheduler.py` fire loop + `plugins/scheduler.py` tools). Mitigation: integration-test the fire loop against the new `core.scheduling.recurrence.next_occurrence` before ripping out the duplicate. Keep changes behavior-preserving (same outputs, same tz handling).
- **`plugins/scheduler.py` → package form** (if we also convert the tool surface): verify the plugin scanner (`core/plugins/registry.py`) discovers package-form plugins identically to file-form. Likely fine (Python treats `plugins.scheduler` as the package `__init__`), but worth explicit testing.

---

## Phase plan

**Phase 0 — Ground truth (~1h)**
- `uv add --dev tach pydeps`.
- `pydeps backend --show-cycles -o docs/analysis/module-graph-before.svg`.
- Grep `.env*`, deployment configs, Docker compose for `VOICE_CONFIG` usage; document results in the PR description.

**Phase 1 — Voice config typing (~1h)**
- Add `VoiceConfig(BaseModel)` with `nested_model_default_partial_update=True`.
- Migrate `class Config:` → `SettingsConfigDict`.
- Find-and-replace 5 call sites. Ships first; no dependencies.

**Phase 2 — Hoist imports (~3h)**
- `from __future__ import annotations` on files that need it.
- Hoist every local import in `core/plugins/ui.py` and callers.
- Run tests. Fix any real cycle by extraction or `TYPE_CHECKING`, not re-localization.
- Re-run `pydeps` to confirm zero cycles.

**Phase 3 — Orchestrator split (~3h)**
- Extract `HeadlessTurnPool` directly to `services/headless_pool.py`.
- Extract routing/classification to `core/turns/routing.py`.
- `orchestrator.py` shrinks to lifecycle + public class.

**Phase 4 — Scheduler / recurrence consolidation (~4h)**
- Create `core/scheduling/{recurrence,time_parsing,occurrence}.py` as free-function modules.
- Rewrite `plugins/scheduler.py` tool methods to call them.
- Rewrite `services/scheduler.py` fire loop to call them. Delete the duplicate `next_occurrence`.
- Audit `plugins/time_utils.py` — consolidate or leave. Do not create a third home.

**Phase 5 — Lifecycle split (~2h)**
- `core/integrations/lifecycle/{__init__,_shared,composio,bespoke}.py` with explicit `__all__`.
- Verify 5 existing callers still work unchanged.

**Phase 6 — Enforcement (~1h)**
- `tach init` + curate `tach.toml` for the post-cleanup boundaries.
- Add `tach check` to `.pre-commit-config.yaml`.
- Commit `docs/analysis/module-graph-after.svg` alongside the before snapshot.

Phases 0–3 total: ~1 day. Phases 4–5: land when touching those surfaces. Phase 6: opportunistic but do it before the cleanup regresses.

---

## Open questions

- **Is `tach` worth the ongoing config overhead, or is `import-linter` with a single `AcyclicContract` enough?** Default recommendation: start with `import-linter` Acyclic contract (one config block); upgrade to `tach` if we ever need declared-dep enforcement beyond cycles.
- **Does `plugins/time.py` overlap with the proposed `core/scheduling/time_parsing.py`?** Resolve during Phase 4 audit — consolidate in the same PR if yes, leave untouched if no.
- **Should `Settings.VOICE` stay flat-named (`VOICE`) or rename to `voice`?** Current convention is upper-snake (`MONGODB_PORT`, `LLM_MODEL`). Keep `VOICE` for consistency; sub-model field access (`settings.VOICE.vad_threshold`) reads fine either way.
