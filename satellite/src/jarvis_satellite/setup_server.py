"""LAN setup listener so the Host can pair this speaker without SSH."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .pair import PairError, pair_and_write

logger = logging.getLogger(__name__)

SETUP_PORT = 8742
_PAIR_TIMEOUT_S = 20.0


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def make_setup_handler(
    *,
    config_path: Path,
    node_id: str,
    on_paired: Callable[[], None] | None,
) -> type[BaseHTTPRequestHandler]:
    class SetupHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            logger.info("setup %s", format % args)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/health":
                _json_response(self, 404, {"ok": False, "error": "not found"})
                return
            _json_response(self, 200, {"ok": True, "node_id": node_id})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/pair":
                _json_response(self, 404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0 or length > 4096:
                _json_response(self, 400, {"ok": False, "error": "invalid body"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _json_response(self, 400, {"ok": False, "error": "invalid json"})
                return
            if not isinstance(payload, dict):
                _json_response(self, 400, {"ok": False, "error": "invalid json"})
                return
            code = str(payload.get("code") or "").strip()
            url = str(payload.get("backend_url") or "").strip() or None
            if not code:
                _json_response(self, 400, {"ok": False, "error": "code required"})
                return
            try:
                paired_id = pair_and_write(
                    code=code,
                    url=url,
                    config_path=config_path,
                    timeout_s=_PAIR_TIMEOUT_S,
                )
            except (PairError, ValueError) as exc:
                _json_response(self, 401, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "node_id": paired_id})
            if on_paired is not None:
                on_paired()

    return SetupHandler


def start_setup_server(
    *,
    node_id: str,
    config_path: Path,
    on_paired: Callable[[], None] | None = None,
    host: str = "0.0.0.0",
    port: int = SETUP_PORT,
) -> ThreadingHTTPServer:
    """Bind the LAN setup listener on a background thread."""
    handler = make_setup_handler(
        config_path=config_path,
        node_id=node_id,
        on_paired=on_paired,
    )
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="jarvis-setup", daemon=True)
    thread.start()
    logger.info("Setup listener on %s:%s", host, server.server_address[1])
    return server
