"""Consume a Host pairing code and write satellite credentials."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from .backend_url import api_base_from_backend_url, validate_backend_url
from .config import (
    DEFAULT_CONFIG_PATH,
    SatelliteConfig,
    load_config_file,
    merge_config_keys,
)
from .http import post_json
from .identity import load_or_create_node_id

_RECONNECT_HINT = "On the Mac: Rooms → Reconnect."


class PairError(RuntimeError):
    """Pairing failed; message is safe to print."""


def consume_pairing_code(
    *,
    backend_url: str,
    code: str,
    node_id: str,
    node_label: str | None = None,
    timeout_s: float = 15.0,
) -> str:
    """POST /device-auth/pair and return the one-shot device_token."""
    api_base = api_base_from_backend_url(backend_url)
    url = f"{api_base}/api/v1/device-auth/pair"
    payload: dict[str, str] = {
        "code": code,
        "node_id": node_id,
        "client_surface": "satellite",
        "capabilities": "mic,speaker",
    }
    if node_label:
        payload["node_label"] = node_label
    try:
        body = post_json(url, payload, timeout_s=timeout_s)
    except HTTPError as exc:
        if exc.code == 401:
            raise PairError(
                f"Pairing code was invalid, expired, or already used. {_RECONNECT_HINT}"
            ) from exc
        detail = exc.read().decode("utf-8", errors="replace")
        raise PairError(f"Pair request failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        raise PairError(
            f"Could not reach JARV1S at {api_base}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise PairError(f"Pair request to {api_base} timed out") from exc

    token = str(body.get("device_token") or "").strip()
    if not token:
        raise PairError("Pair response did not include a device token")
    return token


def resolve_backend_url(*, url: str | None, file_values: dict[str, Any]) -> tuple[str, bool]:
    """Return (backend_url, write_url). write_url is true when --url was passed or config had none."""
    if url:
        validate_backend_url(url)
        return url, True
    existing = str(file_values.get("backend_url") or "").strip()
    if not existing:
        raise PairError("Pass --url wss://…/api/v1/ws (copy it from Rooms on the Mac).")
    validate_backend_url(existing)
    return existing, False


def pair_and_write(
    *,
    code: str,
    url: str | None,
    config_path: Path | None = None,
    state_dir: Path | None = None,
    timeout_s: float = 15.0,
) -> str:
    """Consume the code, merge-write token (and URL when needed), return node_id."""
    path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    file_values = load_config_file(path)
    backend_url, write_url = resolve_backend_url(url=url, file_values=file_values)
    if state_dir is not None:
        resolved_state = state_dir.expanduser()
    elif file_values.get("state_dir"):
        resolved_state = Path(str(file_values["state_dir"])).expanduser()
    else:
        resolved_state = DEFAULT_CONFIG_PATH.parent
    node_id = load_or_create_node_id(
        SatelliteConfig(
            node_id=str(file_values.get("node_id") or "").strip() or None,
            node_label=str(file_values.get("node_label") or "").strip() or None,
            state_dir=resolved_state,
        )
    )
    token = consume_pairing_code(
        backend_url=backend_url,
        code=code,
        node_id=node_id,
        node_label=str(file_values.get("node_label") or "").strip() or None,
        timeout_s=timeout_s,
    )
    updates = {"device_token": token}
    if write_url:
        updates["backend_url"] = backend_url
    merge_config_keys(path, updates)
    return node_id
