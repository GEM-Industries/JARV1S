"""Tests for OAuth error handler widget payloads."""

from __future__ import annotations

import pytest

from core.auth.error_handler import handle_integration_auth_error
from core.integrations.manager import NeedsReauth


@pytest.mark.asyncio
async def test_error_handler_pushes_oauth_widget(monkeypatch):
    pushed: list[dict] = []

    def _capture_ui(envelope):
        pushed.append(envelope.data)

    monkeypatch.setattr("core.auth.error_handler.push_ui", _capture_ui)

    import core.integrations.manager as mgr_mod

    manager = mgr_mod.IntegrationManager()
    manager.register("calendar", lambda _c: object(), provider="google", required_scopes=["cal"])
    monkeypatch.setattr("core.integrations.integrations", manager)

    result = await handle_integration_auth_error("calendar", NeedsReauth("google"))
    assert result is not None
    assert result.code == "reauth_needed"
    assert "requires re-authorization" in result.message
    assert pushed
    assert pushed[0]["provider"] == "google"


@pytest.mark.asyncio
async def test_error_handler_pushes_oauth_widget_without_metadata(monkeypatch):
    pushed: list[dict] = []

    def _capture_ui(envelope):
        pushed.append(envelope.data)

    monkeypatch.setattr("core.auth.error_handler.push_ui", _capture_ui)
    monkeypatch.delenv("JARVIS_PRODUCT_OAUTH", raising=False)

    import core.integrations.manager as mgr_mod

    manager = mgr_mod.IntegrationManager()
    manager.register("calendar", lambda _c: object(), provider="google", required_scopes=["cal"])
    monkeypatch.setattr("core.integrations.integrations", manager)

    result = await handle_integration_auth_error("calendar", NeedsReauth("google"))
    assert result is not None
    assert pushed[0] == {"provider": "google"}


@pytest.mark.asyncio
async def test_error_handler_pushes_oauth_widget_for_multi_provider_calendar(monkeypatch):
    """Calendar registers without a primary provider; NeedsReauth('calendar') must still push UI."""
    pushed: list[dict] = []

    def _capture_ui(envelope):
        pushed.append(envelope.data)

    monkeypatch.setattr("core.auth.error_handler.push_ui", _capture_ui)

    import core.integrations.manager as mgr_mod

    manager = mgr_mod.IntegrationManager()
    manager.register("calendar", lambda _c: object())
    manager.register_aux_provider_scopes(
        "google",
        ["calendar.readonly"],
        integration_name="calendar",
    )
    manager.register_aux_provider_scopes(
        "microsoft",
        ["Calendars.Read"],
        integration_name="calendar",
    )
    monkeypatch.setattr("core.integrations.integrations", manager)

    result = await handle_integration_auth_error("calendar", NeedsReauth("calendar"))
    assert result is not None
    assert result.code == "reauth_needed"
    assert "setup card has appeared" in result.message
    assert pushed
    assert pushed[0]["provider"] == "google"
