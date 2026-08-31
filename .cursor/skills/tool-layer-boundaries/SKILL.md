---
name: tool-layer-boundaries
description: Keep JARV1S tool fixes in the right layer. Use when changing backend plugins, @tool methods, integration clients, OAuth/auth handling, external API calls, or tool execution behavior.
---

# Tool Layer Boundaries

Use the narrowest layer that owns the problem:

- `backend/plugins/<name>/__init__.py`: LLM-facing tools, docstrings, parameter normalization, consent, and user-shaped return values.
- `backend/plugins/<name>/client.py` or provider modules: external API URLs, payloads, parsing, request retry/refresh mechanics, and provider-specific errors.
- `backend/core/integrations/`: dependency injection, client lifecycle, scope validation, and cached-client refresh.
- `backend/core/integrations/lifecycle/`: the only connect/disconnect/reconcile mutation path (`oauth.py`, `composio.py`, `bespoke.py`).
- `backend/core/auth/`: OAuth grant custody (Keychain-backed tokens, Mongo metadata), refresh, scope checks, and reauth signaling.
- `backend/core/decorators.py`: `@tool` metadata only (inject list, signature, return schema).
- `backend/core/plugins/dispatcher.py`: runtime injection, auth translation, `ToolResult` unwrap, and invocation ledger.
- `backend/services/watchers/` and `backend/services/push/`: background polling/push adapters only; do not hide API failures as empty data unless the integration is genuinely unconfigured.

Before editing, ask: is this a tool contract issue, provider API issue, integration lifecycle issue, or auth issue? Fix it there; avoid patching symptoms in the LLM-facing tool when the lower layer owns the invariant.

## Search and Lookup Tools

- Make lookup tools accept ordinary user phrasing in code, not by adding prompt rules. Normalize domain-owned vocabulary near the data model, and index provider metadata such as names, aliases, labels, floors/areas, and types before adding fuzzy matching.
- Return stable identifiers with the same field names downstream tools accept (`entity_id` -> `control_devices(entity_ids=[...])`). Do not make the LLM translate generic `id` fields into provider-specific parameters.
- Keep setup/status tools for diagnostics. Routine find -> inspect -> mutate flows should use first-class search/list tools that return the concrete targets needed for the mutation.

## Time Fields

- Plugins must store and compare datetimes as aware UTC values, then format user-facing tool results through `core.time` helpers.
- For tool result payloads that expose scheduled or event times, use `local_datetime_fields(...)` so `time` is local to the user and `utc_time` is audit/debug only. Do not return raw UTC under a field named `time`.
- For recurring schedules, persist the user's intended wall-clock time (`original_local_time`, e.g. `23:00`) alongside the UTC fire time so future materialized instances survive timezone and DST conversion.

## Receipts

- Use `receipt_envelope(...)` only for glanceable confirmations, not readable artifacts.
- Keep receipt fields short: category title, one human line, optional compact metadata. Do not pack body text, IDs, or verbose relative time into the rail.
- Receipt visual treatment lives in the shared `ContentWidget` compressed receipt variant. Do not create bespoke receipt widgets or per-plugin receipt styling unless the interaction is genuinely domain-specific.
