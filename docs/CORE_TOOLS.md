# Tools

First-party plugin inventory and how capabilities are offered each turn.
Authoring conventions live in [`.cursor/rules/plugin-tool-conventions.mdc`](../.cursor/rules/plugin-tool-conventions.mdc).
Routing mechanics: [DYNAMIC_TOOL_ROUTING.md](./proposals/built/DYNAMIC_TOOL_ROUTING.md).

## What exists today

| Plugin | Tools | Notes |
|--------|-------|--------|
| **system** | `search_tools`, `think`, `stop_listening`, `set_volume`, `open_application`, `quit_application`, `get_status`, `list_integrations`, `diagnostics`, `top_processes`, `repeat_last`, `exec`, `approve_pending`, `deny_pending`, `resolve_pending_input`, `connect_integration`, `disconnect_integration`, `refresh_integrations`, `reset_integration`, `retrain_wakeword` | OS controls, consent-gated shell, diagnostics, scratchpad, integration connect/disconnect. `search_tools` is the discovery hatch. |
| **files** | `read`, `find`, `grep`, `edit`, `write`, `move`, `delete` | Sandboxed to home. Writes/moves return receipts; delete is consent-gated. Skill bodies are ordinary Home files; the model reads them with `files.read`. Plugin is `routable=False`; file tools are always-on. |
| **display** | `push_content`, `delete_widget` | Generic on-screen content. `routable=False`; `push_content` is always-on. Domain widgets attach from their own tools. |
| **attention** | `set_mode`, `mute`, `get_mode`, `resume`, `set_quiet_window`, `list_quiet_windows`, `clear_quiet_window` | Owner-level proactive delivery. Session `soft_muted` is a separate input gate. |
| **agents** | `dispatch`, `resume`, `inspect`, `get_status`, `get_result`, `list_tasks`, `cancel_task`, `close` | Delegated work. `mode="code"` is Cursor or Claude Code with named work (`work_id` + title). `mode="jarvis"` is in-process. See [BACKGROUND_AGENTS.md](./BACKGROUND_AGENTS.md). |
| **scheduler** | `remind`, `defer`, `add_timer`, `add_alarm`, `get_alerts`, `get_next_alert`, `replace_alert`, `cancel_alert`, `snooze_alert`, `skip_next`, `add_exception`, `remove_exception`, `pause_series`, `resume_series` | Time-based authoring. Discover configured definitions through `setups.find`. |
| **automations** | `create_rule`, `update_rule`, `delete_rule`, `suppress_event`, `test_rule`, `pause_all`, `resume_all`, `list_available_triggers` | External-event rules. Inventory via `setups.find(setup_type="automation")`. Named delete via `delete_rule`; pause/resume via `setups`. |
| **protocol** | `create_protocol`, `get_protocol`, `update_protocol`, `delete_protocol`, `run_protocol`, `add_protocol_step`, `remove_protocol_step` | Saved routines. Discover via `setups.find(setup_type="protocol")`. |
| **todo** | `get_tasks`, `add_task`, `complete_task`, `toggle_task`, `update_task`, `delete_task`, `clear_tasks` | `get_tasks` attaches the todo widget. |
| **habits** | `create_habit`, `log_habit`, `log_habit_by_name`, `log_measured_habit_by_name`, `get_habit_status`, `get_habit_setup`, `list_habit_checkins`, `schedule_habit_checkin`, `replace_habit_checkin`, `delete_habit_checkin`, `pause_habit_checkin`, `resume_habit_checkin` | Cue-based habits and trigger-backed check-ins. |
| **profile** | `add_memory`, `update_memory`, `remove_memory`, `clear_memories`, `get_memories`, `remember`, `forget`, `recall` | Core facts + archival recall. |
| **search** | `web` | Built-in DDGS; optional SearXNG / Exa. |
| **time** | `countdown`, `duration`, `time_in` | Time math. |
| **setups** | `find`, `get`, `pause`, `resume`, `delete` | Cross-domain discovery and common lifecycle. Structural edits stay domain-owned. |
| **activity** | `recent`, `why_last_fire` | Operational timeline. Configured inventory lives in `setups`. |
| **db** | `reset_conversation_window` | Hidden. Closes this node's prompt window without deleting history. `store_tool_data` / `get_tool_data` are internal. |
| **weather** | `get_weather` | Current + 7-day; attaches WeatherWidget. |
| **google_maps** | `search_places`, `search_nearby`, `get_place_details`, `get_route`, `get_current_location` | Bespoke Composio wrapper; resolves omitted/"here" origins at the tool boundary. |
| **calendar** | `get_events`, `get_event`, `search_events`, `get_next_event`, `create_event`, `update_event`, `delete_event`, `find_free_time` | EventKit on this Mac (read/search) plus Google + Outlook OAuth. |
| **gmail** | `get_inbox`, `search_emails`, `get_email`, `get_thread`, `get_thread_full`, `send_email`, `reply_to_thread`, `create_draft`, `archive_email`, `mark_read` | Direct Google OAuth; `get_inbox` attaches InboxWidget. |
| **spotify** | `play`, `pause`, `skip`, `queue`, `search`, `get_playing`, `get_my_playlists`, `save_track`, `add_to_playlist`, `shuffle`, `repeat`, `set_volume`, `transfer_playback` | Direct Spotify Web API; household/Advanced OAuth. |
| **smart_home** | `get_setup_status`, `list_rooms`, `create_room`, `rename_room`, `search_devices`, `control_lights`, `adjust_lights`, `control_devices`, `get_device_states`, `bind_node_area`, `refresh_home_assistant`, `organize_device` | Home Assistant via Smart Home connect or `task setup:home`. `"in here"` uses turn-origin room, not chat history. |
| **composio** | `search_catalog`, `execute_tool` | Hidden remote fallback for tools not mounted locally. Prefer `system.search_tools` first. |

## Connected MCP / Composio

Mounted tools that are not in this turn's `tools=` set are discovered through `system.search_tools`. Unmounted Composio catalog tools use `composio.search_catalog` / `composio.execute_tool`.

Packaged servers live in `backend/mcp_servers.json`. Extra stdio/HTTP servers belong in Agent Home `mcp.json` — see [AGENT_HOME.md](./AGENT_HOME.md).

| Service | Notes |
|---------|-------|
| **gmail** | Bespoke plugin, direct Google OAuth |
| **calendar** | Bespoke plugin; EventKit on this Mac plus Google + Outlook OAuth |
| **smart_home** | Bespoke Home Assistant client |
| **spotify** | Bespoke plugin, direct Spotify Web API |
| **google_maps** | Bespoke Composio wrapper; location resolution in the plugin |
| **github** | Curated Composio tools in `mcp_servers.json` |
| **slack** | Curated Composio tools in `mcp_servers.json` |

Connect a new Composio app with “Connect my [service]”. Composio mounts only the `tools` allowlist in `mcp_servers.json`; an absent allowlist mounts nothing. Apps labels these **Cloud connector — powered by Composio**.

## How tools are offered

Offered capabilities are sent once as provider `tools=` JSON schemas. The system prompt does not repeat signatures or return schemas.

**Always-on** (`tool_router.ALWAYS_ON_FQNS`): `system.search_tools`, all `files.*`, `display.push_content`, `system.exec`, `system.approve_pending` / `deny_pending`, `search.web`, `profile.add_memory` / `remember` / `update_memory`, and `agents.dispatch` / `resume` / `get_status` / `cancel_task` / `close`. `display.delete_widget`, `agents.inspect`, `agents.list_tasks`, and `agents.get_result` are not always-on. Disabled plugins are omitted. Routable domain create/edit tools are not always-on.

**Per-turn routed set:** all tools from plugins matched by the active policy (`voice_default` / `text_default` / `system_hint`). Voice typically matches 0–3 plugins. `system.search_tools` is the escape hatch; named `edit_tool` / `fqn` values from a successful result are offered on the next iteration.

`ToolRouter` scores routable plugins by max-pool cosine over utterance vectors (metadata, `core/routing/utterances/`, or generated cache). Policies live in `core/routing/policies.py`. The router does not drop a match to shrink schema size; `schema_tokens` on diagnostics is measurement, not a fit-to-budget trim. Follow-up recall uses the latest successful plugin set (tool focus) plus one-shot delivered-route handoff after a live proactive turn. Tool output and object ids are not router state.

## Consent

`core/plugins/consent.py` `require_consent(description, action, detail="")` gates destructive work behind `pending_inputs`. Voice/text yes/no against a live pending is resolved by the harness; confirm also via `system.approve_pending` / `deny_pending` or `PendingInputWidget`. In `mode="jarvis"` background work, a deferred resolver marks the task `attention="approval"` and waits. `mode="code"` does not use this in-process path.

## Triggers

Authoring contract: [TRIGGER_AUTHORING.md](./TRIGGER_AUTHORING.md).

`TriggerAction.decision` is whether the user hears from JARV1S: `tell` always presents, `offer` may speak / `NO_REPLY` / `DEFER`, `act` stays silent. Delegated background work uses `agents.dispatch`, not a second trigger action kind.
