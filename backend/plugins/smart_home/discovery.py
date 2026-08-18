"""Fixed-candidate Home Assistant discovery for setup and connect."""

from __future__ import annotations

import asyncio

import httpx

from plugins.smart_home.config import resolve_ha_connection
from plugins.smart_home.ha_client import normalize_ha_url

DISCOVERY_TIMEOUT_S = 3.0

_FIXED_CANDIDATES = (
    "http://homeassistant.local:8123",
    "http://127.0.0.1:8123",
    "http://localhost:8123",
)


def _candidate_urls(extra: str | None = None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw:
            return
        try:
            url = normalize_ha_url(raw)
        except ValueError:
            return
        if url in seen:
            return
        seen.add(url)
        ordered.append(url)

    add(extra)
    for candidate in _FIXED_CANDIDATES:
        add(candidate)
    return ordered


async def _probe_url(client: httpx.AsyncClient, url: str) -> bool:
    try:
        resp = await client.get(f"{url}/api/")
    except httpx.RequestError:
        return False
    return resp.status_code in {200, 401}


async def discover_home_assistant(
    *,
    preferred_url: str | None = None,
    timeout_s: float = DISCOVERY_TIMEOUT_S,
) -> str | None:
    """Probe known local HA candidates concurrently and return the preferred hit.

    `preferred_url` is only for trusted callers (CLI / stored config). The public
    discover route does not accept arbitrary user URLs.
    """
    stored_url: str | None = None
    try:
        stored_url, _ = await resolve_ha_connection()
    except Exception:
        stored_url = None

    candidates = _candidate_urls(preferred_url or stored_url)
    if not candidates:
        return None

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        results = await asyncio.gather(*(_probe_url(client, url) for url in candidates))

    return next((url for url, found in zip(candidates, results) if found), None)
