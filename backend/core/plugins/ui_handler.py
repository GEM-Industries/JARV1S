from typing import Dict, Any, Optional, Tuple

from core.id import generate_id
from core.plugins.capabilities import CapabilityCall, reset_invocation_source, set_invocation_source
from core.plugins.dispatcher import dispatcher
from core.plugins.types import UIEnvelope


async def process_ui_action(
    plugin_name: str,
    tool_name: str,
    args: Dict[str, Any],
) -> Tuple[Any, Optional[UIEnvelope]]:
    """
    Execute a UI action through the capability dispatcher.

    UI clicks share validation/routing with the agent loop but do not open a turn ledger;
    foreground/background blocks own harness tracing.
    """
    fqn = f"{plugin_name}.{tool_name}"
    source_token = set_invocation_source("ui")
    try:
        outcome = await dispatcher.dispatch(
            CapabilityCall(
                capability=fqn,
                arguments=dict(args),
                call_id=generate_id("tcall-"),
            )
        )
    finally:
        reset_invocation_source(source_token)

    ui_update = outcome.ui_events[0] if outcome.ui_events else None
    if outcome.error is not None:
        if outcome.status.value == "not_executed":
            raise ValueError(outcome.error.message)
        return outcome.error.message, ui_update
    return outcome.data, ui_update
