# UI Action Bus

> **Partial** — Live: `ui.action` → plugin tool. Historical: decorator bus below.

**Status:** Partially implemented — `ui.action` → `process_ui_action()` (plugin + tool + args) is live (`handlers.py`, `PendingInputWidget`, `TodoWidget`, `InboxWidget` archive/mark-read). The **decorator/registry bus** design in this doc was not built and should not be implemented as-is.
**Date:** 2026-04-19
**Priority:** Extend the shipped plugin-tool path to more widgets as needed ([ROADMAP](../ROADMAP.md) Phase 10 marks the initial Gmail slice done). Do not build the historical registry design below unless patch-only semantics prove necessary.
**Depends on:** Direct Action Bus design (already documented in `FRONTEND_ARCHITECTURE.md §5`); WebSocket handler (`backend/api/websockets/handlers.py`); existing `PendingInputWidget` consent pattern (the proof-of-concept this generalizes).

---

## Problem

Today the UI talks to the backend through one of two paths:

| Path | Latency | Used by |
| :--- | :--- | :--- |
| Voice / text turn → LLM → tool → response | 1.5–3s | Everything the user types or says |
| `ui.action` -> plugin tool | <50ms plus backend work | `PendingInputWidget`, `TodoWidget`, `InboxWidget`, and other direct widget actions |

`FRONTEND_ARCHITECTURE.md §5` calls the second path the "Direct Action Bus" and frames it as a general pattern. The shipped implementation routes `ui.action` to plugin tools with `{ plugin, tool, args }` and returns optional `ui.update` envelopes. That is simpler than the registry/patch design below and should remain the default unless repeated patch-only interactions justify another layer.

The pattern works (consent flow proves it). It's just never been generalized.

---

## Design

A typed action registry on the backend + one new WebSocket message type. That's it.

### Backend: `core/plugins/ui_actions.py`

```python
from typing import Callable, Awaitable, TypedDict, NotRequired

class ActionResult(TypedDict, total=False):
    patch: dict          # partial UIEnvelope.data merged into the source widget
    follow_up: str       # text injected as a system turn — re-engages the LLM
    sound: str           # optional notification sound key

ActionHandler = Callable[[dict, "Session"], Awaitable[ActionResult | None]]

_REGISTRY: dict[str, ActionHandler] = {}

def ui_action(name: str) -> Callable[[ActionHandler], ActionHandler]:
    def decorate(fn: ActionHandler) -> ActionHandler:
        if name in _REGISTRY:
            raise ValueError(f"ui_action {name!r} already registered")
        _REGISTRY[name] = fn
        return fn
    return decorate

async def dispatch(name: str, params: dict, session: "Session") -> ActionResult | None:
    handler = _REGISTRY.get(name)
    if handler is None:
        raise KeyError(f"unknown ui_action {name!r}")
    return await handler(params, session)
```

Three return shapes, all optional:

| Return | Frontend behavior | Use when |
| :--- | :--- | :--- |
| `None` | No-op (handler already mutated state) | Pure side effect, optimistic UI handles the visual |
| `{patch: {...}}` | Shallow-merge into source widget's `data` | "Mark done" → flip the checked flag on a list item |
| `{follow_up: "..."}` | Inject as system-source turn, run through agent | "Tell me more" / "explain this" buttons |

Plugins register handlers like tools:

```python
# backend/plugins/gmail/__init__.py
from core.plugins.ui_actions import ui_action

@ui_action("gmail.archive")
async def _(params: dict, session: Session) -> dict | None:
    msg_id = params["message_id"]
    await session.deps.gmail.archive(msg_id)
    return {"patch": {"emails": [...]}}  # widget refreshes its row list
```

### Frontend: extend the existing `ui.action` message

The shipped shape is plugin/tool based rather than registry-key based:

```ts
// outbound from frontend
client.send('ui.action', {
  plugin: string,
  tool: string,
  args: Record<string, unknown>,
})

// inbound (only if the tool returns UI)
{ type: 'ui.update', data: UIEnvelope }
```

Widgets should usually return a fresh full envelope, not a patch. Add patch semantics only if real interactions show repeated full-envelope updates are too heavy or awkward.

The `follow_up` path reuses the existing `system_context` orchestrator entry — the same prompt-building path used by trigger deliveries, just with a widget-originated caller.

### Validation and safety

- Handlers receive `Session` so they can check `trust_level` (when that lands per the OpenClaw lessons doc) and reject guest-initiated destructive actions.
- Action names are namespaced (`gmail.archive`, `todo.complete`) — the same plugin-prefix convention tools use. No anonymous actions.
- An `_REGISTRY` collision check at registration prevents two plugins from claiming the same name.
- Idempotency keys are NOT in v1; add per-handler when a real double-fire bug appears (per OpenClaw lesson §1).

---

## What this enables, concretely

The capability is small but the unlock list is real and non-speculative — every entry below is a button click that exists in the current product and currently round-trips through the LLM:

| Action | Today | After |
| :--- | :--- | :--- |
| Approve / deny destructive op | Direct (only widget that does this) | Direct (unchanged) |
| Archive email from `InboxWidget` | Direct (archive / mark-read buttons) | Direct (unchanged pattern) |
| Mark todo done from `TodoWidget` | LLM round-trip | <50ms |
| Snooze alarm from notification | LLM round-trip | <50ms |
| Dismiss System Pulse alert | LLM round-trip | <50ms |
| Cancel running background task | LLM round-trip via `cancel_task` tool | <50ms |
| "Reconnect" / "Show details" / "Open in browser" buttons | Doesn't exist yet | Possible |

Every one of these is something the user already decided on; none of them benefit from LLM mediation.

---

## Why this stays out of `CONTENT_WIDGET_EXPANSION.md`

The companion proposal (`CONTENT_WIDGET_EXPANSION.md`) adds `button` and `input` blocks that consume this bus. But the bus has standalone value: existing bespoke widgets (`InboxWidget`, `BackgroundTaskWidget`, future `TimerWidget`) can wire buttons through it without any visual primitives changing. Bundling the two means neither ships until both are ready, and the visual-primitives question is the more speculative half of the pair.

The plugin-tool path is shipped. `InboxWidget` was the first daily-action smoke test (archive / mark read via `gmail.archive_email` / `gmail.mark_read` with optimistic UI). Wire additional widgets through the same `{ plugin, tool, args }` shape rather than introducing a second registry layer.

---

## Phase plan (historical — registry bus not built)

**Phase 1 — Shipped via `process_ui_action()` (not the registry below)**
- WS handler in `backend/api/websockets/handlers.py` for `ui.action`; validates schema, calls plugin tool through `process_ui_action()`, returns `ui.update` if the tool produced one.
- `InboxWidget` row actions call `gmail.archive_email` / `gmail.mark_read` with optimistic local state.
- Frontend store upserts returned `ui.update` envelopes when tools push them.

**Phase 2 — Migrate consent (~0.5 day)**
- `PendingInputWidget` already resolves approvals through `ui.action` -> `system.resolve_pending_input`.
- Confirms the plugin-tool router holds for the hardest existing case.

**Phase 3 — Wire the obvious wins (~0.5 day, opportunistic)**
- Todo complete, snooze alarm, cancel background task, dismiss System Pulse alert.
- Each is a 5-minute change once the bus exists.

Total: ~1.5 days, ships in three independent steps.

---

## Open questions

- **Trust level enforcement.** When session trust levels exist (per OpenClaw lessons §4), the dispatcher should consult them before invoking a handler. v1 ships without this — single-user assumption holds — but the seam is `dispatch(..., session)`, so the addition is local.
- **Whether `follow_up` should be a separate message type.** Keeping it inside the action result means one round-trip; making it a separate `system_text` send means two but is simpler to reason about. Lean toward the unified result for now; revisit if the LLM re-engagement flow needs more metadata.
- **Should handlers be allowed to push *new* widgets?** They already can via `push_ui()`. Whether to encourage it (handler returns a fresh widget) is a style question, not an architectural one. Default: handlers patch their source widget; if they want to push new ones, they call `push_ui()` directly.
