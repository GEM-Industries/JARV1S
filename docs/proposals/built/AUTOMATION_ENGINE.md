# Automation Engine

**Status:** Implemented (Phase 1 only)  
**Date:** 2026-03-03

**Reading guide:** Phase 1 below is shipped. Phases 2–3 in this doc are the original expansion plan — **not implemented** as written. Track intelligence work on [ROADMAP.md](../../ROADMAP.md) Phase 11. Normative automation behavior: [ARCHITECTURE.md](../../ARCHITECTURE.md).

---

## Problem

JARV1S is entirely reactive. Nothing watches external systems between turns. "Remind me 5 minutes before my standup" is impossible because nothing observes the calendar and acts without user prompting.

This extends beyond calendar — WhatsApp, email, smart home, and any future integration share the same gap: **no mechanism to observe external sources, evaluate preferences, and act autonomously.**

---

## Industry Context

Every mature automation platform converges on the **Event-Condition-Action (ECA)** model. Home Assistant is the gold standard for personal automation. Its `CalendarEventListener` (in `trigger.py`) uses the **materialized trigger** pattern:

1. Poll the calendar entity every 15 min for upcoming events in a sliding window
2. Compute fire times: `event.start + offset` for each event
3. Schedule point-in-time callbacks at those fire times
4. Dispatch when fire time arrives, passing calendar event data into the trigger context

Key design principle: **conditions must be declarative and mechanical** (field comparisons in microseconds), not LLM-evaluated. The LLM is the interface for *creating* rules and the executor for *speaking* — but a $0.01 LLM call per event evaluation is wasteful and slow.

---

## What Exists Today

The scheduler is not juvenile. It handles time-based triggers with production patterns: atomic claim-and-fire, rule/occurrence separation, recurrence, exception dates, series control, DST safety, orphan recovery, and offline buffering.

What's missing:

- **External observation** — no background process monitors any external API
- **Condition evaluation** — raw triggers always fire, no filtering ("only standups", "not 1:1s")

### Current Note: Triggers Are the Delivery Primitive

The original implementation used `ALERT_TRIGGERED` as the shared delivery event. The current architecture routes automation output through the trigger substrate instead:

```
TriggerInstance {origin, action, attention, delivery}
  → TRIGGER_DUE
  → notification sound → process_turn(system_context) → Agent → TTS
```

If `action.protocol_name` is set, the orchestrator builds protocol context and runs the protocol. If not, it speaks or evaluates the action message. This path doesn't care where the trigger came from — scheduler, automation, system pulse, and background agents all materialize `TriggerInstance` rows.

---

## Design

### Principle: Keep Automation Focused On External Events

- **TriggerScheduler** stays the durable time-trigger specialist
- **AutomationService** handles external triggers
- Both converge on `TriggerInstance` / `TRIGGER_DUE` so the orchestrator treats them identically
- Protocols are the "complex action" primitive — the engine stays simple (one action per rule)

### Architecture

```
  ┌──────────────┐
  │   Calendar    │──poll──┐
  │   Watcher     │        │
  └──────────────┘         │     ┌─────────────────────────────┐
  ┌──────────────┐         ├────▶│     AutomationService       │
  │    Email     │──poll──┤     │                             │
  │   Watcher    │         │     │  1. Poll watchers           │
  └──────────────┘         │     │  2. Evaluate rules          │
  ┌──────────────┐         │     │  3. Materialize fire times  │
  │  (future)    │──poll──┘     │  4. Create TriggerInstance │
  │   Watcher    │              │                             │
  └──────────────┘              └──────────┬──────────────────┘
                                           │
                              event_bus   │  TRIGGER_DUE
                                          │  {instance_id, owner_id}
                                           ▼
                                ┌─────────────────────────────┐
                               │  Orchestrator                 │
                               │  _handle_trigger_due()        │
                               │  → same path as scheduler     │
                                └─────────────────────────────┘
```

### Two Components

**1. Watchers** — thin, per-integration, passive data providers

A watcher polls one external source and returns current state. It does not evaluate rules, track timing, or publish to the bus. The AutomationService manages its lifecycle.

```python
class Watcher(Protocol):
    source: str                               # "calendar", "email"
    async def poll(self) -> list[dict]        # current items with source-specific fields
```

`CalendarWatcher.poll()` returns upcoming events (24h window) via the existing calendar integration client. Each item is a dict: `{id, title, start, end, location, is_all_day, ...}`.

Watchers are **auto-discovered**: `AutomationService.start()` scans `services/watchers/` with `pkgutil.iter_modules`, finds any class with a `source` attribute and `poll` method, and registers it. Adding a new watcher (e.g. `EmailWatcher`) requires only dropping a new file in `services/watchers/` — no changes to `main.py` or `AutomationService`.

**2. AutomationService** — single background service

One service evaluates all rules, regardless of source.

```
every tick (60s):
  prune _fired entries older than 24h
  skip if globally paused
  for each source with active rules:
    items = watcher.poll()
    for each rule × item:
      skip if item.id in rule.suppressed_event_ids
      skip if rule.paused_until > now
      skip if (rule.id, item.id) in _fired              # unified dedup
      skip if conditions fail
      skip if item.is_all_day                            # all-day events have no meaningful fire time
      compute fire_time = item.start + trigger.offset
      if fire_time <= now and (now - fire_time) <= MAX_LATENESS (10 min):
        claim (rule.id, item.id) in automation_fired     # mark BEFORE firing
        _create_fire_task(rule, item)                    # immediate
      elif fire_time <= now and (now - fire_time) > MAX_LATENESS:
        claim (rule.id, item.id) in automation_fired     # skip stale, mark handled
      else:
        _schedule_fire(rule, item, delay)                # asyncio.call_later
```

**Unified dedup (`automation_fired` + `_fired`)**: entries are claimed when an automation actually fires or is skipped as stale, not when a future timer is merely scheduled. The unique `(rule_id, item_id)` index prevents cross-process double dispatch, while `_pending` prevents duplicate in-process timers. The collection uses a TTL index (`expireAfterSeconds: 86400`) so MongoDB handles 24h expiry automatically.

**MAX_LATENESS (10 min)**: semantic staleness guard — if `now - fire_time > 10 min`, the event is skipped. Prevents stale notifications ("your 9am standup starts in 5 minutes") when a new rule is created for an event already in progress, or when `_fired` could not be restored from DB at startup.

**Calendar freshness**: pre-start calendar reminders (`offset < 0`) carry `FreshnessPolicy(stale_if_source_event_started=True)` so they expire once the event has started. At-start and after-start calendar rules do not carry that policy by default; they usually represent event-time work such as muting, running a protocol, or sending an "starts now" notification.

**Precise fire-time scheduling**: the poll loop is the *discovery* mechanism, not the fire mechanism. `_schedule_fire` uses `asyncio.call_later` for future events; `_create_fire_task` wraps the coroutine in a tracked `asyncio.Task` with `add_done_callback` for GC-safe exception logging — avoids "Task exception was never retrieved" failures.

**Offline delivery**: automation fires now create trigger instances. If the user is offline, user-facing delivery moves to `awaiting_delivery` and is retried on reconnect. Silent/evaluative automation work can instead complete as `completed` without a user-facing delivery.

### How Protocols Fit

Protocols are already the "complex action" mechanism. The automation engine doesn't need multi-step actions, branching, or workflow chains because protocols handle all of that:

| Want | Automation Rule |
|---|---|
| Simple notification | `message: "Your {title} starts in 5 min"` |
| Complex routine | `protocol: "morning standup"` → agent executes the protocol's steps |
| Semantic action | `protocol: "email task extractor"` → agent reads email, extracts tasks, adds to todo |

When an automation fires with a `protocol`, the orchestrator builds protocol context via `build_protocol_context()` — the same path as alarm-linked protocols today. The source item is included in the `context` field so the agent knows what triggered it.

This means: **zero new delivery infrastructure**. Speak-only, protocol-only, and speak-then-protocol all work identically for both scheduled and automation triggers. Semantic understanding (e.g., "extract the task from this email") lives in the protocol's steps, not in the condition evaluator — the LLM is already involved in action execution.

---

## Data Model

### External-Origin `trigger_rules`

```python
TriggerRule(
    id=str,
    owner_id=str,
    name="Standup reminder",
    enabled=True,
    origin=TriggerOrigin(
        kind="external",
        source="calendar",
        event="starting",
        offset_minutes=-5,
    ),
    conditions=[
        TriggerCondition(
            kind="field",
            parameters={"field": "title", "op": "contains", "value": "standup"},
        ),
    ],
    action=TriggerAction(
        decision="tell",
        message="Your {title} starts in 5 min",
        protocol_name=None,
        instructions=None,
    ),
    attention=AttentionPolicy(level="normal", sound="chime"),
    delivery=DeliveryPlan(),
    freshness=FreshnessPolicy(stale_if_source_event_started=True),
    suppressed_event_ids=[],
    paused_until=None,
)
```

There is no standalone automation definition collection. The `automations` plugin is a product-facing facade over external-origin `TriggerRule`s.

### Condition Evaluation

Declarative, no LLM. Short-circuit on first failure.

```python
def evaluate(conditions: list[TriggerCondition], item: dict) -> bool:
    for c in conditions:
        params = c.parameters
        val = str(item.get(params["field"], "")).lower()
        target = params["value"].lower()
        match params["op"]:
            case "contains":     ok = target in val
            case "not_contains": ok = target not in val
            case "equals":       ok = val == target
            case "not_equals":   ok = val != target
        if not ok:
            return False
    return True
```

### Template Rendering

`{field}` placeholders in `action.message` are resolved from the source item via `str.format_map(defaultdict(str, item))`. Missing fields resolve to empty string rather than raising `KeyError`. The message is system context for the agent, not final speech — the LLM naturally handles omissions and generates natural output.

---

## LLM Tools

Event-rule authoring and management tools on the `automations` plugin (dynamic, routed by Tool Router):

| Tool | Purpose |
|---|---|
| `create_rule(name, trigger, conditions, action, importance="normal")` | Create an automation |
| `update_rule(rule_id, ...)` | Modify conditions, action, importance, enabled, paused_until |
| `delete_rule(rule_id)` | Remove a rule |
| `suppress_event(rule_id, event_id)` | Skip a specific item instance |
| `test_rule(rule_id)` | Dry-run against current watcher state — returns what would fire |
| `list_available_triggers(app_name)` | Inspect valid external trigger events and condition fields |

Inventory is consolidated under `activity.list_setups(kind="automation")`; the old `automations.list_rules` tool has been removed.

`test_rule` improves the creation UX: the LLM creates a rule and immediately tests it. "I've set that up. Looking at your calendar, it would remind you about your 10am Standup and 2pm Team Sync today."

### Example Interactions

> "Remind me 5 minutes before my standups but not my 1:1s"

```python
jarvis.automations.create_rule(
    name="Standup reminder",
    trigger={"source": "calendar", "event": "starting", "offset": -5},
    conditions=[
        {"field": "title", "op": "contains", "value": "standup"},
        {"field": "title", "op": "not_contains", "value": "1:1"},
    ],
    action={"message": "Your {title} starts in 5 minutes"},
    importance="normal",
)
```

> "When my standup starts, run my morning standup protocol"

```python
jarvis.automations.create_rule(
    name="Standup protocol",
    trigger={"source": "calendar", "event": "starting", "offset": 0},
    conditions=[{"field": "title", "op": "contains", "value": "standup"}],
    action={
        "decision": "act",
        "protocol_name": "morning standup",
    },
)
```

Because this rule uses `offset=0`, it does not expire merely because the event
has started. If it cannot run exactly at the boundary, the protocol is still
useful during the meeting.

> "Don't remind me about tomorrow's standup"

```python
jarvis.automations.suppress_event(rule_id="...", event_id="google_cal_abc123")
```

---

## Implementation

### Phase 1: Calendar Watcher + Engine Core ✅

1. **`services/watchers/__init__.py`** — `Watcher` protocol (`source`, `poll`)
2. **`services/watchers/calendar.py`** — `CalendarWatcher` polls via Integration Gate, exposes `is_all_day`
3. **`services/automation.py`** — `AutomationService` with auto-discovery, persisted `_fired` dedup, `MAX_LATENESS` staleness guard, task-safe `_create_fire_task` + `_schedule_fire`, global pause/resume, `test_rule`
4. **`plugins/automations.py`** — LLM-facing authoring/management tools (`create_rule`, `update_rule`, `delete_rule`, `suppress_event`, `test_rule`, `unpause_rule`, `pause_all`, `resume_all`, `list_available_triggers`); inventory lives in `activity.list_setups(kind="automation")`
5. **MongoDB `trigger_rules` collection** — external automation definitions use `origin.kind="external"` and query by `origin.source`
6. **MongoDB `automation_fired` collection** — dispatch dedup log with TTL index (24h auto-expiry), restored on startup
7. **`orchestrator.py`** — trigger delivery routes through `_handle_trigger_due`; generic completion uses `completed`, while successful user-facing output uses `delivered`

No new event types. No new delivery paths. New watchers require only a new file in `services/watchers/`.

### Phase 2: Observability + New Sources — not implemented as this section

> Parts of this plan landed later (watcher enrichment, numeric operators, push triggers — ROADMAP Phases 6–6.5). **Reaction tracking** and several watchers below were not built.

**New watchers** — each adds one `Watcher` class. Zero changes to AutomationService, plugin, or data model.

- **EmailWatcher** — polls inbox, exposes unread messages
- **WhatsAppWatcher** — webhook receiver or poll, exposes new messages
- **SmartHomeWatcher** — polls HA entity states, exposes state changes

**Watcher enrichment** — CalendarWatcher already exposes `is_all_day`. Phase 2 adds `attendees`, `duration_minutes`. Enables conditions like "only meetings with external attendees" and numeric operators like `duration_minutes greater_than 30`.

**Reaction tracking** — log user response to each fired automation (listened, interrupted, dismissed, acted upon). Stored on the automation's delivery record. No model built yet — just collect data.

**DND / quiet hours** — global suppression windows with importance-based exemptions. Rules with `importance="urgent"` or `importance="critical"` bypass quiet mode. Leverages the shared trigger priority semantics in `core/triggers/priority.py`.

**Numeric condition operators** — `greater_than`, `less_than` for duration, attendee count, etc. Trivial evaluator extension when watcher fields demand it.

### Phase 3: Intelligence Layer — not implemented

> See [ROADMAP.md](../../ROADMAP.md) Phase 11.

**Preference model** — lightweight local classifier trained on reaction data from Phase 2. Gates the "should I bother the user?" decision for rules that opt in via an `intelligence` flag. Not an LLM — a small decision tree or logistic regression. Only viable once sufficient reaction data exists.

**Adaptive suppression** — automatically reduce fire frequency or flag for review when a rule's dismiss rate exceeds a threshold.

**Cross-source conditions** — conditions that reference other watchers' state ("only if I'm not in a meeting", "only if I'm home"). Requires the AutomationService to query multiple watchers per rule evaluation.

**Hybrid triggers** — rules that combine a schedule with external state ("every morning at 8am, summarize unread emails"). Bridges the scheduler and automation service.

**Auto-suggest rules** — LLM notices behavioural patterns in archival memory and proposes automations. "I notice you always ask about your calendar before standups — want me to set up an automatic reminder?"

---

## What This Does NOT Do

- **No scheduler replacement.** Time-based triggers stay on `TriggerScheduler`.
- **No LLM condition evaluation.** Semantic understanding lives in protocols (the action), not conditions (the filter).
- **No action branching.** Complex multi-step logic belongs in Protocols.
- **No visual rule builder.** The LLM is the only interface.
- **No webhook ingestion in the original Phase 1.** Later trigger-scaling work added Composio webhooks plus bespoke push adapters for Calendar and Gmail.
- **No OR condition groups.** Two rules cover the OR case; the LLM creates both in one turn. Add if a real use case demands it.
