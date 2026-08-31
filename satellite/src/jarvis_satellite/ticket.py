"""Exchange durable device tokens for short-lived WebSocket tickets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError

from .backend_url import api_base_from_backend_url
from .http import post_json

_RECONNECT_HINT = "On the Mac: Rooms → Reconnect."


class TicketAuthError(RuntimeError):
    """Device token was rejected; the speaker needs Host reconnect."""


@dataclass(frozen=True, slots=True)
class WsTicket:
    ticket: str
    expires_at: datetime


def mint_ws_ticket(backend_url: str, device_token: str, *, timeout_s: float = 10.0) -> WsTicket:
    url = f"{api_base_from_backend_url(backend_url)}/api/v1/device-auth/ws-ticket"
    try:
        body = post_json(url, {"device_token": device_token}, timeout_s=timeout_s)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise TicketAuthError(
                f"ws-ticket request failed (401). {_RECONNECT_HINT}"
            ) from exc
        raise RuntimeError(f"ws-ticket request failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ws-ticket request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("ws-ticket request failed: timed out") from exc

    ticket = str(body.get("ticket") or "").strip()
    if not ticket:
        raise RuntimeError("ws-ticket response missing ticket")
    expires_raw = body.get("expires_at")
    expires_at = (
        datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
        if expires_raw
        else datetime.now()
    )
    return WsTicket(ticket=ticket, expires_at=expires_at)
