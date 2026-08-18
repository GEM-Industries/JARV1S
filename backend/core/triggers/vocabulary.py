"""Shared trigger decision and delivery vocabulary."""

from __future__ import annotations

from typing import Literal, TypeAlias


DECISION_TELL: Literal["tell"] = "tell"
DECISION_OFFER: Literal["offer"] = "offer"
DECISION_ACT: Literal["act"] = "act"

TriggerDecision: TypeAlias = Literal["tell", "offer", "act"]

ContentType: TypeAlias = Literal["plain", "event", "task_result"]


# Runtime trace / delivery tags (outcomes, not authoring knobs).
DELIVERY_ANNOUNCE: Literal["announce"] = "announce"
DELIVERY_SILENT: Literal["silent"] = "silent"
TRACE_EVALUATE: Literal["evaluate"] = "evaluate"

TriggerDeliveryTag: TypeAlias = Literal["announce", "silent", "evaluate"]

TRACE_ANNOUNCE = DELIVERY_ANNOUNCE
TRACE_SILENT = DELIVERY_SILENT
TRACE_SUPPRESSED: Literal["suppressed"] = "suppressed"
TRACE_PREFETCHED: Literal["prefetched"] = "prefetched"

DeliveryTraceTag: TypeAlias = Literal[
    "announce",
    "silent",
    "evaluate",
    "suppressed",
    "prefetched",
]

VISIBLE_DELIVERY_TAGS: frozenset[DeliveryTraceTag] = frozenset({
    TRACE_ANNOUNCE,
    TRACE_EVALUATE,
    TRACE_PREFETCHED,
})
HIDDEN_DELIVERY_TAGS: frozenset[DeliveryTraceTag] = frozenset({
    TRACE_SILENT,
    TRACE_SUPPRESSED,
})


FAILURE_REASON_LABELS: dict[str, str] = {
    "delivery_ttl_expired": "Delivery window expired",
    "freshness_expired": "Freshness window expired",
    "calendar_event_started": "Event already started",
    "offer_no_reply": "Offer dropped after evaluation",
    "evaluate_no_reply": "Evaluation found nothing to deliver",
    "offer_deferred": "Offer deferred for retry",
    "no_session": "No active session",
    "no_audio_sent": "Nothing was delivered",
    "evaluation_failed": "Evaluation failed",
    "runtime_error": "Language model unavailable",
    "scheduler_recovery": "Recovered after restart",
}


def humanize_failure_reason(reason: str | None) -> str | None:
    """Map a failure-reason code to display copy; prettify unknown codes gracefully."""
    reason = (reason or "").strip()
    if not reason:
        return None
    return FAILURE_REASON_LABELS.get(reason) or reason.replace("_", " ").capitalize()
