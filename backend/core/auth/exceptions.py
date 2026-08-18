"""
Auth-layer exceptions.

ScopeGapError: raised when a token exists but lacks required OAuth scopes.
NeedsReauth:   re-exported from integrations.manager for convenience so callers
               can import both exceptions from one place.
"""

from core.integrations.manager import NeedsReauth  # noqa: F401


class ScopeGapError(Exception):
    """Raised when the stored token lacks one or more required OAuth scopes."""

    def __init__(self, provider: str, missing_scopes: list[str]) -> None:
        self.provider = provider
        self.missing_scopes = missing_scopes
        super().__init__(
            f"Missing {provider} scopes: {', '.join(missing_scopes)}"
        )
