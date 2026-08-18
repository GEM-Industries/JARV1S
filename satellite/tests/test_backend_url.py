import pytest

from jarvis_satellite.backend_url import (
    BackendUrlTarget,
    classify_host,
    validate_backend_url,
)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", BackendUrlTarget.LOOPBACK),
        ("127.0.0.1", BackendUrlTarget.LOOPBACK),
        ("192.168.1.10", BackendUrlTarget.LAN_PRIVATE),
        ("10.0.0.5", BackendUrlTarget.LAN_PRIVATE),
        ("jarvis.local", BackendUrlTarget.DEV_HOST),
        ("brain.example.ts.net", BackendUrlTarget.TAILNET),
        ("100.101.102.103", BackendUrlTarget.TAILNET),
        ("jarvis.example.com", BackendUrlTarget.PUBLIC),
    ],
)
def test_classify_host(host: str, expected: BackendUrlTarget):
    assert classify_host(host) is expected


def test_validate_accepts_lan_ws():
    target = validate_backend_url("ws://192.168.1.10:8000/api/v1/ws")
    assert target is BackendUrlTarget.LAN_PRIVATE


def test_validate_accepts_loopback_ws():
    target = validate_backend_url("ws://localhost:8000/api/v1/ws")
    assert target is BackendUrlTarget.LOOPBACK


def test_validate_accepts_tailnet_wss():
    target = validate_backend_url("wss://brain.example.ts.net/api/v1/ws")
    assert target is BackendUrlTarget.TAILNET


def test_validate_rejects_public_ws():
    with pytest.raises(ValueError, match="public-looking host"):
        validate_backend_url("ws://jarvis.example.com/api/v1/ws")


def test_validate_rejects_tailnet_plaintext_ws():
    with pytest.raises(ValueError, match="tailnet target"):
        validate_backend_url("ws://100.101.102.103:8000/api/v1/ws")


def test_validate_allows_tailnet_plaintext_ws_with_override():
    target = validate_backend_url(
        "ws://100.101.102.103:8000/api/v1/ws",
        allow_insecure_ws=True,
    )
    assert target is BackendUrlTarget.TAILNET


def test_validate_rejects_wrong_path():
    with pytest.raises(ValueError, match="/api/v1/ws"):
        validate_backend_url("ws://localhost:8000/ws")


def test_validate_rejects_non_websocket_scheme():
    with pytest.raises(ValueError, match="ws:// or wss://"):
        validate_backend_url("http://localhost:8000/api/v1/ws")


def test_load_config_allows_offline_commands_with_unreachable_backend_url(tmp_path):
    from argparse import Namespace

    from jarvis_satellite.config import load_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'backend_url = "ws://jarvis.example.com/api/v1/ws"\n',
        encoding="utf-8",
    )

    config = load_config(
        Namespace(
            config=config_path,
            backend_url=None,
            device_token=None,
            timezone=None,
            node_id=None,
            node_label=None,
            room_id=None,
            room_name=None,
            ha_area_id=None,
            input_device=None,
            output_device=None,
            audio_backend=None,
            state_dir=None,
            log_level=None,
            list_devices=True,
            dry_run_audio=False,
            activate=False,
        )
    )

    assert config.backend_url == "ws://jarvis.example.com/api/v1/ws"


def test_load_config_coerces_led_enabled_false_env(monkeypatch, tmp_path):
    from argparse import Namespace

    from jarvis_satellite.config import load_config

    config_path = tmp_path / "config.toml"
    config_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("JARVIS_SATELLITE_LED_ENABLED", "false")
    monkeypatch.setenv("JARVIS_SATELLITE_TOOL_CUES_ENABLED", "off")

    config = load_config(
        Namespace(
            config=config_path,
            backend_url=None,
            device_token=None,
            timezone=None,
            node_id=None,
            node_label=None,
            room_id=None,
            room_name=None,
            ha_area_id=None,
            input_device=None,
            output_device=None,
            audio_backend=None,
            state_dir=None,
            log_level=None,
            list_devices=False,
            dry_run_audio=False,
            activate=False,
        )
    )

    assert config.led_enabled is False
    assert config.tool_cues_enabled is False


def test_satellite_client_rejects_public_plaintext_ws(tmp_path):
    from jarvis_satellite.client import SatelliteClient
    from jarvis_satellite.config import SatelliteConfig

    config = SatelliteConfig(
        backend_url="ws://jarvis.example.com/api/v1/ws",
        state_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="public-looking host"):
        SatelliteClient(config)
