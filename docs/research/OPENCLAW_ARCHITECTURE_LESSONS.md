# Lessons from OpenClaw's Architecture for JARV1S

**Date:** 2026-04-02
**Source:** OpenClaw open-source (v2026.3.x), official docs, community deep-dives.

This document distils the most important architectural patterns from OpenClaw that are directly applicable to JARV1S. Focused on what's *different* from the Claude Code lessons — overlapping patterns are noted but not repeated.

---

## 1. Hub-and-Spoke Gateway: Treat AI as Infrastructure

OpenClaw's core thesis: **the AI model provides intelligence; the platform provides the operating system**. A single Gateway process acts as the control plane between *all* input channels and the agent runtime.

```
Channels (WhatsApp, Telegram, Discord, iMessage, Slack, CLI, Web, Mobile)
        │
        ▼
   ┌─────────────┐
   │   Gateway    │  ← single process, WebSocket server
   │  (control    │     typed JSON Schema frames
   │   plane)     │     event-driven subscriptions
   └──────┬──────┘
          │
          ▼
   ┌─────────────┐
   │   Agent      │  ← session resolution → context assembly
   │   Runtime    │     → model invocation → tool execution
   └─────────────┘     → state persistence
```

**How JARV1S compares:** JARV1S has a single WebSocket server (`backend/api/`) that can now distinguish live `connection_id`s from stable `node_id`s and the trusted `owner_id` namespace. The voice pipeline is still delivered through the requesting socket, so true multi-channel adapters and explicit room targeting remain future work.

### What to adopt

- **Channel adapter interface**: Define a `ChannelAdapter` protocol with four methods: `authenticate()`, `parse_inbound()`, `check_access()`, `format_outbound()`. Even if JARV1S only has one frontend today, this abstraction enables future channels (WhatsApp for remote interaction, Telegram for mobile, Slack for work) without touching orchestration code. The key insight is normalising all input to a common `InboundMessage` before it reaches the orchestrator.
- **Typed WebSocket protocol**: OpenClaw validates every WebSocket frame against JSON Schema generated from TypeBox definitions. JARV1S's WebSocket handler (`handlers.py`) uses string-based message type matching. Move to Pydantic-validated frame types — malformed messages are caught at ingestion, not deep in business logic.
- **Event-driven subscriptions over polling**: Clients subscribe to event streams (`agent`, `presence`, `health`, `tick`) instead of polling. JARV1S already uses an event bus internally but the frontend still receives flat WebSocket messages. Consider exposing event subscriptions so the frontend can subscribe to specific event categories.
- **Idempotency keys for side effects**: OpenClaw requires idempotency keys on any mutating operation, making retries safe. JARV1S's automation engine has `_fired` dedup but the general tool system doesn't. Add idempotency to destructive operations like `send_email`, `create_event`.

---

## 2. Composable System Prompt: Sectioned Assembly with PromptMode

OpenClaw's `buildAgentSystemPrompt()` (in `src/agents/system-prompt.ts`) assembles the prompt from ~15 discrete sections, each conditionally included:

| Section | Content | Mode Gating |
|---|---|---|
| **Tooling** | Available tool names + one-line summaries, sorted deterministically | All modes |
| **Tool Call Style** | "Default: do not narrate routine tool calls" | Full only |
| **Safety** | No self-preservation, no safeguard bypass, pause if conflicting | Full only |
| **Skills** | Skill scan + read-on-demand instructions | All modes |
| **Memory** | Memory search prompt (built by plugin) | Full only |
| **Workspace Files** | AGENTS.md, SOUL.md, TOOLS.md, USER.md etc. injected as "Project Context" | Full only |
| **Heartbeats** | HEARTBEAT_OK contract, checklist | Full only |
| **Silent Replies** | `NO_REPLY` sentinel for nothing-to-say turns | Full only |
| **Runtime** | agent ID, host, OS, model, channel, capabilities | All modes |

**Three PromptModes**: `full` (main agent — all sections), `minimal` (subagents — Tooling, Skills, Workspace, Runtime only), `none` (bare identity line). This prevents subagents from inheriting expensive sections they don't need.

**Key patterns from source:**
- **`NO_REPLY` silent token**: When the agent has nothing to say, it responds with exactly `NO_REPLY`. The runtime recognises and discards it. Strict rules: must be the *entire* message, never appended to real content.
- **Tool narration control**: "Do not narrate routine, low-risk tool calls (just call the tool). Narrate only for multi-step work, complex problems, or sensitive actions."
- **Prompt sanitization**: All workspace paths pass through `sanitizeForPromptLiteral()` before injection — prevents prompt injection via crafted directory names.
- **SOUL.md persona instruction**: "If SOUL.md is present, embody its persona and tone. Avoid stiff, generic replies."

**How JARV1S compares:** `PromptBuilder` assembles static persona + dynamic context. Persona is developer-managed YAML. No PromptMode gating for subagents. No silent reply token.

### What to adopt

- **User-editable instruction files**: Create a `~/.jarvis/` directory with editable markdown files: `PROFILE.md` (user facts), `INSTRUCTIONS.md` (persistent preferences). Injected into the dynamic prompt section every turn and survive compaction because they're re-read from disk.
- **Separation of persona from policy**: Split `persona.yaml` into `persona.yaml` (voice, tone) and `policy.yaml` (behavioral constraints, safety rules). Change personality without touching safety.
- **PromptMode for subagents**: When dispatching in-process subagents (`mode="jarvis"`), use a minimal prompt that omits memory, heartbeat, and protocol sections. Reduces token cost and prevents context pollution.
- **Silent reply token**: Add a `NO_REPLY` sentinel that the orchestrator recognises and discards. Useful for automation/heartbeat turns where the agent determines no action is needed.
- **Tool narration rule**: Add an explicit prompt rule: "Do not narrate routine tool calls. Only explain when actions are multi-step, high-risk, or the user explicitly asked."
- **Per-file token caps**: OpenClaw caps each bootstrap file at 20K chars, total at 150K. Add character limits to prevent oversized context sections from consuming the budget.

---

## 3. Memory: Hybrid Search + Pre-Compaction Ingest

OpenClaw's memory system is more mature than a simple vector store:

```
Messages arrive
    │
    ├── Index into SQLite (BM25 full-text + vector embeddings)
    │
    ├── On compaction trigger:
    │   ├── 1. "Memory flush" — promote durable facts to memory files
    │   ├── 2. Chunk full conversation into FTS index (pre-compaction ingest)
    │   └── 3. Summarise and compact older turns
    │
    └── On query:
        └── Hybrid search: BM25 keyword relevance + vector similarity
            → top-K results injected as context
```

**Key details from source** (`src/agents/memory-search.ts`):
- **Hybrid is ON by default**: `DEFAULT_HYBRID_ENABLED = true`, weights: 0.7 vector / 0.3 text
- **Temporal decay**: Newer memories scored higher with configurable half-life (default 30 days)
- **MMR (Maximal Marginal Relevance)**: Diversity ranking to avoid returning 5 near-duplicate results
- **Post-compaction force sync**: After compaction, automatically re-index the session into FTS
- **Chunking**: 400 tokens per chunk with 80-token overlap for continuous context
- **Pre-compaction ingest** (Memory V2): Full conversation indexed before compaction — preserves exact details (numbers, code, decisions) even after summarisation
- **Temporal/entity filters**: `memory_search()` supports `since: "7d"` and `entity: "project-x"`

**How JARV1S compares:** Two-tier memory with MongoDB. Vector-only recall. No hybrid search, no temporal decay, no post-compaction indexing.

### What to adopt

- **Hybrid search**: Add BM25 alongside vector similarity. MongoDB Atlas Search supports both `$search` and `$vectorSearch`. Combine with reciprocal rank fusion. Fixes "what error code did I get yesterday?" — exact-match queries that pure vector search handles poorly.
- **Temporal decay**: Weight recent memories higher. A 30-day half-life means a 60-day-old memory scores ~25% of an identical match from today. Prevents stale context from drowning recent events.
- **Pre-compaction indexing**: Before `fit_to_budget()` drops messages, index them into the search system. Cheaper than LLM extraction and preserves exact details.
- **Temporal filters**: Add a `since` parameter to the archival recall tool for "last week I mentioned..." queries.
- **SQLite for local memory**: For edge deployment, a SQLite backend with `sqlite-vec` acceleration. Aligns with JARV1S's "local-first" vision tenet.

---

## 4. Session-as-Security-Boundary

OpenClaw's most elegant security pattern: **the session key encodes the trust level**.

**Session key format** (from `src/routing/session-key.ts`): `agent:<agentId>:<rest>`

| Key Pattern | Trust | Sandboxing |
|---|---|---|
| `agent:main:main` | Full operator | None |
| `agent:main:direct:<peerId>` | Approved contact (per-peer DM) | Docker |
| `agent:main:<channel>:direct:<peerId>` | Approved contact (per-channel DM) | Docker |
| `agent:main:<channel>:group:<peerId>` | Multi-participant | Docker + network-off |
| `...:<channel>:<accountId>:direct:<peerId>` | Multi-account DM isolation | Docker |

**Four DM scope levels**: `main` (all DMs share operator session), `per-peer`, `per-channel-peer`, `per-account-channel-peer`. Progressively stronger isolation.

**Identity links**: Cross-channel identity resolution — if the same person messages from WhatsApp and Telegram, `resolveLinkedPeerId()` maps both to the same canonical identity, so they share a session.

Security is enforced by the runtime, not the prompt. The model never gets to "decide" to break out of sandbox — tools are simply not available.

**How JARV1S compares:** Single trust level — the authenticated home user gets full access. Multi-user awareness exists (speaker identification) but doesn't change the security posture.

### What to adopt

- **Session-scoped tool availability**: When JARV1S identifies a guest speaker (not the owner), automatically restrict tool access to read-only operations. The `ToolRouter` already selects plugins per-turn — extend it to also consult a session trust level. This is critical for the multi-room vision: a guest in the living room shouldn't trigger destructive operations.
- **Encode trust in session metadata**: Add a `trust_level` field to the Session object (e.g., `owner`, `household`, `guest`). Derive it from speaker identification. Wire it into `require_consent()` — owner operations that normally auto-approve should require explicit consent when a guest initiates them.
- **Runtime enforcement over prompt enforcement**: Don't rely on "you must not help unrecognised users" in the persona. Instead, filter the tool manifest *before* it reaches the model. If the model can't see `system.run()`, it can't call it — regardless of what a prompt injection attempts.

---

## 5. Canvas / A2UI: Agent-Driven Interactive UI

Canvas is a separate HTTP+WebSocket server (`src/canvas-host/server.ts`, default port 18793) that renders agent-generated HTML. Key details from source:

- **Live reload via chokidar**: File watcher on the canvas root directory pushes updates to connected browsers automatically
- **A2UI protocol**: Declarative `a2ui-*` HTML attributes create interactive elements without agent-written JavaScript
- **Isolated iframes**: Each widget renders in a same-origin iframe for CSS/JS isolation
- **Multi-platform**: macOS (native WebKit), iOS (SwiftUI), Android (WebView), Web (browser tab)

```html
<button a2ui-action="complete" a2ui-param-id="123">Mark Complete</button>
```
User click → Canvas server → tool call to agent → agent updates HTML → server pushes to browser.

**How JARV1S compares:** `ui.py` widgets push structured JSON; React renders. More type-safe but each widget type needs a React component.

### What to adopt

- **Widget lifecycle protocol**: Add `widget.update(id, data)` and `widget.remove(id)` tools. Currently widgets are fire-and-forget. This enables persistent dashboards the agent can maintain across turns.
- **Declarative action callbacks**: Use structured widget actions so button clicks flow back as direct backend calls. JARV1S now does this through `ui.action` with `{plugin, tool, args}` rather than widget-specific one-offs.
- **Canvas endpoint for room tablets**: A lightweight HTML-serving endpoint for complex visuals (charts, dashboards) — separate from the voice WebSocket. Aligns with the "distributed presence" vision where a room tablet renders visuals while the speaker handles voice.

---

## 6. Heartbeat: Configuration-Driven Proactive Agent

OpenClaw's heartbeat is a periodic poll (~30 min) that sends the agent a system message with the HEARTBEAT.md checklist. The agent either takes action or responds `HEARTBEAT_OK` (silently dropped).

**Key design decisions:**
- The checklist is a user-editable markdown file, not hardcoded
- Active hours restrict when heartbeats fire (e.g., 8am-10pm)
- `HEARTBEAT_OK` response is **silently consumed** — the user only sees output when something needs attention
- Each heartbeat can target a specific delivery channel (Slack, WhatsApp, etc.)
- Token budget is kept tight: recommended <200 words for HEARTBEAT.md

**How JARV1S compares:** JARV1S has a "System Pulse" concept and the `AutomationService` with watchers, but the proactive check-in loop is infrastructure-driven (watchers poll external data), not agent-driven (the agent decides what to check).

### What to adopt

- **Agent-driven heartbeat alongside infrastructure watchers**: JARV1S's watcher model is good for known data sources (calendar, gmail). But some "check-ins" are better as agent reasoning — "review my upcoming week", "summarise what happened today", "check if any tasks are overdue". Add a `heartbeat` event source on the event bus that fires at configurable intervals, with a user-editable `~/.jarvis/HEARTBEAT.md` checklist.
- **Silent completion pattern**: When the heartbeat produces no actionable output, the result should complete silently — no "all clear" notification, no history pollution. JARV1S now has `TriggerInstance.status="completed"` for generic work completion and `status="delivered"` for user-facing output; heartbeat work should use that split rather than forcing voice delivery.
- **Active hours**: Gate heartbeats to user-configured active hours. Don't burn API tokens checking at 3am. JARV1S's scheduler could enforce this at the event bus level.
- **Heartbeat cost tracking**: OpenClaw tracks token usage per heartbeat run. Essential for budgeting proactive features — heartbeats that cost $2/day need visibility.

---

## 7. Skills: Progressive Disclosure, Not Prompt Stuffing

OpenClaw evolved through three skill injection modes as the skill count grew:

| Mode | What's in System Prompt | Token Cost (70 skills) | When Model Needs Full Docs |
|---|---|---|---|
| `full` | Complete descriptions | 1,100-3,400 tokens | Never (already loaded) |
| `compact` | One-line descriptions | ~200 tokens | Reads SKILL.md on demand |
| `lazy` | Names only + "use `read` to learn more" | ~50 tokens | Reads SKILL.md on demand |

The `lazy` mode achieved **46.9% token reduction** (inspired by Cursor's lazy MCP tool loading).

**How JARV1S compares:** The Semantic Tool Router already does dynamic selection — only relevant plugin packs appear in the prompt. But every selected plugin injects its full docstring.

### What to adopt

- **Two-tier tool descriptions**: For plugins that the ToolRouter activates (cosine similarity hit), inject the full docstring. For plugins that are *available but not activated* this turn, inject only name + one-line description with a note that the agent can request full docs. This gives the model awareness of the full toolkit without the token cost.
- **Token budget for tool manifest**: Set a hard cap (e.g., 4K tokens) for the tool manifest section. If activated plugins exceed this, demote the lowest-similarity ones to summary mode. The ToolRouter already returns similarity scores — use them for prioritisation.
- **Skill files for complex workflows**: For multi-step procedures (e.g., "deploy to production", "set up a new integration"), create skill markdown files that the agent reads on demand rather than injecting into every turn. JARV1S's `protocols` feature partially covers this, but skills are lighter weight — a markdown file vs. a MongoDB-stored protocol with step tracking.

---

## 8. Multi-Agent Session Communication

OpenClaw's session tools provide clean inter-agent primitives:

| Tool | Purpose |
|---|---|
| `sessions_list` | Discover active sessions with metadata |
| `sessions_send` | Message another session (with optional reply loop, up to 5 turns) |
| `sessions_history` | Read another session's transcript |
| `sessions_spawn` | Create a new isolated session |

**Visibility scoping**: `self`, `tree` (current + children), `agent` (all sessions), `all` (cross-agent).

**How JARV1S compares:** `jarvis.agents.dispatch()` with `mode="code"` (subprocess) and `mode="jarvis"` (in-process). Fire-and-forget with completion notification. No session-to-session messaging or transcript reading.

### What to adopt

- **Agent-to-agent messaging (SendMessage equivalent)**: When JARV1S dispatches a background agent, the primary agent should be able to send follow-up instructions *without* spawning a new process. The `resume()` function in `sdk.py` already supports this for subprocess agents — expose it as a first-class tool and extend it to in-process agents.
- **Session history access**: Let the primary agent read the transcript of a completed background agent. Currently, only the final result string is returned. Expose `jarvis.agents.history(task_id)` that returns the full conversation log from the background task. This enables the primary agent to understand *how* the background agent reached its conclusion.
- **Visibility scoping**: When JARV1S gains multi-room or multi-user support, agents in one room shouldn't see transcripts from another room's private conversation. Add scope metadata to background tasks.

---

## 9. Cron Architecture: Execution Modes and Delivery Routing

OpenClaw's cron system is more nuanced than a simple scheduler:

**Execution modes:**
- **Main session**: Enqueues into the existing conversational context
- **Isolated**: Runs in a clean session (no history bleed)
- **Current session**: Runs in the session where the cron was created
- **Custom session**: Named persistent session for recurring tasks

**Delivery routing**: Results can be sent to WhatsApp, Slack, Telegram — not just the last active channel.

**Stagger and dedup**: Deterministic auto-stagger prevents all crons from firing simultaneously. Per-job dedup prevents double-execution on restart.

**How JARV1S compares:** `TriggerScheduler` polls MongoDB for due `trigger_instances` and publishes `TRIGGER_DUE`. `AutomationService` handles ECA rules with poll/push paths and also materializes trigger instances. Execution mode is represented in `TriggerAction` / delivery policy, but multi-session routing is still basic.

### What to adopt

- **Isolated execution mode for scheduled tasks**: When a scheduled alert fires a complex action (e.g., "morning briefing"), run it in an isolated context so it doesn't pollute the user's active conversation history. Create a temporary session, execute, deliver the result, then discard the session.
- **Delivery target on automations**: Let automation rules specify where their output goes. A stock price trigger should go to Slack. A calendar reminder should be voice. JARV1S intentionally keeps `DeliveryPlan` minimal today (`voice` only); channel names, target resolution, fallback policy, and provider metadata should move into a future delivery bridge instead of being hard-coded in the trigger model.
- **Deterministic stagger**: If multiple automations fire at the same minute, stagger execution by a few seconds to avoid LLM API rate limits and prevent the user from being overwhelmed with simultaneous voice alerts.

---

## 10. Deployment Flexibility and State Portability

OpenClaw supports local dev, production macOS (menu bar app), Linux VPS (SSH tunnel / Tailscale), and container (Fly.io) — all with the same architecture. Gateway always binds to `127.0.0.1` by default; remote access is explicitly opt-in.

### What to adopt

- **Persistent state separation**: OpenClaw stores all state under `~/.openclaw/` with clear subdirectories (sessions, credentials, memory, cron). JARV1S uses MongoDB plus host-owned credential and preference files. For portable deployment (Raspberry Pi, remote VPS), consider a SQLite fallback for session/memory and filesystem for config.
- **Secure remote access pattern**: When adding remote channels, document the security model: token auth for non-loopback, device pairing for trusted clients. SSH tunnel as the recommended default.

---

## Summary: Top 5 Changes by Impact

1. **Hybrid memory search (BM25 + vector) with temporal decay and pre-compaction ingest** — Fixes exact-match recall ("what was the error code?"), weights recent memories higher (30-day half-life), and preserves details that compaction would destroy. Biggest impact on long-running memory quality.
2. **Composable prompt with PromptMode + user-editable files** — Minimal mode for subagents saves tokens. User-editable `~/.jarvis/` files (PROFILE.md, INSTRUCTIONS.md) enable personalisation without code changes. Silent reply token (`NO_REPLY`) prevents empty responses from polluting history.
3. **Session-scoped trust with identity linking** — Session keys encode trust level. Cross-channel identity resolution means the same person on WhatsApp and Telegram shares a session. Foundation for safe multi-room and guest interaction.
4. **Widget lifecycle protocol with action callbacks** — Transforms widgets from fire-and-forget to persistent interactive dashboards. Closes the UI→Agent feedback loop.
5. **Agent-driven heartbeat with HEARTBEAT.md** — Complements infrastructure watchers with agent reasoning for proactive check-ins. Clean "silent when nothing to report" pattern via `HEARTBEAT_OK` sentinel.

---

## Comparison: OpenClaw vs Claude Code Lessons

| Area | Claude Code Lesson | OpenClaw Lesson | Combined Insight for JARV1S |
|---|---|---|---|
| **Prompt structure** | 6-layer assembly with cache boundary | Sectioned assembly with PromptMode gating + NO_REPLY | User-editable files + PromptMode for subagents + silent reply token |
| **Context management** | Multi-strategy compaction (4 approaches) | Hybrid search (0.7v/0.3t) + temporal decay + pre-compaction ingest | Summarise before dropping + index before compacting + hybrid recall with temporal weighting |
| **Tool system** | 3-layer filtering + lazy loading | Progressive skill disclosure (full/compact/lazy) | Two-tier descriptions + token budget cap |
| **Multi-agent** | Coordinator with structured notifications | Session tools with visibility scoping + identity linking | Structured completions + inter-agent messaging + cross-channel identity resolution |
| **Security** | OS-level sandbox + permission modes | Session key encodes trust + 4-level DM scoping | Trust encoded in session, tools filtered by trust level, identity links for cross-platform users |
| **Proactive** | Not applicable (CLI tool) | Heartbeat + cron with execution modes | Agent-driven heartbeat + isolated scheduled execution |
| **UI** | Terminal (Ink/React) | Canvas/A2UI (declarative HTML actions) | Widget lifecycle protocol with action callbacks |
