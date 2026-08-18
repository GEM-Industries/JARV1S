"""
Configuration settings for the API.

This module re-exports the settings from core.config for backward compatibility.
"""

from core.config import settings, EnvironmentType, LogLevel

# Export settings and types
__all__ = ["settings", "EnvironmentType", "LogLevel"] 