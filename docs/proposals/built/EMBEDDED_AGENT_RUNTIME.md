# Phase 7.5b: In-Process Agent Runtime

**Status:** Built  
**Superseded by:** [`docs/BACKGROUND_AGENTS.md`](../../BACKGROUND_AGENTS.md) — the current reference for execution paths, `dispatch()` mode selection, context injection, and routed-tools handling for background agents.
**Also superseded:** Implicit pre-turn model routing (`TurnRouter` / `_COMPLEX_EXEMPLARS`) was removed. Direct turns always use the configured assistant model; depth uses explicit `dispatch(mode="jarvis"|"code")` on the shared `BACKGROUND_AGENT_*` / `ANTHROPIC_API_KEY` contract.  
**Current deviations:** `reason()` mid-turn tool removed. Provider reasoning uses per-turn `resolve_reasoning_effort()` + LiteLLM adapter capability detection (`core/llm/adapters/litellm.py`), not model-name equality to a reasoning-tier setting.  
**Date:** 2026-03-29

Historical design for Phase 7.5b. Unbuilt follow-ups: [ROADMAP.md](../../ROADMAP.md) Phase 7.  
**Depends on:** [SUBAGENT_ARCHITECTURE.md](./SUBAGENT_ARCHITECTURE.md) (Phase 7.0 + 7.1), [AGENT_RUNTIME_HARDENING.md](./cleanup/AGENT_RUNTIME_HARDENING.md) (Phase 7.5a — specifically `AGENT_MAX_DEPTH` must be in place before `mode="jarvis"` ships, as in-process agents have full `jarvis.*` access and can call `dispatch()`)

---

## Problem

JARV1S has two ways for a powerful model to act: `reason()` (text-in, text-out, no tools) and `dispatch()` (full autonomy, subprocess, no access to JARV1S). Neither is adequate.

**`reason()` is a blind oracle.** It sends a question to Claude and gets a text answer. The model cannot read files, check the calendar, search the web, query email, or verify anything it claims. When the voice model asks "what are the issues in PR #47?", the reasoning model can only guess from whatever context the voice model included in the question. It cannot look at the PR.

**`dispatch()` is an isolated stranger.** It spawns a subprocess that runs autonomously with Read/Write/Edit/Bash. The subprocess cannot access JARV1S's plugin system — no calendar, no email, no automations, no memory, no widget pushing. It communicates results back through stdout parsing and MongoDB. It takes minutes and returns asynchronously. For a 30-second investigation that needs 3 tool calls, this is architecturally wrong.

**`reason()` also taxes the fast model's metacognition.** The voice model must recognize it can't handle a request, formulate a compressed question for Claude, and hope it included enough context. Research (arXiv 2603.03111) shows models are poor judges of their own capability boundaries. Context is lost in the compression. If the user says "check if my open PRs conflict with my meeting schedule," the fast model has to package both the PR context and the calendar context into a question string — if it forgets one, Claude can't recover.

OpenClaw doesn't have this problem. Its agent runtime runs in-process, the agent can call any Gateway function, and model selection is per-session, not per-tool-call. The agent is a first-class participant in the system it serves. In JARV1S, the powerful model is either blind (`reason()`) or exiled (`dispatch()`).

---

## Design

### Principle: One Interface, Model Selection

The key insight: **Claude can write Python just as well as Groq can.** CodeAct doesn't require a specific model — any model that can write Python in markdown code blocks works against the `jarvis.*` namespace. The powerful model doesn't need a separate tool interface (structured tools, bridge, Tool Runner). It needs the same CodeAct interface with the same plugins, run on a better model.

This eliminates the need for a plugin-to-tool bridge, built-in agent tools, or a separate agent runner. The existing `JarvisAgent` and `CodeExecutor` work unchanged — only the model backing the `LLMService` changes.

### Pre-Turn Routing

Instead of the fast model calling `reason()` mid-turn (metacognition tax, context compression), a lightweight classifier decides which model to use **before any generation happens.** After STT, before the CodeAct loop starts:

```python
# In orchestrator._process_turn(), before agent.process_stream():

routed_agent = await self._route_model(transcript, source=source, session_id=session_id)
async for event in routed_agent.process_stream(user_content, history, session_id, context=context):
    ...
```

`_route_model()` is a method on `AssistantOrchestrator`. It resolves the powerful agent from the integration manager at call-time and applies an embedding classifier (Phase 8.5 replaced the original regex — `_COMPLEX_PATTERNS` was drift-prone on voice transcripts):

```python
_COMPLEX_EXEMPLARS = (
    "analyze this code and identify performance issues",
    "compare these three approaches and recommend one with tradeoffs",
    "review this pull request for bugs and suggest improvements",
    "refactor this function to be async and handle errors",
    "research and summarize the latest approaches to X",
    "plan a multi-step migration from system A to system B",
    "debug why this test is failing across environments",
)
_COMPLEX_THRESHOLD = 0.72  # max-pool cosine; same calibration as ToolRouter

async def _route_model(self, transcript: str, source: str = "user", session_id: str = "") -> JarvisAgent:
    powerful = await self._get_powerful_agent()  # integrations.get("powerful_agent")
    if source == "system" or powerful is None:
        return self.agent  # fast agent for pre-scripted system turns
    vectors = self._ensure_complex_vectors()  # lazy embed_one of _COMPLEX_EXEMPLARS
    query_vec = await asyncio.to_thread(embedding_service.embed_one, transcript)
    score = max(embedding_service.cosine_similarity(query_vec, v) for v in vectors)
    return powerful if score >= self._COMPLEX_THRESHOLD else self.agent
```

System-source turns (alerts, protocols) always use the fast agent — they run pre-scripted flows that don't benefit from the reasoning tier.

The classifier adds ~25-50ms (one fastembed query call, vectors cached). Same `embedding_service` and threshold calibration as `ToolRouter` — no new dependency.

When the powerful model is selected, the user's original input goes directly to Claude. Full context, no compression, no metacognition. Claude writes Python against `jarvis.*` and calls whatever plugins it needs — calendar, email, memory, web search, files — the same way Groq does.

### reason() Stays as a Mid-Turn Escape Hatch

`reason()` is not removed — it's demoted. The primary escalation path is pre-routing. But sometimes the fast model is mid-turn, partway through a multi-step task, and discovers it's out of its depth. `reason()` handles this rare case.

The implementation is unchanged — `@tool(inject=["reasoning_llm"])` resolves the `LLMService` from the integration manager. Only the docstring changes to reflect that pre-routing is the primary path:

```python
@tool(inject=["reasoning_llm"])
async def reason(self, question: str, reasoning_llm=None) -> str:
    """Escalate a specific sub-question to a stronger model.
    Use only when you're mid-task and discover something beyond your capability.
    For complex requests, the system pre-routes to the powerful model automatically.
    Requires LLM_REASONING_API_KEY to be configured."""
    if reasoning_llm is None:
        return "Reasoning LLM not configured. Set LLM_REASONING_API_KEY to enable reason()."
    try:
        return await asyncio.wait_for(
            reasoning_llm.chat(
                user_message=question,
                system_prompt="Think step by step. Be concise and precise.",
                temperature=0.3,
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return "Reasoning timed out. Proceeding with available information."
```

### Two JarvisAgent Instances

`JarvisAgent` takes a `LLMService` in its constructor and creates its own `CodeExecutor` and `PromptBuilder`. Create two instances backed by different models — the `powerful_llm` is the **same instance** registered as `reasoning_llm` in the integration manager (one Claude client, not two):

```python
# In handlers.py:

fast_llm = LLMService(
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,        # Groq
    model=settings.LLM_MODEL,              # gpt-oss-120b
)

# In main.py lifespan (already exists for reason()):
powerful_llm = LLMService(
    api_key=settings.LLM_REASONING_API_KEY,
    base_url=settings.LLM_REASONING_BASE_URL,
    model=settings.LLM_REASONING_MODEL,
)
integrations.register("reasoning_llm", lambda config: powerful_llm, config_keys=[])

# Second agent reuses the same LLMService instance:
default_agent = JarvisAgent(llm_service=fast_llm)
powerful_agent = JarvisAgent(llm_service=powerful_llm)
```

Each agent creates its own `CodeExecutor` instance, but both executors mount plugins to the same global `jarvis` module via `registry.register_tools()`. The plugin namespace is shared — the tool set is identical regardless of which agent runs the turn.

### dispatch() Dual-Mode

Background tasks get the same two-path design:

- **`mode="code"`** → Subprocess via `claude-agent-sdk`. Claude Code with battle-tested Edit, diff handling, Grep/Glob, session resume. Best coding agent available. The right tool for PR reviews, implementations, refactoring. These tasks operate on a git repo and don't need JARV1S plugins.

- **`mode="jarvis"`** → In-process via `asyncio.create_task`. The powerful `JarvisAgent` runs the CodeAct loop with full plugin access. Same MongoDB event streaming and trigger-instance completion delivery as the subprocess path. The right tool for integrated tasks: "check my calendar and email then write a summary," "research this topic and save to memory."

**Historical note:** this proposal originally described automatic mode selection. The current implementation exposes `mode` on `jarvis.agents.dispatch()` and relies on the tool docstring to steer `mode="jarvis"` for integration work and `mode="code"` for file/git/shell work. If `mode="jarvis"` is requested while the in-process runtime is unavailable, dispatch returns a structured `ok=false` result instead of silently falling back to `mode="code"`.

The internal `_dispatch()` and `_dispatch_inprocess()` remain the two execution paths behind the public tool.

### In-Process Dispatch: _dispatch_inprocess

The `AgentsPlugin` resolves the powerful agent at call-time from the integration manager — no constructor injection required:

```python
async def _get_powerful_agent(self) -> JarvisAgent | None:
    try:
        from core.integrations.manager import integrations
        return await integrations.get("powerful_agent")
    except (KeyError, Exception):
        return None
```

The in-process dispatch translates CodeAct `AgentEvent` types to the existing task event infrastructure:

```python
async def _dispatch_inprocess(self, prompt: str, cwd: str, max_budget_usd: float) -> str:
    task_id = str(uuid4())
    # ... create task document in MongoDB (same as _dispatch) ...

    async def _run_inprocess():
        async with self._semaphore:
            col = await mongodb.get_collection("background_tasks")
            try:
                full_response = ""
                async for event in self._powerful_agent.process_stream(
                    prompt, [], settings.DEFAULT_USER_ID,
                    context={**bg_context, "routed_tools": await tool_router.route(prompt, task_id)},
                ):
                    if event.type == AgentEventType.TEXT:
                        full_response += event.content
                        await col.update_one(
                            {"task_id": task_id},
                            {"$set": {"progress_summary": event.content[:100]}},
                        )
                        await _push_task_event(user_id, task_id, {"event_type": "text", "text": event.content[:500]})

                    elif event.type == AgentEventType.TOOL_CALL:
                        await _push_task_event(user_id, task_id, {"event_type": "tool_start", "tool": "CodeAct"})

                await _complete_task(task_id, user_id, result=full_response, summary=full_response[-300:], ...)
            except asyncio.CancelledError:
                await _fail_task(task_id, user_id, "Task was cancelled.")
                raise
            except Exception as e:
                await _fail_task(task_id, user_id, str(e))

    self._spawn_task(task_id, _run_inprocess)
    return f"Background agent started (in-process). task_id={task_id}"
```

This reuses the existing `_push_task_event`, `_complete_task`, `_fail_task`, and task receipt/detail builders from `client.py`. Ambient progress is shown as a throttled review-rail receipt rebuilt from `background_tasks`; `BackgroundTaskWidget` remains the on-demand detail surface regardless of which runtime produced the task.

### Context Window for Background Tasks

`JarvisAgent.process_stream()` calls `fit_to_budget()` once at the start of the loop. For voice turns (1-10 CodeAct iterations), this is fine. For background tasks doing 30+ iterations, `local_history` grows unchecked as tool results accumulate — the context window can fill.

The subprocess SDK handles this via its own compaction. The in-process path needs an equivalent. Two options:

- **Cap `MAX_TURNS` for `mode="jarvis"`** via `AGENT_INPROCESS_MAX_TURNS` (currently default 30). Most JARV1S-integrated tasks are investigation, not long-running implementation. This is simple, with no code changes to `process_stream()` — passed as the `max_iterations` argument.
- **Add mid-loop trimming** to `process_stream()`. After each CodeAct iteration, re-run `fit_to_budget()` on `local_history`. This is the robust solution but touches the agent core.

Start with capped turns. Add mid-loop trimming if tasks hit the cap and need more iterations.

---

## Two Paths, Not a Migration

The subprocess SDK (`claude-agent-sdk`) and the in-process CodeAct agent are permanent, parallel paths serving different task types:

| | `mode="code"` (subprocess) | `mode="jarvis"` (in-process) |
|---|---|---|
| **Runtime** | Claude Code CLI subprocess | `JarvisAgent` with powerful `LLMService` |
| **Tool interface** | SDK built-in (Read, Write, Edit, Bash, Grep, Glob) | CodeAct against `jarvis.*` — same as voice |
| **JARV1S access** | None — isolated process | Full — calendar, email, memory, automations, widgets |
| **Coding quality** | Best available — battle-tested Edit, diff handling, session resume | CodeAct Python — read/write whole files, basic shell |
| **Use case** | PR reviews, code implementations, refactoring | Integrated tasks needing JARV1S context |
| **Cancellation** | SIGTERM → SIGKILL | `task.cancel()` — instant |
| **Context management** | SDK compaction (battle-tested) | Capped turns (initially), mid-loop trimming (later) |

Neither replaces the other. Claude Code is the best coding agent and has a dedicated team maintaining it. The in-process path exists because Claude Code can't see JARV1S.

**New files: zero.** The change is: a second `JarvisAgent` instance using the existing `powerful_llm`, a model router in the orchestrator, `_dispatch_inprocess` in the agents plugin, and a `mode` parameter on `dispatch()`.

---

## Tradeoff: What You Give Up

The pre-routing + CodeAct approach uses the OpenAI-compatible API for Claude (via `LLMService` → `AsyncOpenAI`). This means:

| Capability | Lost? | Impact |
|---|---|---|
| **Prompt caching** | Yes — OpenAI-compat doesn't support it | ~10x higher cost per reasoning turn. At Sonnet rates: ~$0.30 vs ~$0.03 per turn. |
| **Extended thinking** | Yes — reasoning chain not returned | Model can still reason internally, but you can't inspect or benefit from explicit chain-of-thought tokens. |
| **Structured tool_use** | Yes — using CodeAct instead | Not a loss — CodeAct is more flexible for this use case. |

**The cost question is real but measurable.** Start with OpenAI-compatible. Track your API spend on reasoning turns. If it becomes a problem, add the native Anthropic path for the pre-routed powerful turns specifically — at that point you'll know exactly which plugins need bridging and can write targeted wrappers for the 3-4 that matter. Don't build infrastructure for a cost problem you haven't measured yet.

**Extended thinking is the more nuanced loss.** Claude's extended thinking produces better results on complex reasoning tasks. If pre-routed turns show quality gaps on hard problems, adding a native Anthropic `chat()` path for `reason()` (the mid-turn escape hatch) is a small, targeted change — not a full bridge.

---

## Implementation Plan

### Phase 1: Second Agent + Pre-Router

1. Create `powerful_llm` `LLMService` in `main.py` lifespan — **same instance** registered as `reasoning_llm` in the integration manager (already exists, just also pass to the second agent)
2. Create second `JarvisAgent` instance backed by `powerful_llm`; register as `"powerful_agent"` in the integration manager
3. Add `_route_model()` to `AssistantOrchestrator` (originally regex `_COMPLEX_PATTERNS`, replaced in Phase 8.5 with max-pool embedding over `_COMPLEX_EXEMPLARS`); resolve powerful agent via `integrations.get("powerful_agent")` at call-time (no constructor changes)
4. Route complex requests to `powerful_agent.process_stream()` before CodeAct loop starts
5. Keep routing diagnostics per session via `ToolRouter.get_last_diagnostics()`; do not expose a second active-route state.
6. Update `reason()` docstring to reflect pre-routing is primary (implementation unchanged)

### Phase 2: In-Process Background Dispatch

7. Keep `mode` on the LLM-facing `dispatch()` tool; steer mode choice through the tool docstring (`jarvis` for integration work, `code` for file/git/shell work)
8. `powerful_agent` reference resolved at call-time via `integrations.get("powerful_agent")` in `_get_powerful_agent()` — no `AgentsPlugin` constructor/init changes
9. Implement `_dispatch_inprocess`: `powerful_agent.process_stream()` wrapped in `asyncio.create_task`, translating `AgentEvent` types to existing task event infrastructure (`_push_task_event`, `_complete_task`, `_fail_task`)
10. Cap `MAX_TURNS` for `mode="jarvis"` tasks (currently default 30)
11. Trigger-instance delivery on completion (same path as subprocess)
12. `mode="code"` path: existing subprocess dispatch (unchanged, hardened via Phase 7.5a)

### Phase 3: Refinement

13. Tune pre-router accuracy — upgrade from rule-based to embedding similarity if needed
14. Measure API cost on pre-routed turns — determine if native Anthropic path is needed
15. Tune voice model prompt guidance for `mode="code"` vs `mode="jarvis"` dispatch selection
16. If background tasks hit the turn cap, add mid-loop `fit_to_budget()` to `process_stream()`

### If Costs Become a Problem (Phase 3+)

17. Add `AsyncAnthropic` native client for pre-routed reasoning turns only
18. Write targeted `@beta_tool` wrappers for the 3-4 plugins that the reasoning tier actually uses (not a generic bridge)
19. Switch pre-routed turns from CodeAct to Tool Runner for prompt caching + extended thinking
20. Keep CodeAct as the default; native API as the cost-optimized path

---

## What NOT to Build

| Approach | Why skip |
|---|---|
| **Generic plugin-to-tool bridge** | Claude uses CodeAct. No translation needed. If native API is added later, write 3-4 targeted wrappers, not a generic runtime introspection system. |
| **Mid-turn model switching tool** | Research shows -8 to +13pp quality swings from mid-conversation model switching (arXiv 2603.03111). Pre-routing avoids this — the powerful model starts the turn fresh, no foreign dialogue history. |
| **Full Pi port to Python** | Pi's value is its agent loop + compaction. JARV1S already has a working agent loop (CodeAct). Porting Pi gains nothing. |
| **Separate tool definitions for the powerful model** | One interface (CodeAct) for all models. Don't maintain two tool systems. |
| **Second Anthropic client** | The `powerful_llm` `LLMService` registered as `reasoning_llm` in the integration manager serves both the second `JarvisAgent` and `reason()`. One client, two consumers. |

## What to Build Later

| Capability | When |
|---|---|
| **Native Anthropic API for reasoning turns** | When API cost on pre-routed turns is measured and problematic. Targeted change: native client + 3-4 tool wrappers. |
| **Mid-loop context trimming** | When `mode="jarvis"` background tasks hit the turn cap and need more iterations. Add `fit_to_budget()` call between CodeAct iterations in `process_stream()`. |
| **Embedding-based pre-router** | When rule-based heuristics misroute too often. Drop-in replacement, same interface. |
| **Provider failover** | Phase 8. If Groq is down, route all turns to Claude. If Anthropic is down, fall to OpenRouter. |
| **Multi-channel delivery abstraction** | Phase 8. Agent output formatted per channel (voice, widget, notification). |
| **Lane-based priority scheduling** | Phase 8+. Voice > System > Background when concurrent API consumers exceed implicit priority. |
