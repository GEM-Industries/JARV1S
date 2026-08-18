"""Deterministic TriggerCondition helpers."""

from __future__ import annotations

from typing import Any

from core.triggers.models import TriggerCondition

FIELD_CONDITION_KIND = "field"


def field_condition(field: str, op: str, value: str) -> TriggerCondition:
    return TriggerCondition(
        kind=FIELD_CONDITION_KIND,
        parameters={"field": field, "op": op, "value": value},
    )


def field_conditions_from_dicts(conditions: list[dict[str, Any]] | None) -> list[TriggerCondition]:
    return [
        field_condition(
            str(condition["field"]),
            str(condition["op"]),
            str(condition["value"]),
        )
        for condition in conditions or []
    ]


def field_condition_dicts(conditions: list[TriggerCondition]) -> list[dict[str, str]]:
    return [
        {
            "field": str(condition.parameters["field"]),
            "op": str(condition.parameters["op"]),
            "value": str(condition.parameters["value"]),
        }
        for condition in conditions
        if condition.kind == FIELD_CONDITION_KIND
    ]


def evaluate_conditions(conditions: list[TriggerCondition], item: dict[str, Any]) -> bool:
    """Evaluate AND-ed TriggerCondition filters against a watcher item."""
    for condition in conditions:
        if condition.kind != FIELD_CONDITION_KIND:
            return False
        params = condition.parameters
        raw = item.get(str(params["field"]))
        op = str(params["op"])

        if op in ("greater_than", "less_than"):
            try:
                current = float(raw) if raw is not None else 0.0
                target = float(params["value"])
            except (ValueError, TypeError):
                return False
            ok = current > target if op == "greater_than" else current < target
        else:
            if isinstance(raw, bool):
                value = "true" if raw else "false"
            elif isinstance(raw, list):
                value = ", ".join(str(part).lower() for part in raw)
            else:
                value = str(raw if raw is not None else "").lower()
            target = str(params["value"]).lower()
            if op == "contains":
                ok = target in value
            elif op == "not_contains":
                ok = target not in value
            elif op == "equals":
                ok = value == target
            elif op == "not_equals":
                ok = value != target
            else:
                ok = False

        if not ok:
            return False
    return True
