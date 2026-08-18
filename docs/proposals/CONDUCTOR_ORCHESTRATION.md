# Conductor Orchestration

**Status:** Proposed  
**Date:** 2026-04-25  
**Priority:** High — this is the missing layer between background agents, automations, voice, and the UI.  
**Depends on:** `docs/BACKGROUND_AGENTS.md`, `docs/proposals/UI_ACTION_BUS.md`, existing `PendingInputWidget`, `BackgroundTaskWidget`, `AutomationService`, `SystemPulse`.

---

## Problem

JARV1S can already dispatch long-running agents and run proactive automations, but the user still has to supervise too much of the work manually.

Today the system has three separate surfaces:

| Surface | What works | Gap |
|---|---|---|
| Voice loop | Fast, conversational, can dispatch tasks | Does not know when a background task needs input or should be interrupted |
| Background tasks | Runs Claude Code / in-process agents, persists compact task state, and shows review-rail progress receipts | Questions are not first-class; `mode="jarvis"` approvals are durable pending inputs, while `mode="code"` SDK approval callbacks remain deferred |
| Automations / SystemPulse | Can fire proactive turns and suppress empty checks | No shared task/status model, no project-management loop, no durable "waiting on user" state |

The conductor goal is not to build another agent framework. The goal is:

> JARV1S should watch long-running work, decide what deserves user attention, collect feedback with the least friction, and keep the user's life/work systems moving.

This needs a small orchestration layer that turns raw agent events into human-meaningful states:

- "Working, no attention needed."
- "Blocked on a question."
- "Needs approval before a risky action."
- "Completed with a result worth speaking."
- "Stale or stuck; follow up."
- "Project status changed; summarize or update the board."

---

## Research Summary

### Claude Agent SDK

The Claude Agent SDK already exposes the primitives JARV1S should lean on rather than rebuild:

- Built-in tools include `Read`, `Write`, `Edit`, `Bash`, `Glob`, `Grep`, `WebSearch`, `WebFetch`, `Monitor`, and `AskUserQuestion`.
- `TodoWrite` tool use events provide structured plans and progress checklists.
- Hooks (`PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`) can audit, block, or transform tool calls.
- `can_use_tool` / permission modes allow runtime approval decisions for sensitive actions.
- Sessions can be resumed with `session_id`; `ClaudeSDKClient` supports interactive `query(...)` and `interrupt()`.
- `setting_sources` loads user/project Claude Code configuration; `Skills` and `CLAUDE.md` are already Claude Code's native workflow configuration surface.

Implication for JARV1S: **do not wrap every Claude Code primitive. Drain its events, classify them, and surface only the events that require user attention.**

References:
- https://platform.claude.com/docs/en/agent-sdk/user-input
- https://platform.claude.com/docs/en/agent-sdk/hooks
- https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts

### Human-in-the-loop agent frameworks

OpenAI Agents, Microsoft Agent Framework, and Airflow HITL all converge on the same pattern:

1. A run emits an interruption / request.
2. The runtime persists enough state to wait.
3. An external human-facing surface resolves the request.
4. The original run resumes from the paused point.

OpenAI's SDK makes approvals part of `RunState`, including sticky "always approve for this run" decisions. Microsoft calls the same idea a `RequestPort`: workflow executors emit request events, external systems respond, and the workflow routes responses back to the waiting executor.

Implication for JARV1S: **the missing primitive is a durable pending-input request on `background_tasks`, not a new chat mode.**

References:
- https://openai.github.io/openai-agents-python/human_in_the_loop/
- https://learn.microsoft.com/en-us/agent-framework/workflows/human-in-the-loop

### Agentic project management tools

Linear Agent, Asana AI Teammates, and Zenhub Thor all put the agent inside the existing work system instead of replacing it:

- Linear Agent uses existing issues, projects, comments, docs, and permissions. It can chat, comment in-place, save repeatable skills, and run automations on triage.
- Thor proposes action plans from meetings, asks for approval, then creates/updates issues and follows up on stale work.
- Asana AI Teammates emphasize status reporting, blockers, workflow optimization, and visible decision trails.

Implication for JARV1S: **project management should start as status synthesis and follow-up against existing systems, not as a new PM database.** JARV1S should use Linear/Jira/GitHub/Calendar/Gmail via existing integrations and only keep its own light task state for orchestration.

References:
- https://linear.app/docs/linear-agent
- https://linear.app/changelog/2026-03-24-introducing-linear-agent
- https://asana.com/product/ai/ai-teammates
- https://zenhub.com/thor

---

## Options Considered

### Option A — Build a full JARV1S project-management system

JARV1S owns projects, tasks, statuses, dependencies, check-ins, and reporting.

**Pros**
- Full control over UX and data.
- Works even without Linear/Jira/GitHub.

**Cons**
- Rebuilds existing PM tools.
- High schema/UI burden.
- User now has another source of truth.
- Doesn't solve the immediate "agent needs feedback" problem.

**Decision:** Reject. Too broad.

### Option B — Treat Claude Code / Linear / Jira as the source of truth

JARV1S only dispatches work and relays results. Planning, skills, questions, approvals, and PM status live in the external tools.

**Pros**
- Low implementation cost.
- Uses stronger existing primitives.

**Cons**
- JARV1S remains passive.
- Voice cannot answer agent questions or steer tasks.
- No central attention filter.

**Decision:** Reject. This does not make JARV1S a conductor.

### Option C — Conductor event layer over existing runtimes

JARV1S keeps a light orchestration record for tasks, drains structured events from agent runtimes, pauses only for questions/approvals, and synthesizes project status from existing systems.

**Pros**
- Solves the real user problem.
- Keeps Claude Code / PM tools as sources of truth.
- Fits existing `background_tasks`, widgets, event bus, and voice loop.
- Can ship in small phases.

**Cons**
- Requires careful event classification.
- Needs durable pending-input handling.
- Requires better UI affordances in `BackgroundTaskWidget`.

**Decision:** Adopt.

---

## Proposal

Add the smallest conductor capability inside the existing background agent system. The prototype is not a reusable orchestration framework; it is one proven loop:

> Dispatch a Claude Code task → show plan → pause on one question or approval → answer via UI/voice → resume → complete → show concise summary.

The prototype has three responsibilities:

1. Normalize the few background agent events that matter to the user.
2. Persist pending user input requests on the task doc.
3. Filter noisy status into UI-only updates and voice-worthy interruptions.

It does **not** own agent execution, workflow skills, project boards, code editing, or project-management automation. Those come after the loop above works reliably.

---

## Data Model

Extend `background_tasks` rather than creating new collections.

```python
{
    "task_id": str,
    "status": "running" | "completed" | "failed" | "cancelled",
    "attention": "none" | "question" | "approval",
    "cwd": str | None,
    "plan": list[dict],              # latest TodoWrite todos, optional
    "pending_input": dict | None,    # question/approval waiting for user
    "events": list[dict],            # last N conductor events
    "progress_summary": str,
    "created_at": int,
    "completed_at": int | None,
}
```

`status` remains the task lifecycle. `attention` is the user-attention state. A task can remain `status="running"` while it is waiting on a question or approval; `pending_input` explains what is blocking progress.

### `pending_input`

```python
{
    "input_id": str,
    "kind": "question" | "approval",
    "prompt": str,
    "options": list[dict] | None,
    "tool": str | None,
    "risk": "low" | "medium" | "high" | None,
    "evidence": dict | None,          # action, target, expected effect, reason
    "created_at": int,
    "expires_at": int | None,
}
```

This is the durable record. The in-memory worker can wait on a Future, but MongoDB is the source of truth for the UI and for recovery after reconnect.

---

## Conductor Events

Normalize task activity into a small event set. Start with the prototype events and expand only when the UI or voice layer needs a new state.

| Event | Source | User-facing behavior |
|---|---|---|
| `status` | Assistant text / tool summaries | Update timeline, silent by default |
| `plan_update` | Claude `TodoWrite` | Update task checklist, silent by default |
| `question` | `AskUserQuestion` or fallback prompt convention | Set `attention="question"` and `pending_input`, speak if user is present |
| `approval_request` | permission callback / hook | Set `attention="approval"` and `pending_input`, show approval UI, speak if meaningful |
| `completed` | SDK `ResultMessage` / final text | Speak concise summary for user-initiated tasks |

Deferred events:

| Event | Defer until |
|---|---|
| `tool_start` / `tool_result` | The timeline needs richer debugging than `status` provides |
| `blocked` | There is a real stale/loop detector |
| `failed` | Failure handling diverges from existing task status |

The current task event stream already supports `text` and `tool_start`; the prototype should mainly add first-class `plan_update`, `question`, and `approval_request`.

---

## Status Filtering Rules

The conductor should be quiet by default.

### Speak immediately

- A task is waiting on a user question.
- A high-risk approval is needed.
- A user-initiated task completed or failed.
- After Phase 5, a project-status automation found a blocker that affects the user today.

### Show in UI, do not speak

- Todo/checklist changes.
- Routine tool calls.
- Low-risk approvals already auto-decided.
- Long-running task heartbeat ("still working").

### Suppress entirely

- Repeated status with no meaningful delta.
- Tool calls that are only internal search/read steps.
- Completion of offer automations where `action.decision="offer"` returned `NO_REPLY`.

### Batch

If multiple background tasks complete or request input while the user is away, JARV1S should summarize:

> "Three things need your attention: the OpenClaw PR has one question, the Gmail digest is waiting on approval, and the calendar cleanup finished."

This can start as a simple grouped notification from active `background_tasks` where `attention != "none"`.

---

## User Feedback Flow

### Question

1. Worker emits `question`.
2. Task keeps its lifecycle `status` and sets `attention="question"` plus `pending_input`.
3. `BackgroundTaskWidget` renders the question and options.
4. Voice layer, if idle and user present, speaks the question.
5. User answers by voice or UI action.
6. `ui.action` / voice tool resolves `pending_input`.
7. Worker resumes with the answer.

### Approval

1. Worker attempts a sensitive action.
2. Permission callback emits `approval_request`.
3. JARV1S classifies the risk and presents plain English:
   - "Commit these changes?"
   - "Send this email?"
   - "Post this update to Linear?"
4. User can choose:
   - Allow once.
   - Deny once.
   - Allow similar actions for this task.
5. Decision is logged as an event.
6. The side effect runs only after an allow decision.
7. Worker resumes.

V1 should avoid global "always allow" rules. Task-scoped decisions are enough and safer.

### Mid-task steering

The prototype should not implement arbitrary running-task steering. First prove explicit `question` and `approval_request` pauses.

After that works, running feedback can follow the same task-scoped route:

- For completed `mode="code"` tasks, keep using `resume(task_id, feedback)`.
- For running `mode="code"` tasks, use `ClaudeSDKClient.interrupt()` then `query(feedback)` on the same session.
- For `mode="jarvis"` tasks, V1 can require cancel + redispatch; interruptible in-process tasks can come later.

This keeps the prototype scoped to the SDK path that already supports session resume, while avoiding a second control flow before pending input is reliable.

---

## Scenario Matrix

The model must handle these scenarios before expanding into broader project management.

| Scenario | Expected behavior | Prototype support |
|---|---|---|
| User dispatches a code task and it finishes cleanly | Show plan/status in widget; speak one completion summary | Yes, Phase 1-2 |
| Agent emits `TodoWrite` repeatedly | Replace latest `plan`; do not append noisy duplicates | Yes, Phase 2 |
| Agent needs a requirement decision | Persist `pending_input(kind="question")`; speak if user present; answer via UI/voice; resume | Yes, Phase 3 |
| Agent wants to send/post/delete/push | Persist `pending_input(kind="approval")`; show evidence pack; allow/deny; resume | Yes, Phase 4 |
| User is away when a task asks | Leave `pending_input` on the task; show attention badge; notify later as a batch | Yes, once pending input exists |
| Two tasks ask at once | Do not guess from voice; show both in UI or ask which task | Yes, simple rule |
| User gives feedback after completion | Existing `resume(task_id, feedback)` | Already exists |
| User gives feedback while task is running | Defer interrupt/steering until question/approval flow is proven | Deferred |
| Worker crashes while waiting | Task still has `pending_input`; user can see it, but live resume may require restarting/resuming the SDK session | Partially supported |
| SDK question primitive is unreliable | Fallback to prompt convention or permission hook that emits a `question` proposal | Planned fallback |
| Project status check finds nothing | Produce no speech (`NO_REPLY` / silent result) | Existing delivery-mode pattern |
| Project status check finds blocker | Push content widget + short voice summary | Phase 5 |
| Approval request times out | Auto-deny/no-op and log event; do not execute action | Phase 4 |
| Agent loops/no progress | Not solved by V1; later add stale detector from repeated statuses / no plan progress | Deferred |

This scenario table is the guardrail against overbuilding. If a proposed primitive does not serve one of these scenarios, leave it out of the prototype.

---

## Project Management Scope

Keep project management tight:

JARV1S should not own a project board. It should manage **attention over existing work systems**. This is deliberately after the conductor prototype, because project checks are a consumer of the same attention model, not part of the foundation.

### Phase 5 capabilities

- Tag background tasks with optional `project`.
- Let `list_tasks(project=...)` and `recall(project=...)` filter by project.
- Add a `project_status` skill/protocol that:
  - reads Linear/Jira/GitHub issues via Composio/MCP,
  - finds stale work, blockers, and due dates,
  - produces a concise status summary,
  - optionally drafts updates but asks before posting.
- Add an automation action:
  - "Every weekday morning, check my active projects and tell me only if something is blocked, stale, or due today."

### Still out of scope

- No JARV1S-native kanban board.
- No full project registry.
- No alias system.
- No automatic issue creation without approval.
- No team-wide PM workflows until personal workflow works well.

The durable state stays in the existing work tools. JARV1S stores only orchestration metadata and learned preferences.

---

## Implementation Plan

### Phase 0 — Spike the SDK behavior

Goal: prove the runtime can produce and resume the events the design depends on.

- Build a local-only spike in `plugins/agents/client.py` or a small script.
- Confirm the pinned SDK exposes `TodoWrite` tool use details.
- Confirm whether `AskUserQuestion` appears as a normal tool call and can be intercepted.
- Confirm permission callbacks/hooks work with current Composio MCP usage.
- Confirm whether switching off `bypassPermissions` breaks existing background tasks.

Acceptance:
- One captured sample event for `TodoWrite`.
- One captured sample event for a question or documented fallback.
- One captured sample approval callback or documented blocker.

If this spike fails, do not implement the broader conductor layer yet. Fall back to plan display + completion summaries only.

### Phase 1 — Event contract and task state

Goal: make background task state reliable and human-readable without changing behavior.

- Add a minimal conductor helper near the current agents client code:
  - `append_event(task_id, event)`,
  - `set_attention(task_id, attention, pending_input=None)`,
  - extract to `core/agents/conductor.py` only after a second producer uses it.
- Extend `background_tasks` docs with `attention`, `plan`, `pending_input`, and capped `events`.
- Normalize current `_push_task_event` calls through the conductor helper.
- Update `GET /tasks/{task_id}` response models to include `attention`, `plan`, and `pending_input`.

Acceptance:
- Existing background tasks still run.
- Widget can display the same events as today from the new event contract.
- Task docs clearly show whether they need attention.

### Phase 2 — Plan/status display

Goal: make the UI show what is happening without narrating noise.

- Detect Claude `TodoWrite` `ToolUseBlock` in `mode="code"` and persist it as `plan`.
- For `mode="jarvis"`, keep status events only unless the in-process agent later gets an explicit planning tool.
- Update `BackgroundTaskWidget`:
  - plan checklist as the main body when present,
  - activity timeline collapsed by default,
  - attention banner when `pending_input` exists,
  - feedback box always available for running tasks.
- Add status filtering in the backend: only `question`, `approval_request`, `completed`, and lifecycle failures become voice candidates.

Acceptance:
- A Claude Code task visibly shows its checklist.
- Routine tool chatter stays in the timeline but does not speak.

### Phase 3 — Pending questions

Goal: let a dispatched agent ask the user and continue.

- Wire Claude `AskUserQuestion` into `question` conductor events.
- Store `pending_input` on task doc.
- Add a `ui.action` handler:
  - `agents.answer_pending_input`
  - args: `task_id`, `input_id`, `answer`.
- Route the next voice reply to the pending input when there is exactly one task with `pending_input`.
- If multiple tasks need input, JARV1S asks which one or pushes a summary widget.

Acceptance:
- A code-mode task can pause, ask a question, receive a UI/voice answer, and continue.

### Phase 4 — Task-scoped approvals

Goal: replace background auto-approval with user-visible control where it matters.

- Change `mode="code"` away from blanket `bypassPermissions` only after Phase 0 proves callbacks/hooks work with Composio MCP.
- Add risk classifier:
  - allow read/search/plan silently,
  - allow file edits inside `cwd` by default,
  - ask for shell mutations, git push, external sends/posts/deletes,
  - always ask for destructive shell patterns.
- Store task-scoped decisions in memory and task events:
  - allow once,
  - deny once,
  - allow similar for this task.
- Add a concise evidence pack to every approval:
  - action,
  - target,
  - why the agent wants it,
  - expected effect,
  - risk label.
- Reuse `PendingInputWidget` / `ui.action` for approval resolution.

Acceptance:
- Routine refactors do not spam prompts.
- Sending/posting/deleting/pushing asks the user.
- Every approval decision is visible in the task timeline.

### Phase 5 — Project-management loop

Goal: automate coordination without creating a new PM tool.

- Add `project` optional param to `agents.dispatch`, `agents.list_tasks`, and `recall`.
- Create a `project_status` skill/protocol:
  - "summarize progress, blockers, stale work, and next actions for project X."
- Add automation examples:
  - weekday morning project check,
  - stale assigned issues,
  - "draft but do not post weekly update."
- Integrate with connected PM systems through existing MCP/Composio paths.

Acceptance:
- JARV1S can answer "what's blocked on OpenClaw?"
- JARV1S can proactively surface "this needs attention today."
- Posting external updates still requires approval.

---

## Scaling Notes

This model should scale if the conductor remains an **attention filter**, not a workflow runtime.

What scales well:

- Capped `events` arrays with the latest meaningful events.
- One `pending_input` per task in V1.
- Task-scoped approval decisions.
- Project status as a protocol/skill using existing PM tools.
- UI timeline for detail, voice only for attention.

What will not scale:

- Speaking every tool event.
- Multiple simultaneous pending inputs per task.
- Global permanent trust rules before observing real usage.
- A custom PM board.
- Trying to make `mode="jarvis"` and `mode="code"` fully symmetric in V1.
- Keeping worker state only in memory while the task doc has `pending_input`.

Future scale upgrades, only when needed:

- Move from one `pending_input` to a `pending_inputs[]` queue.
- Add stale/loop detection based on no plan progress, repeated status text, or elapsed time.
- Add idempotency keys for external side effects.
- Add a weekly review of approved/denied actions to tune risk rules.
- Extract conductor helpers into `core/agents/conductor.py` once more producers use them.

---

## Backend Shape

### New module

`backend/core/agents/conductor.py` once the prototype graduates. During the first slice, keep this as a small helper near `plugins/agents/client.py` to avoid premature module structure.

Responsibilities:

- shape conductor event payloads,
- append capped task events,
- set `attention` and `pending_input`,
- publish WebSocket task events,
- provide a small in-memory registry of pending Futures for live workers.

It should not import SDK types directly. `plugins/agents/client.py` translates SDK messages into conductor events.

### Existing modules touched

- `backend/plugins/agents/client.py`
  - translate Claude SDK messages into conductor events,
  - detect `TodoWrite`, `AskUserQuestion`, approval requests,
  - later use `ClaudeSDKClient` for running-task interrupt/feedback once pending input works.
- `backend/plugins/agents/__init__.py`
  - expose `answer_pending_input`,
  - add optional `project` and `list_tasks(project=...)` in Phase 5.
- `backend/api/routes/tasks.py`
  - include new fields in response models.
- `frontend/src/components/features/widgets/BackgroundTaskWidget.tsx`
  - render plan/checklist, attention banner, question/approval actions.

---

## UI Shape

`BackgroundTaskWidget` becomes the conductor panel.

Default collapsed state:

- task title/source,
- current status,
- attention badge (`working`, `needs question`, `needs approval`, `done`),
- elapsed time.

Expanded state:

1. **Plan** — Todo/checklist, if present.
2. **Needs You** — question or approval, if present.
3. **Activity** — timeline, collapsed noise.
4. **Steer** — follow-up through the main Jarvis turn (`get_result()`, `resume()`, or a new task), not an inline chat panel.

No new global "Project Management" page in V1. Project status should appear as content widgets generated by protocols/automations.

---

## Voice Behavior

Voice should stay sparse:

- On dispatch: "I'll take that and keep you posted if I need you."
- On question: speak the question, then wait for answer.
- On approval: speak only the action and risk, not the full tool input.
- On status request: summarize from `background_tasks`, not from raw events.
- On completion: one concise result.
- On quiet completion from automation: no speech unless the result matters.

Examples:

- "The OpenClaw task has a question: should it update the call site or pin the dependency?"
- "The agent wants to push changes to `main`. Allow once, or deny?"
- "Two tasks finished. The PR review found one failing test; the calendar cleanup completed silently."

---

## Safety Rules

- V1 does not add global permanent trust rules.
- Approvals time out to deny/no-op.
- If the user is absent, tasks wait rather than auto-execute risky actions.
- All decisions are logged on the task.
- External writes (email, Slack, Linear/Jira comments, issue creation, deletes) require approval until a real usage pattern proves otherwise.
- Shell operations outside `cwd` require approval.
- Destructive shell patterns always require approval.

---

## What This Deliberately Does Not Build

- A new PM database.
- A new project board.
- A general workflow engine.
- A replacement for Claude Code skills.
- A full multi-agent framework.
- A global permissions UI.
- Per-project conversation silos.

If a later feature needs one of these, it should be justified by observed usage, not by this proposal.

---

## Open Questions

- How reliable is `AskUserQuestion` in the current pinned Claude Agent SDK version? If unreliable, use a prompt convention plus `can_use_tool`/hooks to surface questions as a fallback.
- Can `can_use_tool` run without regressing current Composio MCP behavior? Test with Gmail/GitHub/Linear before changing the default from `bypassPermissions`.
- Should `mode="jarvis"` get its own `ask_user` tool for parity, or should V1 focus on `mode="code"` only?
- How long should pending questions wait before becoming "stale"?
- Should project-management automations use a fixed protocol first, or should they be user-authored skills?

---

## Success Criteria

The prototype is successful when:

- A dispatched Claude Code task can show its plan in the UI.
- JARV1S can tell the user only when the task needs attention.
- The user can answer a task question by voice or widget.
- Sensitive external actions are approval-gated without spamming the user.
- The implementation reuses current task docs, widgets, event bus, and SDK sessions rather than introducing a separate orchestration platform.

Phase 5 is successful when JARV1S can run a morning project check and only interrupt for blockers, stale work, or due-today items.
