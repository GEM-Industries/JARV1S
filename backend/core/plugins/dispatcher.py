"""Capability dispatcher — single choke point for jarvis.* invocations."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from core.auth.error_handler import handle_integration_auth_error
from core.id import generate_id
from core.integrations import integrations
from core.plugins.capabilities import (
    CapabilityCall,
    CapabilityDefinition,
    CapabilityErrorDetail,
    CapabilityOutcome,
    InvocationRecord,
    InvocationStatus,
    get_capability_task_id,
    get_capability_turn_id,
    get_invocation_ledger,
    get_invocation_source,
    get_tool_call_id,
    redact_args_preview,
    reset_active_invocation_id,
    set_active_invocation_id,
    status_for_result,
)
from core.plugins.result import ToolResult
from core.plugins.serialization import tool_output_data
from core.plugins.types import UIEnvelope
from core.plugins.ui import push_ui


class CapabilityError(Exception):
    """Recoverable capability lookup/validation failure."""

    def __init__(self, message: str, *, capability: str | None = None):
        super().__init__(message)
        self.capability = capability


def _status_for_exception(exc: BaseException) -> tuple[InvocationStatus, str]:
    """Map invoke failures to ledger status. Timeouts/cancels are interruptions."""
    if isinstance(exc, asyncio.CancelledError):
        return InvocationStatus.INTERRUPTED, "CancelledError"
    if isinstance(exc, TimeoutError):
        return InvocationStatus.INTERRUPTED, "TimeoutError"
    if isinstance(exc, CapabilityError):
        return InvocationStatus.NOT_EXECUTED, "CapabilityError"
    return InvocationStatus.FAILED, type(exc).__name__


def _error_detail(code: str, message: str, *, details: dict[str, Any] | None = None) -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message, details=details or {})


class CapabilityDispatcher:
    """Resolve, validate, inject, invoke, normalize, and record capability calls."""

    def __init__(self, registry: Any | None = None) -> None:
        self._registry = registry

    @property
    def registry(self) -> Any:
        if self._registry is None:
            from core.plugins.registry import registry as global_registry

            return global_registry
        return self._registry

    def resolve(self, fqn: str) -> CapabilityDefinition:
        definition = self.registry.get_capability(fqn)
        if definition is None:
            raise CapabilityError(
                f"Unknown capability: jarvis.{fqn}",
                capability=fqn,
            )
        if not definition.enabled:
            raise CapabilityError(
                f"Capability disabled: jarvis.{fqn}",
                capability=fqn,
            )
        return definition

    def bind_arguments(
        self,
        definition: CapabilityDefinition,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind caller args against the LLM-visible signature."""
        try:
            bound = definition.visible_signature.bind(*args, **kwargs)
            bound.apply_defaults()
        except TypeError as exc:
            raise CapabilityError(
                f"{exc}\nCorrect signature:\n"
                f"jarvis.{definition.fqn}{definition.visible_signature_str}",
                capability=definition.fqn,
            ) from exc
        return dict(bound.arguments)

    def validate_arguments(
        self,
        definition: CapabilityDefinition,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate JSON arguments against the capability's input model."""
        if definition.input_model is not None:
            try:
                validated = definition.input_model.model_validate(arguments)
            except ValidationError as exc:
                raise CapabilityError(
                    f"Invalid arguments for jarvis.{definition.fqn}: {exc.errors()}\n"
                    f"Correct signature:\n"
                    f"jarvis.{definition.fqn}{definition.visible_signature_str}",
                    capability=definition.fqn,
                ) from exc
            return validated.model_dump()
        return self.bind_arguments(definition, (), arguments)

    async def dispatch(self, call: CapabilityCall) -> CapabilityOutcome:
        """Execute a resolved capability call and always return an outcome.

        ``CancelledError`` is recorded then re-raised so task cancellation
        still propagates.
        """
        ledger = get_invocation_ledger()
        active_token = None
        invocation_id: str | None = None
        call_id = call.call_id or generate_id("tcall-")
        fqn = call.capability
        arguments = dict(call.arguments or {})

        try:
            definition = self.resolve(fqn)
            bound_args = self.validate_arguments(definition, arguments)
        except CapabilityError as exc:
            record = self._open_record(
                ledger,
                capability=exc.capability or fqn,
                args_preview=redact_args_preview(arguments),
                call_id=call_id,
            )
            if record is not None:
                ledger.close(  # type: ignore[union-attr]
                    record.invocation_id,
                    InvocationStatus.NOT_EXECUTED,
                    error_type=type(exc).__name__,
                )
            return CapabilityOutcome(
                call_id=call_id,
                capability=exc.capability or fqn,
                status=InvocationStatus.NOT_EXECUTED,
                error=_error_detail("CapabilityError", str(exc)),
                invocation=record,
            )

        record = self._open_record(
            ledger,
            capability=definition.fqn,
            args_preview=redact_args_preview(arguments, bound_args=bound_args),
            call_id=call_id,
        )
        if record is not None:
            invocation_id = record.invocation_id
            active_token = set_active_invocation_id(invocation_id)

        try:
            injected = await self._inject(definition, bound_args)
            if not isinstance(injected, dict):
                return self._close_outcome(
                    call_id=call_id,
                    capability=definition.fqn,
                    ledger=ledger,
                    invocation_id=invocation_id,
                    record=record,
                    result=injected,
                )
            result = await definition.implementation(**injected)
        except BaseException as exc:
            sentinel = await self._auth_sentinel(definition, exc)
            if sentinel is not None:
                return self._close_outcome(
                    call_id=call_id,
                    capability=definition.fqn,
                    ledger=ledger,
                    invocation_id=invocation_id,
                    record=record,
                    result=sentinel,
                )
            status, error_type = _status_for_exception(exc)
            if ledger is not None and invocation_id is not None:
                ledger.close(invocation_id, status, error_type=error_type)
            outcome = CapabilityOutcome(
                call_id=call_id,
                capability=definition.fqn,
                status=status,
                error=_error_detail(error_type, str(exc) or error_type),
                invocation=record,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            return outcome
        else:
            return self._close_outcome(
                call_id=call_id,
                capability=definition.fqn,
                ledger=ledger,
                invocation_id=invocation_id,
                record=record,
                result=result,
            )
        finally:
            if active_token is not None:
                reset_active_invocation_id(active_token)

    async def invoke(self, fqn: str, *args: Any, **kwargs: Any) -> Any:
        """Python/test helper: dispatch a capability and return data or raise.

        Builds a ``CapabilityCall``, dispatches, then projects the outcome back
        to a data/exception shape.
        """
        try:
            definition = self.resolve(fqn)
            arguments = self.bind_arguments(definition, args, kwargs)
        except CapabilityError:
            arguments = dict(kwargs)
        call = CapabilityCall(
            capability=fqn,
            arguments=arguments,
            call_id=get_tool_call_id() or generate_id("tcall-"),
        )
        outcome = await self.dispatch(call)
        return self.project_outcome(outcome)

    @staticmethod
    def project_outcome(outcome: CapabilityOutcome) -> Any:
        """Project an outcome back to a data/exception shape for Python callers."""
        for envelope in outcome.ui_events:
            push_ui(envelope)
        if outcome.status == InvocationStatus.SUCCEEDED:
            return outcome.data
        if outcome.status in (InvocationStatus.BLOCKED, InvocationStatus.FAILED):
            if outcome.invocation is not None and outcome.invocation.error_type:
                message = outcome.error.message if outcome.error else outcome.status.value
                raise RuntimeError(message)
            if outcome.error is not None:
                return outcome.error
            raise RuntimeError(outcome.status.value)
        message = outcome.error.message if outcome.error else outcome.status.value
        if outcome.status == InvocationStatus.NOT_EXECUTED:
            raise CapabilityError(message, capability=outcome.capability)
        if outcome.status == InvocationStatus.INTERRUPTED:
            if outcome.error and outcome.error.code == "CancelledError":
                raise asyncio.CancelledError()
            raise TimeoutError(message)
        raise RuntimeError(message)

    def _open_record(
        self,
        ledger: Any,
        *,
        capability: str,
        args_preview: dict[str, Any],
        call_id: str,
    ) -> InvocationRecord | None:
        if ledger is None:
            return None
        return ledger.open(
            capability=capability,
            args_preview=args_preview,
            source=get_invocation_source(),
            tool_call_id=call_id,
            turn_id=get_capability_turn_id(),
            task_id=get_capability_task_id(),
        )

    def _close_outcome(
        self,
        *,
        call_id: str,
        capability: str,
        ledger: Any,
        invocation_id: str | None,
        record: InvocationRecord | None,
        result: Any,
    ) -> CapabilityOutcome:
        data, ui_events, status, error = _normalize_result(result)
        if ledger is not None and invocation_id is not None:
            ledger.close(invocation_id, status)
        return CapabilityOutcome(
            call_id=call_id,
            capability=capability,
            status=status,
            data=data,
            error=error,
            ui_events=ui_events,
            invocation=record,
        )

    async def _inject(
        self,
        definition: CapabilityDefinition,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | CapabilityErrorDetail:
        kwargs = dict(arguments)
        for param in definition.injected:
            if param in kwargs and kwargs[param] is not None:
                continue
            try:
                kwargs[param] = await integrations.get(param)
            except Exception as exc:
                sentinel = await handle_integration_auth_error(param, exc)
                if sentinel is not None:
                    return sentinel
                raise
        return kwargs

    async def _auth_sentinel(
        self,
        definition: CapabilityDefinition,
        exc: BaseException,
    ) -> CapabilityErrorDetail | None:
        if not isinstance(exc, Exception):
            return None
        for param in definition.injected:
            sentinel = await handle_integration_auth_error(param, exc)
            if sentinel is not None:
                return sentinel
        return None


def _normalize_result(
    result: Any,
) -> tuple[Any, list[UIEnvelope], InvocationStatus, CapabilityErrorDetail | None]:
    ui_events: list[UIEnvelope] = []
    if isinstance(result, ToolResult):
        ui_events = list(result.ui)
        result = result.content
    elif isinstance(result, UIEnvelope):
        ui_events = [result]

    if isinstance(result, CapabilityErrorDetail):
        return None, ui_events, status_for_result(result), result

    return tool_output_data(result), ui_events, InvocationStatus.SUCCEEDED, None


dispatcher = CapabilityDispatcher()
