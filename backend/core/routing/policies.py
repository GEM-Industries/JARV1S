"""Canonical ToolRouter policies.

Keep production and eval policy names here so benchmark reports and runtime
diagnostics refer to the same objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


BASELINE_THRESHOLD = 0.72
BASELINE_FALLBACK_THRESHOLD = 0.60
FALLBACK_TOP_K = 1
BASELINE_MAX_MATCHED = 2
DECAY_BONUS = 0.10
VOICE_MAX_MATCHED = 3
DEFAULT_SCHEMA_CHAR_BUDGET = 16_000
SYSTEM_SCHEMA_CHAR_BUDGET = 18_000


@dataclass(frozen=True)
class RoutingPolicy:
    """Configuration for one router variant."""

    name: str
    threshold: float = BASELINE_THRESHOLD
    fallback_threshold: float = BASELINE_FALLBACK_THRESHOLD
    fallback_top_k: int = FALLBACK_TOP_K
    max_matched: int = BASELINE_MAX_MATCHED
    segment_top_k: int = 1
    decay_bonus: float = DECAY_BONUS
    multi_intent: bool = False
    session_carryover: bool = False
    schema_char_budget: int | None = None
    max_segments: int = 6


BASELINE_POLICY = RoutingPolicy(name="baseline")
VOICE_POLICY = RoutingPolicy(
    name="budget_aware_multi_intent",
    threshold=0.74,
    fallback_threshold=0.70,
    max_matched=VOICE_MAX_MATCHED,
    segment_top_k=2,
    multi_intent=True,
    session_carryover=True,
    schema_char_budget=DEFAULT_SCHEMA_CHAR_BUDGET,
)
TEXT_POLICY = RoutingPolicy(
    name="text_budget_aware_multi_intent",
    max_matched=5,
    segment_top_k=2,
    multi_intent=True,
    session_carryover=True,
    schema_char_budget=DEFAULT_SCHEMA_CHAR_BUDGET,
)
SYSTEM_POLICY = RoutingPolicy(
    name="system_budget_aware_multi_intent",
    max_matched=5,
    segment_top_k=2,
    multi_intent=True,
    session_carryover=False,
    schema_char_budget=SYSTEM_SCHEMA_CHAR_BUDGET,
)


PRODUCTION_POLICIES = MappingProxyType({
    "baseline": BASELINE_POLICY,
    "voice_default": VOICE_POLICY,
    "text_default": TEXT_POLICY,
    "system_hint": SYSTEM_POLICY,
})

POLICY_ALIASES = MappingProxyType({
    "current": "baseline",
    "budget_aware_multi_intent": "voice_default",
    "text_budget_aware_multi_intent": "text_default",
    "system_budget_aware_multi_intent": "system_hint",
})


def resolve_policy(policy: RoutingPolicy | str | None) -> RoutingPolicy:
    """Resolve a caller-supplied policy without eval-only name collisions."""
    if policy is None:
        return VOICE_POLICY
    if isinstance(policy, RoutingPolicy):
        return policy
    key = POLICY_ALIASES.get(policy, policy)
    try:
        return PRODUCTION_POLICIES[key]
    except KeyError as exc:
        raise ValueError(f"Unknown routing policy: {policy}") from exc
