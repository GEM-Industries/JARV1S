# Roadmap

This roadmap is intentionally historical: completed items stay in place so the path remains visible. The main work queue is the numbered phase list below — read it top-to-bottom, and keep adding new work where it naturally belongs rather than maintaining a separate "current" board.

- **Phased Roadmap** — shipped history plus the next concrete product/engineering work.
- **Supporting Tracks** — platform, hardware, deployment, and inference work that supports the phase list.
- **Parking Lot** — valuable ideas that are intentionally deferred or not yet shaped into phase work.

## Completed
- [x] Local Wake Word — custom ONNX adapter model
- [x] Visual Widgets — SDUI with layout system, expiry, pinning
- [x] Voice Pipeline — streaming STT (on-device Apple Speech or Cartesia `ink-whisper` cloud) → structured capability-call agent → TTS (Cartesia or on-device Kokoro)
- [x] Jarvis Host first-run path — `task start` launches localhost DB services + backend-served UI, gates chat on setup readiness, persists LLM config in `system_config`, stores cloud keys in `CredentialStore`, and initializes the text runtime via explicit setup endpoints (no main LLM `.env` path)
- [x] Keyless onboarding lanes (Phases 1–4) — capability lanes, Open-Meteo weather, consent-first Google/Microsoft, SearXNG/Exa search ladder, local/cloud LLM setup wizard (see [proposal](./proposals/built/KEYLESS_ONBOARDING_LANES.md))
- [x] Plugin Architecture — auto-discovery, DI, runtime tool manifest
- [x] Integration Gate — lazy-loading client factory with fail-safe registration
- [x] Event Bus — pub/sub with wildcard support
- [x] Alert Scheduler — time-based alerts with proactive voice delivery
- [x] Offline Buffering — missed alerts delivered on reconnect
- [x] Agent Home — user-owned `PROMPT.md`, skills, and extra MCP under `$JARVIS_DATA_DIR/home` (see [AGENT_HOME.md](./AGENT_HOME.md))

## Phased Roadmap

### Core Tools Build-Out (see [CORE_TOOLS.md](./CORE_TOOLS.md))

**Phase 1 — High Impact, Low Effort:** ✅
- [x] Web Search — SearXNG local search with Exa upgrade
- [x] Recurring Alerts — recurrence patterns, snooze, catch-up-safe rescheduling
- [x] Audio Chimes — notification sounds via WebSocket, file-based with AEC-safe routing
- [x] Distinct Alert Types — `remind`, `add_timer`, `add_alarm` with per-type sounds
- [x] Time Utilities — `countdown()`, `duration()`, `time_in()` plugin

**Phase 1.5 — Scheduler & Protocol Hardening:** ✅
- [x] Schedule Rules — `trigger_rules` collection with `id`, `exceptions[]`, `paused_until`, and `enabled`; concrete fires live in `trigger_instances`
- [x] Series Identity — `series_id` on recurring scheduler results, linking occurrences back to their `TriggerRule`
- [x] Scheduler Service — reads the current rule when materializing recurrence, generates the next occurrence from durable intent, startup recovery for orphaned claimed/executing trigger instances
- [x] Series Control Tools — `skip_next`, `pause_series(until)`, `resume_series`, `cancel_alert(series_id=...)`, `cancel_alert(instance_id=...)`, and `replace_alert(series_id=... | instance_id=...)` on scheduler plugin
- [x] DST Guard — handle non-existent local times (spring-forward) in `_next_occurrence`
- [x] Protocol Execution History — `protocol_runs` collection (triggered_by, status, steps executed/skipped)
- [x] Protocol Run Metadata — `last_run_at`, `run_count` on protocol documents, enriched context via `build_protocol_context`
- [x] Protocol Step Editing — `add_step` / `remove_step` tools (voice-friendly incremental edits)

**Phase 2 — Expanding the OS:** ✅
- [x] Protocols — user-defined routines (named step lists), alarm-linked or on-demand execution
- [x] Core Memory — inject user profile facts into system prompt via PromptBuilder
- [x] Archival Memory — `remember()`/`recall()` for timestamped event search (semantic via fastembed + numpy cosine similarity)
- [x] Token-Budgeted Context — `context_manager.py` with per-turn budget trimming, tool result offloading, and `context.metrics` WS event for frontend visibility
- [x] System Control — `set_volume`, `open_application`, `get_status`, `repeat_last`, consent-gated `run`/`approve_pending`/`deny_pending`
- [x] File System — `list_dir`, `read_file`, `write_file` and `rename_file` with receipts, `delete_file` consent-gated
- [x] Generalized Consent System — `core/plugins/consent.py`; any plugin can gate destructive ops behind durable pending-input approval
- [x] System Diagnostics — CPU/RAM/disk/battery/network via psutil (in jarvis.system)

**Phase 2.5 — Input & Multimodal:**
- [x] Text Input — `user_text` WS message type, skip-STT path in orchestrator, input field in ControlBar
- [x] Multimodal Pipeline — image/vision support through LLM service, multimodal message format in agent
- [x] Widget Snapshot Restore — reconnect displays from backend-owned widget snapshots; live updates remain backend-pushed via `ui.update`

**Phase 3 — Core Integrations:** ✅
- [x] Calendar — Google Calendar via OAuth (`google-auth` + `httpx.AsyncClient`), 5 tools, SDUI widget, protocol-linkable
- [x] Smart Home — Home Assistant via direct REST + WebSocket client, 8 curated tools (validate/setup/search/control/state/bind/refresh/organize)
- [x] Tool Router — semantic utterance-based routing, per-session union + sticky fallback, core/dynamic plugin split
- [x] Setup CLI — `task setup:home` for guided Home Assistant setup (connect existing, onboard fresh, or Docker bootstrap via `task setup:home:bootstrap`)

**Phase 4 — Automation Engine** (see [proposal](./proposals/built/AUTOMATION_ENGINE.md)) ✅
- [x] CalendarWatcher — polls calendar integration every 60s, returns upcoming events (24h window)
- [x] AutomationService — reconciliation loop with point-in-time precision (`call_later` + tick-as-reconciler), `IntegrationHealth` tracking, suppression pruning
- [x] Automations plugin — event-rule authoring and structural management tools (`create_rule`, `update_rule`, `delete_rule`, `suppress_event`, `test_rule`, `unpause_rule`, `pause_all`, `resume_all`); cross-domain inventory is consolidated under `setups.find`.
- [x] MongoDB `trigger_rules(origin.kind="external")`, `automation_fired`, and `automation_config` — external automation definitions, dedup with TTL index, persisted pause state
- [x] Global pause/resume — persisted to MongoDB, survives restarts
- [x] Priority field (1-5) on rules — enables future DND/quiet-hours exemptions

**Phase 5 — Integration Scaling** (see [setup proposal](./proposals/built/INTEGRATION_SETUP.md) + [multi-provider proposal](./proposals/built/MULTI_PROVIDER_CALENDAR.md)) ✅
- [x] MCP Auto-Bridge — `MCPBridgePlugin` in `core/integrations/mcp/` auto-generates capability tools from any MCP server's `tools/list` schema (stdio + Streamable HTTP transports, schema caching, tool count guardrail)
- [x] Ecosystem OAuth Reuse — CLI scripts replaced by `AuthManager` (`core/auth/`) + `OAuthWidget` SDUI. One OAuth app per ecosystem (Google: Calendar + Gmail; Microsoft: Calendar + OneDrive). In-UI configure → authorize → connected flow; scopes aggregated dynamically across all registered integrations.
- [x] Email — Gmail via reused Google OAuth, bespoke plugin with inbox summaries, send/reply, priority filtering

**Phase 6 — Automation Expansion** ✅
- [x] Watcher enrichment — CalendarEvent exposes `is_all_day`, `attendees`, `attendee_count`, `duration_minutes`; all flow through to automation conditions via `model_dump()`
- [x] Numeric condition operators — `greater_than`, `less_than` in `evaluate_conditions` for duration, attendee count, etc.

**Phase 6.5 — Convenience Integrations** ✅
- [x] Composio Gateway — `composio_gateway.py` manages Connect Links, OAuth callbacks, per-app MCP tool discovery, and hot-reload into the live registry + ToolRouter. Uses Composio v3 REST API (auth configs + per-toolkit MCP server configs). `ConnectWidget` SDUI component for the auth flow. `disconnect_integration` tool for cleanup.
- [x] Trigger Scaling Phase 1 — push trigger path alongside the polling path. `TriggerEvent` canonical model, durable `inbound_events` inbox (persist before ACK), `POST /api/v1/webhooks/composio`, `AutomationService.on_push_event()`, Composio trigger lifecycle, `mcp_servers.json` `triggers` field, LLM tools for available triggers.
- [x] Trigger Scaling Phase 2 — PushAdapter layer: Google Calendar Watch API (signal push → kick_source), PushRegistry lifecycle manager, generic push webhook route, `_evaluate_source()` refactor. Gmail remains polling-only until authenticated Pub/Sub push JWT verification is implemented. (see [proposal](./proposals/built/TRIGGER_SCALING.md))
- [x] Auto-bridged plugins — connected Composio toolkits mount as `jarvis.<toolkit>.*`; hidden mounted tools are discoverable via `system.search_tools`, with `composio.search_catalog` / `execute_tool` reserved for remote catalog fallback. JSON entries optional for routing tuning only.

**Phase 7 — Autonomous Agents** ← _in progress_
- [x] Background task `background_tasks` collection + indexes + crash recovery
- [x] `AgentsPlugin` — `dispatch`, `resume`, `inspect`, `get_status`, `get_result`, `list_tasks`, `cancel_task`, `close`
- [x] Named work V1 — `work_id` + title lineage over `background_tasks`; code-mode resume keeps cwd/`worker_kind`/vendor `session_id`; see [NAMED_WORK.md](./proposals/NAMED_WORK.md)
- [x] `opencode-agent-sdk` / `claude-agent-sdk` integration via `sdk.py` abstraction layer (claude-agent-sdk preferred)
- [x] Background task progress surface — `TASK_EVENT` updates MongoDB task state; throttled `ui.update` progress receipts render in the top-right review rail
- [x] `BackgroundTaskWidget` — on-demand detail panel for task result, artifacts, trace, and approval state
- [x] `GET /api/v1/tasks/` + `GET /api/v1/tasks/{task_id}` endpoints
- [x] Reasoning tier — historically `LLM_REASONING_*` + powerful in-process agent; **superseded** by shared `BACKGROUND_AGENT_*` + `ANTHROPIC_API_KEY` background agent (no implicit turn routing)
- [x] `think()` tool in `system` plugin (zero-cost scratchpad)
- [x] `jarvis.agents.dispatch` / `resume` from voice and background paths
- [x] Agent Runtime Hardening (Phase 7.5a) — post-hoc budget enforcement, Grep/Glob in `allowed_tools`, `AGENT_MAX_DEPTH` / `AGENT_MAX_PER_SOURCE` / `AGENT_INPROCESS_MAX_TURNS` config, per-source count tracking, two-phase subprocess cleanup (SIGTERM → SIGKILL), orphan scan on startup, early TTS flush at clause boundary. Reasoning effort later moved to per-turn `resolve_reasoning_effort()` (see Phase 8 items below).
- [x] Embedded Agent Runtime (Phase 7.5b) — historical pre-turn routing + powerful agent; **superseded** by assistant-only direct turns and explicit `dispatch(mode="jarvis"|"code")` on one background model. Remaining: `_IN_BACKGROUND` guard, `_prepare_task()` depth/per-source limits, dual runtimes.
- [x] Agent Prompt Hardening (Phase 7.5c) — XML-tagged background agent system prompt (`<identity>`, `<rules>`, `<env>`, `<conversation-context>`, `<user-preferences>`); last-6-message conversation context injected at dispatch time so agent has task history without re-prompting; enriched `ToolUseBlock` progress events (shows command/file path, not bare tool name); 12-character alphanumeric task IDs; `session_id` captured from first `AssistantMessage` (not deferred to completion); `max_budget_usd` + `setting_sources` in `AgentOptions`; server-side `~` expansion in `dispatch()`; `cwd` defaults to the JARV1S project root; `dispatch()` returns structured JSON success/failure with explicit "task not started" errors; `dispatch()` docstring teaches absolute paths, one-task-per-call behavior, good prompts, and `resume()`-first preference; complex-routing exemplars extended to cover coding/build/modification requests so powerful model handles agent dispatch turns
- [x] Mid-task approvals for `mode="jarvis"` background tasks — deferred resolver creates durable `pending_inputs`, marks `attention="approval"`, and routes the progress receipt to `PendingInputWidget`; SDK `mode="code"` approval callbacks remain deferred.
- [ ] Composio URL refresh for long-running tasks

**Phase 7.6 — Display & Memory Primitives** ✅
- [x] ContentWidget — section-based structured display (markdown, table, list, code, kv); `push_content()` backend helper; `WidgetSize` constant for default size
- [x] Source-tagged conversation turns — `source` field on all messages (`user` | `system`); automation identity lives in origin metadata; two-tier history loading (prior user turns + small delivered-assistant system tail) stops briefings from evicting debugging context
- [x] Conversation history in recall() — embed user/assistant turns at write time (tool results excluded); extend `recall()` to search both `memories` and `conversations` (30-day window, bge-small query prefix)
- [x] Frontend history hydration — `GET /api/v1/history/` REST endpoint with Pydantic response model; `loadHistory()` on WS connect; stops blank transcript on reload

**Phase 8 — Hardening & Voice Quality** ← _make it reliable enough to run 24/7_
- [x] LLM retry with backoff — exponential retry (3 attempts, 1s/2s backoff) for transient API errors (429, 5xx, Anthropic 529) in `LLMService.chat_stream`; raises on exhaustion so agent handles failure cleanly without injecting fake error text into history
- [x] TTS fallback — if Cartesia fails, deliver response as text-only instead of silently dropping it (text streams via `conversation.response` in parallel with TTS; Cartesia errors logged only)
- [x] Barge-in flush — explicitly drain the TTS `sentence_queue` on interruption so in-flight audio chunks don't race past `task.cancel()`
- [x] Tool execution trust slice — global prompt invariant that tool results are the source of truth; approval-needed actions are described as pending until `approve_pending()` returns; Gmail draft docstring requires recipient/subject/body preview; long tool-call generation surfaces a composing state before execution starts.
- [x] Adaptive silence threshold — silence window scales linearly from 0.5s (short commands) up to 1.5s (long utterances >2s) so thinking pauses between sentences don't trigger premature endpointing
- [x] Fast turn recovery — if the user resumes speaking within 2s of a VAD endpoint while the assistant attempt is still active, await post-commit handoff (streaming STT finalize) so segment text is captured, cancel/retract the speculative assistant run, keep the same logical user `VoiceInputTurn`, stream only the new continuation audio, and merge transcript text without replaying prior PCM.
- [x] Turn-scoped TTS cancellation — fast recovery/interruption now cancels the active `VoiceDelivery` worker and resets the Cartesia websocket on abandoned streams; removed the session-global `tts_cancel` event so a new turn cannot clear an old turn's cancellation signal. Also dropped the legacy `turn_runs.user_id_1_turn_id_1` index so telemetry persists under `owner_id`.
- [x] Single remote presence identity — WebSocket sessions now separate `connection_id` (live socket), `owner_id` (trusted storage/account namespace), `node_id` (stable endpoint), capabilities, and optional location refs. Frontend persists `node_id`; backend keeps `owner_id` internal/config-derived and stores presence metadata on turn traces. This enables a remote mic/speaker/display node without committing to full multi-room routing or speaker identity yet.
- [ ] Process supervision — `launchd` plist (macOS) with auto-restart on crash. Superseded for end users by the packaged Host app ([JARVIS_HOST_APP.md](./proposals/JARVIS_HOST_APP.md)) once Phase 1b ships; keep for headless contributor installs until then.
- [ ] Cost tracking — daily LLM spend counter (per-turn token usage), surfaced in `diagnostics` tool
- [x] Stable tool manifest ordering — sort core plugins alphabetically as fixed prefix, dynamic by name; improves provider cache hit rate
- [x] Context summarization — two-phase compaction in `context_manager.py`: offload oversized tool results, then summarize oldest messages into a ~150-word digest via LLM before newest-first fill
- [x] LiteLLM adapter migration — `LLMService` delegates to `LiteLLMAdapter` (`litellm.acompletion`); provider streams normalize to tagged `LLMChunk` (`content` | `reasoning`); removed direct `AsyncOpenAI` / native Anthropic HTTP paths
- [x] Thinking effort by latency contract — `resolve_reasoning_effort()` gates `reasoning_effort` per turn (`None` on audio-bound or non-powerful models; `LLM_TEXT_REASONING_EFFORT` / `LLM_HEADLESS_REASONING_EFFORT` for eligible turns). Provider reasoning streams on `conversation.reasoning` (text clients), persists as `metadata.turn_type="reasoning"` (excluded from prompt history), and records `reasoning_effort` / `reasoning_chars` in `turn_runs`. Mid-turn `reason()` tool removed; escalation is pre-turn routing + in-process powerful agent.
- [ ] Executor security tests — verify `FORBIDDEN_MODULES` blocks `os`, `subprocess`, `importlib`, `ctypes`; verify sandbox restrictions

**Phase 8.5 — Architecture Cleanup** ✅

Targeted simplifications surfaced by an end-to-end architecture review. Each item either deleted code, restored cache hits, or committed to one direction on a half-built abstraction. Landed before Phase 9 so the orchestrator decomposition (9.0) works against a smaller surface.

_Tool router & manifest:_
- [x] Two-density prompt manifest + stable-prefix cache — **historical.** Later replaced by provider `tools=` JSON schemas; the system prompt no longer lists tool signatures. See [DYNAMIC_TOOL_ROUTING.md](./proposals/built/DYNAMIC_TOOL_ROUTING.md).
- [x] Plugin-level routing only — dropped per-tool description embeddings, `TOOL_THRESHOLD`, orphan scoring, and the `TOP_TOOLS` cap from `tool_router.py`. Matched plugins promote all mounted tools into the routed tail; session continuity is a +0.10 decay bonus for previously routed plugins, not a sticky union.
- [x] Composio out of the hot-path manifest — `system.search_tools` owns mounted-tool discovery; `composio.search_catalog` + `composio.execute_tool` remain the remote fallback for tools not mounted locally. Bespoke wrappers like Gmail/Calendar stay on the hot path.
- [x] Curated + generated routing utterances — high-value internal/confusable phrases live in source under `core/routing/utterances/`; `core/integrations/utterance_cache.py` is disposable generated fallback keyed by plugin description, sibling context, and tool docs. Hand-written plugin metadata still wins, and the eval runner is the promotion gate for moving generated phrases into curated/source-controlled inputs.

_Hybrid execution paradigm (CodeAct + tool_use):_
- [x] JSON Schema generation from `@tool` — `pydantic.TypeAdapter` extraction in `core/decorators.py` produces a return schema per tool, cached by annotation so shared return types (e.g. `CalendarEvent` across calendar tools) build once. Primitives skipped to keep the manifest signal-to-noise high. Pure refactor, zero behavior change.
- Parsing-strategy decouple + Sonnet `tool_use` flip deferred — see Deferred section. Value-per-effort didn't clear the bar once the manifest work landed on CodeAct-only.

_Dead code & half-built abstractions:_
- [x] Removed `session.conversation_history` in-memory cache — MongoDB is the single source of truth. Deleted the field from `Session`, the per-turn sync/clear/extend sites in `orchestrator.process_turn`, and the parameter from the signature; `handlers.py` no longer plumbs it through.
- [x] Replaced `_COMPLEX_PATTERNS` model-router regex with embedding exemplars — **later superseded**: pre-turn model routing removed; direct turns always use the configured assistant model.
- [x] Consent resolver direction for background — `mode="jarvis"` uses deferred pending-input approval instead of auto-approving destructive actions; `mode="code"` remains SDK-isolated with `bypassPermissions` while SDK approval callbacks stay deferred.
- [x] Documented the two-runtime split — `docs/BACKGROUND_AGENTS.md` gains a "Two Runtimes, By Design" section; `agents/dispatch` docstring tightened to steer callers toward the right mode. Rejected the MCP-bridge collapse: subprocess+MCP overhead eats the latency advantage of in-process `jarvis.*`, and cross-process consent is a new surface area for no real win.

_Follow-up housekeeping (landed alongside):_
- [x] `core/decorators.py` cleanup — extracted `_handle_auth_error` to `core/auth/error_handler.py`, added a `TypeAdapter` result cache keyed by annotation, collapsed the double `inspect.signature(fn)` call, and short-circuited the wrapper when `inject=()` so no-inject tools skip the async indirection.

**Phase 9 — Proactive Infrastructure** ← _decouple work from delivery_

The foundational layer that enables JARV1S to act without always speaking. Starts with a targeted orchestrator decomposition, then builds three primitives on top: delivery modes control *whether* a completed turn produces output, System Pulse provides a periodic evaluation loop, and prefetch uses both to pre-compute results for scheduled delivery.

_Phase 9.0 — Orchestrator decomposition (prerequisite):_ ✅
- [x] Extracted `_execute_turn()` from `process_turn()` — new delivery-agnostic method in [backend/core/turns/orchestrator.py](../backend/core/turns/orchestrator.py) takes `delivery: DeliveryStrategy` + `result: TurnResult` kwargs, runs the agent loop and forwards each `AgentEvent` as a `StreamEvent`. No `session` / `manager` / `tts` references inside — headless callers (Phase 9a/b/c) pass a minimal `session_context` dict.
- [x] `StreamEvent` tagged union + single-method `DeliveryStrategy` protocol — [backend/core/turns/delivery.py](../backend/core/turns/delivery.py). Eight tags (`text`, `reasoning`, `tool_call`, `tool_output`, `ui_update`, `ui_delete`, `context_metrics`, `final_text`); `frozen=True, slots=True` for cheap per-event allocation. Two implementations: `VoiceDelivery` (WS fan-out + sentence buffering + speech gate + TTS worker; forwards `conversation.reasoning` on text turns only; single writer of `session.tts_sentence_queue` / `session.first_audio_sent` / `session.current_delivery`) and `HeadlessDelivery` (no-op).
- [x] Package split: `backend/core/voice/` now holds audio workers only (STT, TTS, VAD, wakeword, `SpeechProcessor`); turn lifecycle + delivery moved to `backend/core/turns/`. `voice/` becomes a voice-I/O implementation detail behind the orchestration layer; `turns/` is channel-agnostic and is where Phase 9a/b/c and future channels land.
- [x] `process_turn()` is now the thin caller — session lookup + STT/text/system ingest + `perf.start("turn_latency")` + `set_mode(ACTIVE_AI_TURN)` + `async with session.turn_lock: delivery.start() → _execute_turn(...) → delivery.aclose()` + `_persist_trace()` + `VOICE_TURN_END` publish. Same barge-in/fast-recovery/interruption semantics; WS message shapes unchanged.
- [x] Turn lock semantics preserved — voice/text `process_turn()` holds `session.turn_lock`. `_HEADLESS_TURN_SEMAPHORE` (module-level, `settings.AGENT_HEADLESS_CONCURRENCY` default `2`) is in place for Phase 9a headless callers to use directly with `async with` — no wrapper helper (premature abstraction for an unused primitive).
- [x] Two-step landing — Step 1 extracted `_execute_turn` with transitional session/manager/tts kwargs; Step 2 introduced `delivery.py` and the strategy split. Headless smoke test (mocked agent stream through `HeadlessDelivery`) confirms `TurnResult` populates cleanly with zero WS/TTS side effects. `perf.end("turn_latency")` marker preserved (closes inside `VoiceDelivery._tts_worker`).

_Decomposition hazards — outcomes:_
- VoiceDelivery stays session-coupled (`VoiceDelivery(session, manager, tts, *, session_id, produce_audio)`) — barge-in reads `session.tts_sentence_queue` and drains via `drain_sentence_queue` imported from `delivery.py`.
- CancelledError is caught once in `_execute_turn` (sets `result.interrupted`, appends partial `text_only` trace entry, re-raises); `delivery.aclose(cancelled=True)` drains and joins the TTS worker in a `finally`; `process_turn`'s outer `except asyncio.CancelledError` calls `_persist_trace` once (guarded by a `persisted` flag so the success path doesn't double-write).
- Speech gate (early clause flush, mid-chain suppression, pre-tool flush) lives in `VoiceDelivery.on_stream`; turn trace and tools_called live in `_execute_turn`. Both write during the same event loop without coupling.
- Voice mode (`ACTIVE_AI_TURN` set/reset) stays entirely in `process_turn`. VoiceDelivery exposes `first_audio_sent` as a property — the caller's `finally` reads it to choose direct `ACTIVE_IDLE` reset vs deferred `playback_end`.
- Session-owned TTS fields (`tts_sentence_queue`, `first_audio_sent`, `current_delivery`) are reset inside `VoiceDelivery.aclose()` so the caller's `finally` never reaches into delivery-owned state.
- `perf.start("turn_latency")` fires in `process_turn` ingest; `perf.end("turn_latency")` fires on first audio chunk inside `VoiceDelivery._tts_worker`. Span crosses the boundary by design — documented in the worker's docstring.

_Phase 9a — Trigger Delivery And Evaluate Actions:_ ✅

Design note: reporting intent lives on `TriggerAction.decision` (`tell`, `offer`, `act`); physical routing hints live on `DeliveryPlan`. Protocols remain pure recipes and inherit from the invocation context. Phase 9a requires a connected session for user-facing presentation; Phase 9c prefetch lifts that by passing a session-less context shim into `_run_headless_turn`.

- [x] Shared trigger vocabulary — [backend/core/triggers/vocabulary.py](../backend/core/triggers/vocabulary.py) centralizes trigger decisions (`tell`, `offer`, `act`) and delivery/trace tags (`announce`, `silent`, `evaluate`, `suppressed`, `prefetched`) so the codebase no longer carries legacy `delivery_mode` semantics.
- [x] `NO_REPLY` / `DEFER` sentinels — `is_no_reply()` and `is_defer()` exact-equals-after-strip checks in [backend/core/turns/delivery.py](../backend/core/turns/delivery.py); the orchestrator's `_run_evaluate_turn` inspects `TurnResult.full_response` and skips presentation on `NO_REPLY`, or marks `awaiting_delivery` on `DEFER`. The agent decides whether a candidate announcement is worth speaking now, not the system. The sentinels are scoped to evaluate turns so voice/user turns cannot accidentally emit them.
- [x] Automation action model — `ActionConfig` uses `decision: "tell" | "offer" | "act"` in [backend/plugins/automations.py](../backend/plugins/automations.py). `AutomationService._fire()` creates `TriggerInstance` rows through [backend/core/triggers/service.py](../backend/core/triggers/service.py) instead of publishing legacy alert events.
- [x] Prompt injection for evaluate turns — evaluate turns get an evaluative `INSTRUCTION` built by `build_system_turn_message(SystemTurnContext(...))` that embeds the `NO_REPLY` rule. Self-contained in the per-turn `system_context` — no global prompt file changes.
- [x] Scheduled reminders/protocols on trigger substrate — `remind`, `add_timer`, and `add_alarm` in [backend/plugins/scheduler.py](../backend/plugins/scheduler.py) persist scheduled work as `TriggerRule` / `TriggerInstance` documents. Recurring occurrences materialize from the current `TriggerRule`, and offline/no-audio outcomes move to `awaiting_delivery` for retry.
- [x] Orchestrator branch + headless runners — `_handle_trigger_due` in [backend/core/turns/orchestrator.py](../backend/core/turns/orchestrator.py) resolves `TriggerAction.decision` once and routes to `tell`, `offer`, or `act` behavior. It atomically claims pending trigger instances before execution, then moves claimed work to `executing`. `tell` keeps the existing `process_turn` path + `session.current_run_task` assignment; delivery finalization is guarded so acknowledged/snoozed/cancelled instances are not overwritten. `offer` triggers settle as `suppressed`, `delivered`, `awaiting_delivery`, `offer_deferred`, or `failed`; generic silent work uses `trigger_service.complete_instance()`.
- [x] Protocol logging refactor — `_run_and_log_protocol` now accepts a `runner: Callable[[], Awaitable[Any]]` factory so `protocol_runs` logging stays symmetric across announce / silent / evaluate paths. Call sites pass a lambda wrapping the right runner.
- [x] Unit tests — `backend/tests/test_delivery_modes.py`, `backend/tests/test_attention.py`, `backend/tests/test_automation_service.py`, `backend/tests/test_scheduler_plugin.py`, and `backend/tests/test_offer_timing.py` cover evaluate routing, `NO_REPLY`/`DEFER`, offer commitment context, trigger context plumbing, preview-before-persist, and the absence of legacy `delivery_mode` writes.

_Phase 9b — System Pulse:_ ✅
- [x] `SystemPulse` service — configurable interval (default 30 min, opt-in via `SYSTEM_PULSE_ENABLED`), runs in the event loop alongside `TriggerScheduler`, `AttentionReconcileService`, and `AutomationService`. Lifecycle in [backend/services/system_pulse.py](../backend/services/system_pulse.py); first tick sleeps `interval_s` to prevent crash-restart loops spamming checks. While enabled, owner attention gates presentation at trigger delivery, not pulse execution.
- [x] Mechanical pre-evaluation — each tick queries existing state only (no separate event buffer, no schema changes to automations): (1) `trigger_instances` overdue, (2) `trigger_instances` stuck executing, (3) `automation_fired` with `status=failed` in the last hour, (4) `trigger_instances` awaiting delivery. When all buckets return empty, a single `pulse_runs` doc is logged and **no LLM is called**.
- [x] Findings-level dedup — `DEDUP_WINDOW=6h` prevents re-escalating the same finding across ticks. Each escalated `pulse_runs` doc records `findings_keys` (namespaced `{bucket}:{key}`); the next tick compares `current_keys - last_keys` and only escalates when there are new keys, logging `reason="suppressed"` otherwise. Regression-tested.
- [x] LLM escalation — creates a system-origin `TriggerInstance` with `action.decision="offer"` and publishes `TRIGGER_DUE`. Reuses the Phase 9a offer path end-to-end.
- [x] Automation failure signal — `AutomationService._fire` now wraps dispatch in try/except and persists `status=failed` + error + `failed_at` on the existing `automation_fired` doc; `_mark_fired` stamps `status=fired` so pulse's failure query is unambiguous. Additive schema; no migration.
- [x] `pulse_runs` collection — `tick_at`, `escalated`, `reason` (`empty` | `suppressed` | `escalated`), `findings_keys`, `new_keys`. TTL 30 days via `mongodb._ensure_indexes`. Exact per-turn LLM token cost is deferred to Phase 8 cost-tracking (TODO at insert site).

_Phase 9c — Prefetch:_ ✅
- [x] Prefetch service — `PrefetchService` polls every `PREFETCH_POLL_INTERVAL_S` (default 60s) and scans `tell` protocol-linked triggers firing within `PREFETCH_WINDOW_MIN` (default 5 min) from both `trigger_instances` and `AutomationService.iter_upcoming_protocol_fires` (anticipated fires). Tell-only by design — `act` has no output to cache and `offer` needs live evaluation. The orchestrator is injected at `start()`; system_context is built via `SystemTurnContext(decision="tell")` so prefetched and live prompts can never drift. NO_REPLY / empty responses are dropped. In-flight prefetch tasks are tracked and drained on shutdown.
- [x] Result cache — `prefetched_results` MongoDB collection keyed by unique `(source, trigger_id, protocol_name)`; `expires_at` drives a TTL index so crashed `running` docs self-heal. `_claim_slot` is single-flight via one atomic `find_one_and_update` (replace stale/failed rows) plus a fallback `insert_one` whose `DuplicateKeyError` collapses races against fresh ready/running rows — overlapping ticks never double-render.
- [x] Instant delivery — `_try_prefetched_delivery(trigger_data=...)` atomically consumes the cache via `find_one_and_delete` in the announce branch of `_handle_trigger_due`. On hit it schedules `_deliver_text` as the turn runner (preserves barge-in via `session.current_run_task`), opens `perf.start("turn_latency")` so VoiceDelivery closes the span on first audio chunk, marks the trace entry as `delivery="prefetched"`, and settles the trigger through the same guarded delivery-finalization path as live execution.
- [x] Fallback — any miss, expired row, or `fire_time` drift >60s rejects the cache and falls through to the live `process_turn` path. Cache lookup failures are caught and degrade silently.

**Phase 9.5 — Multi-Provider Calendar** (see [proposal](./proposals/built/MULTI_PROVIDER_CALENDAR.md)) ✅
- [x] `CalendarProvider` Protocol — minimal async interface (`list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `refresh`) in [backend/plugins/calendar/providers/base.py](../backend/plugins/calendar/providers/base.py). Raw provider payloads never cross the boundary — each provider returns already-parsed `CalendarEvent` / `EventConfirmation` stamped with its account label.
- [x] `GoogleProvider` refactor — Google-specific parsing, Meet payload, `calendarList` discovery, and httpx call sites moved out of the plugin into [backend/plugins/calendar/providers/google.py](../backend/plugins/calendar/providers/google.py). Owns its own calendar-ID cache.
- [x] `OutlookProvider` — new Microsoft Graph backend in [backend/plugins/calendar/providers/outlook.py](../backend/plugins/calendar/providers/outlook.py). Single `Calendars.ReadWrite` scope covers list/get/create/update/delete across `/calendars` + `/events`; Graph event shape → `CalendarEvent` (all-day, attendees, Teams `onlineMeeting.joinUrl` → `meet_link`, cancelled/declined filter). `add_meet=true` maps to `isOnlineMeeting=true` + `onlineMeetingProvider="teamsForBusiness"`.
- [x] `UnifiedCalendarClient` — fan-out reads merged + deduped by `(account, id)` + sorted by start; account-routed writes via `resolve_account(label)` against `settings.ACCOUNT_PROVIDERS`. `build_unified_client` loads only providers with valid tokens via `auth_manager.ensure_scopes` per provider; zero providers raises `NeedsReauth("calendar")`, one works fine.
- [x] Plugin rewrite — [backend/plugins/calendar/__init__.py](../backend/plugins/calendar/__init__.py) now injects `UnifiedCalendarClient`. Added `account: Optional[str]` to `get_event` / `create_event` / `update_event` / `delete_event`; docstrings teach the LLM to pass the `account` field from events it read straight back into mutations. `CalendarEvent` gains an `account` field that providers stamp themselves.
- [x] Multi-provider scope aggregation — `IntegrationManager.register_aux_provider_scopes(provider, scopes)` lets an integration declare scopes across multiple providers. Calendar declares Google + Microsoft scopes, so the existing OAuth consent screens already include them without changes to [backend/api/routes/auth.py](../backend/api/routes/auth.py).
- [x] Watcher + push adapter — [backend/services/watchers/calendar.py](../backend/services/watchers/calendar.py) calls `unified_client.list_events` directly; events carry `account` automatically so automation conditions can match on it. [backend/services/push/calendar.py](../backend/services/push/calendar.py) is constrained to Google (via `unified_client.get_provider("google")`); Outlook uses the 60s poll backstop. No Graph subscriptions in V1.
- [x] Tests — [backend/tests/test_calendar_unified.py](../backend/tests/test_calendar_unified.py) covers fan-out, dedup-by-(account, id), write routing, unknown-label raise, single-provider omit-account fallback, one-provider-failure tolerance. [backend/tests/test_calendar_outlook_provider.py](../backend/tests/test_calendar_outlook_provider.py) covers Graph → `CalendarEvent` conversion (all-day, attendees, Teams link, cancelled/declined filter).

**Phase 9.6 — Turn Lifecycle Cleanup** (see [proposal](./proposals/partial/TURN_LIFECYCLE_CLEANUP.md)) ← _architecture hygiene before Phase 10 grows turn visibility/proactive complexity_
- [x] Phase 1: renamed voice input state to `VoiceInputTurn`, added concise turn vocabulary sections to `ARCHITECTURE.md` and `SYSTEM_STATES.md`, and explicitly documented that `current_turn_task` still has a dual role until split. This is a clarity pass, not a reliability/performance fix.
- [x] Phase 2: split accepted voice-input handoff from assistant execution — `accepted_input_task` covers final STT/local-command/scheduling after endpointing commit; `current_run_task` is the active `process_turn()` / trigger / protocol / prefetch execution handle. Fast recovery awaits handoff when still finalizing STT, then cancels `current_run_task` while keeping the same `VoiceInputTurn`. Barge-in and disconnect cancel both.
- [ ] Defer `AssistantRun` until it deletes session-level run fields (`current_run_task`, `current_delivery`) and exposes a real `cancel(reason)` boundary. Do not add it as a wrapper-only abstraction.
- [ ] Defer `visibility` / `outcome` metadata and schema backfills until read-side filtering or diagnostics outgrow the current `delivery`-based hidden/visible constants. Do not add `delivery="voice"` or `delivery="text"`; that belongs to `modality` or a future `channel` axis.

**Phase 9.7 — Attention Plugin** ✅

Unified proactive output control so local commands and LLM tool calls set the same owner-level mode.

- [x] `AttentionMode = "active" | "quiet" | "paused"` — three-level owner attention model stored in MongoDB (`attention_state` collection, one document per owner). Auto-expires timed quiet/paused periods. Survives restarts.
- [x] `resolve_trigger_delivery()` — pure trigger policy combining attention mode, `AttentionPolicy.level`, and `TriggerAction.decision` into channel-neutral `agent_execution`, `presentation`, `blocked_result`, and `delivery_tag`. No I/O; fully testable.
- [x] `TriggerAction.decision` field — `"tell" | "offer" | "act"` on all trigger rules and instances; `DeliveryPlan` now holds physical routing hints only.
- [x] `attention` plugin (`jarvis.attention.*`) — `set_mode(mode, duration_minutes)` (owner attention only; does not soft-mute the live session), `mute()` (explicit “mute yourself” / session soft mute + `quiet`), `get_mode()`, `resume()`, plus recurring quiet windows (`set_quiet_window`, `list_quiet_windows`, `clear_quiet_window`). Non-tool `set_mode_for_identity()` for fast-path local command invocation.
- [x] Attention gate in `_handle_trigger_due` — before `mark_executing`, fetches owner mode and calls `resolve_trigger_delivery`; `tell` can defer to `awaiting_delivery`, `act` completes headless on success, and `offer` runs headless before conditionally presenting content.
- [x] Local command fast-path — exact `mute` / `mute yourself` phrases map to `SOFT_MUTE`: owner attention `quiet` + `session.soft_muted = True`; `UNMUTE` sets attention `active` + clears `soft_muted`. Phrases like `go quiet` are **not** local commands (they route through the agent / `set_mode("quiet")`). Local commands remain zero-LLM-latency.
- [x] `session.soft_muted` is a session-local **input** gate; `AttentionMode` (Mongo) is the source of truth for **proactive** delivery. They are intentionally separable: `quiet` without soft mute defers notifications but still accepts voice turns after reconnect until the user explicitly mutes.
- [x] Scheduled quiet windows — recurring `QuietWindow`s in `attention_schedules`. Effective mode is *derived* (`resolve_effective_attention()`), never stored; `AttentionReconcileService` (startup + 60s poll + window writes) only emits `ATTENTION_CHANGED` on transitions. Pure resolver in [backend/core/attention/resolver.py](../backend/core/attention/resolver.py) handles cross-midnight overlap and DST. Plugin tools: `set_quiet_window`, `list_quiet_windows`, `clear_quiet_window`. Manual `set_mode("active")` writes a `ManualOverride` bounded to the current window end.

**Phase 10 — Trusted Daily Use** ← _make existing capability visible, controllable, and reliable enough to use every day_ (barge-in: [brief](./proposals/built/BARGE_IN_RELIABILITY.md))

_Current focus: the bottleneck is trust, not capability. Prefer DDSD / demo-safe barge-in and Host dogfood exits over new capability fronts. Rooms & devices + Availability private access are shipped; do not open Beeper/messaging or broad HA/memory UI until the local voice loop feels reliable day-to-day. Satellite E2E stays under the hardware track until that room is the active bottleneck._

- [x] Agent/LLM behavior regression (V1) — trajectory eval harness reuses `_execute_turn` + `TurnResult` with deterministic scorers (`backend/evals/trace_extractor.py`, `agent_scorers.py`, `agent_behavior.yaml`). Mock P0 plumbing via `task be:eval-agent`; three live P0 canaries (scheduler vs todo, evaluate `NO_REPLY`, consent/false-completion) via `--live` with executor-stub safety, production routing by default, and shared offline bootstrap in `evals/bootstrap.py`. Deliberate-break validated (damaged scheduler docstring → case goes red). **Ongoing:** lock new behaviors as they land — add or freeze one canary per win; do not grow the suite by iterating prompts against the eval.
- [x] Voice + agent eval ladder (offline) — `task be:eval` already runs wakeword → STT → routing → agent mock P0 and is part of `release:candidate`. Live probes stay separate: `be:latency` (running backend) and `be:eval-agent --live`. Ambient FA/hr and enrolled free-speech need explicit wakeword flags + local corpora. **Still open:** satellite E2E on real device audio — tracked under Local Hardware / Satellite eval gate; build when that room is being hardened, not as standalone harness work.
- [x] Acoustic hygiene / AEC validation — validated the first-room XVF3800/ReSpeaker path after routing TTS as 2-channel playback so channel 0 carries the hardware AEC reference. Clean repro no longer creates a follow-on false STT turn after assistant playback; residual room-specific tuning can use `AUDIO_MGR_SYS_DELAY` / `AUDIO_MGR_REF_GAIN` if needed.
- [x] Barge-in candidate window — treat speech during `ACTIVE_AI_TURN` as a candidate instead of immediate interruption; capture a short STT window, duck/continue TTS, then commit interruption or roll back. Policy stays asymmetric: proactive alerts require wake-prefix or exact local controls; conversational answers still commit on endpointed text / max wait ([brief](./proposals/built/BARGE_IN_RELIABILITY.md)).
- [x] Owner speaker gate on barge-in — session-scoped `EnrolledSpeakerVerifier` reuses the Stage 2b embedding gallery; while `ACTIVE_AI_TURN`, commit only for enrolled-owner match (or wake-prefix / exact local control); suppress other voices / verifier failure. Matched `speaker_id` + confidence attach to `VoiceInputTurn` and user-turn metadata. Onset-forward PCM excludes TTS-contaminated pre-roll. `barge_in_speaker_threshold` is calibrated separately from wake via `eval_barge_in_speaker.py`. Echo/self-cutoff still needs acoustic hygiene — speaker match alone will not reject voice-cloned owner playback.
- [x] Shared turn-admission seam — `backend/core/voice/turn_admission.py` generalizes barge-in commit/suppress into pre-agent admission. Barge-in remains enforced. `ACTIVE_IDLE` follow-up is owner-gated when a speaker profile exists (fail-open until enrollment, and fail-open when the clip is too short to score). Wake-prefix is not a follow-up identity bypass. Wake/barge-in/PTT stamp `VoiceInputTurn.admission_source`/`admission_reason`; `Directedness` remains the future DDSD evidence field for owner side-speech. Household/guest allow-lists come later.
- [ ] Device-directed speech detection (DDSD) gate — populate `Directedness` for follow-up (then barge-in when latency allows) to distinguish speech meant for JARV1S vs side conversation. Build after the shared admission seam; keep the classifier swappable. Identity ≠ addressivity: speaker says who spoke; DDSD says whether to seize the floor. Multi-user enrollment is not a prerequisite.
- [ ] Demo-safe barge-in profile — optional config/toggle that raises the `ACTIVE_AI_TURN` barge-in threshold or requires wake-word-like intent during demos while the permanent fix lands.
- [x] Activity and Configured workspace — cursor-paginated, source-aware Activity projection over canonical stores with day grouping, visible filters, human outcomes/sources, and lazy trace detail. Configured unifies surfaced schedules, event rules, reminders, timers, alarms, deferred instructions, and protocols; safe rule enable/pause mutations use optimistic rollback while protocols remain read-only.
- [x] Apps and Settings information architecture — Apps is a trust surface for connection, account/provider, health, capabilities, and recent use, with detail-first connect/disconnect management and Discover. Runtime audio, model/voice, and credentials live in Settings.
- [x] Local-first connections (see [proposal](./proposals/LOCAL_FIRST_INTEGRATIONS.md)) — EventKit calendar on this Mac; OAuth tokens in Keychain-backed CredentialStore; official Google Desktop client via bundled `product_oauth.json` (Connect Gmail is Google sign-in when present, Advanced otherwise); Composio labeled **Cloud connector**; un-allowlisted Composio mounts nothing.
- [x] Bounded runtime diagnostics — correlated/redacted Python diagnostics, rotating desktop host logs, metadata-first exports with explicit user-content opt-in, and separately retained prompt dumps.
- [x] Direct widget actions — implemented the smallest `ui.action` path needed for real daily actions, starting with Gmail archive / mark read from `InboxWidget`.
- [x] Wakeword false-trigger tuning (runtime) — local hard-negative finetune promoted to `Jarvis.onnx`; cascade defaults `wakeword_sensitivity=0.70`, `wakeword_patience=3`, `wakeword_vad_threshold=0.5`, TitaNet `wakeword_speaker_threshold=0.21`; Stage 2b speaker verifier enabled when an owner profile exists (Settings → Voice & Audio enrollment; accept-all until then); feedback counts in diagnostics; no pre-roll while soft-muted. See [WAKEWORD_ARCHITECTURE.md](./proposals/WAKEWORD_ARCHITECTURE.md).
- [x] Owner voice enrollment — optional first-run setup step plus Settings → Voice & Audio; five hybrid samples (three “Jarvis”, two natural requests) write a model-bound embedding gallery under `{DATA_DIR}/voice/speaker-profiles/`; live sessions reload Stage 2b without restart; packaged builds ship no personal profile. Speaker-model changes deliberately require re-enrollment.
- [x] Wakeword eval harness + retrain loop — `backend/tools/eval_wakeword.py` (clip replay, `--grid`, `--failures`); workflow in `training/wakeword/README.md`; `retrain_wakeword` appends post-export eval summary.
- [x] Setup control plane — `setups.find/get` discover schedules, automations, habit check-ins, quiet windows, protocols, and pending one-offs; `setups.pause/resume/delete` delegate common lifecycle operations to domain owners. `activity.why_last_fire` remains history-only. No mirrored `Behavior` entity or generic CRUD framework.

**Phase 10.5 — Personal Routines And Readiness** ← _prove daily loops on existing trigger rails before adding broader intelligence_
- [x] Habits V0 (see [proposal](./proposals/partial/HABITS_AND_GOALS.md)) — native `habits` plugin with owner-scoped habit definitions, append-only logs, cue-based check-ins through `TriggerService`, and focused tests for the voice-first accountability loop.
- [x] Offer timing — `decision="offer"` runs headless with live alarm/timer commitment context from [backend/core/triggers/offer_context.py](../backend/core/triggers/offer_context.py). Commitments and offers are presets over decision/attention/freshness, not a sealed enum. `FreshnessPolicy` handles deterministic stale checks plus expire/force-deliver deadline behavior, `NO_REPLY` suppression is audited with a reason, and `DEFER` stores `next_retry_at` so semantic defers are retried by pulse even without connection/attention changes. Morning sleep debrief uses a time-based offer, not anchor events.
- [ ] Habits V1 — use the V0 logs to decide whether to add lightweight goal review, weekly reflection, adjustment suggestions, or richer widgets. Do not build scoring/gamification before proving the behavior loop.
- [ ] Morning briefing V0 (see [proposal](./proposals/MORNING_BRIEFING.md)) — build the `morning_briefing` protocol first. Pull useful overnight offer results from `trigger_instances`, compose one concise briefing with calendar/weather/tasks, and trigger it through existing `scheduler.remind` with `decision="offer"` and `instructions`.
- [ ] Phrase-triggered briefing (V1+) — only after V0 proves composition. “Brief me when I say good morning” via voice local-command or a normal external event (`source="voice"`, `event="good_morning"`); start with local-command-consumes to avoid double responses. No separate timing subsystem.

**Phase 10.6 — Home Assistant Setup Assistant** ← _make JARV1S able to get HA to the first real device, not just consume an already-configured hub_
- [x] Direct HA client — `plugins/smart_home/ha_client.py` (REST + WebSocket); `HA_URL`/`HA_TOKEN` in config; lifecycle health via `check_liveness` / `check_readiness`
- [x] Setup wizard — `task setup:home` (connect existing / onboard fresh / HA Green guide); `task setup:home:bootstrap` for Docker provisioning (macOS/Windows/Linux)
- [x] Docker bootstrap V1 — pinned HA Container image, compose under `.data/home-assistant/`, full onboarding-to-token flow, `.env` upsert, fixture capture + drift task
- [x] Connection validation — `get_setup_status` (plus setup CLI liveness/readiness checks) with honest unconfigured/down/token errors
- [x] Boundary decision — HA/vendor apps commission devices (Wi-Fi, QR, app-only toggles). JARV1S owns post-HA reconciliation: reload integration, read registry, name/area assignment, and voice control without opening the HA UI.
- [x] First vertical slice: Grid Connect E27 / Tuya cloud — `refresh_home_assistant` and `organize_device` wire the post-commissioning path. QR/account linking and Smart Life commissioning remain human handoff steps with instructions returned by the tools.
- [x] HA discovery/registry surface — `ha_client` config-entry list/reload, registry reads/writes, and `refresh_home_assistant` with explicit reload outcomes (`integration_missing`, `reload_failed`, `reload_ok_no_entities`, `reload_ok_with_entities`). Tuya lights are identified by config-entry membership, not cache diffs.
- [x] Naming and room wiring — `organize_device` creates/matches HA areas, assigns device/entity registry fields, and invalidates inventory cache after writes. HA areas remain the room source of truth; no JARV1S room model.
- [x] Room-relative voice commands — `bind_node_area` is optional and confirmation-based only when a physical node needs "this room" / "in here" resolution. Device setup does not require satellite binding.
- [x] Smart Home status panel — StatusBar overlay (`HomeAssistantPanel`); `GET /api/v1/smart-home/status` maps liveness/readiness into explicit UI states; Open Home Assistant + Refresh; controllable devices grouped by HA area.
- [ ] Second vertical slice: Tapo/Kasa hard case — after the Grid Connect reconcile path works, dogfood the per-device `tplink` flow. Build a config-flow handoff/driver only for the parts HA exposes (`config_entries/flow/progress` plus `/api/config/config_entries/flow` or the frontend's chosen transport), with consent before creating entries. When the flow hits app-only third-party compatibility, factory reset, cloud auth, or fragile credential gates, return exact next steps and resume from HA's discovered/in-progress flow.
- [ ] Defer LAN scanning — do not build a JARV1S-side UDP/mDNS scanner until HA discovery/registry APIs prove insufficient for a real device. If needed later, keep it diagnostic only: "what is on my network that HA did not discover?", not a parallel pairing system.

**Phase 10.7 — Multi-Device Trust Foundation** ← _make one satellite, the browser, and a phone behave like one trusted assistant before adding more rooms_

This supersedes the scattered "room history policy", "multi-device routing", and `sat:proxy` cleanup notes. The goal is not follow-me intelligence or speaker ID yet; it is the smallest set of primitives that makes daily multi-device use predictable, private, and secure. _Last verified against code: 2026-06-07._

- [x] Reachable backend (LAN/private/public) — replace the dev-only `sat:proxy` path with an explicit deployment choice. Implemented via `task be:dev` / `task be:dev:lan`, satellite `backend_url` guardrails, and [MULTI_DEVICE_REACHABILITY.md](./deployment/MULTI_DEVICE_REACHABILITY.md). Reachability alone is not device-secure; pair with per-device auth below for identity on remote clients.
  - LAN/dev: bind the backend on `0.0.0.0` and point satellites at `ws://<host>:8000/api/v1/ws`
  - roaming/private: prefer Tailscale/WireGuard so phone + satellites reach the brain without a public voice endpoint; classify tailnet targets deliberately (`100.64.0.0/10`, `*.ts.net`) and prefer Tailscale Serve + `wss://` for browser/phone trust
  - public/reverse-proxy: require TLS and `wss://` before any remote phone/satellite connection
- [x] Per-device WebSocket auth — issue one credential per browser/phone/satellite at provisioning, validate it before creating a `Session`, resolve `owner_id` server-side from the credential (`token -> owner_id`) rather than global `DEFAULT_USER_ID`, bind it to `node_id`, capabilities, and optional room refs, and make individual devices revocable. Never trust `owner_id` from query params. Implemented via owner-bearing device credentials, REST ws-ticket exchange, Rooms **Connect speaker** (Host LAN pair; `jarvis-satellite pair` fallback; `POST /device-auth/satellites` is CLI recovery), and CLI recovery (`task devices:*`).
- [x] Turn-origin voice delivery — user-initiated turns answer on the originating `connection_id` / `node_id`, not `default_connection_by_owner_id`. `process_turn(connection_id=...)` uses strict connection lookup. Tool follow-ons (`run_protocol`, `stop_listening`) carry live-origin `connection_id` only; background agents omit it so protocol output uses owner-default. Dead origin connections settle without fan-out.
- [ ] On-device wakeword mode for satellites — implementation is available behind `edge_wakeword`: local openWakeWord PASSIVE detection, rolling pre-roll flush on wake via `voice.activate`, then Host streaming for VAD/STT/endpointing/barge-in; idle rooms stop raw PCM. Enable with optional `wakeword` deps + `Jarvis.onnx` under `~/.jarvis-satellite/models/` (`SATELLITE_EDGE_WAKEWORD=1 task sat:deploy`). Keep this open until the live edge path passes wake → STT → TTS in-room with unclipped commands and reduced idle bandwidth.
- [x] Conversation session scoping — long-term memory and `recall()` stay owner-global; short-term LLM history is scoped by `owner_id + origin node_id` via `mongodb.get_history(node_id=...)`, loaded from the originating node, and bounded by an inactivity window over recent user turns plus visible proactive deliveries (`announce` / `evaluate` / `prefetched`). `db.reset_conversation_window` applies the same cut immediately without deleting rows. Stale sessions stay durable and searchable, but are not injected into fresh prompts. Node-set coalescing and enrolled `speaker_id` are deliberately deferred — the `node_id` filter leaves room to add them without rewriting history.
- [x] Proactive delivery presence resolver — `core.triggers.endpoint_router.resolve_proactive_endpoints()` resolves the output endpoint at fire time from live presence: explicit `DeliveryPlan.target` `node_id` → `location_ref` → last-active speaker-capable node, else `awaiting_delivery`. Targeting is author intent: `deliver_to="anywhere"` (follow-me), `"here"` (snapshot origin `node_id`), or a bound room name (snapshot `location_ref`). Cross-room broadcast, visual/push fallback, and body tracking remain out of scope.
- [x] Node↔room binding — `bind_node_area` maps the current node to a Home Assistant area via durable `ws_device_credentials.location`; scheduler `deliver_to=<room>` resolves to `location_ref` for wake alarms and room-targeted reminders. Live presence refreshes best-effort on bind; reconnect rebuilds from the credential. Host UI **Assign room** in Rooms & devices calls the same presence assign path.
- [x] Rooms & devices UI — StatusBar/Home-linked overlay (`PresencePanel`); `GET /api/v1/presence/` merges live WebSocket sessions with provisioned device credentials; plain-language device kinds, assign room, view turns, remove access; **Connect speaker** (Host LAN pair, CLI fallback) + poll; phone/browser pairing cards. Private access enablement lives in **Settings → Availability** (`enable_host_serve`). CLI `task devices:*` remains recovery. Smart Home no longer hosts a parallel Endpoints manager.
- [ ] Phase gate — before checking off any behavior-changing 10.7 item, rerun the Phase 10 voice + agent eval ladder on the affected node(s). Edge wakeword especially is not done until the satellite path passes the same reliability bar as the browser/backend-owned path.

**Phase 11 — Automation Intelligence** (see [proposal Phase 3](./proposals/built/AUTOMATION_ENGINE.md)) ← _only after Phase 10 is useful in daily use_
- [ ] Reaction tracking — log user response per fired automation (listened, dismissed, acted); prerequisite for preference learning.
- [ ] Intelligent Triage — activity-state awareness (in a meeting, asleep, away); agent reasons about interrupt worthiness rather than hard rules. Builds on `decision="offer"` but needs visible feedback loops first.
- [ ] Preference model — lightweight local classifier trained on reaction data. Gates "should I bother the user?" for rules that opt in.
- [ ] Adaptive suppression — auto-reduce fire frequency for high-dismiss-rate rules.
- [ ] Auto-suggest rules — LLM notices behavioral patterns in archival memory and proposes automations.
- [ ] Cross-source / hybrid triggers — combine schedule and external state only when concrete daily use cases justify the extra rule complexity.

### Phase 12 — Channel Expansion (when needed)
- [ ] Gateway Input Abstraction — `ChannelAdapter` protocol (`authenticate`, `parse_inbound`, `check_access`, `format_outbound`); normalize Audio/Text/Telegram/Slack events to common `InboundMessage` before orchestration. Prerequisite for multi-channel delivery.
- [ ] Multi-channel delivery — Telegram bot as first remote channel; delivery target field on automation rules and System Pulse alerts so proactive output can reach the user outside the house. Proactive features double in value with at least one remote channel.
- [ ] Beeper / messaging integration — high-value remote channel (unified messaging), but build it as a `ChannelAdapter` behind the Gateway abstraction above, not a bespoke plugin. Deferred until the local voice loop is trusted; adding it earlier creates a second half-built front.

## Supporting Tracks

These are not a second priority queue. They are supporting workstreams that unblock or harden the phased roadmap above.

### Local Hardware / Acoustic Deployment
- [ ] Satellite hardware path — get a Pi Zero 2 W (or replacement edge node), XVF3800/ReSpeaker, and speaker running as a real room endpoint; if supply-blocked, keep the blocker visible here rather than hiding the work behind presence protocol readiness. See [VOICE_SATELLITE_EDGE.md](./proposals/partial/VOICE_SATELLITE_EDGE.md).
- [x] Satellite client V0 — `satellite/` Python service with stable `node_id`, `capabilities`, optional location refs, continuous 16 kHz mono PCM capture, backend-owned reasoning/STT/TTS, reconnect, and `audio.playback_end` handling. Continuous idle streaming is acceptable for first-room bring-up only; Phase 10.7 moves PASSIVE wakeword to the edge.
- [ ] Pi bring-up — verify USB audio enumeration, microphone capture, speaker playback, user-systemd supervision, reconnect behavior, and boot persistence (`loginctl enable-linger` or equivalent if needed).
- [x] Satellite playback hardening — backend `audio.tts_end` now retires happy-path drain debounce; the satellite sends `audio.playback_end` after local drain + marker, with a per-turn missing-marker timeout to avoid stuck `ACTIVE_AI_TURN`.
- [ ] Satellite protocol hardening — move PCM from JSON/base64 envelopes to binary WebSocket frames before adding multiple active satellites.
- [ ] Room onboarding — commission devices in vendor apps, run `refresh_home_assistant` + `organize_device`, then bind each satellite node to the matching HA area with `bind_node_area` from that room.
- [ ] Satellite eval gate — run wakeword, STT, and `be:latency` checks against real device audio before calling a room endpoint usable.

### Performance / Inference Path
- [ ] **On-device streaming STT quality** — validate Apple Speech with real-voice repeats and tune remaining commit outliers (see [LOCAL_STREAMING_STT.md](./research/LOCAL_STREAMING_STT.md)).
- [ ] Satellite audio architecture decision — V1 should keep inference on the central brain unless evidence says otherwise: Pi captures/plays audio, brain handles STT/agent/TTS, and edge STT/LLM work stays deferred.
- [x] Managed local LLM implementation — desktop Host supervises isolated Ollama on `:11435`; setup consent + Settings install/activate/remove use the existing provider boundary (see [LOCAL_MODEL_LANE.md](./proposals/LOCAL_MODEL_LANE.md)).
- [x] Local TTS implementation — desktop Host supervises Kokoro helper on `:9092`; Spoken replies Off / Cartesia / On this Mac (see [LOCAL_TTS.md](./research/LOCAL_TTS.md)).
- [ ] Managed local LLM release qualification — pin the immutable `gemma4:e4b-mlx` digest, run packaged interrupt/resume/switch/remove lifecycle checks, and measure first-token latency, tokens/sec, memory pressure, 32K stability, voice turns, and structured tool-call reliability on 16 GB and 24/32 GB Macs before declaring it the baseline.
- [ ] Local TTS release qualification — curated int8 voice quality, warm/cold first-audio latency, helper crash/restart, barge-in, and coexistence with managed local LLM on the oldest supported Apple Silicon Mac.

### Windows / WSL Deployment
- [ ] CUDA/WSL STT provider — define a separate provider contract for streaming-native NVIDIA ASR and validate `latency_probe` tail metrics
- [ ] Docker Compose for WSL — GPU passthrough (`deploy: resources: reservations: devices`), volume mounts, env wiring
- [ ] Validate full stack on Windows WSL2 — wake word, STT, agent, TTS, frontend, MongoDB

### Packaged Desktop Host App (see [JARVIS_HOST_APP.md](./proposals/JARVIS_HOST_APP.md))
- [x] Phase 0 — Tauri shell, supervisor, startup phases, backend sidecar, packaged runtime build (`apps/desktop/`)
- [x] Phase 1a (partial) — arm64 signing/notarization/release CI, static updater artifacts, diagnostics export command, macOS app data paths
- [ ] Phase 1a exit — dogfood `N-1`→`N` internal updates; validate signed-build `getUserMedia` voice
- [x] Phase 1b — bundled native `mongod`; `ServiceProvider`; remove Docker from non-technical user path (validation pending)
- [ ] Phase 1b exit — clean-machine checklist on signed build without Docker
- [ ] Phase 2 — release channels, staged rollout, dynamic update endpoint (after Phase 10 eval gate for external auto-update)

## Parking Lot

These items are intentionally outside the top-to-bottom phase queue. Promote one back into the phased roadmap only when the adjacent capability is in daily use or the bottleneck is obvious.

### Deferred Until Adjacent Work Is Active
- [ ] Google Pub/Sub Auth — proper service-account auth for Gmail Pub/Sub; 60s poll fallback covers the gap until needed
- [ ] Commitment executor — LLM creates commitments ("I'll send that email by 5pm") via a `commitments` plugin; a background evaluator checks due items and either reminds or auto-executes (opt-in per commitment). Cancel window maps to existing consent-gated destruction. Depends on delivery modes + System Pulse. Revisit after Phase 10.
- [x] Decouple parsing strategy from `JarvisAgent` — agent loop yields `AgentEvent`s; LiteLLM adapter assembles OpenAI-style `tools=` deltas into complete `ToolCallEvent`s for every action-capable model, including Gemma 4 on Cerebras.
- [x] Native `tools=` on the voice path — structured capability calls for cloud and local OpenAI-compatible hosts. LiteLLM function-calling catalogs are not a gate; `probe_action_capability()` is the live proof.

### Ideas To Revisit
- Diary/journal tool
- Acknowledgment loops — repeat/escalate unacked alarms, SMS fallback
- Timer/alarm countdown widgets — ephemeral TTL widgets for active timers
- Household speaker identity + speaker-routed context — multi-enroll centroids, named `speaker_id` on transcripts, history keyed by optional speaker, and dynamic profile/memory injection by who is speaking. Builds on Phase 10's per-turn speaker evidence + DDSD spine; do not start here, and do not treat full diarization as the first barge-in fix.
- Follow-me / body-presence prediction — defer until explicit turn-origin delivery, conversation session scoping, and last-active proactive routing have real daily usage. Do not build this as the first multi-room routing answer.
- Widget persistence (survive page refresh) — active domain-owned widgets restore through `ui.snapshot`; one-off receipts remain local/ephemeral, while domain-backed progress receipts can be rebuilt from their owner store
