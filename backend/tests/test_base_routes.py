import pytest

from api.routes import base as base_routes


@pytest.mark.asyncio
async def test_version_returns_host_metadata(monkeypatch):
    expected = {
        "app": "0.1.0",
        "api": "api/v1",
        "python": "3.12.10",
        "node": "20",
        "mongodb": "8.2",
    }
    monkeypatch.setattr(base_routes, "get_version_info", lambda: expected)

    assert await base_routes.version() == expected
