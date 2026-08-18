---
name: build-jarvis-plugin
description: Build, refactor, or review JARV1S backend plugins. Use when adding tools under backend/plugins, choosing plugin structure, wiring integrations, persistence, ToolResult UI, consent, routing utterances, or plugin tests/evals.
---

# Build JARV1S Plugin

Use this skill when adding, refactoring, or reviewing a backend plugin in `backend/plugins/`. Keep new plugins small and copy the closest working shape before adding abstractions.

## Pick The Smallest Shape

- Read-only context tool: copy `backend/plugins/activity.py`.
- Pure logic tool: copy `backend/plugins/time.py`.
- User-scoped CRUD + UI: copy `backend/plugins/todo.py`.
- Indexed/queryable domain: copy `backend/plugins/habits/`.
- External API integration: copy `backend/plugins/weather/` or `backend/plugins/search/`.
- Large provider/domain package: copy `backend/plugins/smart_home/`.

Prefer a single file until the plugin needs `models.py`, `store.py`, `client.py`, provider modules, or enough domain helpers that tests become noisy.

## Required Contract

- Subclass `JarvisPlugin` and define `metadata = PluginMetadata(...)`.
- Ensure `metadata.name` matches the module/package name.
- Add concise `metadata.utterances` unless the plugin is hidden or `routable=False`.
- Decorate every LLM-facing method with `@tool`.
- Use `@tool(inject=[...])` for Integration Gate clients.
- Keep docstrings short, prescriptive, and specific to when/how the model should use the tool.

## Persistence Choice

- Simple owner-scoped list of Pydantic items: `plugins.db.load_models()` / `save_models()`.
- Single owner config blob: `plugins.db.get_tool_data()` / `store_tool_data()`.
- Queryable domain, indexes, history, or cross-tool reads: create `store.py` with a named Mongo collection.
- Partial nested config update: fetch existing, use `core.plugins.mutations.merge_model_patch()`, validate domain invariants, then write.

Do not mix persistence styles inside one plugin unless the boundary is explicit, such as config blob plus indexed event history.

## Return And UI

- Simple read: return a typed Pydantic model/list when the result feeds follow-up calls.
- Domain widget on a typed read: keep the typed return and `push_ui(envelope)`. Do not wrap the read in `ToolResult` just to attach UI, and do not expose a public `render_*_widget`.
- Small mutation confirmation: return `ToolResult(content=..., ui=[receipt_envelope(...)])`.
- Readable artifact: return `ToolResult(content=..., ui=[content_envelope(...)])`.
- Expected bad input or wrong state: return `CapabilityErrorDetail(code="...", message="...")` with valid options or the next action.
- Valid no-op: return a prefix-free confirmation or the existing domain receipt.
- Unexpected provider/runtime failure: raise and let the dispatcher surface it.

Prefer terminal `ToolResult` over `push_ui()` plus a bare string. Use streaming `push_ui()` for progress, or for attaching a widget when the typed model must stay on the observation.

## Safety And Mutation

- Validate cheap, knowable bad input before side effects and before `require_consent()`.
- Resolve concrete targets inside the tool before destructive actions.
- Use `require_consent()` for destructive operations.
- Do not rely on the LLM to preserve opaque IDs without re-fetching or checking expected guards.
- For durable mutations, validate the full intended state and domain invariants before writing.
- Keep domain truth with the owner: valid fields in watchers, device actions in smart home, schedule scope in scheduler, provider capabilities in clients.

## Tests And Evals

Follow `.cursor/rules/test-strategy.mdc`. Default to no new test unless it protects a real risk (contract, safety, non-obvious logic, or observed regression).

When a test is warranted:
- Prefer one tool-boundary unit test for a mutation, normalization path, or expected error — not one per helper.
- Use `tool_context(...)` from `backend/tests/conftest.py` when owner, node, timezone, location, or UI invocation source matters.
- Use `invoke_tool(plugin, "tool_name", ...)` when testing plugin logic without injection/UI wrapper behavior.
- Use `fake_tool_data_store` for plugins backed by `plugins.db` tool-data helpers.
- Add a routing/eval check when tool selection or always-on offering is part of the risk.
- Add one eval in `backend/evals/agent_behavior.yaml` when tool selection or prompt behavior is part of the risk.

## Avoid

- Generic CRUD mixins, new base classes, code generators, or store abstractions.
- Package-shaped plugins for tiny tools.
- Prompt rules for behavior that belongs in code.
- Broad compatibility shims for unshipped plugin work.
