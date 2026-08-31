from http.client import HTTPConnection
from pathlib import Path

from jarvis_satellite.setup_server import start_setup_server


def test_setup_health_and_pair(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(
        'backend_url = "wss://brain.example.ts.net:8443/api/v1/ws"\n'
        'node_id = "jarvis-satellite-1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jarvis_satellite.setup_server.pair_and_write",
        lambda **_kwargs: "jarvis-satellite-1",
    )
    paired = []

    server = start_setup_server(
        node_id="jarvis-satellite-1",
        config_path=config,
        on_paired=lambda: paired.append(True),
        host="127.0.0.1",
        port=0,
    )
    try:
        port = server.server_address[1]
        health = HTTPConnection("127.0.0.1", port, timeout=2)
        health.request("GET", "/health")
        response = health.getresponse()
        assert response.status == 200
        assert b"jarvis-satellite-1" in response.read()

        pair = HTTPConnection("127.0.0.1", port, timeout=2)
        pair.request(
            "POST",
            "/pair",
            body='{"code":"SHR8YT"}',
            headers={"Content-Type": "application/json"},
        )
        paired_response = pair.getresponse()
        assert paired_response.status == 200
        assert paired == [True]
    finally:
        server.shutdown()
        server.server_close()
