# Lessons from Claude Code's Architecture for JARV1S

**Date:** 2026-04-02
**Source:** Claude Code open-source (v2.1.x), claw-code-parity port, public analysis, official docs.

This document distils the most important architectural patterns from Claude Code that are directly applicable to JARV1S. Organised by impact, not by source structure.

---

## 1. The Agentic Loop: "Less Scaffolding, More Model"

Claude Code's core philosophy is radical simplicity in the loop itself:

```
while tool_call:
    prepare messages (compress if too long)
    call API with streaming
    collect response + tool requests
    handle errors silently if possible
    execute requested tools
    check budgets (money, tokens, turns)
    if tool results → send back, continue
    if no tools → exit
```

**What JARV1S does differently:** JARV1S uses a character-level state machine to detect code blocks mid-stream, breaks the stream on `\`\`\``, executes in-process, then resumes. This is the CodeAct paradigm — the model writes Python rather than emitting JSON `tool_use` blocks.

**Key insight:** Both approaches are valid agentic loops. Claude Code's JSON tool_use is provider-native (tighter integration with Anthropic's API), while CodeAct gives JARV1S model-agnostic flexibility and composable multi-step code. The critical shared principle is: **the loop should be trivially simple**. All complexity lives in the tools, the prompt, and the context — not in the orchestration scaffolding.

### What to adopt

- **Max output recovery**: Claude Code retries up to 3 times when the model's output is truncated (`MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3`). JARV1S has no equivalent — if the LLM stream yields zero chunks, we just warn. Add retry logic for empty/truncated responses with exponential backoff.
- **Budget guards in the loop**: Claude Code checks money, tokens, and turn count at every iteration. JARV1S checks iteration count (`max_iterations=10`) but has no per-turn cost tracking or token budget enforcement *within* the loop itself. The `fit_to_budget` call happens once before the loop. Consider a mid-loop budget check.

---

## 2. Context Management: Multi-Strategy Compression

This is where Claude Code is significantly more sophisticated than JARV1S.

### Claude Code's approach (4 strategies)

| Strategy | Trigger | Effect |
|---|---|---|
| **Auto-Compact** | Context hits ~95% of window | Summarise conversation, reload CLAUDE.md, condense 100K+ tokens to 5-10K |
| **Reactive Compact** | Prompt too long for API call | Dynamic compression on the spot |
| **History Snip** | Targeted | Surgically remove specific segments |
| **Max Output Recovery** | Output truncated | Retry with adjusted parameters |

### JARV1S's approach (1 strategy)

`fit_to_budget()` runs once per turn: oversized tool results are replaced with previews, then oldest messages are dropped until the budget fits.

### What to adopt

- **Summarisation-based compaction**: Instead of just dropping oldest messages, summarise them first. When history exceeds 70% of budget, use a fast model call to compress the oldest N messages into a 200-token summary. This preserves conversational continuity that raw truncation destroys. Critical for voice sessions that span hours.
- **Tool result previews survive compaction, full results don't**: Claude Code's insight is that CLAUDE.md-style persistent context survives compaction while tool outputs don't. JARV1S already stores full tool output in MongoDB — the LLM context should only ever see previews. Ensure `cap_tool_result` is aggressive (current implementation is good, but consider reducing the cap for historical messages vs. the current turn's results).
- **Reload dynamic context after compaction**: After a compaction event, re-inject the user profile, active automations summary, and runtime context fresh. Don't let compaction stale the agent's awareness of the user's environment.

---

## 3. Prompt Architecture: The Six-Layer Assembly

Claude Code doesn't use a single system prompt. It dynamically assembles from six layers:

1. **Default instructions** — identity, behavior rules, output format
2. **Memory mechanics** — CLAUDE.md files (persistent user/project context)
3. **Append fragments** — mode-specific additions (plan mode, coordinator mode)
4. **User context** — CLAUDE.md content, current date
5. **System context** — git status, environment details
6. **Tool descriptions** — full JSON schemas for available tools

### The `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` pattern

Claude Code explicitly marks the boundary between static and dynamic content in the system prompt:

```
[static persona + instructions]

__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__

[dynamic: environment, tools, project context]
```

Everything above the boundary is identical across turns → maximises provider prefix cache hits. Everything below varies per turn.

**JARV1S already does this** with `SystemPrompt(static, dynamic)`. Good. But there are refinements:

### What to adopt

- **Tool sorting for cache stability**: Claude Code sorts built-in tools alphabetically as a stable prefix, then appends MCP tools after. If an MCP tool is inserted in the middle, all downstream cache keys invalidate. JARV1S's ToolRouter dynamically selects which plugins are active — this means the tool manifest changes every turn, breaking the cache. **Fix**: Sort core plugins alphabetically first (always present), then append dynamic plugins in a stable order. The ToolRouter already returns a list — just sort it deterministically.
- **Instruction files (CLAUDE.md pattern)**: Claude Code walks the directory tree from cwd to root, collecting `CLAUDE.md` files at each level, deduplicating by content hash, and injecting them into the prompt with scope labels. JARV1S has a two-tier memory system but no equivalent to persistent per-session instruction files. Consider a `~/.jarvis/context.md` that users can edit to provide persistent instructions that survive compaction and are reloaded fresh each turn.
- **Deterministic language in prompts**: Claude Code's analysis reveals that prompts using "must/may/must not" with explicit if-then logic outperform those using ambiguous words like "should" or "try to". Audit JARV1S persona YAML files for ambiguous language.

---

## 4. Tool System: Three-Layer Filtering and Lazy Loading

### Claude Code's three layers

| Layer | When | What |
|---|---|---|
| **Feature flags** | Compile time | Dead code elimination — tools behind feature flags don't exist in the binary |
| **Permission rules** | Runtime | Deny rules filter tools out before the model sees them |
| **Tool pool assembly** | Per-turn | Built-in + MCP tools merged, deduped (built-ins win on name conflict) |

### ToolSearchTool (lazy loading)

When Claude Code has too many tools (40+), it exposes only tool *names* in the system prompt. The model must call `ToolSearchTool` to retrieve the full schema before invoking a tool. This dramatically reduces prompt token consumption.

**JARV1S equivalent**: The Semantic Tool Router already solves this differently — embedding-based cosine similarity activates only relevant plugin packs. But the approaches can complement each other.

### What to adopt

- **Tool description token budget**: Count the tokens consumed by the tool manifest. If it exceeds a threshold (e.g., 4K tokens), switch to a summary-only mode where dynamic plugins show name + one-line description, with a `jarvis.tools.search(query)` function available for the model to retrieve full schemas. This becomes critical as the plugin ecosystem grows past 15-20 dynamic plugins.
- **Built-in tools win on name conflicts**: If a Composio MCP bridge and a bespoke plugin both expose `send_email`, the bespoke plugin should always take priority (better docstrings, tighter integration). Add a `uniqBy(name)` dedup step in the tool manifest generator with bespoke-first ordering.
- **Permission-based filtering before manifest generation**: JARV1S has `_disabled` plugins in the registry, but disabled plugins can still appear in router calculations. Filter them out earlier — before embedding computation, not just at manifest time.

---

## 5. Multi-Agent Orchestration: Coordinator Pattern

Claude Code's multi-agent system has three tiers:

### Tier 1: SubAgents (what JARV1S has now)
Simple fire-and-forget dispatch. Parent spawns a child agent, child runs to completion, result comes back.

### Tier 2: Coordinator Mode (what JARV1S should move toward)
A central coordinator manages multiple workers with:
- **Structured notifications**: Workers report back via XML:
  ```xml
  <task-notification>
    <task-id>{agentId}</task-id>
    <status>completed|failed|killed</status>
    <summary>{Human-readable summary}</summary>
    <result>{Full response}</result>
    <usage>
      <total_tokens>N</total_tokens>
      <tool_uses>N</tool_uses>
      <duration_ms>N</duration_ms>
    </usage>
  </task-notification>
  ```
- **Send follow-up messages**: The coordinator can send additional instructions to a running worker via `SendMessageTool`.
- **Shared scratchpad**: A directory where workers persist knowledge that other workers can read.

### Tier 3: Agent Teams (peer-to-peer)
Workers message each other directly. Shared task list with self-coordination. No central bottleneck.

### What to adopt

- **Structured completion triggers**: JARV1S's `_complete_task` creates a system-origin `TriggerInstance` with a text summary. Upgrade the stored payload to include token usage, cost, duration, and a machine-readable result (not just a string). This enables the coordinator to make informed follow-up decisions.
- **Worker follow-up (SendMessage)**: The `resume()` function in `sdk.py` already supports continuing a completed subprocess task. Expose this as a first-class tool so the primary agent (or user) can send follow-up instructions to a running background agent without spawning a new one.
- **Shared scratchpad for multi-agent tasks**: When dispatching multiple agents (e.g., "check my calendar AND review PRs"), give them a shared temp directory. Agent A writes findings; Agent B reads them. Currently each dispatch is fully isolated.
- **Cost tracking per agent**: Claude Code tracks `total_cost_usd` per agent session. JARV1S tracks `cost_usd` in the task document but doesn't aggregate across an interaction. Add session-level cost aggregation for visibility.

---

## 6. Streaming Architecture: Execute Tools Before Stream Completes

Claude Code's `StreamingToolExecutor` starts executing tools **as soon as the model streams out the tool_use block**, without waiting for the complete response.

JARV1S already does this — the code block detection state machine `break`s the stream the moment a complete ` ``` ` block is found and immediately executes. This is architecturally equivalent and arguably more elegant for CodeAct (code blocks are self-delimiting).

### What to adopt

- **Parallel prefetching at startup**: Claude Code runs MDM subprocess, Keychain read, and module imports in parallel at startup. JARV1S's `main.py` initialises services sequentially. Identify independent init tasks (MongoDB connection, embedding model load, MCP server connections, plugin discovery) and run them concurrently with `asyncio.gather`.
- **Deferred context loading**: Claude Code defers `getUserContext()` and `getSystemContext()` to after the first render (using `startDeferredPrefetches`). JARV1S could defer ToolRouter initialisation (embedding computation) to after the first WebSocket connection rather than blocking startup.

---

## 7. Prompt Engineering Patterns (Extracted Best Practices)

These patterns are derived from Claude Code's actual system prompt text and tool descriptions.

### 7.1 Tool descriptions are the primary LLM interface

Claude Code's philosophy: **"Tools as Policy"**. Each tool description includes:
- What the tool does (1-2 sentences)
- When to use it vs. alternatives
- Parameter semantics and edge cases
- Return value structure
- Behavioral constraints ("never read coordinates aloud")

JARV1S already follows this with "CodeAct-Optimized Docstrings". The refinement is:

**Aim for 3-4 sentences minimum per tool**. Anthropic's own docs state that tool performance improves significantly with detailed descriptions over terse ones. Audit any one-liner tool descriptions.

### 7.2 Give verification criteria

> "The single highest-leverage thing you can do" — Claude Code docs

When JARV1S dispatches a background agent, include verification criteria in the prompt:
- "After sending the Slack message, verify delivery by checking the conversation history"
- "After creating the calendar event, read it back to confirm the details are correct"

### 7.3 Explore first, plan second

Claude Code recommends separating research from implementation. For JARV1S's CodeAct loop, this means the reasoning prompt examples should encourage a pattern of:
1. Read/query first (gather data)
2. Plan (the model's internal reasoning)
3. Act (mutate state)
4. Verify (confirm the action succeeded)

The current `reasoning.yaml` examples partially follow this but some jump straight to action.

### 7.4 The chaining rule

Claude Code's prompt explicitly states: after receiving a tool result, emit the next tool call immediately — no spoken text between tool calls. Only speak to the user after ALL tools have completed.

JARV1S implements this as "speech-gated intermediate text" in the TTS pipeline. The lesson is: **make this an explicit prompt rule, not just an infrastructure behavior**. The model should know it shouldn't generate conversational text between code blocks.

### 7.5 System prompt anti-patterns to avoid

From Claude Code's actual prompt:
- "Do not add speculative abstractions, compatibility shims, or unrelated cleanup"
- "If an approach fails, diagnose the failure before switching tactics"
- "Report outcomes faithfully: if verification fails or was not run, say so explicitly"

These are good rules to add to JARV1S's reasoning prompt — they prevent the model from hallucinating success or silently changing approach without explanation.

---

## 8. Security Patterns (Applicable Subset)

Most of Claude Code's security architecture (sandbox, OS-level isolation, TOCTOU defense) is designed for an untrusted coding environment. JARV1S runs in a trusted home environment, but several patterns still apply:

### What to adopt

- **Forbidden module list hardening**: JARV1S's `FORBIDDEN_MODULES` list blocks `os`, `subprocess`, etc. Claude Code also blocks `importlib`, `pty`, `commands`. Add these to JARV1S's list. Also consider blocking `ctypes` and `multiprocessing`.
- **Denial tracking**: Claude Code tracks consecutive permission denials and falls back to prompting after 3. JARV1S's consent system (`require_consent`) has no equivalent — if the user denies a destructive operation, the model may retry indefinitely. Add a denial counter that instructs the model to stop after 2 consecutive denials.
- **Environment variable scrubbing for subagents**: When JARV1S dispatches Claude Code / Codex as subprocesses, sensitive env vars (API keys, OAuth tokens) should be scrubbed from the child environment unless explicitly needed. Currently `sdk.py` inherits the full parent environment.

---

## 9. Performance Optimisation Patterns

| Pattern | Claude Code | JARV1S Status | Priority |
|---|---|---|---|
| Parallel prefetching at startup | MDM, Keychain, Git all parallel | Sequential init | **High** |
| Lazy loading of heavy modules | OpenTelemetry (~1.1MB) deferred | Not applicable (Python) | Low |
| Token cache for prompt rendering | LRU cache, plain text fast path | No caching | Medium |
| Cost tracking per model | Full breakdown by model | Per-task only | Medium |
| Startup profiling | Sample 0.5% of sessions | Turn summaries in MongoDB | Low |
| Auto-compact at 95% context | Multi-strategy | Single `fit_to_budget` | **High** |
| Tool manifest caching | Stable sort for cache prefix | Regenerated per turn | **High** |

---

## 10. Summary: Top 5 Changes by Impact

1. **Multi-strategy context compression** — Add summarisation-based compaction alongside the existing truncation. Biggest impact on long voice sessions.
2. **Stable tool manifest ordering** — Sort core plugins alphabetically as a fixed prefix. Directly reduces LLM API cost through better cache hit rates.
3. **Parallel startup** — `asyncio.gather` for independent init tasks. Reduces cold start latency.
4. **Structured agent completion payloads** — Machine-readable results from background agents enable smarter coordinator behavior.
5. **Prompt hardening** — Add explicit chaining rules, verification criteria expectations, and failure diagnosis instructions to `reasoning.yaml`.
