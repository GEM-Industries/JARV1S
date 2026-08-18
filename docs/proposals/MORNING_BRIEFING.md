# Morning Briefing

**Status:** Proposed  
**Date:** 2026-05-25 (revised 2026-06)  
**Priority:** Medium — prove briefing composition before adding new trigger mechanics.  
**Depends on:** Existing `TriggerService`, `AutomationService`, `AttentionService`, protocols, `TriggerInstance.result_text`, and the `offer` delivery path.

**Related:** Context-sensitive announcements use `decision="offer"` with live commitment context from `core/triggers/offer_context.py`. The sleep debrief incident was solved that way — not via a separate timing subsystem.

---

## Problem

JARV1S should gather useful information in the background, then brief the user when they are ready rather than dumping overnight findings at a fixed clock time.

The first question is not “what new subsystem do we need?” It is whether existing trigger, attention, and offer paths already make morning briefings good enough.

---

## Current Primitives

A morning briefing can be built without new infrastructure:

1. Create a `morning_briefing` protocol.
2. Schedule it with `scheduler.remind(..., recurrence="daily")`.
3. Use `decision="offer"` plus `instructions` so the reminder speaks only when useful.
4. Let the offer path speak only if the briefing has useful content (`NO_REPLY` otherwise).
5. Put a morning freshness deadline on the trigger so deferral cannot turn into an afternoon briefing.
6. Let `DEFER` park the trigger with `next_retry_at` so semantic defers are reconsidered even when connection and attention do not change.
7. Let availability blockers retry when the user reconnects or resumes attention until freshness expires.

Retry hooks already exist:

| Signal | Published by | Consumed by |
|---|---|---|
| `presence_active` | `SESSION_CONNECTED` | retry `awaiting_delivery` triggers |
| `attention_resumed` | attention mode becomes `active` | retry `awaiting_delivery` triggers |
| `retry_due` | `TriggerScheduler` sees `next_retry_at <= now` | retry semantic `DEFER` triggers |
| `turn_idle` | turn completion / trigger queue availability | deferred trigger execution |

Local composite events (alarm dismissed, arrived home, calendar transitions) can enter the same automation rule engine via existing `AutomationService.on_push_event(source=..., event=...)` — no new collection, scheduler, or notification queue.

---

## Actual Gaps

### 1. Time-Bounded Freshness

If the 7am briefing cannot deliver until 4pm, it is stale. Morning briefing should use `FreshnessPolicy` (`expires_at` or `expire_after_due_s`) so stale work reaches a deterministic deadline before delivery/retry. Routine briefings should usually `expire`; user-expected "brief me sometime this morning" offers can use `on_expiry="force_deliver"` as the deliver-by backstop.

### 2. Phrase-Triggered Briefing

“Brief me when I say good morning” cannot be expressed today. Time-based reminders can wait for availability, but they cannot bind delivery to a particular utterance.

### 3. First-Interaction Timing

“First time I’m active after 5am” is not a clock time. It may need a session-connected external rule or an evaluate offer on first interaction — not a new subsystem.

---

## Morning Briefing Protocol (V0)

The high-value part is composition, not trigger mechanics:

1. Query useful overnight offer results from `trigger_instances`.
2. Add today’s calendar/weather/tasks as needed.
3. Compose one concise response.
4. Mark consumed findings as delivered or otherwise prevent repeated surfacing.

Example query shape:

```python
{
    "owner_id": owner_id,
    "action_snapshot.decision": "offer",
    "completed_at": {"$gt": since},
    "result_text": {"$exists": True, "$nin": [None, "", "NO_REPLY"]},
    "status": {"$ne": "delivered"},
}
```

Do not create a `background_findings` collection until real usage shows `TriggerInstance.result_text` and `source_event` are insufficient.

Schedule V0 with existing `scheduler.remind(..., decision="offer")` and instructions:

```text
Prepare a concise morning briefing. Include today’s calendar, urgent overnight items,
weather, and anything actionable. If there is nothing useful, respond NO_REPLY.
```

Run this for a week. If the problem is poor composition, better timing will not help. Fix the protocol first.

The briefing trigger should carry an explicit freshness deadline. `DEFER` is reversible and retries through `next_retry_at`; `NO_REPLY` is a permanent drop and should remain auditable on the `TriggerInstance` as a suppression reason. Use `on_expiry="force_deliver"` only when the user expects delivery by the deadline.

---

## Future: Phrase-Triggered Briefing (V1+)

Only pursue after V0 proves useful composition.

If the user says “good morning, Jarvis,” the system must avoid double responses (briefing + normal turn). Two options:

**Option A — Local command consumes:** phrase matcher handles the utterance like a local command; briefing protocol output is the response. Simpler; matches existing local-command pattern.

**Option B — Context fold:** user turn proceeds normally; matching phrase injects briefing context into the same turn. More conversational; adds a path in `process_turn()`.

Start with Option A unless usage proves the fold is necessary.

Implementation paths (pick one when needed):

- Voice utterance match → run protocol / inject briefing context
- Publish a normal external event: `source="voice", event="good_morning"` → automation rule

Neither requires a `source="anchor"` namespace or anchor-specific publishers.

---

## Non-Goals

- No new `background_findings` collection.
- No new notification queue.
- No first-class anchor or timing subsystem.
- No broad prompt rewrite.
- No fuzzy voice command matcher in V1.
- No attempt to solve all “opportune moment” delivery before the morning briefing protocol proves useful.

---

## Open Questions

- Should consumed briefing findings become `delivered`, or should a separate consumed marker be added later?
- Does `TriggerInstance.result_text` contain enough structured signal for good briefing composition?
- What is the right user-facing way to author the morning freshness deadline?
- Should phrase-triggered briefing consume the turn or fold context into the user’s normal “good morning” turn?

---

## Smallest Next Build

Build only the `morning_briefing` protocol first.

The trigger mechanism is replaceable later. The protocol composition is not. If the protocol cannot produce a useful briefing from existing `TriggerInstance` records and current tools, better timing will only deliver weak content at a better moment.
