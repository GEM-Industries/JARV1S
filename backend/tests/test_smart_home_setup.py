"""Lifecycle and setup CLI tests for smart_home."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli import setup_home_env
from core.integrations.lifecycle.composio import list_integrations
from core.plugins.types import PluginMetadata
from plugins.smart_home.status import LivenessStatus


class _FakePlugin:
    metadata = PluginMetadata(
        name="smart_home",
        version="2.0.0",
        description="Smart home",
    )

    def get_tools(self):
        return ["a", "b", "c", "d", "e", "f", "g"]


def test_upsert_env_creates_and_updates(tmp_path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.setattr(setup_home_env, "_ENV_PATH", env_path)

    setup_home_env.upsert_env("HA_URL", "http://localhost:8123")
    assert env_path.read_text(encoding="utf-8") == "HA_URL=http://localhost:8123\n"

    setup_home_env.upsert_env("HA_TOKEN", "secret")
    content = env_path.read_text(encoding="utf-8")
    assert "HA_URL=http://localhost:8123" in content
    assert "HA_TOKEN=secret" in content
    assert "secret" not in capsys.readouterr().out

    setup_home_env.upsert_env("HA_URL", "http://ha.local:8123")
    assert "HA_URL=http://ha.local:8123" in env_path.read_text(encoding="utf-8")


def test_default_env_path_is_repo_root() -> None:
    assert setup_home_env.env_path().name == ".env"
    assert setup_home_env.env_path().parent.name == "JARV1S"


@pytest.mark.asyncio
async def test_list_integrations_marks_smart_home_unconfigured() -> None:
    fake_registry = MagicMock()
    fake_registry.plugins = {"smart_home": _FakePlugin()}
    fake_registry.bespoke_names = set()
    fake_registry.is_enabled.return_value = True

    liveness = LivenessStatus(
        configured=False,
        reachable=False,
        authenticated=False,
        message="Home Assistant is not configured. Connect it in the Smart Home panel.",
    )

    with patch("core.integrations.lifecycle.composio.registry", fake_registry), \
         patch("core.integrations.lifecycle.composio.get_composio_gateway", return_value=None), \
         patch("core.integrations.manager.integrations.get_provider_name", return_value=None), \
         patch("plugins.smart_home.status.check_liveness", AsyncMock(return_value=liveness)):
        views = await list_integrations()

    smart_home = next(v for v in views if v.name == "smart_home")
    assert smart_home.connected is False
    assert smart_home.status == "error"
    assert "Smart Home panel" in (smart_home.last_error or "")
