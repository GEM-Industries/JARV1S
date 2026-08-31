"""JSON HTTP helpers for satellite Host calls."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen


def post_json(url: str, payload: dict, *, timeout_s: float) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))
