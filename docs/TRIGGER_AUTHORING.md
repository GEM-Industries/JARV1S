# Trigger Authoring Contract

Canonical guide for proactive JARV1S behavior: how tools author `TriggerRule`s
and how those rules interact with runtime delivery.

**Authoring chooses intent; runtime chooses execution and settlement.** Do not
hide runtime behavior behind domain-specific flags unless the flag maps cleanly
to the trigger axes below.

## Trigger Axes

`TriggerRule` has six independent axes:

| Axis | Model | Meaning |
| :--- | :--- | :--- |
| When | `TriggerOrigin` | Why or when the trigger fires: `time`, `interval`, `external`, `system`, or `manual`. |
| Filter | `conditions` | Cheap deterministic prefilters before agent work. |
| Work | `TriggerAction` | What runs at fire time: `decision`, `message`, `instructions`, `protocol_name`, `content_type`, and optional `reply_grounding`. |
| Interrupt | `AttentionPolicy` | How hard this may try to reach the user: `normal`, `urgent`, or `critical`. |
| Routing | `DeliveryPlan` | Physical delivery hints (`channel`, `target`). Whether the user hears from JARV1S is **not** here. |
| Freshness | `FreshnessPolicy` | When the trigger is no longer worth delivery or retry. |

Keep these axes separate. Schedule/ingestion, reporting intent, interruption
strength, physical routing, and staleness are different decisions.

## Decision axis (`TriggerAction.decision`)

`decision` answers **whether the user should hear from JARV1S** after the work
runs. It is about **reporting**, not permission. Approval and consent live in
`require_consent()` / `pending_inputs` and must not be folded into `offer`.

| `decision` | Meaning | Runtime presentation |
| :--- | :--- | :--- |
| `tell` | Always speak / present a result. | `presentation=always` (then attention gate). |
| `offer` | Speak only if worth it now. | `presentation=if_content` — may `NO_REPLY`, `DEFER`, or speak. |
| `act` | Do the work and stay silent. | `presentation=never` — headless only. |

`message` and `instructions` can coexist. Example: a briefing title in
`message` plus gather criteria in `instructions`.

Validation (enforced on `TriggerAction`):

- `tell` and `offer` require at least one content source: `message`,
  `instructions`, or `protocol_name`.
- `act` requires `instructions` or `protocol_name` (a bare `message` has no
  user-facing consumer on a silent path).
- `protocol_name` implies protocol phrasing; there is no `content_type="protocol"`.

### Content type (`TriggerAction.content_type`)

Optional hint for prompt selection: `plain`, `event`, or `task_result`. Do not
store `offer` or `deferred_instruction` here — those semantics are carried by
`decision`.

### Instructions (`TriggerAction.instructions`)

Fire-time work and delivery criteria passed into `SystemTurnContext` as
`INSTRUCTIONS:`. Renamed from the legacy `directive` field. Use for policy the
agent must interpret at fire time ("only if not cancelled", "check garage and
report if open").

### Reply grounding (`TriggerAction.reply_grounding`)

`reply_grounding` is a small semantic frame deliberately shown to the model when
the trigger fires and again beside the delivered assistant turn for a bounded
reply window. Use it only when natural proactive wording omits context the agent
will need to interpret that reply; for example, a habit id, habit name, and
check-in kind. Presence means the proactive turn intentionally establishes
reply context.

It is not a generic metadata bag. In particular:

- domain ownership and correlation belong in `management`;
- fire-specific provider data belongs in `source_event`;
- live offer decision state belongs in `CURRENT_STATE`;
- executable policy belongs in `instructions`.

Grounding accepts only scalar data (`str` / `int` / `float` / `bool`), ignores
nested values, and has no separate item or character cap — ordinary prompt
budgeting applies. It is not executable policy. It is eligible only for the
next two user turns after authoritative delivery (settlement-gated), allowing
one intervening turn without creating durable pending state. It never enters
routing text or embeddings; capability carryover remains a separate handoff
restored from conversation `routed_tools` after a same-node reconnect.

### Trigger data boundaries

| Data | Field | Model-visible | Example |
| :--- | :--- | :--- | :--- |
| Static action wording/policy | `message`, `instructions` | yes | "How did your Reading habit go?"; conditional delivery criteria |
| Bounded reply frame | `reply_grounding` | yes, at fire time and for up to two reply turns | `habit_id`, `habit_name`, `checkin_kind` |
| Concrete fire payload | `TriggerInstance.source_event` | selected projection | calendar/email item; background `task_id` / `input_id` |
| Internal ownership/correlation | `management` | no | `provider="habits"`, `resource_id=<plan_id>` |
| Live offer decision state | assembled `CURRENT_STATE` | yes, offer only | trigger age and active alarms/timers |

Do not duplicate source events into the action snapshot. Do not put internal
plan ids in reply grounding. Background task identifiers remain in `source_event`;
approval turns explicitly project the resource references the model needs.

## Delivery routing (`DeliveryPlan`)

`DeliveryPlan` holds **physical routing** only: `channel`, `target`, optional
`fallback`, and future fire-time resolved endpoints. The legacy `mode` field
(`announce` / `silent`) is removed; use `TriggerAction.decision` instead.

| Author intent (`scheduler.*`) | Stored target | Fire-time behavior |
| :--- | :--- | :--- |
| `deliver_to="anywhere"` | none | Route to last-active speaker |
| `deliver_to="here"` | `node_id` | Pin to originating node |
| `deliver_to="<room>"` | `location_ref` | Pin to bound room speaker |
| Wake alarm + room target | `location_ref` + `fallback=follow_me_if_target_unavailable` | Try room first; if offline, route to last-active speaker |
| Normal reminder + room target | `location_ref`, `fallback=none` | Strict room only; offline → `awaiting_delivery` |

`get_alerts()` defaults to all pending scheduled inventory, including silent
instructions from `defer()`. Named product filters (`reminder`, `timer`,
`alarm`) are available through `kind`; find other scheduled work by
`query=`/`message=` over message+instructions. Pass `status="awaiting_delivery"`
when diagnosing delivery backlog or retries.

**Discovery split:** upcoming time-based work (including one-shot defers and
recurring schedules, even when the user says "automation") →
`scheduler.get_alerts`. Cross-domain configured behavior (rules, protocols,
habit check-ins, quiet windows, disabled duplicates) → `setups.find` /
`setups.get`. Results identify `setup_type`, `managed_by`, `supported_actions`,
exact downstream IDs, and the owning `edit_tool`. Use `resource_ref` or a
natural query for common pause/resume operations — `setups.pause` applies to
every pause-capable match and keeps `status=paused` (omit `until` for
indefinite). `delete` stays singular and fail-closed. Schedule or structural
edits stay in the owning tool
(`scheduler.replace_alert`, `automations.update_rule`, `habits.*`,
`attention.*`, `protocol.*`). `activity.why_last_fire` remains the history path
for "why did that fire?". Permanent removal of time-based duplicates →
`setups.delete`. A named external automation can also be removed with
`automations.delete_rule`. Domain tools that own a verb resolve
unique names, `resource_ref`, or ids via `resolve_managed_setup`; `edit_tool`
promotion stays lookup-then-edit only.
Pending one-shot work can appear in broad `setups.find` results, but explicit
upcoming, cancel, snooze, and reschedule requests should use `get_alerts`.

Recurring series materialize at most one future `pending` occurrence per
`rule_id`. Failed occurrences may sit in `awaiting_delivery`, but retries
collapse to the latest row per series and older siblings expire on settlement.
Wake alarms also carry a bounded freshness TTL so missed days do not replay
forever.

## External / push origins

`TriggerOrigin.kind="external"` matches realtime provider events (Composio,
Calendar push, trusted agent systems). Authoring is `automations.create_rule`
after `list_available_triggers(source)`: copy the exact `(source, event)` pair
and advertised `condition_fields` into `field`/`op`/`value`.
Unknown events, fields, operators, or
provider-configured Composio triggers fail closed before persistence. Identity
filters belong in those catalog fields, not `instructions`. Semantic
policy that cannot be expressed as a field filter belongs in
`action.instructions`. Additional AND filters use `update_rule`. Delivery requires **External triggers** (Host Availability /
Funnel) or contributor `EXTERNAL_INGRESS_BASE_URL`. Without public ingress,
polling watchers remain the backstop where available.

Canonical trusted agents POST `POST /api/v1/webhooks/external/{source}` with a
per-source bearer credential (`event_id`, `event_type`, `occurred_at`,
`payload`). Third-party providers still need adapter-owned signature verification.

## Freshness (`FreshnessPolicy`)

Freshness answers **when the trigger is no longer useful**, not when it was
scheduled. Keep it tied to the user-facing claim the trigger would make.

- `stale_if_source_event_started` is for calendar rules that are only useful
  before the source event starts, such as "starts in 5 minutes" reminders.
  `automations.create_rule` sets it only for calendar
  `offset < 0`.
- Do not set source-event staleness for `offset=0` or after-start rules. At-start
  automations are usually event-time actions ("mute during this meeting", "run
  the standup protocol") and remain useful after the nominal start.
- `calendar_event_started` is a hard stale reason: it expires stale pre-start
  work and is not force-delivered, even when `on_expiry="force_deliver"`.

## Runtime paths

At fire time `resolve_trigger_delivery(..., decision=)` maps:

| `decision` | Agent execution | Presentation |
| :--- | :--- | :--- |
| `tell` | user-facing | `always` |
| `offer` | headless first | `if_content` |
| `act` | headless | `never` |

`AttentionPolicy.level` is applied after presentation is resolved: `quiet` admits
`urgent`+, `paused` admits nothing.

Anticipated calendar/external rules arm `call_later` timers in
`AutomationService._pending`. Those timers may retain a schedule-time rule
snapshot for prefetch, but dispatch reloads the live `trigger_rules` document
before creating the `TriggerInstance`. So `automations.update_rule` changes to
`action` / `instructions` apply to already-armed fires without waiting for
`fire_time` to change or a process restart.

Offer triggers receive live state from `core.triggers.offer_context` (trigger
age, interruptive commitments). Semantic defers park the instance with
`next_retry_at`; `NO_REPLY` settles as `suppressed` with an auditable reason.

## Turn context axes (runtime)

Proactive turns have separate context axes. Dialogue history may carry only the
immediate-reply frame; durable decision state is read at fire time.

| Axis | Where it lives | Use |
| :--- | :--- | :--- |
| Dialogue | `conversations` via `HistoryPolicy` (`core/turns/history.py`) | Recent node-scoped turns for pronouns, corrections, and same-session follow-ups. Bounded by `CONVERSATION_SESSION_INACTIVITY_MINUTES` (default 2h) or an explicit `reset_conversation_window`. |
| Durable decision state | trigger snapshots, freshness, attention, and assembled `current_state` | Pending alarms/timers/reminders and trigger age for `offer` evaluate turns. Injected as `CURRENT_STATE`, not transcript replay. |
| Long-tail recall | `recall()` / memory tools | Pull-based only when the user asks or a turn explicitly needs older history. |
| Prompt surface | `PromptBuilder` + `SystemTurnContext` | `tell` gets imperative alert framing; `offer` gets decision framing (`DEFER` / `DEFER_UNTIL` / `NO_REPLY`). |
| Tool routing | `routing_hint` on system turns | Routes from a bounded combination of `message`, `instructions`, and protocol context. It does not route from `reply_grounding`, `source_event`, rendered output, or `CURRENT_STATE`. Offer evaluates may use injected `ACTIVE_COMMITMENTS` for timing without mandatory scheduler lookup. |
| User follow-up routing | `ToolRouter` + delivered-route handoff | When proactive content reaches the user, plugins already selected for that system turn remain available for exactly one user turn. Conversation metadata restores the same handoff after a same-node reconnect. The user transcript still routes normally. |

Trigger authors do not declare follow-up tools or routing domains. The delivered
assistant text remains the conversational referent; optional `reply_grounding`
preserves semantic context for its first reply, while routing reuses the
capabilities selected before delivery. Grounding is not embedded into routing.
The live handoff is connection-scoped; the same settled assistant row can restore
it after a same-node reconnect. Silent, suppressed, deferred, no-audio, and
TTS-failed deliveries create neither handoff. Trigger settlement remains
authoritative: replay occurs only when the instance actually
transitions to `delivered`.

## Authoring surfaces

| Surface | Best use | Mapping |
| :--- | :--- | :--- |
| `scheduler.remind` | Time-based reminders and live briefings. | `decision` default `tell`; pass `offer` for conditional reminders. `instructions` for fire-time criteria. |
| `scheduler.defer` | Time-based side-effect work. | `decision` default `act`; `offer` for "check later and tell me if …". |
| `scheduler.add_timer` | Countdown timers. | `tell` + `urgent` + timer sound. |
| `scheduler.add_alarm` | Wake alarms. | `tell` + `critical` + `requires_ack` + alarm sound. |
| `habits.schedule_habit_checkin` | Habit-owned prompts and reviews. | `decision="offer"` + `instructions`. |
| `automations.create_rule` | External-event rules. Discover with `list_available_triggers`, then persist directly. | Explicit `decision` (`tell`, `offer`, `act`). |

## Authoring examples

| User intent | `decision` |
| :--- | :--- |
| Plain reminder | `tell` |
| Turn off lights later | `act` |
| Turn off lights and let me know when done | `tell` |
| Check X and tell me if Y | `offer` |
| Tell me only if this email is important | `offer` (word "tell" does not override) |
| Tell me when the meeting starts | `tell` |
| Habit check-in | `offer` |
| Event alert | `tell` |
| Semantic event triage | `offer` |
| Background task completion | `tell` / `offer` / `act` from task value |

## Storage Invariant

Persist only the current shape. Trigger rows must use `action.decision`,
`action.instructions`, optional `action.content_type` / `action.reply_grounding`,
`management` for ownership, `source_event` for fire payloads, and routing-only
`delivery`. Old trigger fields are invalid data and should be fixed in the DB,
not handled by runtime compatibility code.

## Related docs

- `ARCHITECTURE.md` — system overview
- `SYSTEM_STATES.md` — instance lifecycle and settlement
- `CORE_TOOLS.md` — tool-level parameters
- `docs/proposals/built/ATTENTION_GATE_AXES.md` — priority vs decision vs routing
