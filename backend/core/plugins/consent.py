"""
Shared consent/approval mechanism for JARV1S.

Any plugin can gate a destructive action behind user approval using require_consent().
The LLM-facing approve_pending / deny_pending tools remain on SystemPlugin as the
single entry-point, delegating here.

Flow:
  1. Plugin calls require_consent(description, detail, action)
  2. PendingInputWidget is pushed; pending_inputs stores visible state
  3. Tool returns a blocked CapabilityErrorDetail; the agent loop stops
  4. Voice/text yes/no against a live pending is resolved by the harness
  5. Widget clicks call resolve_pending_input(input_id); approve_pending/deny_pending remain a fallback

Headless/background contexts:
  Set _consent_resolver to a callable before running a headless agent. The resolver receives
  (description, detail) and returns True (execute) or False (skip). The default (None) always
  uses the pending-input widget flow above.

  Background dispatch installs a resolver for its execution context. mode="jarvis"
  now emits a durable pending approval row and waits for resolution. mode="code"
  remains SDK-isolated and does not call this in-process function.
"""

import contextvars
import logging
import inspect
from typing import Awaitable, Callable, TypeVar

from core.context import get_owner_id
from core.pending_inputs import (
    create_pending_input,
    get_latest_pending_input,
    resolve_pending_input,
)
from core.plugins.capabilities import (
    CapabilityErrorDetail,
    get_active_invocation_id,
    get_invocation_ledger,
)

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_SECS = 120

# When set, called instead of the interactive widget flow.
# Receives (description, detail) -> True (execute action) | False (skip action).
ConsentResolver = Callable[[str, str], bool | Awaitable[bool]]
_consent_resolver: contextvars.ContextVar[ConsentResolver | None] = contextvars.ContextVar(
    "_consent_resolver", default=None
)

T = TypeVar("T")

_AFFIRM_PHRASES = frozenset({"yes", "yeah", "yep", "yup", "go ahead", "do it"})
_DENY_PHRASES = frozenset({"no", "nope"})


def _approval_needed(description: str) -> CapabilityErrorDetail:
    message = f"Approval needed: {description} The action has not executed yet."
    return CapabilityErrorDetail(code="approval_needed", message=message)


def _skipped(description: str) -> CapabilityErrorDetail:
    return CapabilityErrorDetail(
        code="skipped",
        message=f"{description} was not approved in background mode.",
    )


async def require_consent(
    description: str,
    action: Callable[[], Awaitable[T]],
    detail: str = "",
) -> T | CapabilityErrorDetail:
    """
    Gate a destructive action behind user approval.

    Pushes a PendingInputWidget, stores the async callback, and returns a
    blocked outcome. The harness stops the agent loop and collects yes/no.

    When a consent resolver is set (via _consent_resolver contextvar), the resolver
    is called instead of the widget flow. Returning True executes the action immediately;
    returning False skips it with a blocked skipped outcome.

    Args:
        description: Plain-English summary shown on the approval widget and spoken by the LLM.
        action: Async callable that performs the actual operation when approved.
        detail: Optional technical detail shown behind the "show detail" toggle (e.g. shell command).
    """
    resolver = _consent_resolver.get(None)
    if resolver is not None:
        decision = resolver(description, detail)
        approved = await decision if inspect.isawaitable(decision) else decision
        ledger = get_invocation_ledger()
        if ledger is not None:
            ledger.annotate(
                get_active_invocation_id(),
                consent_decision="approved" if approved else "denied",
            )
        if approved:
            logger.debug("Consent resolver approved action: %s", description)
            return await action()
        logger.debug("Consent resolver skipped action: %s", description)
        return _skipped(description)

    doc = await create_pending_input(
        owner_id=get_owner_id(),
        prompt=description,
        detail=detail,
        source={"type": "foreground_turn"},
        callback=action,
        timeout_s=APPROVAL_TIMEOUT_SECS,
        publish="push_ui",
    )
    ledger = get_invocation_ledger()
    if ledger is not None:
        ledger.annotate(
            get_active_invocation_id(),
            pending_input_id=doc.get("input_id"),
            consent_decision="pending",
        )

    return _approval_needed(description)


def _confirmation_phrase(text: str) -> str:
    from core.voice.local_commands import normalize_local_command

    normalized = normalize_local_command(text)
    if normalized.endswith(" please"):
        return normalized[: -len(" please")].strip()
    return normalized


def _spoken_result(result: object) -> str:
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, str) and content:
        return content
    message = getattr(result, "message", None)
    if isinstance(message, str) and message:
        return message
    return str(result)


async def resolve_pending_from_utterance(owner_id: str, text: str) -> str | None:
    """Resolve a live pending approval from an exact yes/no utterance.

    Returns None when there is no pending input or the transcript is not a
    confirmation, so the caller can fall through to the normal turn.
    """
    pending = await get_latest_pending_input(owner_id)
    if not pending:
        return None
    phrase = _confirmation_phrase(text)
    if phrase in _AFFIRM_PHRASES:
        return _spoken_result(
            await resolve_pending_input(owner_id=owner_id, decision="approve")
        )
    if phrase in _DENY_PHRASES:
        return _spoken_result(
            await resolve_pending_input(owner_id=owner_id, decision="deny")
        )
    return None


async def execute_pending(owner_id: str) -> str:
    """
    Run the stored action callback for the owner's pending approval.
    Called by SystemPlugin.approve_pending() after the user confirms.
    """
    return await resolve_pending_input(owner_id=owner_id, decision="approve")


async def cancel_pending(owner_id: str) -> str:
    """
    Cancel the owner's pending approval and dismiss the widget.
    Called by SystemPlugin.deny_pending().
    """
    return await resolve_pending_input(owner_id=owner_id, decision="deny")
