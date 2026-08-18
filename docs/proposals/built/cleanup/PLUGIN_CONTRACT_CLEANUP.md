# Plugin Contract Cleanup

**Status:** Built
**Date:** 2026-04-24
**Priority:** High. Foundation for everything else in the plugin-system refresh; ~1 day of work, removes a whole class of silent bugs.
**Depends on:** Nothing. This lands first.

---

## Problem

The plugin contract in `backend/core/plugins/types.py` is three places at once:

1. **Metadata is a freeform `Dict[str, Any]`.** Every plugin reimplements a partially-overlapping dict of `name`, `version`, `description`, `core`, `utterances`, `dependencies`, `routable`, `composio_app`. Nothing enforces which are required; misspellings are silent. `.cursor/rules/plugin-tool-conventions.mdc` carries the contract in prose instead of code.

2. **Tool names are declared twice.** Plugins write `@tool`-decorated methods **and** maintain a string-keyed dict:

   ```python
   def get_tools(self) -> Dict[str, Callable]:
       return {
           "get_tasks": self.get_tasks,
           "add_task": self.add_task,
           ...
       }
   ```

   Rename a method, forget the dict — the tool silently vanishes from the LLM manifest. No one notices until a runtime call fails.

3. **Plugin identity lives in three places.** Module name (`todo.py`), class name (`TodoPlugin`), `metadata["name"]`. Mismatches are silent; the MongoDB namespace, the runtime module, and the LLM tool prefix can drift.

---

## Design

Three surgical changes to `core/plugins/types.py` and `core/plugins/registry.py`. Nothing else moves.

### 1. Typed, frozen `PluginMetadata`

```python
# backend/core/plugins/types.py
from pydantic import BaseModel, ConfigDict

class PluginMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str = "0.1.0"
    description: str = ""
    core: bool = False
    hidden: bool = False
    routable: bool = True
    composio_app: str | None = None
    dependencies: list[str] = []
    utterances: list[str] = []
```

Field choices cover everything production code currently reads from metadata:

- `routable` — `core/tool_router.py:66` (`plugins/composio.py`, `plugins/display.py` opt out).
- `composio_app` — `core/integrations/lifecycle.py:325,458` (`plugins/spotify.py` uses it; future Composio-backed plugins will).
- `hidden` — replaces the hardcoded `_HIDDEN_PLUGINS = {"db", "profile", "composio"}` in `core/integrations/lifecycle/_shared.py`. Wired up in Phase 1 (see below); leaving the field without a consumer would be dead surface.

`extra="forbid"` is deliberate: unknown fields (e.g. typos like `composio_App`) become `ValidationError` at import time, which is exactly the silent-drift failure mode this proposal exists to kill. `frozen=True` makes metadata read-only and lets us declare it as a `ClassVar` (see below) without worrying about mutation.

### 2. `metadata` is a `ClassVar`, not a property

```python
from typing import ClassVar
from abc import ABC

class JarvisPlugin(ABC):
    metadata: ClassVar[PluginMetadata]
```

Existing plugins switch from `@property` returning a dict to a class-level assignment:

```python
class TodoPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="todo",
        version="1.1.0",
        description="Simple task management with server-driven UI.",
        core=True,
    )
```

One frozen instance per class — no rebuild on every `plugin.metadata.name` access. `scanner.py`, `tool_router.py`, and `lifecycle/` all read metadata on the hot path; the `@property` pattern allocates a new dict/model every call.

This also mirrors the container pattern used by `pydantic-ai`'s `FunctionToolset` (metadata declared at class scope, not recomputed).

### 3. Auto-discover tools from `@tool`-decorated methods

The `@tool` decorator already attaches `_tool_meta` to every method it wraps. `get_tools()` becomes a concrete method on the base class (not `@abstractmethod` anymore):

```python
class JarvisPlugin(ABC):
    def get_tools(self) -> Dict[str, Callable]:
        return {
            name: method
            for name, method in inspect.getmembers(self, inspect.ismethod)
            if getattr(method, "_tool_meta", None) is not None
        }
```

Plugins stop overriding `get_tools()` entirely. Every `@tool`-decorated method auto-registers under its own function name.

Two existing plugins don't fit the auto-discovery pattern and keep overriding `get_tools()` explicitly:

- **`DbPlugin`** — exposes module-level functions (`store_tool_data`, etc.), not methods. Keeps its manual dict.
- **`MCPBridgePlugin`** and Composio auto-bridge plugins — tool list is built dynamically at `__init__` time from the server's schema. Already overrides.

The override remains available; it's just no longer the default.

### 4. Invariant: module name = class-declared name, enforced at import

Rather than a boot-time check in `registry.load_plugins()`, use `__init_subclass__` (PEP 487). Drift is caught the moment the plugin module is imported, not after the whole plugin tree has been walked:

```python
class JarvisPlugin(ABC):
    metadata: ClassVar[PluginMetadata]

    def __init_subclass__(cls, register: bool = True, **kwargs):
        super().__init_subclass__(**kwargs)
        if inspect.isabstract(cls) or not register:
            return
        expected = cls.__module__.rsplit(".", 1)[-1]
        if cls.metadata.name != expected:
            raise PluginContractError(
                f"{cls.__name__}.metadata.name={cls.metadata.name!r} "
                f"must match module name {expected!r}"
            )
```

`register=False` is the escape hatch for classes whose name is known only at `__init__` time — `MCPBridgePlugin` and any future dynamic-bridge class declare `class MCPBridgePlugin(JarvisPlugin, register=False):`. Those classes also own the loosened name rules (e.g. MCP server names that aren't Python identifiers).

The `registry.load_plugins()` boot loop becomes simpler: if the module imports cleanly, the invariant has already passed.

---

## Name mismatches to resolve

Three existing bespoke plugins violate the module-name invariant today. They must be reconciled before enforcement lands; pick the direction once and apply it in the same PR:

| File | `metadata.name` today | Resolution |
|---|---|---|
| `plugins/composio_meta.py` | `"composio"` | Rename module → `plugins/composio.py` (keeps the `jarvis.composio.*` tool prefix, which prompts depend on). |
| `plugins/protocol.py` | `"protocols"` | Rename metadata → `"protocol"` (singular matches module; no prompts reference `jarvis.protocols.*`). |
| `plugins/time_utils.py` | `"time"` | Rename module → `plugins/time.py` (keeps `jarvis.time.now()` contract in `docs/CORE_TOOLS.md`; the file is not imported by other modules so the rename is local). |

Downstream touch-ups when `composio_meta` → `composio`:

- `core/integrations/lifecycle/_shared.py` — `_HIDDEN_PLUGINS` entry already says `"composio"`, no change. The set itself gets retired later in Phase 1 (see below).

Downstream touch-ups when `protocols` → `protocol`:

- Any stored MongoDB data under the `"protocols"` tool-data key (check `db.get_tool_data` callers in `plugins/protocol.py` — currently none; the plugin uses its own collection). Safe.
- No prompt or doc references `jarvis.protocols.*`.

Nothing else in the tree (searched with `rg 'jarvis\.(time|protocols|composio)\b'`) needs touching beyond these.

---

## Migration

Every existing plugin gets two mechanical changes. Example — `plugins/todo.py`:

**Before:**
```python
class TodoPlugin(JarvisPlugin):
    @property
    def metadata(self) -> Dict:
        return {
            "name": "todo",
            "version": "1.1.0",
            "description": "Simple task management with server-driven UI.",
            "core": True,
        }

    def get_tools(self) -> Dict[str, Callable]:
        return {
            "get_tasks": self.get_tasks,
            "add_task": self.add_task,
            "update_task": self.update_task,
            "toggle_task": self.toggle_task,
            "delete_task": self.delete_task,
            "clear_tasks": self.clear_tasks,
            "render_todo_widget": self.render_todo_widget,
        }
```

**After:**
```python
class TodoPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="todo",
        version="1.1.0",
        description="Simple task management with server-driven UI.",
        core=True,
    )
```

Plus `@tool` on every currently-undecorated tool method. The net LOC delta is **roughly flat**: ~10 lines of metadata+get_tools boilerplate deleted per plugin, ~7 `@tool` lines added per plugin. The win is the contract, not the line count — rename-safety and typed metadata are the whole point.

Tests that touch `plugin.metadata["name"]` become `plugin.metadata.name`. Find-and-replace. Same for all other metadata field reads across `core/tool_router.py`, `core/integrations/lifecycle/`, `core/integrations/utterance_cache.py`, and `core/plugins/registry.py`.

---

## Cleanup (delete after migration)

- `core/plugins/types.py` — remove the `name` / `description` `@property` shims (lines 59–65); callers use `plugin.metadata.name` / `plugin.metadata.description` directly.
- `core/plugins/types.py` — `get_tools()` loses `@abstractmethod`; the base class provides the default.
- `core/plugins/types.py` — remove the `PluginMetadata = Dict[str, Any]` type alias (line 12); replaced by the real model.
- `core/integrations/lifecycle/` — remove `_HIDDEN_PLUGINS` frozenset. Callers switch to `plugin.metadata.hidden`. Three plugins (`db`, `profile`, `composio`) gain `hidden=True` in their metadata.
- `.cursor/rules/plugin-tool-conventions.mdc` — trim the metadata-contract prose now that `PluginMetadata` is the source of truth; leave behavior/docstring conventions in place.

---

## What this does NOT do

- **Does not change the `@tool` decorator itself.** `inject=`, `manifest=`, signature extraction, return-schema caching all stay exactly as-is.
- **Does not introduce a plugin lifecycle state machine, a plugin DSL, or a plugin generator script.** Scope creep.
- **Does not change how tools are mounted onto the `jarvis.*` runtime module.** `register_tools()` and `mount_plugin_tools()` are untouched.
- **Does not refactor `IntegrationManager` or consent.** Separate proposals.
- **Does not adopt a `Toolset`-style composition layer** (filtering tools per-turn, tracing wrappers, combined toolsets). If that need emerges, the `pydantic-ai` `AbstractToolset` pattern is the reference. Out of scope here — noted so it's not reinvented badly later.

---

## Risks

- **Auto-discovery picks up methods that happen to have `_tool_meta` but aren't meant to be tools.** Mitigation: `@tool` is the only way `_tool_meta` gets set, and `@tool` is already the convention. If a plugin needs a decorated-but-hidden helper, explicitly override `get_tools()` to filter.
- **Plugins with no `@tool`-decorated methods load as empty.** Mitigation: the existing `manifest="compact"` default means adding `@tool` is a one-line change per method. Audit pass during migration.
- **Existing test fixtures may instantiate `JarvisPlugin` subclasses with raw metadata dicts.** Mitigation: Pydantic accepts dicts as input via `PluginMetadata(**d)`; the migration replaces those fixtures wholesale rather than shimming.
- **Dynamic-bridge classes (`MCPBridgePlugin`) can't declare `metadata` at class scope** because the name comes from runtime config. Mitigation: `register=False` opts them out of the `__init_subclass__` check; they keep computing metadata in `__init__` and keep overriding `get_tools()`.

---

## Phase plan

**Phase 1 — Types + invariant + hidden wire-up (~3h)**
- Add `PluginMetadata` Pydantic model (`frozen=True`, `extra="forbid"`, all documented fields).
- Convert `JarvisPlugin.metadata` to `ClassVar[PluginMetadata]`. Add `__init_subclass__` validator with `register=False` escape hatch.
- Replace `_HIDDEN_PLUGINS` lookups in `core/integrations/lifecycle/` with `plugin.metadata.hidden`. Delete the constant.
- Port `DbPlugin` first (smallest surface) to validate the shape.
- Resolve the three name mismatches (`composio_meta` → `composio`, `protocols` → `protocol`, `time_utils` → `time`).

**Phase 2 — Auto-discovery (~2h)**
- Concrete `get_tools()` on `JarvisPlugin` base (drop `@abstractmethod`). Add `@tool` to every currently-undecorated tool method.
- Delete `get_tools()` overrides where auto-discovery matches exactly.
- Declare `class MCPBridgePlugin(JarvisPlugin, register=False)` and any Composio auto-bridge class similarly.

**Phase 3 — Migrate remaining plugins (~3h, mechanical)**
- Flat-file plugins: `todo`, `scheduler`, `automations`, `protocol`, `profile`, `spotify`, `system`, `files`, `search`, `composio` (renamed), `display`, `time` (renamed).
- Package plugins: `calendar`, `weather`, `gmail`, `smart_home`, `agents`.
- Each is <10 minutes.

**Phase 4 — Cleanup (~30m)**
- Delete `name`/`description` `@property` shims, the `PluginMetadata = Dict[str, Any]` alias, and the prose contract in `.cursor/rules/plugin-tool-conventions.mdc`.
- `rg "metadata\[" backend/tests` and port any remaining dict-style accesses.

Total: ~1 day. Ships as one PR or split along phase boundaries.

---

## Open questions

- **Should `PluginMetadata.utterances` move out to a separate `plugin.utterances.json` sidecar file?** Not yet. Keep utterances next to the plugin that owns them until a real pain point emerges.
- **Should `metadata` support a `@classmethod` form for plugins whose metadata legitimately depends on runtime config (e.g. a single class spawning multiple named instances)?** Not today. The current codebase has exactly one pattern for that (`MCPBridgePlugin`) and `register=False` handles it. Revisit only if a second case appears.
