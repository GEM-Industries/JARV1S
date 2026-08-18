"""Tests for Home Assistant Docker bootstrap."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tools.capture_ha_fixtures import compare_fixture_shapes
from plugins.smart_home import bootstrap
from plugins.smart_home.bootstrap_config import (
    BOOTSTRAP_HA_URL,
    FIXTURE_MANIFEST_PATH,
    HA_CONTAINER_IMAGE,
)
from plugins.smart_home.ha_client import HomeAssistantClient

FIXTURES = Path(__file__).parent / "fixtures" / "ha"


def test_indieauth_client_id_uses_loopback_trailing_slash() -> None:
    client = HomeAssistantClient(base_url=BOOTSTRAP_HA_URL)
    assert client.indieauth_client_id() == "http://127.0.0.1:8123/"


def test_bootstrap_supported_with_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "docker_available", lambda: True)
    monkeypatch.setattr(bootstrap, "docker_daemon_reachable", lambda: True)
    monkeypatch.setattr(bootstrap, "docker_compose_command", lambda: ["docker", "compose"])
    ok, reason = bootstrap.bootstrap_supported()
    assert ok is True
    assert reason is None


def test_bootstrap_unsupported_when_docker_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "docker_available", lambda: True)
    monkeypatch.setattr(bootstrap, "docker_daemon_reachable", lambda: False)
    ok, reason = bootstrap.bootstrap_supported()
    assert ok is False
    assert reason is not None
    assert "not running" in reason


def test_bootstrap_unsupported_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "docker_available", lambda: False)
    ok, reason = bootstrap.bootstrap_supported()
    assert ok is False
    assert "Docker" in (reason or "")


def test_write_compose_file_pins_image_and_loopback_port(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "ha-data"
    compose_file = data_dir / "docker-compose.yml"
    config_dir = data_dir / "config"
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_DATA_DIR", data_dir)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_CONFIG_DIR", config_dir)
    monkeypatch.setattr(bootstrap, "BOOTSTRAP_COMPOSE_FILE", compose_file)

    path = bootstrap.write_compose_file()
    content = path.read_text(encoding="utf-8")
    assert HA_CONTAINER_IMAGE in content
    assert "127.0.0.1:8123:8123" in content
    assert str(config_dir) in content
    assert config_dir.is_dir()


def test_fixture_manifest_matches_bootstrap_config() -> None:
    manifest = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["ha_image"] == HA_CONTAINER_IMAGE
    assert manifest["bootstrap_url"] == BOOTSTRAP_HA_URL
    assert manifest["indieauth_client_id"] == f"{BOOTSTRAP_HA_URL}/"


def test_required_fixtures_exist_and_have_expected_top_level_shapes() -> None:
    manifest = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    for name in manifest["required_fixtures"]:
        path = FIXTURES / name
        assert path.exists(), f"Missing fixture: {name}"
        data = json.loads(path.read_text(encoding="utf-8"))
        if name == "api_ping.json":
            assert "message" in data
        elif name == "onboarding_users_response.json":
            assert "auth_code" in data
        elif name == "auth_token_response.json":
            assert "access_token" in data
            assert data["access_token"] == "fixture-access-token"
            assert data["refresh_token"] == "fixture-refresh-token"
        elif name.startswith("onboarding_status"):
            assert isinstance(data, list)
            assert all("step" in step and "done" in step for step in data)
        elif name.endswith("_list.json") or name == "states_sample.json":
            assert isinstance(data, list)


def test_fixture_shape_compare_detects_drift(tmp_path) -> None:
    for path in FIXTURES.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    assert compare_fixture_shapes(tmp_path) == []

    (tmp_path / "auth_token_response.json").write_text(
        json.dumps({"access_token": "fixture-access-token"}) + "\n",
        encoding="utf-8",
    )
    diffs = compare_fixture_shapes(tmp_path)
    assert diffs
    assert "auth_token_response.json" in diffs[0]


@pytest.mark.asyncio
async def test_run_bootstrap_orchestrates_docker_and_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    progress: list[str] = []

    def _record(message: str) -> None:
        progress.append(message)

    monkeypatch.setattr(bootstrap, "bootstrap_supported", lambda: (True, None))
    monkeypatch.setattr(bootstrap, "pull_image", lambda *, progress: progress("pull"))
    monkeypatch.setattr(bootstrap, "start_container", lambda *, progress: progress("start"))
    monkeypatch.setattr(
        bootstrap,
        "wait_for_home_assistant",
        AsyncMock(side_effect=lambda **kwargs: _record("wait")),
    )
    monkeypatch.setattr(
        bootstrap,
        "bootstrap_fresh_instance",
        AsyncMock(
            return_value=bootstrap.BootstrapResult(
                base_url=BOOTSTRAP_HA_URL,
                long_lived_token="ll-token",
                onboarding_complete=True,
            )
        ),
    )

    result = await bootstrap.run_bootstrap(password="secret", pull=True, progress=_record)
    assert result.long_lived_token == "ll-token"
    assert "pull" in progress
    assert "start" in progress
    assert "wait" in progress


@pytest.mark.asyncio
async def test_wait_for_home_assistant_polls_onboarding_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("not ready")
        return httpx.Response(200, json=[{"step": "user", "done": False}])

    client = HomeAssistantClient(
        base_url=BOOTSTRAP_HA_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(bootstrap, "HomeAssistantClient", lambda **kwargs: client)
    await bootstrap.wait_for_home_assistant(timeout_s=5, progress=lambda _: None)
    await client.aclose()
    assert calls["n"] >= 2


def test_setup_home_bootstrap_path_exits_on_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from cli import setup_home

    monkeypatch.setattr(setup_home, "bootstrap_supported", lambda: (False, "Docker required"))
    with pytest.raises(SystemExit) as exc:
        asyncio.run(setup_home._setup_bootstrap())
    assert exc.value.code == 1
