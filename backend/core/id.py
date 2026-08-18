"""
Centralised ID generation for JARV1S.

All user-facing and harness entity IDs (tasks, series, reminders, automations,
todos, widgets, turns, tool calls, invocations) use the same format:
12-character alphanumeric nanoid.

- Compact enough for LLM pass-through and voice readback
- 62^12 ≈ 3.2×10²¹ combinations — collision-safe for single-user
- URL-safe, no hyphens, no ambiguity
- Consistent across every entity type

NOT replaced by this module:
- MongoDB ObjectId (_id) — DB-internal, never user-facing
- WS message / event bus IDs — protocol-level UUID
- OAuth nonces / push tokens — security-sensitive (secrets module)
"""

from nanoid import generate

_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SIZE = 12


def generate_id(prefix: str = "") -> str:
    """Generate a 12-char alphanumeric ID, optionally with a prefix.

    >>> generate_id()           # 'k8Tm4xQ2pR7n'
    >>> generate_id("task-")    # 'task-k8Tm4xQ2pR7n'
    """
    return f"{prefix}{generate(_ALPHABET, _SIZE)}"
