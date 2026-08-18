"""
Translate integration auth errors into blocked capability outcomes.

When a decorated tool's injected integration raises ``NeedsReauth`` or
``ScopeGapError``, we push an ``OAuthWidget`` to the user and return a
blocked ``reauth_needed`` error so the LLM can explain the situation
instead of failing the turn.
"""

from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.types import UIEnvelope, WidgetLayout, WidgetSize
from core.plugins.ui import push_ui


async def handle_integration_auth_error(
    param: str,
    exc: Exception,
) -> Optional[CapabilityErrorDetail]:
    """Return a blocked reauth outcome and push an OAuthWidget, or None for non-auth errors."""
    from core.auth.exceptions import ScopeGapError
    from core.integrations.manager import NeedsReauth

    auth_status: int | None = None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        auth_status = exc.response.status_code

    if not isinstance(exc, (ScopeGapError, NeedsReauth)) and auth_status is None:
        return None

    from core.integrations import integrations

    provider = integrations.resolve_oauth_provider(param, exc)

    if provider:
        data: dict[str, Any] = {"provider": provider}
        if isinstance(exc, ScopeGapError):
            data.update(missing_scopes=exc.missing_scopes)

        push_ui(UIEnvelope(
            widget_id=f"oauth-{provider}",
            component="OAuthWidget",
            data=data,
            layout=WidgetLayout(size=WidgetSize.WIDE, priority=100),
            expires_at=int(
                (datetime.now(timezone.utc).timestamp() + 1200) * 1000
            ),
        ))

    card_hint = (
        "A setup card has appeared — ask the user to complete it."
        if provider
        else "Call connect_integration to push a setup card."
    )

    if isinstance(exc, ScopeGapError):
        message = (
            f"{exc.provider} requires re-authorization — missing permissions: "
            f"{', '.join(exc.missing_scopes)}. {card_hint}"
        )
    elif auth_status is not None:
        target = provider or param
        message = (
            f"{target} authorization failed with HTTP {auth_status}. {card_hint}"
        )
    else:
        message = f"{exc.integration} requires re-authorization. {card_hint}"
    return CapabilityErrorDetail(code="reauth_needed", message=message)
