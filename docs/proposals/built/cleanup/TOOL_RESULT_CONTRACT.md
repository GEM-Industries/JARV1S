# Unified Tool Result Contract

**Status:** Built. Prefix/return-protocol follow-on superseded by the 2026-08-13 typed-returns cutover; see [`PLUGIN_ARCHITECTURE.md`](../../PLUGIN_ARCHITECTURE.md).
**Date:** 2026-04-24
**Priority:** Medium. Depends on the foundational proposals landing first. Delete real ambiguity without adding ceremony.
**Depends on:** [`PLUGIN_CONTRACT_CLEANUP.md`](./PLUGIN_CONTRACT_CLEANUP.md) (for the cleaner `@tool` base). Optional but cleaner after [`PLUGIN_REPOSITORY.md`](./PLUGIN_REPOSITORY.md).

---

## Problem

Tools can return in three completely different shapes today, often all within the same plugin:

1. **Plain `str`** — spoken/displayed to the user. Example: `TodoPlugin.add_task`.
2. **`UIEnvelope`** — detected by `core/plugins/ui_handler.py` during UI-initiated actions and forwarded as a widget update.
3. **Side-effect via `push_ui()`** — callable anywhere, dispatches through either an event bus *or* a tool-output buffer via contextvar (see `core/plugins/ui.py`). Example: `TodoPlugin.add_task` **also** calls `push_ui(...)` before returning its string.

Consequences:

- **No single way to "return a result with a widget update."** Every plugin author picks their own pattern, usually whatever the nearest example uses.
- **`push_ui()` has two invisible modes.** From the call site there is no way to tell whether the widget will dispatch immediately or get buffered into the executor's output.
- **`render_{plugin}_widget` fallback in `ui_handler.py`** exists to refresh widgets after UI-initiated actions whose tool return wasn't a `UIEnvelope`. Its presence compensates for the return-shape inconsistency even though its original purpose is legitimate.

Consent is tracked separately (`core/plugins/consent.py`) and its magic-string sentinel (`"APPROVAL_NEEDED: ..."`) has the same "avoid magic-string sentinels" smell — but it's a cross-cutting concern best handled as a pre-tool-use hook, not a return-value shape. **Out of scope here.** See [Deferred: consent cleanup](#deferred-consent-cleanup).

---

## Prior art

This design mirrors two production patterns:

- **MCP 2025-11-25** ([`CallToolResult`](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)) splits results into `content` (human/model-oriented text) and `structuredContent` (typed JSON for clients). Semantically equivalent when both present; clients SHOULD NOT feed both to the model.
- **LangChain `ToolMessage`** uses `content` (what the model sees) + `artifact: Any` (full data not sent to the model).

`ToolResult` below is the two-field version of both: `content` for the LLM, `ui` for the frontend.

---

## Design

Introduce one optional return type. Existing `return str` keeps working unchanged — this is **additive**, not a rewrite.

### `ToolResult`

```python
# backend/core/plugins/result.py
from pydantic import BaseModel, Field
from core.plugins.types import UIEnvelope

class ToolResult(BaseModel):
    """Envelope for tool output.

    - `content` — the string the LLM sees (MCP `content` / LangChain message content).
    - `ui`      — side-channel widgets for the frontend (MCP `structuredContent` /
                  LangChain `artifact`). Never shown to the model.
    """
    content: str
    ui: list[UIEnvelope] = Field(default_factory=list)
```

No generics. Typed non-textual returns keep using direct return annotations (`-> list[TodoItem]`) — those already route through `_extract_return_schema` in `core/decorators.py` and are the *preferred* shape per MCP's guidance. `ToolResult` is only for the real minority case: "text reply + side UI."

### Unwrap lives in the `@tool` decorator

Tools are called from LLM-generated Python code via `jarvis.<plugin>.<tool>(...)` — there is no single dispatcher to wrap. The only correct seam is the `@tool` wrapper in `backend/core/decorators.py:68-126`.

Today `@tool` returns the function as-is when `inject=()`. That fast path goes away; every `@tool`-decorated function now passes through one wrapper:

```python
# backend/core/decorators.py (inside decorator(fn))
@wraps(fn)
async def wrapper(*args, **kwargs):
    for param in inject_params:  # existing injection logic
        ...
    result = await fn(*args, **kwargs)
    if isinstance(result, ToolResult):
        from core.plugins.ui import push_ui
        for env in result.ui:
            push_ui(env)
        return result.content
    return result
```

The wrapper overhead is one `isinstance` check per tool call. Measured in nanoseconds.

### One additional site: `process_ui_action`

UI-initiated actions flow through `core/plugins/ui_handler.py::process_ui_action` (called from `api/websockets/handlers.py`), which does its own `isinstance(result, UIEnvelope)` check. That branch gains a single additional case:

```python
# backend/core/plugins/ui_handler.py
if isinstance(result, ToolResult):
    ui_update = result.ui[0] if result.ui else None
    result = result.content
elif isinstance(result, UIEnvelope):
    ui_update = result
else:
    render_tool = tools.get(f"render_{plugin_name}_widget")  # existing fallback
    ...
```

### Manifest schema stays honest

The LLM sees the tool's declared return annotation, not `ToolResult`. Because the annotation `-> ToolResult` resolves to `{content: str, ui: [...]}` via `TypeAdapter`, `_extract_return_schema` in `core/decorators.py:38-65` needs one extra line: when the annotation is `ToolResult`, treat it as `str` for schema purposes (the LLM only ever sees `content`).

---

## Migration

Not opportunistic — **scoped and bounded**. Five plugins today return `str` while side-effecting `push_ui()` for widget refresh:

- `plugins/todo.py`
- `plugins/profile.py`
- `plugins/protocol.py`
- `plugins/scheduler.py`
- `plugins/automations.py`

Those five plugins get converted in Phase 2. `push_ui()` stays as an internal primitive (used by `consent.py`, `ui_handler.py`, and background/streaming callers), but is removed from the plugin-author-facing surface. `.cursor/rules/plugin-tool-conventions.mdc` is updated to point at `ToolResult.ui` and drop the `push_ui()` recipe.

### Example: `TodoPlugin.add_task`

**Before (`plugins/todo.py:79-93`):**
```python
async def add_task(self, text: str, group_id: str = "inbox") -> str:
    groups = await self._get_state()
    group = next((g for g in groups if g["id"] == group_id), groups[0])
    new_item = TodoItem(text=text)
    group["items"].append(new_item.dict())
    await db.store_tool_data("todo", {"groups": groups})

    from core.plugins.ui import push_ui
    ui = await self.render_todo_widget(group_id=group["id"])
    push_ui(ui)

    return f"Success: Added '{text}'"
```

**After:**
```python
@tool
async def add_task(self, text: str) -> ToolResult:
    """Add a new task. Widget auto-refreshes."""
    items = await self._get_state()
    items.append(TodoItem(text=text))
    await db.store_tool_data("todo", {"items": [i.model_dump() for i in items]})
    return ToolResult(
        content=f"Added '{text}'",
        ui=[await self._todo_envelope(items)],
    )
```

`render_todo_widget` becomes `_todo_envelope(items) -> UIEnvelope` — pure, async envelope construction with no side effects. Pairs naturally with `PLUGIN_REPOSITORY.md` if that lands, but doesn't require it.

---

## What this does NOT do

- **Does not remove `push_ui()` internally.** It stays for `consent.py`, `ui_handler.py`, and streaming/progress cases where a tool emits intermediate widgets during long work. Only the plugin-author-facing recommendation changes.
- **Does not touch consent.** `require_consent()` and its sentinel string stay exactly as-is. See [Deferred: consent cleanup](#deferred-consent-cleanup).
- **Does not add a "follow-up turn" shape** like [`UI_ACTION_BUS.md`](../../UI_ACTION_BUS.md)'s `follow_up`. Different surface (UI-initiated vs LLM-initiated).
- **Does not change `UIEnvelope` itself.**
- **Does not generalize to streaming partial results.** One return per tool call.

---

## Risks

- **Every `@tool` now goes through a wrapper.** Previously, tools with no `inject=` returned `fn` directly. One extra `isinstance` check per call. Trivial, but noted because it affects every tool in the system.
- **Schema extraction for `ToolResult` annotations.** Without special-casing, `TypeAdapter(ToolResult).json_schema()` surfaces `{content, ui}` in the manifest. Mitigation: one branch in `_extract_return_schema` that treats `ToolResult` as `str`. Covered by a unit test per manifest density (`compact`, `full`).
- **`process_ui_action` must recognize `ToolResult` too.** Missed in naive designs that only patch the executor path. Handled explicitly above.
- **`CodeExecutor._run` final-return branch.** Lines 162-175 currently special-case `UIEnvelope` and `BaseModel` for the final expression of LLM-generated code. `ToolResult` is a `BaseModel`; without handling it'd JSON-dump as `{"content": "...", "ui": [...]}` into the output buffer. Add a `ToolResult` branch that `push_ui`s each envelope and writes `content` to the buffer — same logic as the decorator wrapper, one place.

---

## Phase plan

**Phase 1 — Types + unwrap seams (~4h)**
- Add `backend/core/plugins/result.py` with `ToolResult`.
- Decorator: wrap every `@tool` (drop the inject-less fast path). Unwrap `ToolResult` before return.
- `ui_handler.process_ui_action`: add `ToolResult` branch before the existing `UIEnvelope` branch.
- `executor._run`: add `ToolResult` branch to the final-return handler.
- `decorators._extract_return_schema`: `ToolResult` → `str` shortcut.
- Tests: decorator unwrap, `process_ui_action` unwrap, executor final-return unwrap, manifest schema shows `str` not `ToolResult`.

**Phase 2 — Migrate the five widget-pushing plugins (~3h)**
- `todo`, `profile`, `protocol`, `scheduler`, `automations`.
- Each: replace `push_ui(...)` + `return str` with `return ToolResult(content=..., ui=[...])`.
- Extract inline `render_*_widget` helpers into `_*_envelope()` methods that return `UIEnvelope` without side effects. Keep the public `render_*_widget` tool for UI-initiated refresh where it's still called.
- Update `.cursor/rules/plugin-tool-conventions.mdc` — remove `push_ui()` from the author-facing recipe, add `ToolResult.ui`.

**Phase 3 — Audit (~1h)**
- `rg 'push_ui\(' backend/plugins/` should return zero hits (aside from the five plugins' internal refresh tools if retained). Any stragglers convert or move their `push_ui` use behind a documented exception.

Total: ~8h (one day). Shippable as one PR or split Phase 1 / Phase 2+3.

---

## <a name="deferred-consent-cleanup"></a>Deferred: consent cleanup

The `"APPROVAL_NEEDED: <desc>"` sentinel deserves its own cleanup but doesn't belong in the return-envelope. Industry consensus ([Anthropic Agent SDK `PreToolUse` hooks](https://docs.claude.com/en/agent-sdk/user-input), [production HITL guide](https://claudelab.net/en/articles/api-sdk/claude-api-human-in-the-loop-agent-approval-gate-production-guide)) is to insert approval as a **pre-tool-use lifecycle hook** — the gate fires before the tool runs, state persists separately, and rejection returns `is_error: true` to the model. That's a different refactor. File as a follow-up proposal.

---

## Open questions

- **Should `ToolResult` also carry a `status: Literal["success", "error"]` like LangChain's `ToolMessage`?** Not yet. Errors today are raised exceptions; adding a parallel status field risks two truths. Revisit if/when the consent cleanup lands and needs `"pending_approval"`.
- **Should the executor dedupe multiple `UIEnvelope` pushes for the same `widget_id`?** No. Last-write-wins at the frontend via `widget_id` is the update key. Don't add server-side dedupe until a real flicker bug demands it.

---

## References

- MCP Tools spec (2025-11-25): https://modelcontextprotocol.io/specification/2025-11-25/server/tools
- MCP SEP-1624 (content vs structuredContent): https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1624
- LangChain `ToolMessage.artifact`: https://reference.langchain.com/python/langchain-core/messages/tool/ToolMessage
- Anthropic Agent SDK approvals: https://docs.claude.com/en/agent-sdk/user-input
