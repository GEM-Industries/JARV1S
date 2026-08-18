# Trigger Delivery Attempts Future Proposal

## Status

Deferred. The current trigger pipeline should not include attempt records.

## Why This Is Deferred

`TriggerInstance` is enough while JARV1S has one primary user-facing delivery path:

- claim a due instance
- execute it through the voice/headless turn path
- mark the instance `completed`, `delivered`, `awaiting_delivery`, `failed`, `acknowledged`, `snoozed`, or `cancelled`

Attempt records become useful only when the system needs to reason about multiple user-facing delivery tries independently from the trigger instance itself. Adding them before that point creates schema and service surface area that looks authoritative but is not actually used by delivery.

## Reintroduce Attempts When

Bring back embedded `TriggerDeliveryAttempt` records when at least one of these is true:

- more than one user-facing channel is active, such as voice plus Telegram or WhatsApp
- delivery fallback is real, for example voice fails then web/push is tried
- retry policy needs per-try data, such as target, timestamp, status, error, or audio outcome
- preference learning needs delivery telemetry, for example which channel gets acknowledged fastest

## Proposed Shape

Attempts should stay embedded on `TriggerInstance` until there is a proven query need for a separate collection.

```python
class TriggerDeliveryAttempt(BaseModel):
    id: str
    instance_id: str
    owner_id: str
    channel: str
    target: str | None = None
    status: Literal["queued", "sending", "sent", "delivered", "acknowledged", "suppressed", "failed", "no_target"]
    turn_id: str | None = None
    response_id: str | None = None
    audio_sent: bool = False
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
```

`TriggerService` should expose only two methods at first:

- `append_attempt(instance_id, attempt)`
- `update_attempt(instance_id, attempt_id, ...)`

Do not add a `trigger_delivery_attempts` collection until attempts need independent pagination, retention, analytics, or cross-instance querying.

## Non-Goals

- Do not use attempts to replace the instance lifecycle.
- Do not add a planner/executor abstraction just to support attempts.
- Do not record attempts for internal state transitions that are not actual delivery tries.
