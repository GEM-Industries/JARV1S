# Plugin Repository (Typed Persistence Helpers)

**Status:** Built
**Date:** 2026-04-24
**Priority:** Medium. Real maintainability win, but narrower scope than it first looked — only `todo` + `profile` facts actually fit. ~3h to ship, deletes ~70–100 lines of duplicated logic.
**Depends on:** [`PLUGIN_CONTRACT_CLEANUP.md`](./PLUGIN_CONTRACT_CLEANUP.md) is **required** for the example `TodoPlugin` as written (uses `PluginMetadata`). If that proposal slips, fall back to keeping `get_tools()` + dict-metadata in the migrated plugins.

---

## Problem

Two plugins reimplement the same load-mutate-save loop around `plugins.db.store_tool_data` / `get_tool_data`:

```python
# plugins/todo.py — appears 6 times in this one file
async def add_task(self, text: str, group_id: str = "inbox") -> str:
    groups = await self._get_state()
    group = next((g for g in groups if g["id"] == group_id), groups[0])
    new_item = TodoItem(text=text)
    group["items"].append(new_item.dict())
    await db.store_tool_data("todo", {"groups": groups})
    ...
```

The same shape appears in:

- `plugins/todo.py` — `TodoItem` collections (6 mutation points on `store_tool_data`).
- `plugins/profile.py` — core facts only (`_load_facts` / `_save_facts`). Facts are stored as raw dicts today, not Pydantic models.

Both:
1. Fetch the whole collection.
2. Deserialize from dicts by hand (todo) or keep them as dicts (profile).
3. Mutate in place.
4. Reserialize via `.model_dump()` / `.dict()`.
5. Write the whole collection back.
6. Push a refreshed widget (todo only).

No typed access on either path. `profile` facts don't even have a Pydantic model.

### What's explicitly NOT in scope

- **`plugins/protocol.py`** — does not use `plugins.db` at all. Uses `mongodb.db["protocols"]` with per-document `insert_one`/`update_one`/`delete_one`, case-insensitive regex lookup (`$regex ... $options: "i"`), and `$inc` on a run counter. Protocol execution now flows through the trigger/protocol-run stores rather than legacy `alerts` / `schedules` collections. Load-all/save-all would be a regression.
- **`plugins/profile.py` archival events** — live in the `memories` Mongo collection with per-doc embeddings, TTL, and semantic-similarity search in `recall()` / `forget()`. Doesn't fit a kv-under-one-doc shape.
- **`plugins/scheduler.py`** — migrated to the trigger substrate (`trigger_rules` / `trigger_instances`) for scheduled reminders, timers, alarms, and recurring series. Separate module cleanup is tracked in [`MODULE_GRAPH_CLEANUP.md`](./MODULE_GRAPH_CLEANUP.md).

---

## Design

Pick the **smallest thing that removes the duplication**. We have 1.5 real callers; a generic `Repository[T]` class is premature abstraction at that scale (see "Considered and rejected" below). Ship typed helpers on `plugins/db.py` instead:

```python
# backend/plugins/db.py — additions
from typing import TypeVar
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

async def load_models(tool_name: str, model: type[T], *, key: str = "items") -> list[T]:
    """Load a list of Pydantic documents from a user-scoped tool_data doc."""
    data = await get_tool_data(tool_name)
    return [model(**d) for d in data.get(key, [])]

async def save_models(tool_name: str, items: list[BaseModel], *, key: str = "items") -> None:
    """Replace the list of Pydantic documents in a user-scoped tool_data doc."""
    await store_tool_data(tool_name, {key: [i.model_dump() for i in items]})
```

That's it. No class, no `Generic[T]`, no `id_field` abstraction, no `.get()` / `.update()` / `.delete()` helpers — callers keep list-comprehension semantics they already understand. Storage is the same kv-under-one-doc shape; concurrency is the same last-write-wins it already is.

### What `TodoPlugin` looks like after migration

Assumes `PLUGIN_CONTRACT_CLEANUP` has landed (`PluginMetadata`, auto-registered `@tool` methods). If it hasn't, keep the existing dict-metadata + `get_tools()` form and only swap the persistence calls.

```python
class TodoPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="todo",
        version="2.0.0",
        core=True,
        description="Simple task management with server-driven UI.",
    )

    @tool
    async def get_tasks(self) -> list[TodoItem]:
        """List all tasks. Never read .id aloud."""
        return await db.load_models("todo", TodoItem)

    @tool
    async def add_task(self, text: str) -> str:
        """Add a new task."""
        items = await db.load_models("todo", TodoItem)
        items.append(TodoItem(text=text))
        await db.save_models("todo", items)
        await self._refresh_widget()
        return f"Added '{text}'"

    @tool
    async def toggle_task(self, task_id: str) -> str:
        """Toggle completion for a task by id."""
        items = await db.load_models("todo", TodoItem)
        for i, item in enumerate(items):
            if item.id == task_id:
                items[i] = item.model_copy(update={"completed": not item.completed})
                await db.save_models("todo", items)
                await self._refresh_widget()
                return "Task updated"
        raise ValueError("Task not found")
```

`plugins/todo.py` goes from 224 lines to ~130–150 lines (imports, `TodoItem`, `TodoPlugin`, 6 tool methods, `render_todo_widget`). The previously advertised "~80" was not credible once `render_todo_widget` (~50 lines on its own) is accounted for.

### Breaking changes

1. **`group_id` parameter is dropped from all todo tools** (`get_tasks`, `add_task`, `clear_tasks`, `render_todo_widget`). Only `"inbox"` has ever existed at runtime, but this is still a change to the **LLM-facing tool signature**, not a pure internal refactor. Prompt manifests and any persisted tool-call history referencing `group_id` will need to tolerate the drop.
2. **`TodoGroup` class is deleted.** `render_todo_widget` recomputes `progress` inline. Frontend `TodoWidget.tsx` already only reads `title`/`items`/`progress` — no frontend change needed.
3. **Empty-state behavior changes slightly.** Current code auto-seeds an `inbox` group on first read (`todo.py:62-64`). After migration, fresh users see an empty list until they add something. Harmless but worth calling out in the changelog.

If groups ever come back as a real feature, add a `group_id` field to `TodoItem` and filter client-side — don't bring back the nested-collection shape.

### What about `profile` facts?

Two changes:
1. Introduce a `Fact(BaseModel)` model (`text: str`, `added: date`, `source: str = "explicit"`). Facts are currently raw dicts.
2. Replace `_load_facts()` / `_save_facts()` with `db.load_models("profile", Fact, key="facts")` / `db.save_models("profile", facts, key="facts")`.

The `key="facts"` keeps the wire format identical — no data migration. Archival events (`memories` collection) are untouched.

---

## Migration order

1. **`plugins/todo.py`** — cleanest example, highest boilerplate density. Lands first as the pattern reference.
2. **`plugins/profile.py` (facts only)** — introduces the `Fact` model as part of the migration. Validates that the helpers handle a non-default `key=` correctly. Archival events stay on raw Mongo.

Total: ~3h across two independent PRs.

---

## What this does NOT do

- **No `Repository[T]` class, no `Generic[T]`.** Two callers is below the bar for a generic abstraction. See "Considered and rejected."
- **No query DSL, no indexes, no unique constraints, no pagination.** If those become real needs for `todo` or `facts`, the right move is migrating those collections out of the kv-under-one-doc shape — not bolting features onto this layer.
- **No concurrency primitives.** `store_tool_data` is last-write-wins already; no plugin has shown concurrent-write bugs.
- **No migration framework.** Pydantic's own `model_validator` covers field-rename / default shifts. If a destructive migration is ever needed, write a one-off script.
- **Does not touch `protocol.py`, `scheduler.py`, or profile's archival events.** Those are per-document Mongo or daemon-queried; the kv helpers don't fit.
- **No caching.** Every call hits Mongo. `store_tool_data` already does.

---

## Considered and rejected

### `pydantic-mongo` ([AsyncAbstractRepository](https://pydantic-mongo.readthedocs.io/en/stable/))

Mature library, exact typed-CRUD shape we'd want. **Assumes per-document Mongo storage** (one Pydantic model = one Mongo doc, keyed by `_id`), which our `plugins.db` kv-under-one-doc pattern is not. Adopting it would require migrating `todo` and `facts` to their own Mongo collections first — a bigger change than this proposal is trying to make. Keep as a future option if/when those collections grow out of the kv shape.

### Beanie ODM ([beanie 2.1.0, Mar 2026](https://github.com/roman-right/beanie))

Full async ODM on Motor + Pydantic. Same structural mismatch as `pydantic-mongo` — per-document, not kv-under-one-doc. Would be the right answer if we wanted a unified ODM across `protocols`, `memories`, `trigger_rules`, `trigger_instances`, automation runtime collections, and `conversations` — but that's a stack-level decision, not a Todo-plugin-cleanup decision. Track separately.

### Generic `DocumentRepository[T]` class

Original design. Rejected because:
- Only 1.5 real callers at current scope. Generic abstraction over two concrete implementations is the textbook premature-generics shape (see Go/C# community consensus on generic-repository as anti-pattern).
- Adds a new module (`core/plugins/repository.py`), a `Generic[T]`, and ~80 lines for what two module-level helpers deliver in ~15.
- Makes the kv-under-one-doc shape look load-bearing, when actually the right long-term move is to get out of that shape.

---

## Future work (not blocking this proposal)

- **If `todo` grows** (thousands of items, per-item queries, TTLs, atomic ops): migrate to a dedicated `todo_items` Mongo collection indexed on `user_id + created_at` and adopt `pydantic-mongo`'s `AsyncAbstractRepository[TodoItem]`. The typed helpers here don't block that migration.
- **If the whole backend adopts Beanie:** that's a separate stack-level RFC covering `protocols`, `memories`, `trigger_rules`, `trigger_instances`, automation runtime collections, and `conversations`. Would subsume this proposal.

---

## Risks

- **`.model_dump()` on every write re-serializes the full list.** Already true today with `store_tool_data`. No regression. Becomes a real cost at thousands of items per user, which isn't realistic for these plugins.
- **Two collections on the same `tool_name` with different `key=` values share a MongoDB doc.** `ProfilePlugin` will exercise this (`key="facts"`). Document in the helper docstrings.
- **Dropping `group_id` from todo tools is LLM-visible.** Covered under "Breaking changes" above. Update any protocols/persona prompts that reference the parameter.

---

## Phase plan

**Phase 1 — Helpers + todo migration (~2h)**
- Add `load_models` / `save_models` to `plugins/db.py` with tests.
- Migrate `TodoPlugin`. Delete `_get_state()`, `TodoGroup`, and the per-method save blocks.
- Verify widget still refreshes correctly through existing `push_ui` path.
- Update any persona/protocol prompts that reference `group_id`.

**Phase 2 — Profile facts migration (~1h)**
- Introduce `Fact(BaseModel)`. Replace `_load_facts` / `_save_facts`. Archival events untouched.

Total: ~3h across two independent PRs.

---

## Open questions

- **Should the helpers live in `plugins/db.py` or a new `core/plugins/persistence.py`?** `plugins/db.py` — they're thin wrappers over the existing module-level `store_tool_data` / `get_tool_data`. Creating a new module for 15 lines is the same overshoot this proposal is trying to avoid.
- **Should we expose `.list_sync()` for synchronous callers?** No. Nothing in the backend needs synchronous access to per-user plugin data.
- **Should the helpers enforce unique ids?** No — Pydantic's own validators do that at model level if the plugin cares. Keep persistence dumb.
