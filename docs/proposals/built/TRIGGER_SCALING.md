# Trigger Scaling: Dual-Ingestion Automation

**Status:** Phase 1 Built / Phase 2 Built  
**Date:** 2026-03-07  
**Depends on:** [INTEGRATION_SETUP.md](./INTEGRATION_SETUP.md) (Composio Gateway, MCP Auto-Bridge)

---

## Problem

The Automation Engine (Phase 1) only supports **polling**: a `Watcher` calls an API every 60 seconds and the `AutomationService` computes future fire times. This works for anticipated events ("5 minutes before my meeting") but is architecturally wrong for reactive events ("someone @mentioned me on Slack") where the event is unknowable until it occurs.

Forcing reactive events into polling watchers creates three problems:

1. **Latency** — 30s average delay (half the poll interval). Unacceptable for chat mentions or PR review requests.
2. **Waste** — 98.5% of polling requests return no new data (Zapier measured this). With 10 integrations polling at 60s, that's 14,400 empty API calls per day.
3. **Boilerplate** — a custom `Watcher` class per integration, even when the integration already pushes events.

The goal: JARV1S should be able to watch for events from any connected integration — poll-based or push-based — without writing a custom watcher for each one.

---

## Original Baseline

### Polling Path

```
CalendarWatcher.poll() → AutomationService._tick()
→ evaluate_conditions() → compute_fire_time()
→ call_later timer → TriggerInstance + TRIGGER_DUE at precise moment
```

The `Watcher` protocol is minimal (`source: str`, `async def poll() -> list[dict]`), auto-discovered from `services/watchers/`, and evaluated by `AutomationService` every 60 seconds. Fire-time computation, dedup, and trigger delivery via `TriggerInstance` / `TRIGGER_DUE` all work correctly.

### Push Path

Implemented in two layers: Composio webhooks feed `AutomationService.on_push_event()`, and bespoke push adapters accelerate Google Calendar and Gmail while poll remains the backstop.

### Composio Gateway

`ComposioGateway` handles Connect Links, OAuth callbacks, tool hot-reload, webhook subscription lifecycle, and trigger registration for auto-bridged integrations.

---

## Two Trigger Patterns, One Rule Engine

| Pattern | When | Transport | Example |
|---|---|---|---|
| **Anticipated** | Fire time is computable in advance from external state | Polling | "5 min before my meeting" |
| **Reactive** | Event is unknowable until it happens | Webhooks / push | "When someone @mentions me on Slack" |
| **Hybrid** | Push notifies of state change; polling materializes fire times | Both | "Calendar event moved" → re-poll → reschedule timer |

Both patterns evaluate the same rules, apply the same conditions/suppression/dedup, and dispatch through the same trigger substrate. The only difference is how data enters the system.

---

## Design

### Canonical Event Model

Both paths normalize data into one shape before the rule engine sees it:

```python
class TriggerEvent(BaseModel):
    source: str                       # "calendar", "slack", "github"
    event_type: str                   # "event_starting", "new_message", "review_requested"
    event_id: str                     # stable ID for dedup (provider event ID or deterministic hash)
    occurred_at: datetime             # when the event happened (or will happen)
    provider: str                     # "google", "composio", "homeassistant"
    payload: dict                     # full event data — conditions evaluate against this
    raw_event_type: Optional[str]     # original provider slug (e.g. "SLACK_NEW_MESSAGE") for debugging
```

The poll path wraps watcher items into `TriggerEvent` before evaluation. The push path normalizes inbound webhooks into `TriggerEvent` on arrival. `AutomationService` consumes one shape regardless of origin.

`raw_event_type` preserves the original provider slug before normalization — essential for debugging when a rule doesn't fire as expected. Without it, you can't tell whether the normalizer mapped the slug wrong or the rule conditions didn't match.

Fields deliberately omitted vs enterprise patterns: `connected_account_id` (single-user), `user_id` (single-user, derive from `settings.DEFAULT_USER_ID`), `trace_id` (add when observability warrants it), `ingestion_mode` (log at the ingestion point, not a model field — no consumer branches on it).

### AutomationService Changes

One new method — `on_push_event(event: TriggerEvent)`:

1. Load enabled external-origin `TriggerRule`s where `origin.source == event.source`.
2. Run deterministic `TriggerCondition` evaluation against `event.payload`.
3. Check suppression (`event_id in rule.suppressed_event_ids`).
4. Check dedup (`(rule_id, event_id) in _fired`).
5. Fire immediately — no `compute_fire_time`, no `call_later`. The event is happening **now**.

The existing `_tick()` loop is unchanged. It wraps watcher items in `TriggerEvent` internally for consistency but the poll → timer → fire pipeline stays identical.

### Rule Model Extension

`TriggerOrigin.source` works for both poll and push paths. A rule with `origin.source="slack"` matches events from any Slack trigger. `TriggerOrigin.event` is the provider-normalized event type like `"starting"`, `"new_message"`, `"commit"`, or `"review_requested"`; do not assume it only means temporal lifecycle events.

No standalone automation rule schema exists anymore. External automations are `TriggerRule(origin.kind="external")`; field-level filters are `TriggerCondition(kind="field", parameters={...})`.

**Normalizer best practice for structured fields:** when a provider payload contains structured data (e.g., Slack mentions as user ID arrays, DM vs channel flags), the normalizer should surface these as dedicated `payload` fields (`mentions_user`, `mentioned_users`, `is_direct_message`) rather than forcing rules to pattern-match on raw text. A rule like `{"field": "mentions_user", "op": "equals", "value": "true"}` is stable across provider formatting changes, whereas `{"field": "text", "op": "contains", "value": "@geoff"}` breaks if the provider changes mention formatting.

### Webhook Ingestion Endpoint

One FastAPI route handles all Composio trigger webhooks:

```
POST /api/v1/webhooks/composio
```

Responsibilities:
- **Verify HMAC signature** — Composio provides a `secret` at webhook subscription creation time. Constant-time comparison via `hmac.compare_digest`. Reject unsigned or invalid payloads with 401.
- **Persist before ACK** — insert into the durable `inbound_events` inbox (unique `idempotency_key`) **before** returning 2xx. Duplicate key → 200 idempotent duplicate. Mongo failure → 503 so the provider retries. A process crash after ACK cannot silently drop an event.
- **Leased worker** — after ACK, a background worker atomically leases `pending`/`retry` rows (reclaims expired leases after crashes), normalizes, and dispatches. Transient failures use bounded exponential backoff; exhausted rows move to `dead_letter` and are replayable from Activity.
- **Normalize** — extract `trigger_slug`, `data`, `metadata` from the Composio V3 payload. Map `trigger_slug` → `(source, event_type)`. Build `TriggerEvent`.
- **Dedup** — prefer `webhook-id`, else `metadata.log_id`, as the inbox idempotency key. Downstream `automation_fired` / `trigger_instances.dedup_key` remain side-effect guards. Webhooks are at-least-once by nature — duplicate delivery is normal, not exceptional.
- **Dispatch** — call `automation_service.on_push_event(event)`.

No separate message broker is required at personal-assistant scale. The durable inbox replaces ACK-before-persist `BackgroundTasks` and short-lived `webhook_payloads` / `webhook_dedup` collections. Retention is ~7 days for diagnosis/replay.

### Composio Trigger Lifecycle

Extend `ComposioGateway` with trigger lifecycle methods. The gateway's boundary is: it owns **Composio API lifecycle** (auth, tools, triggers). It does **not** own automation rule evaluation or provider-independent event semantics — those belong to `AutomationService` and the normalizer layer respectively.

- **`ensure_webhook_subscription()`** — called once at startup. `POST /api/v3/webhook_subscriptions` with the JARV1S callback URL. Stores the HMAC secret. Idempotent — checks for existing subscription first.
- **`register_trigger(app_name, trigger_slug, config)`** — enables a trigger instance for the user's connected account. `POST /api/v3/trigger_instances/{slug}/upsert`. Called either:
  - Automatically in `on_app_connected` for well-known trigger types (declared in `mcp_servers.yaml`).
  - On-demand via an LLM-facing tool: "Notify me when someone mentions me on Slack."
- **`list_trigger_types(app_name)`** — `GET /api/v3/triggers_types?toolkit_slug={app}`. Returns available trigger types for the LLM to present options.
- **`disable_trigger(trigger_id)`** — disables a trigger instance when a rule is deleted or the app is disconnected.

### Config Extension

`mcp_servers.yaml` gains an optional `triggers` list per Composio app:

```yaml
servers:
  - name: slack
    type: composio
    triggers:
      - SLACK_NEW_MESSAGE
    utterances:
      - "slack message"
      - "mentioned on slack"

  - name: github
    type: composio
    triggers:
      - GITHUB_COMMIT_EVENT
      - GITHUB_PULL_REQUEST_EVENT
```

On `on_app_connected`, the gateway reads these and registers trigger instances automatically.

### Trigger-Slug-to-Source Mapping

Composio trigger slugs like `SLACK_NEW_MESSAGE` need to map to the `(source, event_type)` pair that rules match against. A static mapping covers common cases:

```python
_TRIGGER_MAP: dict[str, tuple[str, str]] = {
    "SLACK_NEW_MESSAGE":          ("slack", "new_message"),
    "GITHUB_COMMIT_EVENT":        ("github", "commit"),
    "GITHUB_PULL_REQUEST_EVENT":  ("github", "pull_request"),
    "GMAIL_NEW_EMAIL":            ("gmail", "new_email"),
}
```

Unknown slugs fall back to deriving source from the toolkit name and event_type from the slug suffix. **Fallback mappings log at WARNING level** with the derived `(source, event_type)` so they're visible and reviewable — provider slug naming conventions are not a stable semantic contract, and silent derivation can cause subtle rule-matching bugs.

---

## Calendar: Hybrid Path (Phase 2)

Google Calendar supports push notifications via its Watch API. When events are created, modified, or deleted, Google sends a lightweight notification (no event details — just "something changed on calendar X").

This enables a hybrid approach:
- **Push** wakes up the poll loop immediately on calendar changes, instead of waiting for the next 60s tick.
- **Poll** fetches changed events using incremental sync (`syncToken`) rather than re-fetching the full 24h window. Google Calendar's sync API returns only events modified since the last sync, reducing payload size and API quota usage.
- Result: near-instant rescheduling when meetings move, with minimal API cost, without fundamentally changing the timer pipeline.

Implementation: register a Calendar Watch channel for each tracked calendar. On push notification, trigger an out-of-cycle `_tick()` for the calendar source only. The existing reconciliation logic (reschedule on fire_time change, cancel orphaned timers) handles the rest.

This is additive — the current 60s polling continues as a safety net.

**Sync token and watch channel lifecycle:**
- Persist `syncToken` per calendar after each sync response. Use it on subsequent fetches to get only changed events.
- On HTTP 410 or invalid/expired `syncToken`, clear local sync state and perform a full resync. This is Google's documented recovery path.
- Persist watch channel `resourceId`, `channelId`, and `expiration` from the watch response. Renew based on the provider-returned `expiration` timestamp — do not hardcode a TTL assumption.
- If channel renewal fails, fall back to poll-only until the next successful renewal attempt.

---

## When You Still Need a Bespoke Watcher

Composio triggers and the push path cover most convenience SaaS integrations. Bespoke watchers remain correct when:

- **Privacy-sensitive**: calendar, email, smart home data should not route through third-party servers.
- **Richer semantics needed**: the upstream trigger is too coarse (e.g., "any Slack message" when you need "message in thread I'm participating in with sentiment analysis").
- **Local-only sources**: Home Assistant state changes, local file watchers, device sensors.
- **Timing precision required**: fire times computed from event data need the poll → timer pipeline, not instant dispatch.

The decision tree:

```
Is it a Composio-backed convenience integration?
  Yes → Use Composio Triggers (push). No watcher needed.
  No  → Is the trigger reactive?
          Yes → Write a webhook endpoint + normalizer for that provider.
          No  → Is the trigger anticipated / needs fire-time computation?
                  Yes → Write a polling Watcher.
```

---

## What NOT to Build

| Approach | Why skip |
|---|---|
| **Per-integration polling watchers for SaaS tools** | Composio Triggers handles this. Don't write a `SlackWatcher` that polls every 60s. |
| **`PollSource` / `PushSource` protocol hierarchy** | Over-abstraction for a single-user system. The existing `Watcher` protocol handles polling. Push events arrive through HTTP endpoints. They share `TriggerEvent`, not a base class. |
| **Message queue / Redis for webhook processing** | Durable `inbound_events` inbox + leased worker is enough at personal-assistant scale. Do not add Celery/Redis streams for ~50–100 webhook events/day. |
| **Generic polling watcher factory** | Auth, pagination, cursor state, and rate limits vary too much per API. Custom watchers are appropriate for the few that need polling. |
| **WebSocket-based watchers** | WebSockets are for bidirectional streaming, not server-to-server event push. Webhooks are the right transport for SaaS triggers. If a future source requires a long-lived connection, it's just another push adapter producing `TriggerEvent`. |

---

## Implementation

### Phase 1: Push Trigger Path + Composio Triggers

- `TriggerEvent` model in `services/automation.py` (with `raw_event_type`)
- `AutomationService.on_push_event()` — immediate rule evaluation, dedup, fire
- `POST /api/v1/webhooks/composio` — HMAC verify → persist `inbound_events` → ACK → leased worker normalizes/dispatches
- `inbound_events` MongoDB collection (unique idempotency key, lease/retry/dead-letter, ~7d TTL)
- `ComposioGateway.ensure_webhook_subscription()` — reconciles callback URL + persists HMAC secret in CredentialStore
- `ComposioGateway.register_trigger()` / `disable_trigger()` / `list_trigger_types()`
- `_TRIGGER_MAP` with explicit mappings; fallback derivation logs at WARNING
- `mcp_servers.yaml` `triggers` field support
- Auto-register triggers in `on_app_connected` for configured apps
- LLM-facing tools: `enable_trigger`, `list_available_triggers`, `disable_trigger`
- Wrap existing poll-path items in `TriggerEvent` internally for consistency

### Phase 2: PushAdapter Layer — Built ✓

**Architecture: push accelerates, poll backstops.** Both paths produce `TriggerEvent`s flowing through the same rule evaluation, dedup (`_fired`), and dispatch. The AI and automation rules never know which transport delivered the event.

**New files:**
- `services/push/__init__.py` — `PushAdapter` Protocol + `PushChannel` dataclass
- `services/push/registry.py` — `PushRegistry`: auto-discovery, registry-owned lifecycle (renewal timers, MongoDB persistence, restart recovery), dispatch routing
- `services/push/calendar.py` — `CalendarPushAdapter`: per-calendar watch channels, signal push → `kick_source()` out-of-cycle poll
- `api/routes/push.py` — generic `POST /api/v1/push/{source}` webhook route; adapter-owned verification, durable inbox enqueue before ACK

**Key design decisions:**
- **Registry-owned lifecycle:** adapters return `PushChannel` state; registry owns timers, persistence, retry. Adapters are stateless and testable.
- **Per-resource state:** `PushChannel` keyed by `(source, resource_id)` — supports multiple watch channels per source (e.g. multiple calendars).
- **Verification before ACK:** `adapter.verify()` runs synchronously in the request handler so invalid requests are rejected immediately.
- **No syncToken for Calendar:** incompatible with CalendarWatcher's rolling `timeMin/timeMax` window. Push triggers an early full re-poll via `kick_source()` instead.
- **Durable inbox:** push notifications share `inbound_events` with Composio/external webhooks (persist before 2xx, leased retry, dead-letter replay).
- **`_evaluate_source()` extraction:** `AutomationService._tick()` and `kick_source()` share the same evaluation path with no duplication.

**Gmail:** Pub/Sub push is disabled until the endpoint verifies Google-signed push JWTs and audience. The existing polling watcher remains the production fallback.

**Validation checklist (before marking production-ready):**
- [ ] Restart recovery: channels survive server restart with correct renewal timers
- [ ] Renewal drift: channels renewed >1h before expiry without gaps
- [ ] Duplicate delivery: same event via push + poll produces one `TriggerInstance` / `TRIGGER_DUE`
- [ ] Cursor invalidation: expired Gmail historyId triggers full resync correctly
- [ ] Calendar multi-calendar: separate watch channel per calendar with independent tokens

### Phase 3: Trigger Intelligence

- LLM-assisted condition authoring: "notify me about important Slack messages" → LLM generates condition set
- Cross-source rules: "when I have a meeting starting AND someone mentions me on Slack, prioritize the meeting notification"
- Adaptive trigger tuning: learn which triggers the user consistently dismisses and suggest condition refinements

---

## Tradeoffs

**Composio dependency for push triggers:**
- Pro: one webhook endpoint covers 500+ integrations. No per-service webhook setup.
- Con: Composio controls trigger semantics, delivery reliability, and pricing. Not all desired event types will exist as native triggers — some require downstream filtering on `payload` fields (e.g., filtering Slack messages for @mentions).
- Mitigation: Composio is the provider adapter, not the abstraction boundary. `TriggerEvent` is JARV1S's contract. Swapping Composio for direct webhooks later requires only a new normalizer, not a rule engine rewrite.

**No formal `PushSource` protocol:**
- Pro: less abstraction, faster to build. One webhook endpoint + one normalize function per provider.
- Con: if you add 5+ direct webhook providers, the pattern becomes repetitive enough to formalize.
- Mitigation: extract a protocol when the third direct webhook provider appears, not before. Each webhook provider has fundamentally different verification (HMAC key vs channel token vs signing secret) and different payload shapes — a shared Protocol adds indirection without reducing code until there's a real shared pattern. Convention over abstraction: `normalize_<provider>(payload) -> TriggerEvent`.

**Downstream filtering for coarse triggers:**
- Some Composio triggers deliver more events than the user wants (all Slack messages vs just @mentions). The rule engine's `conditions` field handles this — the Composio adapter normalizes the full payload, and conditions like `{"field": "text", "op": "contains", "value": "@geoff"}` narrow the scope.
- This means some rules will evaluate and reject frequently. For a single-user system the cost is negligible (in-memory dict lookups).
