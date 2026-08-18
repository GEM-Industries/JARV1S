# Background Agents

Reference for the background agent system — execution paths, task lifecycle, spawn flows, fan-out controls, and known quirks.

---

## Two Runtimes, By Design

Background agents ship with two runtimes on purpose — collapsing them to one was considered and rejected.

- Both runtimes use `BACKGROUND_AGENT_MODEL` (default `claude-opus-4-8`) with medium reasoning effort. The Anthropic key is stored through **Settings → Credentials** and read from `CredentialStore`; `.env` keys are not used. Code mode passes the key only to its SDK subprocess.
- **`mode="jarvis"` (in-process)** keeps every active integration, credential, context-var (including `_consent_resolver`), and already-hot singleton (embedder, tool router, MongoDB client). Tool calls cross a Python function boundary, not a process or network boundary — sub-millisecond overhead. Best fit for any dispatch that touches Slack, Gmail, GitHub, Calendar, Memory, Smart Home, or anything cross-service.
- **`mode="code"` (SDK subprocess)** gives the user the best-available coding model with first-class Bash, file edit, diff, and session-resume primitives. Best fit for pure file/git/shell work where the subprocess-grade tooling is the point.

Why not collapse to one runtime (e.g. a universal MCP bridge)? An in-process capability call is a direct function invocation in the same asyncio loop. An equivalent call from a subprocess has to traverse stdio/MCP framing, JSON-encode both request and response, and re-hydrate context on each hop. For high-fan-out plugin sequences (read memory, check calendar, draft email, push widget) the overhead dominates the actual work. Cross-process consent would also need a new request/response protocol whose only job is to reproduce a contextvar that already works in-process.

Consent posture differs by runtime. `mode="jarvis"` installs a deferred approval resolver on the existing `_consent_resolver` contextvar: destructive tools create a durable `pending_inputs` row, update the task with `attention="approval"` / `pending_input`, push a `PendingInputWidget`, and wait for approval, denial, or timeout before the plugin action runs. `mode="code"` remains SDK-isolated with `permission_mode="bypassPermissions"`; it does not call in-process `require_consent()` and high-risk SDK approval callbacks are intentionally deferred until the shared approval path is proven.

## Two Execution Paths

Every background task runs in one of two modes, selected by the voice model when calling `agents.dispatch()`.

| | `mode="code"` | `mode="jarvis"` |
|---|---|---|
| **Runtime** | Claude Code SDK subprocess (`claude-agent-sdk`) | In-process `JarvisAgent` via `asyncio.create_task` |
| **Tools available** | Read, Write, Edit, Bash, Grep, Glob + Composio MCP (Slack, GitHub, Gmail…) | Full plugin catalog via structured capability calls — calendar, email, memory, smart_home, weather, automations, widgets |
| **JARV1S access** | None — isolated process | Full — same plugins as a voice turn |
| **File/shell work** | Best-in-class (battle-tested Edit, diff handling, session resume) | `files.*` / `system.exec` tools — no native Bash |
| **Max turns** | `max_turns` param (default 40) | `AGENT_INPROCESS_MAX_TURNS` (default 30) |
| **Cancellation** | `task.cancel()` → drain coroutine → SIGTERM → SIGKILL | `task.cancel()` → `asyncio.CancelledError` — instant |
| **Session resume** | Yes — stores `session_id`, resume restores full prior context | No — always starts fresh |
| **When to use** | Pure file editing, Git operations, shell pipelines — tasks with no JARV1S plugin dependency | Any task involving integrations (Slack, Gmail, GitHub), JARV1S plugins (calendar, memory, automations, smart home), or cross-service work |

The voice model specifies `mode` explicitly; there is no deterministic runtime router inside `dispatch()`. The `dispatch()` docstring steers pure file/git/shell work toward `mode="code"` and JARV1S integration work toward `mode="jarvis"`.

`cwd` is optional on the public tool. If omitted, it defaults to the JARV1S project root. For file-producing tasks, prompts should still use explicit absolute output paths so artifacts are predictable and verifiable.

`dispatch()` returns a JSON string, not prose:

```json
{
  "ok": true,
  "task_id": "k8Tm4xQ2pR7n",
  "mode": "code",
  "error_code": null,
  "error_message": null,
  "message": "Task started."
}
```

If `ok=false`, no task row was created and the task did not start. Common `error_code` values include `already_in_background`, `source_limit_reached`, `depth_limit_reached`, `agents_not_initialized`, and `jarvis_runtime_unavailable`. Batch delegation is still one `dispatch()` call per task; callers must inspect each result before starting the next task. A compound prompt that says "do task 1, task 2, and task 3" creates one cancelable task row, not three independent agents.

Completed or failed task results are retrieved with `get_result()`, not `get_status()`:

```json
{
  "ok": true,
  "task_id": "k8Tm4xQ2pR7n",
  "status": "completed",
  "result": "Full stored task result...",
  "error_code": null,
  "error_message": null,
  "message": "Task result found."
}
```

`get_status(task_id)` is intentionally status-focused. For completed or failed tasks it returns a terminal-state message and points the model to `get_result(task_id)` instead of returning a truncated `progress_summary`; repeated status polling after that is wasted. If `task_id` is omitted, `get_result()` returns the most recent completed or failed task for the owner, which supports natural follow-ups like "what was that result?" without redispatching the work.

---

## Task Lifecycle

### States

```
queued → running → completed
                 → failed
                 → cancelled
```

| State | Set by | Notes |
|---|---|---|
| `running` | `_prepare_task()` at insert time | Task is in MongoDB before the asyncio Task is created |
| `completed` | `_complete_task()` | Creates a system-origin `TriggerInstance` and publishes `TRIGGER_DUE` for delivery |
| `failed` | `_fail_task()` | On exception or `CancelledError` |
| `cancelled` | `_fail_task("Task was cancelled.")` | Set inside the `CancelledError` handler |

**There is no `queued` state in practice.** Tasks are written as `running` immediately and the asyncio Task is created synchronously after. If the semaphore blocks, the task sits at `running` in MongoDB while the coroutine awaits the semaphore.

**Task IDs** are 12-character alphanumeric nanoids (e.g. `k8Tm4xQ2pR7n`) generated by `core.id.generate_id()` — compact enough for reliable LLM pass-through and voice readback.

**TTL / cleanup:** Completed and failed tasks have an `expires_at` BSON Date field set 30 days after completion. A sparse TTL index on `expires_at` auto-deletes them. Running tasks have no `expires_at` while the backend process is alive.

### Crash Recovery

On startup, any task left in `running` state is marked `failed` with `interrupted_reason="backend_restart"`, `completed_at`, and `expires_at`. Recovery does **not** retry or auto-resume work. `mode="jarvis"` cannot resume, and `mode="code"` resume should remain explicit through `resume(task_id, feedback)` so file edits and shell work are not restarted unexpectedly.

---

## Spawn Flow

### `mode="code"` — Subprocess Path

```
agents.dispatch(prompt, cwd=None, mode="code")
  ↓
_dispatch()
  ├── _get_connected_composio_apps()                         concurrent
  ├── PromptBuilder.build_conversation_context()             concurrent
  ├── _resolve_tools_for_dispatch(apps)    → list[{type, url, name, headers}]
  └── PromptBuilder.build_subprocess_prompt(owner_id, cwd, conv_context)
  ↓
_prepare_task() → MongoDB insert (status="running")
  ↓
_push_task_progress_receipt(force=True) → review-rail progress receipt
  ↓
asyncio.create_task(_run())
  └── semaphore.acquire()
      └── _run_agent(task_id, prompt, cwd, max_turns, mcp_servers, system_prompt)
            ├── AgentOptions(permission_mode="bypassPermissions", mcp_servers=mcp_dict)
            ├── SDKClient.connect() + query(prompt)
            ├── drain loop: AssistantMessage → MongoDB $push events, progress_summary
            │              ToolUseBlock → _push_task_event → throttled receipt upsert
            │              ResultMessage → capture session_id, cost_usd
            └── _complete_task() → terminal receipt + TriggerInstance + TRIGGER_DUE
```

**System prompt injected:** Built by `PromptBuilder.build_subprocess_prompt()` — XML-tagged with `<identity>`, `<rules>`, `<env>` (owner, home, cwd, platform, date), `<conversation-context>` (last 6 turns, tool results truncated to 300 chars), `<user-preferences>` (last 10 memories). The subprocess agent has no access to JARV1S internals — only what's in this prompt plus the MCP tools.

**MCP tools:** Composio apps are resolved fresh per dispatch (late-binding). Each app becomes an HTTP MCP server config passed to the SDK. The subprocess calls Composio's MCP endpoint directly over HTTP — no in-process connection.

### `mode="jarvis"` — In-Process Path

```
agents.dispatch(prompt, cwd=None, mode="jarvis")
  ↓
_dispatch_inprocess()
  ├── _get_background_agent() → integrations.get("background_agent")
  └── _prepare_task() → MongoDB insert (status="running")
  ↓
_push_task_progress_receipt(force=True) → review-rail progress receipt
  ↓
asyncio.create_task(_run_inprocess())
  └── _IN_BACKGROUND.set(True)
      semaphore.acquire()
      └── bg_context = PromptBuilder.build_background_context(owner_id, cwd)
          bg_context["routed_tools"] = tool_router.route(prompt, task_id)
          background_agent.process_stream(
              prompt,
              [],                                    # empty history
              owner_id,
              context=bg_context,                    # source, cwd, user_profile, timezone, local_time
              max_iterations=AGENT_INPROCESS_MAX_TURNS,
              prompt_mode=PromptMode.BACKGROUND
          )
          ├── AgentEventType.TEXT        → MongoDB progress_summary/live_status, throttled receipt upsert
          ├── AgentEventType.TOOL_CALL   → throttled receipt upsert
          ├── require_consent()          → pending_inputs row + task attention=pending approval + receipt/action update
          └── AgentEventType.TOOL_OUTPUT → capped activity/artifact extraction
  └── _complete_task() → terminal receipt + TriggerInstance + TRIGGER_DUE
```

---

## UI Surfaces

Background agents use two complementary surfaces:

| Surface | Component | When | User action |
|---|---|---|---|
| **Review rail** | `ContentWidget` progress receipt (`receipt_kind: task_progress`) | On dispatch, throttled while running, briefly after complete/fail | Click opens detail or approval |
| **Detail widget** | `BackgroundTaskWidget` | On explicit click from rail, activity timeline, or follow-up | Read artifacts, trace, approval state |

**Progress receipts** are built by `task_progress_receipt_envelope()` on top of the generic `progress_receipt_envelope()` primitive in `core/plugins/ui.py`. Each receipt carries:

- `line` — human-readable progress from `live_status` / `progress_summary` (not runtime mode names)
- `status` — `running` | `completed` | `failed`
- `attention` — `none` | `approval`
- `action` — click routing metadata (`open_background_task`, `activate_widget`)

Receipts upsert through `ui.update` with stable `widget_id: task-receipt-{task_id}`. Updates are throttled (~2s) while running and forced on start, approval transitions, and terminal state.

**Reconnect:** `background_task_snapshot_widgets` restores **progress receipts** for running tasks, not full `BackgroundTaskWidget` heroes.

**Completion:** Voice delivery via `TRIGGER_DUE` is the primary completion surface. The rail receipt updates to a short terminal summary (45s TTL). The detail widget is not promoted into the main canvas automatically.

**Approval:** When `attention="approval"`, the receipt action prefers `PendingInputWidget` (`activate_widget`) and falls back to opening task detail if that widget is not yet present.

**Detail fetch:** `BackgroundTaskWidget` loads richer task state from `GET /api/v1/tasks/{task_id}` when opened or when the task reaches a terminal state inside an already-open detail view.

**System prompt:** Built by `PromptBuilder.build(mode=BACKGROUND)` — includes identity, protocols, and reasoning sections but **skips** voice, style, and examples (irrelevant for background execution). The dispatch prompt is routed with the task id as its isolated session key and injected as `routed_tools`, so the background agent gets a task-specific manifest instead of the full plugin namespace. The `[EXECUTION MODE]` block is injected via runtime context: *"You are a background agent. Execute the task directly. Do NOT call agents.dispatch."*

**Context injection:** `PromptBuilder.build_background_context()` loads user profile (via `get_profile_block()`) and resolves timezone from the session document — identical to the context `process_turn()` provides for voice turns.

If `mode="jarvis"` is requested and the in-process JARV1S runtime is unavailable, dispatch returns `ok=false` with `error_code="jarvis_runtime_unavailable"`. It does not silently fall back to `mode="code"`, because code mode has a powerful model but no JARV1S plugin access.

---

## Completion Delivery

Both paths use the same delivery mechanism:

```
_complete_task()
  → TriggerInstance(action.decision="tell", origin.kind="system")
  → TRIGGER_DUE event (source="agents")
  → AssistantOrchestrator._handle_trigger_due()
      ├── User connected, no turn → chime + system turn + TTS summary
      ├── User connected, turn in progress → deferred (turn_lock)
      └── User offline → trigger moves to awaiting_delivery and retries on reconnect
```

The trigger message is: `"Finished. {summary}"`. For `mode="code"`, `summary` = the final SDK text block. For `mode="jarvis"`, `summary` = the text emitted after the last tool call — the agent's final response. The orchestrator executes this as a trigger delivery, not as a user-authored task. Background-mode prompting tells the agent to base final summaries on observed tool results, but `_complete_task()` still stores the agent-reported result; task reviewability comes from separate `artifacts` and `activity` fields on the task document.

System task-result turns are instructed to relay the completed result in a short, voice-first summary. They must not call `dispatch()` again; if the user later asks for the result to be repeated, the foreground turn should call `agents.get_result()`.

If a completion announcement is interrupted, the browser stops audio via the existing `system.stop` path. When the backend includes the active `response_id`, the frontend marks that streamed assistant response as no longer partial so the transcript does not stay in the blue "still streaming" state. This is a visual delivery cleanup only; task completion remains on `background_tasks`, and delivery outcome remains owned by `TriggerInstance`.

`BackgroundTaskWidget` is the on-demand detail surface for artifacts, result, activity, and trace. It is not the ambient progress indicator — that role belongs to the review-rail progress receipt. When opened, the widget fetches task detail from `GET /api/v1/tasks/{task_id}` for terminal tasks or richer in-flight state. Artifacts record what the runtime can verify about file paths (`exists`, `exists_verified`, size, and whether before/after metadata changed for SDK file edits). In `mode="code"`, artifact candidates come from direct SDK file-edit events plus explicit file paths in the dispatch prompt and tool inputs, which covers files created through Bash or SDK child agents without scanning the whole workspace.

Task state and evidence are split by purpose:

- `events`: small live progress stream stored in MongoDB for diagnostics and future drill-down, still capped for responsiveness. Frontend progress in the review rail comes from throttled `ui.update` receipt upserts, not a separate client event buffer.
- `activity`: concise receipt rows derived from observed output.
- `trace`: durable task-scoped evidence. `mode="jarvis"` stores structured capability names, arguments, and result previews. `mode="code"` stores SDK text/tool starts, input previews, verified artifacts, and explicit notes where the SDK does not expose structured tool output.
- `attention` / `pending_input` / `live_status`: the small control-plane fields used when a task is running but waiting on the user. They do not replace task lifecycle status; a task can remain `status="running"` while `attention="approval"`.

The widget deliberately has no inline refine, open, raw-log actions, or result preview truncation — follow-up work should go through the main Jarvis turn so the assistant can choose whether to call `get_result()`, `resume()`, `dispatch()`, or another tool.

---

## Remaining Trust Gaps

Background agents are operational, but not yet fully trustworthy for high-stakes or file-producing work. The next hardening work should stay runtime-led rather than prompt-led:

- **Artifact verification:** file-producing tasks now store first-class artifacts, but existence is not the same as semantic correctness. Richer diff/review can come later.
- **Tool-output trail:** background tasks now keep task-scoped `trace` rows, but this remains a compact audit trail rather than compliance-grade tracing.
- **Runtime consent plumbing:** `mode="jarvis"` now uses the same consent contract as foreground tools via durable pending inputs. `mode="code"` SDK approval/question callbacks remain deferred behind an explicit policy decision.
- **Failure-aware completion:** `_complete_task()` should distinguish completed work from completed-but-unverified work when required evidence is missing.
- **Review surface:** the widget shows artifacts, result, and activity in a receipt-style layout. Richer diff/review actions are still deferred until there is a broader UI action pattern worth reusing.
- **Focused evals:** keep regression coverage narrow: mode-selection guidance, result replay, and artifact verification behavior. Avoid transcript-specific prompt tests.

---

## Triggers

| Source | Path | `depth` | `source` value |
|---|---|---|---|
| Voice turn | `agents.dispatch()` capability call | 0 | `"voice"` |
| Automation rule (`dispatch_agent` action) | `AgentsPlugin._dispatch()` directly | 0 | `"automation"` |
| `resume()` | New task, old `session_id` passed to SDK | 0 | `"resume"` |
| Background agent trying to re-dispatch | Blocked by `_IN_BACKGROUND` check | — | Rejected with error |

---

## Fan-Out Controls

Three independent guards, checked in `_prepare_task()`:

| Config | Default | Scope |
|---|---|---|
| `AGENT_MAX_CONCURRENT` | `2` | Global semaphore — max total running tasks across all sources |
| `AGENT_MAX_PER_SOURCE` | `3` | Per-source counter (in-memory `Counter`, **resets on restart**) |
| `AGENT_MAX_DEPTH` | `1` | Depth stored on task doc; checked at dispatch; prevents agent-spawning-agent |

`_IN_BACKGROUND` ContextVar is a second depth guard specific to `mode="jarvis"`: if `dispatch()` is called from within an in-process task, it returns an error immediately before any DB write.

Guard failures are command results, not task states. They return `ok=false` from `dispatch()` and do not create a `background_tasks` row.

The foreground model is responsible for fan-out shape. If the user asks for "three separate agents," the correct execution is three `dispatch()` calls with three focused prompts. One `dispatch()` call with three assignments is still one task, one task id, one cancellation target, and one review receipt.

---

## Session Resume (`mode="code"` only)

```
resume(task_id, feedback)
  → look up task in MongoDB
  → re-resolve Composio MCP servers (fresh credentials)
  → _run_agent(..., resume_session_id=doc["session_id"])
      → AgentOptions(resume=session_id)
```

`session_id` is captured from the **first `AssistantMessage`** (not `ResultMessage`) so it's available even for cancelled or failed tasks. Session files live in `~/.claude/projects/` (Claude SDK) or `~/.opencode/sessions/` (OpenCode SDK). If session files are missing (e.g. different machine, clean install), resume will start a fresh session — it won't error.

`mode="jarvis"` tasks do **not** support resume. Their `session_id` field is always `null`.

---

## Known Quirks

**`permission_mode` for `mode="code"` is now `bypassPermissions`**
Previously `acceptEdits` (which auto-approves file edits only), causing Composio MCP tool calls to fail with a permissions error. Fixed: subprocess agents now run with `bypassPermissions` — all tools execute without prompts. JARV1S runs in a trusted home environment; the subprocess is the appropriate isolation boundary.

**`AGENT_INPROCESS_MAX_TURNS` is 30**
Previously 15, which was too low for multi-step Slack/Gmail scans. When the limit is hit, `process_stream()` emits *"I'm sorry, sir. I seem to have encountered a logical loop."* (the `max_iterations` fallback at `agent.py:212`). 30 turns is sufficient for most multi-step integration tasks. For unbounded research tasks, dispatch with a narrower, scoped prompt.

**Source counts are in-memory only**
`_source_counts` is a `Counter` on the `AgentsPlugin` instance. It resets on every server restart. Startup recovery marks persisted `running` tasks as failed, so the counter intentionally starts empty for the new process.

**`mode="jarvis"` tool routing is prompt-scoped**
The dispatch prompt is routed once at task start and injected as `routed_tools`. If the prompt is too vague, the in-process agent may miss a plugin it needs. Prefer concrete dispatch prompts that name the target integration or action.

**Code mode agents have no access to JARV1S plugins**
The subprocess is completely isolated. It has Composio MCP tools and filesystem tools only. It cannot push widgets, access automations, or use any built-in JARV1S plugin. If a task starts as `mode="code"` and turns out to need JARV1S internals, it must report back and be re-dispatched as `mode="jarvis"`.

**`cost_usd` is post-hoc, not real-time**
`max_budget_usd` is stored on the task document but cannot be enforced mid-task (the SDK only returns `total_cost_usd` in the final `ResultMessage`). A task can exceed its budget — the overage is logged and included in the completion trigger message but cannot be prevented.

**No automatic backoff for external API rate limits**
The `[EXECUTION MODE]` prompt instructs the background agent to skip rate-limited resources and continue, but there is no runtime-level throttle. The agent relies on LLM compliance. If the agent retries a 429 immediately, it will waste iterations. For integration scan tasks (Slack, Gmail), the dispatch prompt should prefer targeted search APIs over sequential per-channel/per-thread history fetches — search queries produce fewer API calls for the same coverage.

**Dispatch prompts should include the user's known identity**
When the task is "search for my messages/mentions/assignments," the dispatch prompt should include the user's email or display name. The in-process agent receives `[USER CONTEXT]` in its system prompt, but the dispatch prompt itself often uses "me" or "the user" without an anchor. Including `geoff@example.com` or `Geoff` saves the subagent 2-3 identity-discovery iterations.
