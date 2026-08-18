# Phase 7: Subagent Architecture & Model Tiering

**Status:** Implemented (Phase 7.0 + 7.1)  
**Superseded by:** [`docs/BACKGROUND_AGENTS.md`](../../BACKGROUND_AGENTS.md) — the current reference for execution paths, prompt building, `dispatch()` arguments/return shape, and known quirks.  
**Current deviations:** `reason()` mid-turn escalation removed; use explicit `dispatch(mode="jarvis"|"code")` instead. Pre-turn `TurnRouter` model routing was later removed.  
**Date:** 2026-03-19

Unmarked phases below are **historical** or tracked on [ROADMAP.md](../../ROADMAP.md) (e.g. Phase 7.3 mid-task approvals). Later Phase 7.5 work is documented in [`EMBEDDED_AGENT_RUNTIME.md`](./EMBEDDED_AGENT_RUNTIME.md) and [`cleanup/AGENT_RUNTIME_HARDENING.md`](./cleanup/AGENT_RUNTIME_HARDENING.md).  
**Depends on:** Automation Engine (Phase 4), Trigger Scaling (Phase 6.5), Plugin Architecture

---

## Problem

JARV1S's `process_turn()` holds `session.turn_lock` from STT through TTS. Any tool that takes more than a few seconds blocks the voice loop — a 4-minute PR review renders JARVIS deaf and mute. This caps JARV1S at what completes within a single voice turn (~10-45s).

The secondary problem: the voice loop runs a single model for everything. For complex planning or result evaluation, there's no way to invoke a stronger model without destroying latency.

**References:** Turn lock and CodeAct executor security boundary are documented in [ARCHITECTURE.md](../../ARCHITECTURE.md). The trigger delivery substrate and automation engine are documented in [TRIGGER_SCALING.md](./TRIGGER_SCALING.md).

---

## Design

### Principle: Thin Bridge, Not an Orchestration Framework

The `dispatch_agent` plugin translates a JARV1S tool call into an SDK invocation and translates SDK output back into JARV1S events. The subagent has no access to JARV1S internals — no MongoDB, no event bus, no plugin registry. All JARV1S-specific integration (storing results, creating trigger instances, pushing widgets) happens in the drain coroutine.

However, the subagent is **not** a pure filesystem worker. At dispatch time, JARV1S resolves connected integrations into MCP server configs and passes them to the SDK. The subagent gets the same external tools (calendar, email, GitHub, etc.) via MCP, not CodeAct. See "Tool Resolution" below.

### Agent SDK: Provider-Agnostic Abstraction

JARV1S targets the `opencode-agent-sdk` (PyPI, MIT) as the primary subagent runtime. It's a drop-in replacement for `claude-agent-sdk` with the same API surface (`query`, `AgentOptions`, `create_sdk_mcp_server`, `mcp_servers`, session resume) but supports **any LLM provider** — Anthropic, OpenAI, xAI, or local models. Users bring their own API key.

A thin wrapper in `backend/core/agent/sdk.py` isolates the SDK choice. If OpenCode's SDK has stability issues in early versions, swap to `claude-agent-sdk` by changing one file:

```python
# backend/core/agent/sdk.py — the only file that imports the SDK
from opencode_agent_sdk import SDKClient, AgentOptions, ResultMessage, AssistantMessage, TextBlock, ToolUseBlock
```

**Fallback:** If `opencode-agent-sdk` is unavailable or unstable, switch the import to `claude_agent_sdk` (Claude-only, but battle-tested). Both expose the same API.

`sdk.py` also exposes the active SDK's filesystem conventions so JARV1S writes instructions/skills to the correct paths:

```python
INSTRUCTION_FILE = "AGENTS.md"       # "CLAUDE.md" if using claude-agent-sdk
SKILLS_DIR = ".opencode/skills"      # ".claude/skills" if using claude-agent-sdk
```

**Historical subprocess SDK notes** (superseded by `docs/BACKGROUND_AGENTS.md`; current `mode="code"` uses `permission_mode="bypassPermissions"` so Composio MCP tools can run):
- `Query.close()` can hang causing 100% CPU — requires timeout wrapper on cleanup
- Orphaned CLI processes after sessions — requires force-kill in finally block
- CLOSE_WAIT socket leak on macOS — monitor for idle CPU spin
- Earlier builds used `acceptEdits`; current builds use `bypassPermissions` for subprocess agents.

```python
from backend.core.agent.sdk import query, AgentOptions, ResultMessage, AssistantMessage, StreamEvent

SDK_CLEANUP_TIMEOUT = 5.0

async def _run_agent(
    task_id: str, prompt: str, cwd: str,
    max_turns: int, max_budget_usd: float,
    mcp_servers: dict | None = None,
    allowed_tools: list[str] | None = None,
):
    options = AgentOptions(
        cwd=cwd,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        allowed_tools=allowed_tools or ["Read", "Edit", "Bash", "Write"],
        system_prompt=per_task_persona,
        include_partial_messages=True,
        mcp_servers=mcp_servers or {},
        permission_mode="bypassPermissions",
    )

    try:
        session_id = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, StreamEvent):
                await _process_stream_event(task_id, message)
            elif isinstance(message, AssistantMessage):
                await _push_progress(task_id, message)
            elif isinstance(message, ResultMessage):
                session_id = message.session_id
                await _complete_task(task_id, message, session_id)
    except Exception as e:
        await _fail_task(task_id, str(e))
    finally:
        try:
            await asyncio.wait_for(_sdk_cleanup(), timeout=SDK_CLEANUP_TIMEOUT)
        except (asyncio.TimeoutError, Exception):
            logger.warning("SDK cleanup timed out for task %s — force killing", task_id)
```

### Tool Resolution at Dispatch Time

JARV1S's tool sources are already MCP-shaped:

| Tool source | Subagent mechanism |
|---|---|
| **Composio integrations** (Gmail, GitHub, Calendar, Jira) | Pass Composio's per-user MCP URL as `McpHttpServerConfig` |
| **Non-Composio MCP servers** (`mcp_servers.yaml`) | Pass existing stdio/HTTP config directly |
| **Agent built-ins** (Read, Edit, Bash, Write, Grep, Glob) | Always available via `allowed_tools` |
| **Built-in JARV1S plugins** (automations, scheduler, protocols) | Not exposed — voice-loop management tools |

`ComposioGateway.get_mcp_url(app_name)` returns a per-user Streamable HTTP MCP URL. At dispatch time, collect these concurrently and pass to the SDK:

```python
async def _resolve_tools_for_dispatch() -> tuple[dict, list[str]]:
    mcp_servers: dict = {}
    allowed_tools = ["Read", "Edit", "Bash", "Write", "Grep", "Glob"]

    apps = composio_gateway.connected_apps()
    urls = await asyncio.gather(*[composio_gateway.get_mcp_url(a) for a in apps])
    for app_name, mcp_url in zip(apps, urls):
        if mcp_url:
            mcp_servers[app_name] = {
                "type": "http", "url": mcp_url,
                "headers": {"x-api-key": composio_gateway.api_key},
            }
            allowed_tools.append(f"mcp__{app_name}__*")

    for name, config in mcp_config.servers.items():
        mcp_servers[name] = config
        allowed_tools.append(f"mcp__{name}__*")

    return mcp_servers, allowed_tools
```

**Late-binding:** tools resolved fresh per dispatch. Credentials always current.

**Composio URL lifetime:** Composio MCP URLs are session-scoped tokens. Long-running tasks (30+ min) may outlive them. If a tool call fails mid-task due to expired credentials, the SDK's own retry logic handles reconnection. If it can't recover, the task fails and can be re-dispatched. Monitor this in production; if it's frequent, cache and refresh URLs in the drain coroutine.

**Built-in vs Composio: one path per integration.** Built-in plugins are voice-loop optimizations (custom OAuth, push notifications). Composio is the subagent-compatible path. Not intended to run simultaneously for the same app. If a built-in tool is later needed by subagents, `create_sdk_mcp_server()` wraps it as in-process MCP — one function, not an architecture change.

### Data Model: background_tasks Collection

```python
{
    "task_id": str,              # uuid
    "user_id": str,
    "status": str,               # queued | running | completed | failed | cancelled
    "source": str,               # voice | automation | pr_comment
    "prompt": str,               # constructed prompt sent to the agent
    "session_id": str | None,    # from ResultMessage — enables resume
    "cwd": str,                  # absolute path — must match on resume
    "mcp_servers": [str],        # names of MCP servers resolved at dispatch
    "result": str | None,        # final result text or summary
    "progress_summary": str,     # latest progress (overwritten, not appended)
    "events": [dict],            # last 50 events — updated via $push + $slice: -50
    "max_turns": int,
    "max_budget_usd": float,
    "started_at": datetime,
    "completed_at": datetime | None,
    "trigger_ref": str | None,   # "PROJ-1234" or "PR#47"
    "cost_usd": float | None,    # from ResultMessage
}
```

Indexes: `user_id`, `status`, `trigger_ref`. Events array maintained via MongoDB `$push` with `$slice: -50` to cap at 50 entries.

### Model & Thinking Strategy

| Tier | Purpose | Model | Provider | Latency | Cost |
|---|---|---|---|---|---|
| **Voice** | Conversational turns, simple tools, intent classification | GPT-OSS-120B | Groq | ~100ms TTFT | Normal |
| **Reasoning** | Complex planning, prompt construction, result evaluation | Claude Sonnet 4 (or equivalent) | OpenRouter | ~1-3s | Per-call |
| **Worker** | Long-running tasks, PR reviews, implementations | User-configured (`AGENT_WORKER_MODEL`) via Agent SDK | OpenCode / Anthropic fallback | Minutes | Per-task |

The voice model recognizes two escalation patterns: (1) "this needs deeper analysis" → `reason()`, (2) "this is a big job" → `dispatch()`. A 120B model handles this classification trivially.

#### think() — Zero-Cost Scratchpad

A no-op tool based on [Anthropic's research](https://www.anthropic.com/engineering/claude-think-tool) showing 54% improvement on complex multi-step tool chains. Gives the fast model a structured place to reason between tool calls without an API call:

```python
async def think(self, thought: str) -> str:
    """Think through a problem before acting. Use between tool calls when you
    need to analyze results, plan multi-step actions, or decide whether to
    dispatch a background task. Scratchpad only — takes no action."""
    return "[Thought recorded]"
```

#### reason() — Reasoning Model Escalation

A second `LLMService` instance for a frontier model via OpenRouter (`LLM_REASONING_*` config). Injected via `@tool(inject=["reasoning_llm"])`. Separate client, separate config — no latency contamination on the voice loop.

```python
@tool(inject=["reasoning_llm"])
async def reason(self, question: str, reasoning_llm: LLMService) -> str:
    """Deep analysis using a stronger model. Blocks the voice turn — only
    use when think() isn't enough for the complexity required."""
    try:
        return await asyncio.wait_for(
            reasoning_llm.chat(
                user_message=question,
                system_prompt="Think step by step. Be thorough and precise.",
                temperature=0.3,
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return "Reasoning timed out. Proceeding with available information."
```

#### Worker Tier — Agent SDK

Runs via `asyncio.create_task()` inside `dispatch()`. Completely outside the voice loop. The agent uses whatever model the user has configured via `AGENT_WORKER_MODEL`. Defaults to Claude Sonnet 4 (best tool-use performance), but works with any provider the SDK supports.

---

## End-to-End Flows

### Voice-Initiated Background Task

```
User: "Hey JARVIS, review my open PRs"

1. STT → Tool Router activates agents plugin
2. CodeAct: fast model → jarvis.agents.dispatch(prompt="...", cwd="...")
3. dispatch() plugin:
   a. _resolve_tools_for_dispatch() → Composio MCP URLs + MCP configs (concurrent)
   b. Writes task to background_tasks: status=queued
   c. asyncio.create_task(_run_agent(...))  ← non-blocking
   d. Returns "Task started. I'll let you know when it's done."
4. Fast model speaks → TTS → turn lock releases (~2s total)

[Minutes later]

5. _run_agent drain coroutine:
   - StreamEvent → structured events → push BackgroundTaskWidget + MongoDB
   - ResultMessage → session_id → status=completed → store result
6. Trigger delivery path:
   - Connected, no turn → chime + system turn + TTS
   - Connected, turn in progress → deferred until lock releases
   - Offline → `awaiting_delivery`, retried on reconnect
```

### Resuming with Feedback

Every completed task stores `session_id`. Any task can be resumed with new instructions — no special mode required:

```
User: "JARVIS, what did that PR review find?"  → get_result() → reads stored result
User: "The auth section is wrong, fix it"      → resume(session_id, feedback)
```

This handles plan→review→execute naturally. If the user wants planning first, they say so in the prompt. The agent produces a plan (or a markdown file, or whatever the SDK's natural output is). The user reviews and resumes with feedback or approval. The agent keeps full context across all rounds via `session_id`.

### Agent Instructions: Skills and Project Files

User preferences ("always check for performance," "always research online first") are handled by the agent's own config system — not a JARV1S-specific template engine.

**Performance constraint:** [AGENTbench (ETH Zurich, Feb 2026)](https://chatpaper.com/paper/237058) shows that bloated instruction files actively degrade agent performance — adding ~4 extra steps per task, increasing costs 19-23%, and yielding only ~4% success improvement. Frontier models reliably follow ~150 total instructions; Claude Code's system prompt already consumes ~50. Keep project-level instructions under 60 lines, command-first, universally applicable.

The three instruction layers, from most to least constrained:

- **`system_prompt` in AgentOptions** — JARV1S injects a **max 10-line** distillation of user preferences from its memory layer at dispatch time. Only the 5-10 most universal, high-signal preferences (e.g., "always verify APIs exist before using them"). This is the most expensive context — it's in every turn.
- **Agent instruction file** (`AGENTS.md` for OpenCode, `CLAUDE.md` for Claude — OpenCode reads both with `AGENTS.md` taking priority) in the `cwd`. Short, command-first: build commands, test commands, critical architectural constraints. Under 60 lines. Not a style guide.
- **Skills directory** (`.opencode/skills/` or `.claude/skills/` — OpenCode discovers both) — the preferred place for detailed, domain-specific knowledge. Skills are **on-demand** (progressive disclosure) — the agent discovers them when relevant, not front-loaded into every turn. This is the correct pattern for review checklists, coding standards, and best practices like [claude-code-best-practice](https://github.com/shanraisshan/claude-code-best-practice).

Zero custom infrastructure. The filesystem and the SDK's native config system do the work.

**Why skills > monolithic instruction files:** Skills are loaded only when the agent's task matches the skill's trigger description. A 200-line review checklist in the instruction file pollutes every task; the same checklist as a skill only loads during review tasks. This directly addresses the AGENTbench finding that extra instructions add steps without improving outcomes.

**Future (documented, not built in Phase 7):**
- Voice/text command to manage skills: "JARVIS, add a review skill that checks for WCAG accessibility" → writes to the active SDK's skills directory.
- Settings panel to paste best-practice markdown that JARV1S writes to the correct project directory.
- JARV1S learning from repeated feedback: if the user gives the same review comment 3+ times, suggest persisting it as a skill (not an instruction file addition — skills are scoped, instruction files are global).

### PR Comment Resumption (Phase 7.2)

```
GitHub PR comment → Composio trigger → AutomationService → dispatch_agent action

1. Look up task by trigger_ref (PR#N, status=completed)
2. If session_id exists → AgentOptions(resume=session_id, cwd=task.cwd)
   with re-resolved MCP servers
3. If session files gone → fresh dispatch with reconstructed context from PR diff
```

---

## Priority Lane Concurrency

Background task completion creates a system-origin `TriggerInstance` and publishes `TRIGGER_DUE`, using the same trigger delivery path as reminders, alarms, system pulse, and protocol-linked triggers.

Concurrency limit: `asyncio.Semaphore(2)` in the plugin. Two concurrent agents is the starting point — each CLI subprocess consumes 370-430MB RSS, so memory is the practical constraint. Configurable via `AGENT_MAX_CONCURRENT`.

---

## Crash Recovery

On startup, mark any task in `running` status as `failed`:

```python
await mongodb.db.background_tasks.update_many(
    {"status": "running"},
    {"$set": {"status": "failed", "result": "Server restarted during execution"}}
)
```

Session resume means a re-dispatched task picks up where the agent left off. Session files live in the SDK's project directory (`~/.claude/projects/` for Claude, `~/.opencode/sessions/` for OpenCode) — in Docker, mount this as a volume or accept that resume requires fresh context reconstruction.

---

## Cost Control

| Mechanism | How |
|---|---|
| **Turn budget** | `max_turns` per source: voice=40, PR comment=15, automation=50 |
| **Dollar budget** | `max_budget_usd` per source: voice=$2, PR comment=$0.50, automation=$5 |
| **Permission scoping** | `allowed_tools` restricts tool access. Unexpected approval needs surface through durable pending inputs / `PendingInputWidget`. |
| **Cost tracking** | `ResultMessage.cost_usd` → stored on task record. Cumulative daily cost logged. |

---

## Task Cancellation

`cancel_task(task_id)` does two things: cancels the `asyncio.Task` (sends `CancelledError` to the drain coroutine) AND signals the SDK to terminate. Because the SDK spawns a CLI subprocess, cancelling the Python task alone is insufficient — the `finally` block in `_run_agent()` handles cleanup with the timeout wrapper. If the subprocess doesn't terminate within `SDK_CLEANUP_TIMEOUT`, it is force-killed via the process handle.

---

## Plugin Structure

```
backend/plugins/agents/
├── __init__.py    # AgentsPlugin: dispatch, reason, get_status, get_result, list_tasks, cancel_task, resume
└── client.py      # _run_agent, _resolve_tools_for_dispatch, _build_system_prompt, helpers
```

| Tool | Purpose |
|---|---|
| `dispatch(prompt, cwd=None, mode="code", max_turns, max_budget_usd)` | Spawn one background task. Current implementation returns a JSON string; see `docs/BACKGROUND_AGENTS.md`. |
| `resume(task_id, feedback)` | Resume a completed task with new instructions via session_id. |
| `reason(question)` | Reasoning-model escalation. Blocks turn (~1-3s, 15s timeout). |
| `get_status(task_id?)` | Running task status + progress from MongoDB; completed/failed tasks point to `get_result()`. |
| `get_result(task_id?)` | Full stored result for completed or failed work; omitting `task_id` returns the latest finished task. |
| `list_tasks(status?)` | List tasks, optionally filtered. |
| `cancel_task(task_id)` | Cancel running task + subprocess cleanup. |

Core plugin (`"core": True`) — always loaded, never routed. The fast model must always be able to escalate.

---

## Frontend: Task Visibility

JARV1S is voice-first. The visual layer is supplementary — a phone/tablet glance, not a primary workstation.

### BackgroundTaskWidget

**Progress (review rail):** status badge, progress summary (single line), elapsed time, and source tag are shown as a compact progress receipt. Tapping opens the detail widget or the pending-input widget when approval is needed.

**Detail widget:** fetches task detail from `GET /api/v1/tasks/{task_id}`. Renders a compact evidence view — artifacts, result, activity, and trace. If the agent produced files (plans, reports), they appear as artifacts.

**Feedback:** follow-up happens through the main Jarvis turn, which can call `get_result()`, `resume(task_id, feedback)`, or start a new task. The task detail widget does not host an inline chat/feedback input.

### Execution Event Stream

The drain coroutine translates `StreamEvent` messages into lightweight structured events, pushed to the frontend in real-time via WebSocket and stored on the task record:

- `content_block_start` with `tool_use` → `{"t": "tool_start", "tool": "Read", "input_summary": "README.md"}`
- `content_block_stop` → `{"t": "tool_end", "tool": "Read"}`
- `text_delta` chunks → debounced into periodic `{"t": "text", "summary": "Found 3 issues..."}`
- `ResultMessage` → `{"t": "complete", "result_summary": "PR #48 opened"}`

### Mid-Task Approvals (Phase 7.3)

For `mode="jarvis"`, destructive in-process tools use the foreground pending-input contract: the task stays `status="running"`, sets `attention="approval"`, and the progress receipt routes to `PendingInputWidget`. `mode="code"` remains SDK-isolated with `bypassPermissions`; SDK approval callbacks are still deferred.

---

## What NOT to Build

| Approach | Why skip |
|---|---|
| **Subprocess management / PID tracking** | The SDK abstracts this. Cleanup handled in `_run_agent()` finally block. |
| **Streaming progress narration to voice** | Background tasks push silent widget updates only. "I'm 50% done" over voice is hostile UX. |
| **Generic tool resolver framework** | Single-user system. One function iterates connected apps and collects MCP URLs. |
| **Agent trace visualization library** | Flat timeline in the widget is sufficient. Subagents can't nest. |
| **Review template engine** | User preferences belong in the agent instruction file, skills directory, and JARV1S memory — not a custom template system. |
| **Supervised vs autonomous mode system** | Every task is resumable. The user decides when to intervene, not the system. |
| **Full IDE-style agent chat panel** | The review rail plus on-demand task detail is sufficient. Follow-up goes through the main Jarvis turn, which can choose `get_result()`, `resume()`, or a new task. |

---

## Implementation Plan

### Phase 7.0: Core — Background Agents ✅

1. `backend/core/agent/sdk.py` — thin abstraction wrapping `opencode-agent-sdk` (with `claude-agent-sdk` fallback)
2. `AGENT_WORKER_MODEL` + `AGENT_MAX_CONCURRENT` config settings
3. `background_tasks` MongoDB collection with indexes
4. `plugins/agents/` — `AgentsPlugin` with `dispatch`, `resume`, `get_status`, `list_tasks`, `cancel_task`
5. `think()` moved to `system` plugin (universal scratchpad, always available)
6. `_resolve_tools_for_dispatch()` — concurrent Composio MCP URL collection
7. `_run_agent()` — `SDKClient` connect/query/receive_response drain loop with `try/except/finally`, timeout cleanup, `bypassPermissions` permission mode
8. Completion delivery via `TriggerInstance` / `TRIGGER_DUE`
9. Startup orphan recovery in `main.py` lifespan
10. Progress receipts in the review rail plus on-demand `BackgroundTaskWidget` detail for artifacts, trace, activity, and results
11. `GET /api/v1/tasks/` + `GET /api/v1/tasks/{task_id}` endpoints
12. `asyncio.Semaphore(AGENT_MAX_CONCURRENT)` concurrency limit
13. `uv add opencode-agent-sdk`

### Phase 7.1: Model Tiering ✅

14. `LLM_REASONING_*` config settings (API key, base URL, model)
15. Second `LLMService` instance registered as `reasoning_llm` in `lifespan()`
16. `reason()` tool with `@tool(inject=["reasoning_llm"])`, `temperature=0.3`, and 15s timeout

### Phase 7.2: Session Resume — largely implemented ✅

18. `session_id` + `cwd` stored on completion
19. Automation rule `action.type = "dispatch_agent"`
20. PR comment trigger: resume via `AgentOptions(resume=...)` with re-resolved MCP
21. Fallback to fresh context if session files missing

### Phase 7.3: Robustness — not implemented

> See [ROADMAP.md](../../ROADMAP.md) Phase 7 open items. Note: `dispatch_agent` as a **trigger** action kind is not wired in `_handle_trigger_due` (voice `jarvis.agents.dispatch` works).

22. Mid-task pending input for unexpected permission requests
23. Watchdog: timeout in drain coroutine (no message in N minutes → cancel)
24. Composio URL refresh for long-running tasks (if expiry observed in production)

---

## Tradeoffs

**Agent SDK abstraction (OpenCode primary, Claude fallback):**
OpenCode Agent SDK (`opencode-agent-sdk`) enables any LLM provider, removing Anthropic lock-in. Both SDKs share known subprocess bugs (process leaks, cleanup hangs). The `_run_agent()` finally block with timeout cleanup mitigates this. All SDK interaction is isolated behind `backend/core/agent/sdk.py` — swapping providers is a single-file change. Trade-off: agent quality varies by model. Weaker models produce worse results on complex tasks. Default recommendation (Claude Sonnet 4) remains the best tool-use model, but users can choose.

**Second LLMService for reasoning:**
Two API keys, two billing accounts. Mitigated by OpenRouter (one key covers all frontier models) and infrequent usage (complex tasks only).

**MCP passthrough for subagent tools:**
Built-in JARV1S plugins not exposed to subagents. Acceptable — those are voice-loop management tools. Escape hatch: `create_sdk_mcp_server()` wraps any built-in tool as in-process MCP if needed later.

**Capped event array (50):**
Long-running tasks lose early history. Full session transcripts live in the SDK's session directory for forensic debugging. MongoDB stores the "glanceable" version.

**Subagent context isolation is the performance win:**
Each subagent gets a fresh context window. Anthropic's internal data shows 90% performance gain from isolated context vs. accumulated single-agent context. The drain coroutine returns only the result to JARV1S — intermediate tool calls, search results, and test output stay in the subagent's context and never pollute the voice loop or future tasks.

**No formal task queue:**
`asyncio.create_task()` + semaphore. Tasks lost on crash — mitigated by crash recovery + session resume. Sufficient at personal-assistant scale (1-5 concurrent tasks).
