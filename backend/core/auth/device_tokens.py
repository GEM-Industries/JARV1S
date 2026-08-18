"""Opaque device-token and WS-ticket helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final

DEVICE_TOKEN_PREFIX: Final[str] = "jarvis_dev_"
PAIRING_CODE_ALPHABET: Final[str] = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_secret(value: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(value), stored_hash)


def generate_device_secret() -> str:
    return secrets.token_urlsafe(32)


def format_device_token(device_id: str, secret: str) -> str:
    return f"{DEVICE_TOKEN_PREFIX}{device_id}_{secret}"


def parse_device_token(token: str) -> tuple[str, str] | None:
    if not token.startswith(DEVICE_TOKEN_PREFIX):
        return None
    body = token[len(DEVICE_TOKEN_PREFIX) :]
    if "_" not in body:
        return None
    device_id, secret = body.split("_", 1)
    if not device_id or not secret:
        return None
    return device_id, secret


def generate_ws_ticket() -> str:
    return secrets.token_urlsafe(24)


def generate_pairing_code(*, groups: int = 2, group_len: int = 3) -> str:
    """Human-friendly pairing code, e.g. ABC-234."""
    parts: list[str] = []
    for _ in range(groups):
        part = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(group_len))
        parts.append(part)
    return "-".join(parts)


def normalize_pairing_code(code: str) -> str:
    return code.strip().upper().replace("-", "").replace(" ", "")
