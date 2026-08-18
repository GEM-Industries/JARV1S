"""Exchange durable device tokens for short-lived WebSocket tickets."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WsTicket:
    ticket: str
    expires_at: datetime


def _api_base_from_backend_url(backend_url: str) -> str:
    parts = urlsplit(backend_url)
    scheme = "https" if parts.scheme == "wss" else "http"
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def mint_ws_ticket(backend_url: str, device_token: str, *, timeout_s: float = 10.0) -> WsTicket:
    api_base = _api_base_from_backend_url(backend_url)
    url = f"{api_base}/api/v1/device-auth/ws-ticket"
    payload = json.dumps({"device_token": device_token}).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ws-ticket request failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"ws-ticket request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("ws-ticket request failed: timed out") from exc

    ticket = str(body.get("ticket") or "").strip()
    if not ticket:
        raise RuntimeError("ws-ticket response missing ticket")
    expires_raw = body.get("expires_at")
    expires_at = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00")) if expires_raw else datetime.now()
    return WsTicket(ticket=ticket, expires_at=expires_at)
