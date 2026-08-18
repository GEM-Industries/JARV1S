# External Integration Plan

**Status:** Complete (all phases implemented)  
**Date:** 2026-02-26

---

## Strategy: Two-Tier Plugin Architecture

Every external integration produces a standard `JarvisPlugin` with `@tool`-decorated functions, VOICE rules, and Pydantic return types. The agent always sees the same interface — the backend is invisible. The distinction between tiers is about what lives *behind* the `@tool` wrapper.

| Tier | When | Backend | Example |
|------|------|---------|---------|
| **Curated Wrapper** | External service with a manageable API surface (5-15 endpoints) | SDK / library / direct API | Calendar, Email, Spotify, Smart Home |
| **Pass-through** | Service with a massive tool surface where coverage > polish | MCP client → external server | Composio auto-bridged apps |

The "handcrafted vs wrapped" distinction is irrelevant at the plugin layer. Voice behavior, Pydantic types, widget rendering, and protocol integration are always yours — the backend client (raw `httpx` vs Google SDK vs MCP) is invisible plumbing. Use whichever client minimizes boilerplate for the given API.

**Why not code-exec MCP / generated server modules?**  
CodeAct already provides the same architectural benefit. The agent writes Python, tool results stay as local variables in the executor sandbox, and only final output enters the context window. Generated modules would add a second discovery surface for zero marginal gain at our scale.

---

## Phase 1: Google Calendar Plugin (Curated Wrapper)

**Scope:** `plugins/calendar/` package, same structure as `plugins/weather/`.

### Backend

- `google-auth-oauthlib` for the one-time local consent script that generates a token file.
- `google-auth` (Credentials) + `httpx.AsyncClient` for runtime API calls — same async pattern as the weather plugin. Google's official SDK (`google-api-python-client`) is sync-only and would block the event loop.
- Token refresh is a single POST to `https://oauth2.googleapis.com/token` via httpx. `IntegrationManager.get()` enhanced with an optional `refresh` callback per integration. On token expiry, refresh transparently. On refresh failure, return a typed `NeedsReauth` error the agent can speak ("I need you to re-authorize Calendar").
- Credentials file path referenced by `.env` (`GOOGLE_CALENDAR_CREDENTIALS`).

### Tools (5)

| Tool | Returns | Notes |
|------|---------|-------|
| `get_events(start_date?, end_date?)` | `List[CalendarEvent]` | Flexible date-range query. Defaults to today. |
| `create_event(title, start, duration_minutes?, end?, description?)` | `EventConfirmation` | Conflict-checking. Default 30min duration. |
| `delete_event(event_id)` | `str` | Consent-gated. Get ID from get_events() first. |
| `find_free_time(date, duration_minutes?, start_hour?, end_hour?)` | `List[TimeSlot]` | Configurable window (default 8am–10pm). |
| `render_calendar_widget()` | SDUI push | `CalendarWidget` component via `push_ui` |

### Protocol Integration

- `get_events` available in protocol context so "morning briefing" can include the day's calendar.

---

## Phase 2: Tool Router

**Scope:** `core/tool_router.py` + small changes to plugin metadata, scanner, PromptBuilder.

### Changes

1. **Plugin metadata** — Dynamic plugins declare `utterances: List[str]` — example phrases a user would say that should trigger the plugin. Core plugins declare `"core": True`. No new dataclass.

2. **`ToolRouter`** — At startup, embed each routable plugin's utterances via fastembed (shared with archival memory). Store one vector per utterance in an in-memory dict (no centroid — scoring is max-pool at query time).

3. **Per-turn routing** — After STT/text ingest, the orchestrator resolves an explicit routing request (`voice_default`, `text_default`, or `system_hint`), embeds the routed utterance in a thread pool (`asyncio.to_thread`), and returns a `set[str]` of fully-qualified tool names from plugins selected by max-pool scoring and the active budget. Successful tool calls receive ordinary tool-focus recall. The `system_hint` policy disables session carryover; after a live voice trigger settles as delivered, its selected plugins may be handed to one same-connection user route. Silent, no-audio, TTS-failed, and prefetched delivery paths create no handoff. Per-session state is cleaned on disconnect. The canonical behavior and modality matrix live in [`DYNAMIC_TOOL_ROUTING.md`](DYNAMIC_TOOL_ROUTING.md).

4. **`PromptBuilder.build()`** — Param: `routed_tools: Optional[Set[str]]`. `generate_tool_manifest()` returns `(prefix, tail)`: every enabled plugin appears as a namespace line in the cacheable prefix, explicit `@tool(manifest="full")` tools also appear there, and routed plugins are promoted to full density in the per-turn tail. Router budget fitting uses the narrower `estimate_tail_stats()` helper so scoring does not render the full manifest.

5. **Plugin tagging** — Core (always loaded): system, scheduler, protocols, memory, todo, files, db. Dynamic (routed): weather, search, time_utils, calendar, smart_home, future integrations.

### Current routing design

`ToolRouter` is plugin-level only (Phase 8.5 dropped per-tool description embeddings and the `TOP_TOOLS` cap). Each routable plugin is scored by max-pool cosine over its utterance vectors; matched plugins promote all mounted tools into the per-turn tail. `MCPBridgePlugin.get_tools_for_manifest()` is gone — `MCPBridgePlugin` is a thin wrapper, and the tool selection logic lives entirely in `ToolRouter`. Mounted tools outside the routed tail are reached via `system.search_tools`; unmounted Composio catalog tools are reached via `composio.search_catalog`. See [`docs/proposals/built/DYNAMIC_TOOL_ROUTING.md`](DYNAMIC_TOOL_ROUTING.md) for the current architecture.

---

## Phase 3: MCP Client Adapter + Smart Home (Pass-through)

> **Superseded (2026-06):** Smart Home no longer uses MCP pass-through. Current implementation: `plugins/smart_home/ha_client.py` (direct HA REST + WebSocket), eight curated setup/control tools, setup via `task setup:home`. The MCP client adapter below remains accurate for Composio auto-bridge integrations.

**Scope:** `core/integrations/mcp/` + historical `plugins/smart_home/` MCP wrapper.

### MCP Client

- Lightweight stdio client: spawn local MCP server process, call `tools/list`, call `tools/call`.
- Registered in `IntegrationManager` like any other integration — lazy-loaded, cached.
- Only allowlisted local MCP servers. Clear failure modes.

### Smart Home Plugin (historical MCP wrapper)

| Tool | Purpose |
|------|---------|
| `search_devices(query)` | Natural language → `List[DeviceSummary]` (entity_id + type + state + available actions + params) |
| `control_device(entity, action, params={})` | Execute action. VOICE: Confirm briefly. |
| `get_device_state(entity)` | Current state. VOICE: Natural language ("Living room lights are on at 80%.") |

Historical backend: MCP client → `ha-mcp` server (stdio, <10ms overhead). Current backend is the direct Home Assistant REST + WebSocket client in `plugins/smart_home/ha_client.py`.

If `search_devices` proves unreliable for natural language → entity_id resolution, add a small semantic layer *inside the plugin* (embed entity names, cosine match). Not a global L2 router.

---

## Setup CLI

Every OAuth-based integration needs a one-time credential setup. Rather than forcing developers to reverse-engineer auth flows, each integration ships with a guided setup script.

### Structure

```
backend/cli/
  setup_calendar.py     # task setup:calendar
  setup_email.py        # task setup:email (future)
  setup_spotify.py      # task setup:spotify (future)
  setup_home.py         # task setup:home (implemented)
```

### Convention

Each script is standalone (~50 lines), uses `input()` prompts and `google-auth-oauthlib` (or equivalent), and follows this flow:

1. Check if credentials already exist → skip with confirmation message if so
2. Print brief instructions + link to the relevant developer console page
3. Prompt for client credentials (paste client_id / client_secret, or path to downloaded JSON)
4. Run OAuth consent flow (opens browser via `InstalledAppFlow.run_local_server`)
5. Save token file to a standard location (`backend/.credentials/calendar_token.json`)
6. Verify the connection works (e.g. "Found 3 events on your calendar")
7. Print next steps ("Add GOOGLE_CALENDAR_CREDENTIALS=... to your .env")

### Taskfile Integration

Wired via `Taskfile.yml` to match the existing `be:*` / `fe:*` convention:

```yaml
setup:calendar:
  desc: Set up Google Calendar OAuth credentials
  dir: backend
  cmd: uv run python cli/setup_calendar.py
  env:
    PYTHONPATH: .
```

### Why no CLI framework

- Zero new dependencies — `google-auth-oauthlib` is already needed for the Calendar plugin
- Each script is isolated — no shared state, no registry, no "setup framework"
- Taskfile is the existing developer command surface — `task setup:calendar` is consistent with `task be:dev`
- Scales linearly: one file per integration, no coupling between them

### `.credentials/` directory

- Gitignored (added to `.gitignore`)
- Standard location for all OAuth token files
- Referenced by `.env` vars (e.g. `GOOGLE_CALENDAR_CREDENTIALS=.credentials/calendar_token.json`)

---

## What Not to Build

| Idea | Why Wait |
|------|----------|
| Auto-generated server modules | CodeAct executor already keeps intermediate results out of context. Zero marginal gain. |
| L2 intra-plugin routing | Implemented for `MCPBridgePlugin` (80+ tool servers). Curated bespoke plugins still don't need it — none exceed ~10 tools. |
| Composio / cloud auth delegation | Adds 200-400ms latency + cloud dependency. Single-user OAuth is simpler. |
| `response_format` enums / pagination | VOICE rules already control verbosity. Add server-side truncation if needed later. |
| Auth framework | Token refresh hook in IntegrationManager is sufficient. Not a framework. |

---

## Design Guardrails

- **One discovery surface.** Registry-scanned `@tool`s + Tool Router pack filtering. No filesystem/tool discovery.
- **Process safety for MCP.** Only allowlisted local servers, controlled env vars, clear failure modes.
- **OAuth must fail gracefully.** Refresh tokens can expire. Build re-auth UX (agent speaks "I need you to re-authorize Calendar") rather than assuming tokens are permanent.
- **Eval loop.** Small docstring refinements produce outsized reliability gains. Track tool-call success rate in dev workflow.
