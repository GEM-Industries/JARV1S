"""Small helpers for validating durable plugin state mutations."""

from typing import Any

from pydantic import BaseModel, ValidationError

from core.plugins.capabilities import CapabilityErrorDetail


def merge_model_patch(
    model: type[BaseModel],
    existing: dict[str, Any] | None,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Merge a partial patch with existing config and validate the full result."""
    return model.model_validate({**(existing or {}), **patch}).model_dump()


def validation_error_message(
    label: str,
    model: type[BaseModel],
    error: ValidationError,
) -> CapabilityErrorDetail:
    """Format a Pydantic ValidationError as an actionable capability error.

    Use when validating LLM-built nested dicts (extra=\"forbid\" schemas). Do not
    invent a broader tool_error helper — catch at the call site and return this.
    """
    issues = "; ".join(
        f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
        for issue in error.errors()
    )
    accepted = ", ".join(model.model_fields)
    return CapabilityErrorDetail(
        code="tool_error",
        message=f"Invalid {label}. Accepted fields: {accepted}. {issues}",
    )
