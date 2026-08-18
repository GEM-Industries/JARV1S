# Tool Execution And Background Work Trust Hardening

> **Partial** — Shipped slices: see [BACKGROUND_AGENTS.md](../../BACKGROUND_AGENTS.md). Current UI uses review-rail progress receipts plus on-demand `BackgroundTaskWidget` detail. Open items below may still describe older proposed card shapes.

**Status:** Partially implemented — dispatch/result replay trust slices landed 2026-05-20  
**Date:** 2026-05-20  
**Priority:** High — JARV1S will not become a trusted daily assistant until foreground tools and background work both have truthful state, clear approval, and reviewable outcomes.

---

## Why This Reframes The Earlier Doc

The first draft over-focused on background task reviewability. The stress tests showed something broader:

- Some failures happened in background work: dispatch missing `cwd`, partial batch dispatch, weak task cards, no diffs, no service-step evidence.
- Many failures happened before background work was involved: Gmail draft creation without preview, smart-home overclaim, nonexistent tool calls, automation creation without preview, and destructive cleanup claims before approval state was clear.

So this proposal has two layers:

1. **Tool Execution Trust** — applies to all tool calls, foreground or background.
2. **Background Work Reviewability** — applies only after work is delegated.

Both matter, but they should not be mixed into one generic "task dashboard" idea.

---

## What The Stress Tests Actually Proved

### Foreground Tool Trust Failures

- JARV1S created a Gmail draft before showing the exact recipient/body.
- JARV1S guessed or attempted nonexistent tools: `gmail.search_messages`, `gmail.delete_draft`, invented names like `smart_home.turn_on_device` (real tool: `control_device`).
- JARV1S claimed smart-home control before checking whether Home Assistant was configured.
- JARV1S created a recurring email briefing without previewing schedule, scope, delivery mode, or disable path.
- JARV1S claimed destructive cleanup had completed while the approval/execution state was unclear.

### Background Work Failures

- `agents.dispatch()` repeatedly failed because `cwd` was required and the model omitted it.
- A five-task dispatch partially succeeded, but JARV1S framed the batch as dispatched.
- The frontend froze during long tool-call generation, making dispatch state unknowable.
- Background task widgets showed that work happened, but not enough to understand what changed or why the result should be trusted.
- `mode="jarvis"` tasks returned useful summaries without showing service evidence.

### Product Insight

The user does not mainly want a debugging trace. The user wants to know:

- What is JARV1S about to do?
- Did it actually start?
- Is it waiting on me?
- What changed?
- Can I review or undo it?
- Why should I trust the result?

That is a product contract, not just observability.

---

## Design Principles

### 1. Confirmed State Only

JARV1S must not claim success until the relevant tool result confirms it.

Examples:

- Do not say "I've dispatched it" until `agents.dispatch()` returns a task ID.
- Do not say "Both files were deleted" unless both file deletions returned success.
- Do not say "I've saved the draft" before `create_draft()` succeeds.

### 2. Preview Before Persistent Or External State Changes

For actions that create, modify, delete, send, schedule, or configure persistent behavior, JARV1S should preview the concrete action before execution unless the tool itself provides an approval interrupt.

Examples:

- Email draft: recipient, subject, body.
- Automation: schedule, scope, delivery mode, disable path.
- Calendar write: account, title, time, attendees.
- Smart home: entity IDs and intended state.

### 3. Failed Dispatch Is Not A Failed Task

If dispatch fails before a task row is created, no task exists.

Task lifecycle remains narrow:

```text
running -> completed
        -> failed
        -> cancelled
```

Dispatch attempts return a command result:

```python
{"ok": False, "error_code": "source_limit_reached", "error_message": "..."}
```

### 4. One Dispatch Call Starts One Task

`agents.dispatch()` should remain single-task. Batch behavior should be the foreground model calling it repeatedly.

Prompt/runtime rule:

```text
After each dispatch call, inspect the result before starting the next task. If dispatch fails, stop or ask the user how to proceed.
```

Do not add a batch dispatch API until there is a real queue design.

### 5. Separate Lifecycle From Attention

Use the conductor shape from `CONDUCTOR_ORCHESTRATION.md`:

```python
status = "running" | "completed" | "failed" | "cancelled"
attention = "none" | "question" | "approval"
pending_input = {...} | None
```

A task can be `status="running"` while `attention="approval"`.

### 6. Spans Are Not Events

Do not flatten everything into event names like `tool_start`, `file_write`, `service_step`, and `completed` as though they are the same kind of thing.

Use a simple span/event distinction:

```python
{
    "kind": "span",
    "span_type": "tool",
    "name": "jarvis.gmail.search_emails",
    "start_ts": int,
    "end_ts": int | None,
    "status": "running" | "completed" | "failed",
    "attributes": {
        "service": "gmail",
        "operation": "search_emails",
        "args_preview": {...},
        "result_preview": "...",
        "error": None,
    },
}
```

```python
{
    "kind": "event",
    "event_type": "artifact_created",
    "ts": int,
    "attributes": {
        "path": "/...",
        "artifact_type": "file",
    },
}
```

Service steps are a derived view over spans grouped by namespace (`gmail`, `calendar`, `profile`, `files`), not something the model must remember to emit.

### 7. Voice Is For Outcome, Screen Is For Evidence

Voice should be short:

```text
Done. I changed two files and put the details on screen.
```

The screen can expose evidence on demand:

- changed files
- diffs
- approval state
- tool spans
- service groups
- final output
- failures and retries

### 8. UI Should Be Collapsed By Default

Do not build a nine-section developer dashboard as the primary experience.

Use the Claude Code pattern:

- one-line current state
- zero or one thing pending from the user
- expandable timeline/evidence

The default surface should answer "what is happening?" without asking the user to inspect traces.

---

## Required Changes

### 1. Dispatch Contract Hardening

**Status:** Implemented for the first slice.

Current behavior:

- `cwd` is optional and defaults to the JARV1S project root.
- `agents.dispatch()` remains single-task.
- The public tool returns a JSON string with `ok`, `task_id`, `mode`, `error_code`, `error_message`, and `message`.
- Guard failures return `ok=false`, do not create task rows, and should be reported as "task not started."
- Dispatch-specific JSON and batch-inspection guidance lives in the `agents.dispatch()` docstring, not the global prompt.
- `agents.get_result(task_id=None)` retrieves the full stored result for completed or failed work.
- `agents.get_status(task_id)` stays status-focused; completed/failed tasks point to `get_result()` instead of returning truncated progress.

Remaining work:

- Consider future `AGENT_WORKSPACE_DIR` for scratch/sandbox work.
- Improve richer reviewability of completed background work, such as file artifacts, service evidence, and diffs.

Suggested return shape:

```python
class DispatchResult(BaseModel):
    ok: bool
    task_id: str | None = None
    mode: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    message: str = ""
```

Decision: do not include latency or cost in the dispatch result yet. Dispatch returns before task completion; task cost is post-hoc and belongs on the task record/result, not the start command.

### 2. Global Prompt And Tool Truthfulness Rules

This is broader than background tasks.

Surfaces:

- `backend/core/prompts/persona/protocols.yaml`
- `backend/core/prompts/capabilities/reasoning.yaml`
- tool docstrings in `backend/plugins/agents/`, `backend/plugins/gmail/`, `backend/plugins/files.py`, `backend/plugins/system.py`, `backend/plugins/automations.py`, `backend/plugins/smart_home/`

Invariants:

```text
When a tool creates, modifies, deletes, sends, schedules, dispatches, or controls something, report only the confirmed state from the tool result.
```

```text
If a tool returns APPROVAL_NEEDED, the action has not executed yet. Ask for approval and do not claim success until approve_pending() returns success.
```

Dispatch-specific return handling belongs to the `agents.dispatch()` docstring because it is a tool-local contract.

Implemented tool-docstring changes:

- `agents.dispatch()` documents the JSON result and one-task-per-call behavior.
- `agents.get_result()` documents replaying existing completed work instead of redispatching.
- Gmail `create_draft()` requires recipient, subject, and body preview before voice/user-turn draft creation.
- Files/system/Gmail approval-capable tools preserve "call when requested" behavior while clarifying that `APPROVAL_NEEDED` means the action has not executed.

```text
If a capability is not known to be configured, check capability/configuration before claiming it can act.
```

### 3. Tool Hallucination Reduction

The plan must explicitly address nonexistent tool attempts.

Observed bad calls:

- `jarvis.gmail.search_messages`
- `jarvis.gmail.delete_draft`
- `jarvis.smart_home.turn_on_device` (invented — use `jarvis.smart_home.control_device`)

Work:

- Inspect routed tool tails for representative utterances from the stress tests.
- Determine whether the correct tools were absent from the prompt or ignored by the model.
- Strengthen tool discovery rule: if the namespace is known but the callable is not visible, call `jarvis.system.search_tools()` before inventing a method.
- Improve tool docstrings/utterances where the router is failing to surface the right tools.
- Add regression tests for no nonexistent tool calls in these domains.

This is upstream of reviewability. If the model invents a tool, no widget contract can save the turn.

### 4. Consent Wording First, Per-Item State Later

Split the earlier consent work into two phases.

Immediate:

- Fix wording in prompts/docstrings so "built-in approval" means "the tool may pause and request approval."
- Ensure JARV1S does not claim success until `approve_pending()` returns the execution result.

After conductor fields exist:

- Track approval state per item.
- Show which item is pending, approved, denied, executed, or failed.
- Make `approve_pending()` return whether more pending approvals remain.

This avoids the dependency cycle where per-item reporting was scheduled before the data model existed.

### 5. Conductor Pending Input

Implement the first conductor slice on `background_tasks` and, where useful, reuse the same contract for foreground approval widgets.

Add fields:

```python
{
    "attention": "none" | "question" | "approval",
    "pending_input": {
        "input_id": str,
        "kind": "question" | "approval",
        "prompt": str,
        "options": list[dict] | None,
        "tool": str | None,
        "risk": "low" | "medium" | "high" | None,
        "evidence": dict | None,
        "created_at": int,
        "expires_at": int | None,
    } | None,
}
```

This should not become a full workflow engine. It exists so JARV1S can pause, show what it needs, and resume truthfully.

### 6. Span/Event Model For Background Work

Normalize task activity around spans and point events.

**First implementation slice:** background tasks keep a compact task-scoped `trace` array on the existing `background_tasks` document. This is intentionally smaller than a generic trace backend: `events` remain the live UI stream, `activity` remains the receipt summary, and `trace` stores review evidence such as CodeAct calls, extracted `jarvis.*` tool names, SDK tool inputs, result previews, verified artifacts, and explicit SDK-output limitations.

Span examples:

- LLM generation
- CodeAct block
- tool call
- subprocess SDK tool use

Point event examples:

- artifact created
- approval requested
- approval resolved
- task completed
- task failed

Derived views:

- service-step summary from tool spans grouped by namespace
- changed files from file write spans/events
- evidence timeline from selected spans

Do not add a generic trace backend. Store this in MongoDB task events for now.

### 7. Background Task Card: Voice-First Shape

Replace the earlier nine-section widget idea with a smaller interaction model.

Default collapsed state:

```text
Task: Morning email briefing
State: Running
Detail: Checking Gmail
```

If it needs the user:

```text
Task: Email reply
Needs approval: Send draft to Helen?
[Review] [Approve] [Deny]
```

Expandable evidence:

- timeline
- tool spans
- changed files/artifacts
- service groups
- errors/retries

Primary actions:

- Review / show evidence
- Approve / deny when pending
- Resume/refine
- Cancel while running
- Dismiss completed

This keeps the ambient assistant surface simple while preserving audit depth.

### 8. Workflow-Specific Review Cards

Do not force every workflow through a generic background task card.

Use specific surfaces when they are the real user task:

- Email draft preview card
- Automation preview/management card
- Calendar event preview card
- Morning briefing card
- Smart-home setup/status card

The generic background task card is for task state and evidence, not for every domain-specific review experience.

### 9. Frontend Responsiveness During Tool Calls

Long tool-call bodies must not freeze the UI.

Required frontend states:

- model streaming
- composing tool call
- executing tool
- dispatch created
- dispatch failed
- task running
- task completed

UI rules:

- Collapse long tool-call code by default.
- Render a lightweight status row immediately.
- Defer or virtualize heavy code blocks.
- Never leave the screen static for long-running work.

### 10. Code Task Artifacts And Diffs

This is useful, but it primarily serves the developer loop, not the ambient assistant vision.

For `mode="code"` tasks, surface:

- changed file paths
- direct file-open actions
- diff/review artifact when available
- final summary

Keep this behind the expandable evidence/timeline path. Do not make code diffs the center of the daily assistant UI.

### 11. `mode="jarvis"` Service Evidence

For in-process integration tasks, derive service evidence from tool spans.

Example collapsed evidence:

```text
Calendar: checked next event
Gmail: searched unread important emails
Memory: recalled "morning briefings"
```

This is not the primary UI. It is the "show evidence" layer behind a concise result.

### 12. Email And Automation Preview Policy

Fix domains that caused trust failures before they ever became background tasks.

Email draft:

- Never invent an email address.
- If finding an address from history, show the selected address.
- Preview recipient, subject, and body before saving.
- Do not call `create_draft()` until the user confirms the exact draft.
- Consider `delete_draft` only if drafts become a regular workflow.

Automation creation:

- Preview trigger, schedule, scope, delivery mode, and disable path.
- Distinguish scheduled daily briefing from event-based Gmail trigger.
- Include rule ID and "how to stop this" in the final response.

Smart home:

- Check setup/config before claiming ability.
- Search devices before control.
- Never guess entity IDs.

---

## Suggested Implementation Order

1. Finish dispatch contract hardening: structured single-task result and explicit start failure.
2. Add global truthfulness prompt/docstring rules.
3. Investigate routed tool tails and fix nonexistent tool hallucination.
4. Fix consent wording; defer per-item consent reporting until pending-input state exists.
5. Fix frontend long-tool-call freeze and explicit tool/dispatch states.
6. Add conductor `attention` / `pending_input` fields.
7. Normalize background task spans/events.
8. Add review-rail progress receipts plus on-demand `BackgroundTaskWidget` evidence detail. ✅ Shipped; richer diff/review actions remain deferred.
9. Add workflow-specific preview cards for email and automations.
10. Add code artifacts/diffs as developer-loop evidence.
11. Add focused regression tests around the invariants.

---

## Future Sandbox Direction

The current default cwd is the JARV1S project root because today's delegated tasks are mostly development tasks.

For a daily assistant, JARV1S should eventually have a dedicated user-owned workspace:

```text
~/JARV1S Workspace/
  scratch/
  scripts/
  reports/
  apps/
  downloads/
```

This should become a config setting:

```python
AGENT_WORKSPACE_DIR: Path = Path.home() / "JARV1S Workspace"
```

Use the workspace for:

- generated scripts
- temporary reports
- small utilities
- AI-created apps/prototypes
- non-repo scratch work

Keep repo-root cwd for explicit codebase tasks only.

---

## Non-Goals

- Do not adopt LangGraph, Temporal, Inngest, LangSmith, or Langfuse for this slice.
- Do not build a full trace dashboard.
- Do not build a full project-management system.
- Do not make code-task diffs the center of the ambient assistant UI.
- Do not solve every system-state issue before tool execution is trustworthy.

The goal is narrower: make JARV1S propose truthfully, act only with confirmed contracts, pause safely, finish reviewably, and speak concisely.
# Previous Draft: Background Task Trust Hardening

**Status:** Superseded by the broader tool-execution framing above. Kept for historical context only.  
**Date:** 2026-05-20  
**Priority:** High — background agents are not ready to become a daily-driver delegation path until task start, progress, approval, and review states are trustworthy.

---

## Problem

The stress tests showed that JARV1S has the right primitives for delegated work, but the user-facing contract is not reliable enough yet.

The failure pattern is not just "we need more observability." The deeper issue is that JARV1S sometimes cannot accurately answer:

- Did the task actually start?
- What is it doing right now?
- Did it need approval?
- What changed?
- What failed?
- What still needs the user?
- Is the final answer supported by enough evidence?

When those states are unclear, the assistant feels like it is bluffing even when the underlying tool call eventually succeeds.

---

## Stress-Test Findings

### Dispatch Contract

- `agents.dispatch()` repeatedly failed because `cwd` was required and the model omitted it.
- Batch dispatch could partially succeed while JARV1S still framed the whole batch as started.
- Source limits produced prose errors instead of a clear "not started" state.

### Truthful State

- JARV1S sometimes claimed work was started, saved, deleted, or completed before the relevant tool result confirmed it.
- Long tool-call rendering froze the frontend, making it unclear whether dispatch was still being composed, had failed, or had started.

### Consent And Destructive Actions

- "Built-in approval" was interpreted as "no permission required."
- Multi-file deletion did not clearly report per-file approval/execution state.
- JARV1S claimed both files were deleted when only one deletion was visibly confirmed.

### Reviewability

- Background task widgets showed summaries, but not enough evidence to review work.
- Code tasks need changed paths, file-open actions, and diffs.
- `mode="jarvis"` integration tasks need service-step traces: Calendar checked, Gmail searched, Memory recalled, etc.
- Voice summaries were too dense for speech and contained screen-only details like long file paths.

---

## Design Principles

### 1. Confirmed State Only

JARV1S must not claim a state change succeeded until the tool result confirms it.

Examples:

- Do not say "I've dispatched it" until `agents.dispatch()` returns a task ID.
- Do not say "Both files were deleted" unless both file deletions returned success.
- Do not say "I've saved the draft" before `create_draft()` succeeds.

### 2. Failed Dispatch Is Not A Failed Task

If dispatch fails before a task row is created, the result is "task not started." Do not force this into task lifecycle state.

Task lifecycle should stay narrow:

```text
running -> completed
        -> failed
        -> cancelled
```

Dispatch attempts can separately return:

```python
{"ok": False, "error": "source_limit_reached", "message": "..."}
```

### 3. Separate Lifecycle From Attention

Use the conductor shape from `CONDUCTOR_ORCHESTRATION.md`:

```python
status = "running" | "completed" | "failed" | "cancelled"
attention = "none" | "question" | "approval"
pending_input = {...} | None
```

A task can be `status="running"` while `attention="approval"`.

### 4. Voice Is For Outcome, Screen Is For Evidence

Voice should be short:

```text
Done. I changed two files and put the details on screen.
```

The screen should carry:

- file paths
- diffs
- tool events
- service steps
- approval state
- cost/budget
- retry/failure details

### 5. Build Product Surfaces, Not A Generic Trace Dashboard

The goal is not LangSmith inside JARV1S. The goal is a task review surface that makes delegated work understandable and controllable.

---

## Required Changes

### 1. Dispatch Contract Hardening

**Status:** Started — `cwd` is now optional and defaults to the JARV1S project root.

Remaining work:

- Return structured dispatch results instead of prose strings.
- Make source-limit failures explicit: task not started.
- Make batch dispatch report partial success accurately.
- Consider future `AGENT_WORKSPACE_DIR` for scratch/sandbox work.

Suggested return shape:

```python
class DispatchResult(BaseModel):
    ok: bool
    task_id: str | None = None
    mode: str | None = None
    error: str | None = None
    message: str
```

### 2. Prompt And Tool Truthfulness Rules

Update prompt/tool surfaces so the model treats tool results as the source of truth.

Surfaces:

- `backend/core/prompts/persona/protocols.yaml`
- `backend/core/prompts/persona/background.yaml`
- `backend/core/prompts/capabilities/reasoning_background.yaml`
- tool docstrings in `backend/plugins/agents/`, `backend/plugins/gmail/`, `backend/plugins/files.py`, `backend/plugins/system.py`, and `backend/plugins/automations.py`

Invariants:

```text
When a tool creates, modifies, deletes, sends, schedules, or dispatches something, report only the confirmed state from the tool result.
```

```text
If a tool returns APPROVAL_NEEDED, the action has not executed yet. Ask for approval and do not claim success until approve_pending() returns success.
```

```text
If dispatch returns an error instead of a task ID, say the task did not start.
```

### 3. Consent State And Pending Input

Implement the first conductor slice on `background_tasks`.

Add fields:

```python
{
    "attention": "none" | "question" | "approval",
    "pending_input": {
        "input_id": str,
        "kind": "question" | "approval",
        "prompt": str,
        "tool": str | None,
        "risk": "low" | "medium" | "high" | None,
        "evidence": dict | None,
        "created_at": int,
        "expires_at": int | None,
    } | None,
}
```

Start with destructive operations and background-agent approvals. Do not build a full workflow engine.

### 4. Background Task Event Enrichment

The current event stream is a useful start, but the UI needs more structured meaning.

Add or normalize event types as needed:

- `dispatch_created`
- `dispatch_failed`
- `tool_start`
- `tool_end`
- `tool_error`
- `file_read`
- `file_write`
- `artifact`
- `service_step`
- `approval_requested`
- `approval_resolved`
- `completed`
- `failed`

Do not add every possible event upfront. Add the events required by the task review UI.

### 5. Background Task Widget Reviewability

Upgrade `BackgroundTaskWidget` from "status card" to "task review card."

Minimum sections:

- task title / prompt summary
- mode: `code` or `jarvis`
- status and attention state
- task ID
- current progress
- changed files / artifacts
- service steps
- approval or question state
- final result
- expandable event log

Minimum actions:

- `Open changed file`
- `View diff`
- `Resume/refine`
- `Cancel` while running
- `Dismiss`

### 6. Frontend Responsiveness During Tool Calls

Long tool-call bodies must not freeze the UI.

Required frontend states:

- model streaming
- composing tool call
- executing tool
- dispatch created
- dispatch failed
- task running
- task completed

UI rules:

- Collapse long tool-call code by default.
- Render a lightweight status row immediately.
- Defer or virtualize heavy code blocks.
- Never leave the screen static for long-running work.

### 7. Code Task Artifacts And Diffs

For `mode="code"` tasks, surface:

- changed file paths
- file sizes or brief summaries
- a diff/review artifact when possible
- direct file-open actions

This can start by parsing SDK tool events for file paths and adding a simple artifact list. A richer diff viewer can come later.

### 8. `mode="jarvis"` Service-Step Trace

For in-process integration tasks, surface service-level evidence:

```text
Calendar: checked next event
Gmail: searched unread important emails
Memory: recalled "morning briefings"
```

The user should not have to trust a final paragraph without seeing what services were actually consulted.

### 9. Gmail Draft And Email Policy

Fix `create_draft` docstring and behavior expectations.

Current wording says "No confirmation required." That is unsafe for voice-driven email creation.

Desired behavior:

- Never invent an email address.
- If finding an address from history, show the selected address.
- Preview recipient, subject, and body before saving a draft.
- Do not call `create_draft()` until the user confirms the exact draft.
- Add `delete_draft` only if drafts become a real daily workflow.

### 10. Small Regression Coverage

Do not build a large eval harness yet. Add focused tests around invariants:

- dispatch without `cwd` starts with default cwd
- dispatch source limit returns "not started"
- approval-needed tools do not produce completion claims
- multi-file delete reports per-file state
- Gmail draft prompt/docstring requires preview before save
- background task widget receives changed paths/events

---

## Suggested Implementation Order

1. Finish dispatch contract hardening: structured return and explicit failure.
2. Add prompt/docstring truthfulness rules.
3. Fix consent wording and per-item destructive-action reporting.
4. Add frontend tool-call lifecycle states and prevent long-call freezes.
5. Enrich background task events for changed files and service steps.
6. Upgrade `BackgroundTaskWidget` around artifacts, status, and attention.
7. Add conductor `attention` / `pending_input` fields.
8. Add focused regression tests.

---

## Future Sandbox Direction

The current default cwd is the JARV1S project root because today's delegated tasks are mostly development tasks.

For a daily assistant, JARV1S should eventually have a dedicated user-owned workspace:

```text
~/JARV1S Workspace/
  scratch/
  scripts/
  reports/
  apps/
  downloads/
```

This should become a config setting:

```python
AGENT_WORKSPACE_DIR: Path = Path.home() / "JARV1S Workspace"
```

Use the workspace for:

- generated scripts
- temporary reports
- small utilities
- AI-created apps/prototypes
- non-repo scratch work

Keep repo-root cwd for explicit codebase tasks only.

---

## Non-Goals

- Do not adopt LangGraph, Temporal, Inngest, LangSmith, or Langfuse for this slice.
- Do not build a full trace dashboard.
- Do not build a full project-management system.
- Do not solve every system-state issue before background tasks are trustworthy.

The goal is narrower: make delegated work start truthfully, run visibly, pause safely, finish reviewably, and speak concisely.
