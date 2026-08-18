# Tools

The full tool ecosystem — core plugins (always loaded) and dynamic plugins (routed per-turn by the Tool Router). Core plugins are the "OS" of the assistant; dynamic plugins activate only when the user's utterance is semantically relevant.

---

## What Exists Today

| Plugin | Tools | Status |
|--------|-------|--------|
| **system** | `search_tools`, `stop_listening`, `set_volume`, `open_application`, `quit_application`, `get_status`, `diagnostics`, `top_processes`, `repeat_last`, `exec`, `approve_pending`, `deny_pending` | Complete (pre-built ops + machine diagnostics + consent-gated shell exec; known read-only / low-risk cmds auto-run) |
| **files** | `read`, `find`, `grep`, `edit`, `write`, `move`, `delete` | Complete (sandboxed to home dir; writes/moves return receipts; deletes are consent-gated) |
| **attention** | `set_mode`, `mute`, `get_mode`, `resume`, `set_quiet_window`, `list_quiet_windows`, `clear_quiet_window` | Complete (owner-level proactive delivery control; recurring quiet-hour schedules reconciled from `attention_schedules`) |
| **rules** | `create` | Complete (thin durable When/Do authoring facade over `TriggerRule`; use for generic persistent routines when scheduler/automations sugar is not a clearer fit) |
| **scheduler** | `remind`, `defer`, `add_timer`, `add_alarm`, `get_alerts`, `replace_alert`, `cancel_alert`, `snooze_alert`, `skip_next`, `pause_series`, `resume_series` | Time-based authoring and occurrence management. `get_alerts()` finds upcoming work and returns `instance_id` / `series_id`; configured definitions are discovered through `setups.find`. Series mutations reject resources owned by other domains. |
| **automations** | `create_rule`, `update_rule`, `delete_rule`, `suppress_event`, `test_rule`, `unpause_rule`, `pause_all`, `resume_all`, `list_available_triggers` | Event-based automation authoring and structural edits. Discover existing rules through `setups.find(setup_type="automation")`. |
| **protocol** | `create_protocol`, `get_protocol`, `update_protocol`, `delete_protocol`, `run_protocol`, `add_protocol_step`, `remove_protocol_step` | Saved routine authoring and execution. Protocols have stable IDs and are discoverable through `setups.find(setup_type="protocol")`. |
| **todo** | `get_tasks`, `add_task`, `toggle_task`, `clear_tasks` | Complete (`get_tasks` attaches the todo widget) |
| **habits** | `create_habit`, `log_habit`, `log_habit_by_name`, `get_habit_status`, `schedule_habit_checkin` | V0 implemented (cue-based habit definitions, append-only logs, trigger-backed check-ins; `sleep_debrief=true` schedules a time-based evaluate offer that may speak, defer with `next_retry_at`, or drop based on live context; no goals/points/routines yet) |
| **profile** | `add_memory`, `update_memory`, `remove_memory`, `clear_memories`, `get_memories`, `remember`, `recall` | Complete (core facts + archival events with semantic recall) |
| **search** | `web` | Complete (built-in DDGS; optional SearXNG / Exa) |
| **time_utils** | `countdown`, `duration`, `time_in` | Complete |
| **setups** | `find`, `get`, `pause`, `resume`, `delete` | Cross-domain discovery and common lifecycle control. Results return `resource_ref`, `setup_type`, `managed_by`, `supported_actions`, exact downstream IDs, and `edit_tool`; structural edits stay domain-owned. |
| **activity** | `recent`, `why_last_fire` | Complete (operational timeline and latest-fire explanation; configured inventory lives in `setups`) |
| **db** | `store_tool_data`, `get_tool_data`, `delete_tool_data`, `clear_conversation_history` | Working (should be internal-only) |
| **weather** | `get_weather` | Complete (current + 7-day; uses `resolve_current_location()` when city omitted; attaches WeatherWidget) |
| **google_maps** | `search_places`, `search_nearby`, `get_place_details`, `get_route`, `get_current_location` | Complete (bespoke Composio wrapper; resolves omitted/"here" origins at the tool boundary) |
| **calendar** | `get_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `find_free_time` | Complete (unified Google + Outlook providers via OAuth; account-aware writes; single-day `get_events` attaches CalendarWidget) |
| **gmail** | `get_inbox`, `search_emails`, `get_email`, `get_thread`, `get_thread_full`, `send_email`, `reply_to_thread`, `create_draft`, `archive_email`, `mark_read` | Complete (`get_inbox` attaches InboxWidget) |
| **spotify** | `play`, `pause`, `skip`, `queue`, `search`, `get_playing`, `get_my_playlists`, `save_track`, `add_to_playlist`, `shuffle`, `repeat`, `set_volume`, `transfer_playback` | Complete (bespoke Composio wrapper; `skip(direction="previous")` for back; transfer/play take a device name) |
| **smart_home** | `get_setup_status`, `search_devices`, `control_lights`, `adjust_lights`, `control_devices`, `get_device_states`, `bind_node_area`, `refresh_home_assistant`, `organize_device` | Complete (Home Assistant via product Smart Home connect or contributor `task setup:home`). Natural light scope via `control_lights` including `"in here"`; relative warmth/brightness via `adjust_lights`; exact HA ids via `control_devices`. |

---

## Historical Build Plan

Kept as historical context for how the core tool surface was built. The current implementation status is summarized above and in `ROADMAP.md`.

### Phase 1: High Impact, Low Effort

#### 1. Web Search (new plugin)
**Why:** Fixes ~30-40% of failed queries. Without this, any general knowledge question dead-ends.
**Effort:** Hours. One plugin file, one API call.
**Approach:** Built-in keyless DDGS by default; optional self-hosted SearXNG and Exa quality upgrade.

```
jarvis.search.web(query: str, max_results: int = 3) -> List[SearchResult]
```

Voice policy: Summarize the top result conversationally. Don't read URLs aloud.

#### 2. Scheduler Expansion (modify existing)
**Why:** One-shot reminders are useful but limited. Recurring reminders ("every weekday at 7am") and snooze are expected.
**Effort:** 1-2 days. Modify existing plugin + MongoDB schema.

Primary tools:
- `remind(when, message, recurrence=None, importance="normal", protocol=None, instructions=None, decision="tell", deliver_to="anywhere")` — primary entry point for time-based reminders and recurring notifications. Use `decision="offer"` and `instructions` for conditional reminders like "only if I'm not in a meeting".
- `defer(when, instruction, recurrence=None, deliver_to="anywhere")` — one-shot or recurring silent instruction to do something later, e.g. "turn off the lights in 10 minutes", "every day at 8am set the lights warm", or "check the garage in 10 minutes and tell me if it is open".
- `add_timer(duration, message="Time's up!")` — countdown timers.
- `add_alarm(time_str, message="Alarm!", recurrence=None)` — explicit acknowledgement alarms.
- `replace_alert(series_id=None, instance_id=None, when=None, message=None, deliver_to=None, ...)` — edit, reschedule, or retarget an existing series or one-shot notification without cancel/recreate.
- `snooze_alert(instance_id, duration)` — reschedule a firing trigger instance.

Event-based requests like "notify me when someone mentions me on Slack" belong to `automations.create_rule`, not scheduler. Explicit intervals like "every 45 minutes" belong to `scheduler.remind` for notifications or `scheduler.defer` for side effects, not automations. Time-based notifications tell the user later (`remind`, `add_timer`, `add_alarm`); time-based deferred instructions do work later (`defer`).

Automations use the same trigger priority vocabulary as scheduler: pass `importance`, not numeric priority or action-level sound. Presentation (`sound` / `requires_ack`) is derived from the trigger attention policy.

#### 3. Audio Chimes (system event, NOT an LLM tool)
**Why:** User-facing trigger deliveries feel like "robot talking at you" without an attention sound first.
**Effort:** 1 day.
**Approach:** This is NOT a tool the LLM calls. It's a system event handler:
1. `TriggerScheduler` publishes `TRIGGER_DUE`
2. Backend sends a `notification.sound` WebSocket message to the target client
3. Frontend plays the chime audio
4. Then TTS speaks the trigger message

Tool lifecycle cues use the same system-owned audio path (`audio.cue`) and are gated by the owner preference `audio.tool_cues_enabled`; the LLM does not call a sound tool.

#### 4. Time Utilities (new plugin)
**Why:** LLMs hallucinate on time math. Explicit tools reduce errors.
**Effort:** Hours. Pure Python, zero dependencies.

```
jarvis.time.now() -> str                           # Current datetime in user's timezone
jarvis.time.duration(start: str, end: str) -> str  # "2 hours 15 minutes"
```

Voice policy: Use 12-hour format with am/pm. Say "quarter past three," not "15:15."

### Phase 2: Expanding the OS

#### 5. ~~Archival Memory (Layer 2 — searched on demand)~~ ✅ Complete
**Why:** Core Memory (Layer 1) handles enduring facts injected into every turn. Archival Memory handles timestamped event recall — "What did I tell you about the dentist appointment?" or "When did I last mention Sarah?"
**Effort:** 1-2 days.

New tools:
```
jarvis.memory.remember(event: str, context: str = "") -> str  # Timestamped log entry
jarvis.memory.recall(query: str, limit: int = 5) -> List[MemoryEntry]  # Search memories
```

`remember` stores to MongoDB with timestamp and context. `recall` does text search (keyword match initially, upgrade to embeddings later when the dynamic routing infra lands). If a remembered event is actually a core fact (permanent, important), it should also call `profile.add_memory()` so it appears in the system prompt next turn.

#### 6. ~~System Control Expansion (extend existing system plugin)~~ ✅ Complete
**Why:** Volume control, repeat-last, and system status are quality-of-life essentials.
**Effort:** 1 day. Wire to existing TTS/voice services via event bus.

New tools:
```
jarvis.system.set_volume(level: int)      # 0-100, adjusts TTS output
jarvis.system.get_status() -> SystemInfo  # Uptime, trigger state, connected devices
jarvis.system.repeat_last() -> str        # Replay the last spoken response
```

Also ships: `open_application`, `quit_application`, `diagnostics`, `top_processes`, allow/ask/deny `exec` plus `approve_pending`/`deny_pending`.

#### 7. ~~File System (plugin — `jarvis.files`)~~ ✅ Complete
**Why:** Bridges the gap between "chatbot" and "system operator." Always-visible computer primitives for local file work.

```
jarvis.files.read(path: str, offset: int = 1, limit: int = 200) -> str   # file or directory
jarvis.files.find(pattern: str, path: str = "~") -> str                  # name search
jarvis.files.grep(pattern: str, path: str = "~", include: str | None = None) -> str
jarvis.files.edit(path, old_text, new_text, replace_all=False) -> ToolResult | str
jarvis.files.write(path, content, overwrite=False) -> ToolResult | str   # refuses accidental overwrite
jarvis.files.move(source, destination) -> ToolResult | str
jarvis.files.delete(path: str) -> str                                    # consent-gated → Trash
```

`delete` uses the generalized consent system (`core/plugins/consent.py`) — same pending-input approval flow as mutating `system.exec`. Write/edit/move execute when explicitly requested and return compact receipts (no consent).

Safety: sandboxed to home dir, credential-bearing paths (`.ssh`, `.aws`, `.env*`, …) blocked; ordinary config dotfiles allowed. Oversized text files return the first page with a continue/grep hint (not a hard Error). Binary rejection. Plugin is `routable=False`; file primitives stay in the explicit always-on `tools=` set.

`system.exec` uses a capability-first allow/ask/deny policy (`plugins/system_exec_policy.py`): commands run by default; file removal, `git push`, and disk erasure ask; secrets paths, destructive patterns, and piping downloaded code into a shell are denied. Oversized stdout/stderr is head+tail previewed and spilled to `~/.jarvis/tool-output/` for `files.grep` / `files.read`.

#### 8. ~~System Diagnostics~~ ✅ Complete (merged into system plugin)
**Why:** "Is the server running?" "How much disk space is left?" Self-awareness for a home assistant.

Ships: `diagnostics(category, detailed)` — CPU, RAM, disk, battery, network, uptime. `detailed=True` adds per-core CPU, per-partition disk, network I/O stats, and battery cycle count/condition (macOS via `system_profiler`). `top_processes(sort_by, limit)` — resource hogs by CPU or memory. Both via `psutil`, fully async.

Voice policy: Round to human terms. "About 60% of your disk is used" not "59.3% utilization."

---

## What Exists via Composio Auto-Bridge

Mounted tools that are not in the current `tools=` set are discovered through `system.search_tools`. Services available only in Composio's remote catalog are reached through `composio.search_catalog` / `composio.execute_tool`. Bespoke plugins (Gmail, Calendar, Smart Home, Spotify, Google Maps) stay on the hot path; Spotify/Maps still use Composio OAuth/MCP under a first-party wrapper:

| Service | Tools | Notes |
|---------|-------|-------|
| **gmail** | Bespoke plugin (~10 tools) | Direct Google OAuth, hand-crafted docstrings |
| **spotify** | Bespoke Composio wrapper | Allowlist + utterances in `mcp_servers.yaml` |
| **google_maps** | Bespoke Composio wrapper | Allowlist + utterances in `mcp_servers.yaml`; location resolution in `plugins/google_maps.py` |
| **github** | 6 curated tools via Composio | Declared in `mcp_servers.yaml` |
| **slack** | 15 curated tools via Composio | Declared in `mcp_servers.yaml` |

To connect a new Composio integration: "Connect my [service]" → JARVIS sends a Connect Link → authorize → tools available immediately. All toolkit tools are mounted; the ToolRouter handles per-turn selection via semantic retrieval. An optional `tools` allowlist in `mcp_servers.yaml` can restrict which tools are mounted.

---

## Architectural Notes

### Generalized Consent System

`core/plugins/consent.py` provides `require_consent(description, action, detail="")` — any plugin can gate a destructive operation behind the pending-input approval flow. `action` is an async callable that runs only after the user confirms via `jarvis.system.approve_pending()` or a `PendingInputWidget` action.

The `detail` field is optional and displayed behind the "show detail" toggle on the widget (e.g. raw shell command for `exec()`, file path for `delete()`).

`approve_pending` / `deny_pending` remain on `SystemPlugin` as the single LLM-facing entry-point. Internally they delegate to `consent.execute_pending()` / `consent.cancel_pending()`.

### Hide the Db Plugin
`DbPlugin` exposes `store_tool_data`/`get_tool_data` to the LLM. These are internal utilities for other plugins (Profile uses them directly via `from plugins import db`). Keep the module-level functions as an internal API. `clear_conversation_history` is in the explicit always-on `tools=` set.

### Tool Schema Budget

Offered capabilities are sent once as provider `tools=` JSON schemas. The system prompt does not repeat signatures, docstrings, or return schemas.

**Always-on set (every action-capable iteration):**
- Explicit FQNs in `tool_router.ALWAYS_ON_FQNS` (computer primitives, `search_tools`, `dispatch`, memory, etc.).
- Disabled plugins are omitted automatically.

**Per-turn routed set:**
- All tools from plugins matched by the active routing policy.
- Typical: 0–3 plugins matched per voice turn.
- `system.search_tools` is the escape hatch when a needed capability is not offered this turn.

**How routing works:** `ToolRouter` scores each routable plugin by **max-pool cosine** over utterance vectors from hand-written metadata, curated files under `core/routing/utterances/`, or generated-cache fallback. Production policy values live in `core/routing/policies.py`; the voice default uses `threshold=0.74`, `fallback_threshold=0.70`, max 3 plugins, top 2 per segment, and a 16K-character schema budget. Follow-up recall uses actual successful tool calls, not phrase matching or last-routed guesses: the orchestrator retains only the latest successful plugin set, and focused plugins can receive a bounded boost unless the next utterance strongly routes to another domain. Tool output and object ids are not router state. The router is recall-oriented; tools must still validate missing ids, destructive operations, and unresolved references before executing.

See [`docs/proposals/built/DYNAMIC_TOOL_ROUTING.md`](proposals/built/DYNAMIC_TOOL_ROUTING.md) for the full routing architecture.

### Voice Policy Convention
Every tool docstring should include a `VOICE:` section with rules for spoken output. This convention is already used in the weather plugin and should be standardized across all core tools.

### Trigger Action Boundary

For the canonical authoring contract across scheduler, automations, habits, and
the low-level rules facade, see [`TRIGGER_AUTHORING.md`](./TRIGGER_AUTHORING.md).

`TriggerAction.decision` chooses whether the user hears from JARV1S at fire time:

- `tell`: always speak / present a result (`presentation=always`).
- `offer`: speak only if worth it (`presentation=if_content`; may `NO_REPLY`, `DEFER`, or `DEFER_UNTIL`).
- `act`: do work silently (`presentation=never`).

`protocol_name` runs a named, reusable protocol regardless of decision. Use
`instructions` for fire-time policy or work the agent must interpret.
`reply_grounding` contains only scalar semantic facts needed to understand a
proactive utterance and its immediate reply (no nested payloads; no separate
size cap beyond ordinary prompt budgeting). Concrete event data belongs in
`TriggerInstance.source_event`; internal domain ownership and resource
correlation belong in `management`.

See [`TRIGGER_AUTHORING.md`](./TRIGGER_AUTHORING.md) for validation rules and
authoring examples.

Do not add new workflow semantics above these paths. `agents.dispatch(mode="jarvis")` is the user/tool path for delegated background work.
