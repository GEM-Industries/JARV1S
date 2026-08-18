"""Request/response control plane shared by the supervised loopback voice helpers.

The Apple Speech and Kokoro helpers expose the same control shape: one short-lived
WebSocket per request, a single JSON reply, and a per-launch token on every message.
Audio paths keep their own connections and do not go through here, so a slow
readiness probe can never block capture or synthesis.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json


async def request_helper(
    url: str,
    message_type: str,
    *,
    fields: dict[str, object],
    connect_timeout_s: float,
    reply_timeout_s: float,
) -> dict:
    """Send one control message to a helper and return its JSON reply."""
    websockets = importlib.import_module("websockets")
    ws = await asyncio.wait_for(websockets.connect(url), timeout=connect_timeout_s)
    try:
        await ws.send(json.dumps({"type": message_type, **fields}))
        raw = await asyncio.wait_for(ws.recv(), timeout=reply_timeout_s)
        if not isinstance(raw, str):
            raise ValueError(f"Invalid helper response from {url}")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid helper response from {url}")
        return payload
    finally:
        with contextlib.suppress(Exception):
            await ws.close()
