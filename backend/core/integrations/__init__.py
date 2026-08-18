"""
Integration Management for Jarvis AI Assistant.

Centralizes credential loading, client lazy-initialization, and optional
OAuth token refresh via per-integration refresh hooks.
"""

from core.integrations.manager import IntegrationManager, NeedsReauth, integrations

__all__ = ["IntegrationManager", "NeedsReauth", "integrations"]
