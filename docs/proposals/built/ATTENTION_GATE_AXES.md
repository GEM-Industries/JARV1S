# Attention Gate: Three Axes

**Status:** Consolidation **shipped**; future seams are **design-only** (documented here, not stubbed in code).

## The problem

The proactive-delivery gate conflated three orthogonal decisions onto one
`AttentionPolicy.level` enum (`passive | normal | urgent | critical`). At the
gate, `passive == normal` and `urgent == critical`, so four named levels
produced two behaviors, and one LLM entry point (automations `priority: 1–5`)
wasn't wired to the gate at all. That numeric automation priority has since been
removed in favor of `importance`. The reference model is Apple's
`UNNotificationInterruptionLevel`, but Apple is single-device and visual-first;
JARV1S is multi-room and voice-first, which changes what the gate needs.

## The model (shipped)

Three axes, kept separate:

1. **Priority** — `AttentionPolicy.level ∈ {normal, urgent, critical}`. The
   *only* input to the quiet/paused gate. Chosen by the LLM from one word
   (`importance`), reasoned from intent, not loudness.
2. **Reporting** — `TriggerAction.decision ∈ {tell, offer, act}`. Whether the
   user should hear from JARV1S after work runs. Chosen explicitly at authoring
   time (`tell` = always speak, `offer` = speak only if worth it, `act` = silent
   side effects). This replaces the old "may speak?" bit that used to live on delivery.
3. **Channel / routing** — `DeliveryPlan` (`channel`, `target`). Physical endpoints
   at fire time. "FYI / show-only" is `decision="act"`, not a priority tier.
4. **Presentation** — `sound` / `requires_ack`. Derived from the tier by
   presets; not a model knob.

Gate floors live in `core/triggers/priority.py` and are applied by
`core/triggers/delivery_policy.py`: `active` admits everything,
`quiet` admits `urgent`+, `paused` admits nothing.

## The three seams (design only; kept here, not reserved in code)

These are documented so the gate's growth path is clear. They are **not** stubbed
in code — adding a keyword-only param or a defaulted return field later is
non-breaking, so reserving them as inert code earned nothing. The intent lives
here until there's a consumer.

### 1. Defer is half the product → re-surfacing planner

When an item is deferred, the question "how does it come back?" is per-tier
(`normal→digest`, `urgent→escalate`, `critical→announce`). The **re-surfacing
planner** — batching a "while you were heads-down" digest of accumulated
`normal`s, escalating an `urgent` that sat unread — runs on
mode-change-to-active and lives *next to* the gate, not inside it. When built, it
recomputes resurface intent from the deferred instance's `attention_snapshot`;
the gate does not need to carry it.

### 2. Voice has higher disruption cost than visual → channel resolved at fire time

Apple's tiers were sized for visual banners. Speech in a quiet room commands the
whole auditory space. The intended growth: `normal` resolves to visual-first
when a screen is reachable (voice as fallback); `urgent`/`critical` resolve to
voice + visual + iOS push. Channel becomes a function of `priority ×
screen-availability × presence`, resolved system-side — the LLM still picks only
`importance`. This is where `DeliveryPlan` grows: keep `channel` / `target` as
author-time routing hints and add fire-time resolved endpoints ("which devices,
given presence"). iOS push is the
follows-you channel and its tiers map 1:1 (`normal→active`, `urgent→timeSensitive`,
`critical→critical`), so the priority axis needs no translation — only quiet/paused
gate the channel as a whole, and iOS Focus/DND gates within it.

### 3. The gate has a "when" axis but no "where" axis → presence at fire time

`critical` piercing `paused` is a *temporal* override. Multi-room is a *spatial*
one: a water-leak alert while the user sleeps in the bedroom should resolve to
the bedroom satellite, not the kitchen one where it originated. When satellites
exist the resolver takes a presence context carrying two endpoint classes — live
co-located nodes (from the connection registry / `PresenceIdentity`) and
registered push targets (iOS devices with last-seen, which are *not* live
connections). `presence.py` already distinguishes `connection_id` / `node_id` /
`location_ref`; the push-target registry is new.

## Deferred decisions

- **Critical pierces `paused`** only for **safety-class** triggers, and the
  class must come from `TriggerOrigin` (a smoke/water sensor, a panic phrase,
  the alarm preset) — never from a model-chosen `level`. This is Apple's
  entitlement gate: only trusted sources may override the user's "off". No
  genuine safety-class source exists today (System Pulse, timers, and alarms
  are not safety-class), so the override is intentionally **unwired**. Add the
  `TriggerOrigin` safety flag when home-sensor integrations land.
- **Retire automations `priority: 1–5`** in favor of the single `importance`
  word. **Done** — automations tools and the Operations definitions surface now
  use `importance: normal | urgent | critical`; gate semantics live in
  `core/triggers/priority.py`.
