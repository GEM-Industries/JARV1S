"""Shared delivery visibility constants."""

from core.triggers.vocabulary import HIDDEN_DELIVERY_TAGS, VISIBLE_DELIVERY_TAGS


VISIBLE_DELIVERIES: frozenset[str | None] = frozenset({None, *VISIBLE_DELIVERY_TAGS})
HIDDEN_DELIVERIES: frozenset[str] = frozenset(HIDDEN_DELIVERY_TAGS)
