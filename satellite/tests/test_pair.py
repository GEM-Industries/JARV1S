from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from jarvis_satellite.config import merge_config_keys
from jarvis_satellite.pair import PairError, pair_and_write, resolve_backend_url


def test_merge_config_keys_does_not_clobber_input_device(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        'backend_url = "wss://brain.example.ts.net:8443/api/v1/ws"\n'
        'input_device = "plughw:Array,0"\n'
        "led_enabled = true\n",
        encoding="utf-8",
    )

    merge_config_keys(path, {"device_token": "jarvis_new_token"})

    text = path.read_text(encoding="utf-8")
    assert 'input_device = "plughw:Array,0"' in text
    assert "led_enabled = true" in text
    assert 'device_token = "jarvis_new_token"' in text
    assert 'backend_url = "wss://brain.example.ts.net:8443/api/v1/ws"' in text


def test_resolve_backend_url_requires_url_when_config_empty():
    with pytest.raises(PairError, match="Pass --url"):
        resolve_backend_url(url=None, file_values={})


def test_pair_and_write_reconnect_writes_token_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "config.toml"
    path.write_text(
        'backend_url = "wss://brain.example.ts.net:8443/api/v1/ws"\n'
        'device_token = "jarvis_old"\n'
        'input_device = "plughw:Array,0"\n',
        encoding="utf-8",
    )

    def fake_consume(**_kwargs):
        return "jarvis_paired_token"

    monkeypatch.setattr("jarvis_satellite.pair.consume_pairing_code", fake_consume)

    node_id = pair_and_write(
        code="ABCD-EFGH",
        url=None,
        config_path=path,
        state_dir=tmp_path,
    )

    text = path.read_text(encoding="utf-8")
    assert node_id
    assert 'device_token = "jarvis_paired_token"' in text
    assert 'backend_url = "wss://brain.example.ts.net:8443/api/v1/ws"' in text
    assert 'input_device = "plughw:Array,0"' in text
    assert text.count("backend_url") == 1


def test_pair_and_write_first_time_writes_url_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "config.toml"
    path.write_text('input_device = "plughw:Array,0"\n', encoding="utf-8")

    def fake_consume(**_kwargs):
        return "jarvis_first_token"

    monkeypatch.setattr("jarvis_satellite.pair.consume_pairing_code", fake_consume)

    pair_and_write(
        code="ABCD-EFGH",
        url="wss://brain.example.ts.net:8443/api/v1/ws",
        config_path=path,
        state_dir=tmp_path,
    )

    text = path.read_text(encoding="utf-8")
    assert 'backend_url = "wss://brain.example.ts.net:8443/api/v1/ws"' in text
    assert 'device_token = "jarvis_first_token"' in text
    assert 'input_device = "plughw:Array,0"' in text


def test_mint_ws_ticket_401_points_at_reconnect(monkeypatch: pytest.MonkeyPatch):
    from jarvis_satellite import ticket as ticket_module

    def fail(*_args, **_kwargs):
        raise HTTPError(
            "https://brain.example.ts.net:8443/api/v1/device-auth/ws-ticket",
            401,
            "Unauthorized",
            hdrs=None,
            fp=BytesIO(b'{"detail":"Invalid device token"}'),
        )

    monkeypatch.setattr("jarvis_satellite.http.urlopen", fail)

    with pytest.raises(ticket_module.TicketAuthError, match="Rooms → Reconnect"):
        ticket_module.mint_ws_ticket(
            "wss://brain.example.ts.net:8443/api/v1/ws",
            "stale-token",
        )


def test_run_pair_refuses_macos(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    import jarvis_satellite.__main__ as main

    monkeypatch.setattr(main.sys, "platform", "darwin")
    monkeypatch.delenv("JARVIS_SATELLITE_ALLOW_LOCAL_PAIR", raising=False)
    assert main.run_pair(["SHR-8YT"]) == 2
    assert "Connect speaker" in capsys.readouterr().err
