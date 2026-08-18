"""Backend WebSocket URL validation for satellite clients."""

from __future__ import annotations

import os
from enum import Enum
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

WS_PATH = "/api/v1/ws"
TAILNET_CGNAT = ip_network("100.64.0.0/10")
RFC1918_NETWORKS = (
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)


class BackendUrlTarget(str, Enum):
    LOOPBACK = "loopback"
    LAN_PRIVATE = "lan_private"
    TAILNET = "tailnet"
    DEV_HOST = "dev_host"
    PUBLIC = "public"


def classify_host(hostname: str) -> BackendUrlTarget:
    host = hostname.lower().strip("[]")
    if host in {"localhost", "127.0.0.1", "::1"}:
        return BackendUrlTarget.LOOPBACK
    if host.endswith(".local"):
        return BackendUrlTarget.DEV_HOST
    if host.endswith(".ts.net"):
        return BackendUrlTarget.TAILNET

    try:
        addr = ip_address(host)
    except ValueError:
        return BackendUrlTarget.PUBLIC

    if addr.is_loopback:
        return BackendUrlTarget.LOOPBACK
    if addr in TAILNET_CGNAT:
        return BackendUrlTarget.TAILNET
    if any(addr in network for network in RFC1918_NETWORKS):
        return BackendUrlTarget.LAN_PRIVATE
    if addr.is_private:
        return BackendUrlTarget.LAN_PRIVATE
    return BackendUrlTarget.PUBLIC


def allow_insecure_ws_override() -> bool:
    return os.getenv("JARVIS_SATELLITE_ALLOW_INSECURE_WS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def validate_backend_url(url: str, *, allow_insecure_ws: bool | None = None) -> BackendUrlTarget:
    """Validate backend_url before the satellite opens a WebSocket.

  Raises ValueError when the URL is malformed or uses plaintext ws:// to a host
  that should use wss://. Returns the classified target for tests/diagnostics.
    """
    if allow_insecure_ws is None:
        allow_insecure_ws = allow_insecure_ws_override()

    parts = urlsplit(url)
    if parts.scheme not in {"ws", "wss"}:
        raise ValueError(f"backend_url must use ws:// or wss://, got {parts.scheme!r}")

    hostname = parts.hostname
    if not hostname:
        raise ValueError("backend_url is missing a host")

    path = parts.path or ""
    if path.rstrip("/") != WS_PATH:
        raise ValueError(f"backend_url path must be {WS_PATH}, got {path!r}")

    target = classify_host(hostname)
    if parts.scheme == "wss":
        return target

    if allow_insecure_ws:
        return target

    if target in {BackendUrlTarget.LOOPBACK, BackendUrlTarget.LAN_PRIVATE, BackendUrlTarget.DEV_HOST}:
        return target

    if target is BackendUrlTarget.TAILNET:
        raise ValueError(
            "backend_url uses plaintext ws:// to a tailnet target "
            f"({hostname!r}). Prefer Tailscale Serve with wss://<machine>.<tailnet>.ts.net{WS_PATH}, "
            "or set JARVIS_SATELLITE_ALLOW_INSECURE_WS=1 for a deliberate private-tailnet exception."
        )

    raise ValueError(
        f"backend_url uses plaintext ws:// to a public-looking host ({hostname!r}). "
        f"Use wss:// for remote access, or set JARVIS_SATELLITE_ALLOW_INSECURE_WS=1 only for local experiments."
    )
