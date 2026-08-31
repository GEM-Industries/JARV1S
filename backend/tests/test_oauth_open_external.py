"""Tests for desktop OAuth external-browser launcher."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app


@pytest.mark.asyncio
async def test_open_external_accepts_google_authorize_url():
    with patch("api.routes.auth.webbrowser.open", return_value=True) as opener:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/oauth/open-external",
                json={"url": "https://accounts.google.com/o/oauth2/auth?client_id=x"},
            )

    assert response.status_code == 200
    assert response.json() == {"opened": True}
    opener.assert_called_once()


@pytest.mark.asyncio
async def test_open_external_accepts_spotify_authorize_url():
    with patch("api.routes.auth.webbrowser.open", return_value=True) as opener:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/oauth/open-external",
                json={"url": "https://accounts.spotify.com/authorize?client_id=x"},
            )

    assert response.status_code == 200
    assert response.json() == {"opened": True}
    opener.assert_called_once()


@pytest.mark.asyncio
async def test_open_external_rejects_non_oauth_url():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/oauth/open-external",
            json={"url": "https://evil.example/phish"},
        )

    assert response.status_code == 400
