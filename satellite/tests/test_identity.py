from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from jarvis_satellite.config import SatelliteConfig
from jarvis_satellite.identity import build_presence_params, build_websocket_url, load_or_create_node_id


def test_presence_params_match_backend_contract():
    config = SatelliteConfig(
        timezone="Australia/Sydney",
        node_label="Bedroom Satellite",
        room_id="bedroom",
        room_name="Bedroom",
        capabilities=("mic", "speaker"),
    )

    params = build_presence_params(config, "jarvis-satellite-1")

    assert params == {
        "timezone": "Australia/Sydney",
        "node_id": "jarvis-satellite-1",
        "node_label": "Bedroom Satellite",
        "capabilities": "mic,speaker",
        "client_surface": "satellite",
        "location_provider": "manual",
        "room_id": "bedroom",
        "room_name": "Bedroom",
    }


def test_websocket_url_preserves_existing_query_and_adds_presence():
    config = SatelliteConfig(
        backend_url="ws://jarvis.local:8000/api/v1/ws?debug=true",
        timezone="Australia/Sydney",
        ha_area_id="area-bedroom",
    )

    url = build_websocket_url(config, "jarvis-satellite-1")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "ws"
    assert parsed.netloc == "jarvis.local:8000"
    assert parsed.path == "/api/v1/ws"
    assert query["debug"] == ["true"]
    assert query["node_id"] == ["jarvis-satellite-1"]
    assert query["capabilities"] == ["mic,speaker"]
    assert query["location_provider"] == ["home_assistant"]
    assert query["ha_area_id"] == ["area-bedroom"]


def test_websocket_url_supports_wss_tailnet():
    config = SatelliteConfig(
        backend_url="wss://brain.example.ts.net/api/v1/ws",
        timezone="Australia/Sydney",
    )

    url = build_websocket_url(config, "jarvis-satellite-1")
    parsed = urlsplit(url)

    assert parsed.scheme == "wss"
    assert parsed.netloc == "brain.example.ts.net"
    assert parsed.path == "/api/v1/ws"


def test_websocket_url_includes_ticket_when_provided():
    config = SatelliteConfig(backend_url="ws://jarvis.local:8000/api/v1/ws")
    url = build_websocket_url(config, "jarvis-satellite-1", ticket="ticket-abc")
    query = parse_qs(urlsplit(url).query)
    assert query["ticket"] == ["ticket-abc"]


def test_node_id_persists_to_state_dir(tmp_path: Path):
    config = SatelliteConfig(state_dir=tmp_path)

    first = load_or_create_node_id(config)
    second = load_or_create_node_id(config)

    assert first == second
    assert first
