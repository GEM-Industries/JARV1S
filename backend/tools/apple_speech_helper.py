#!/usr/bin/env python3
"""Protocol-compatible Apple Speech helper for dev/smoke when the Swift app is unavailable.

Implements the Host ↔ helper WebSocket contract:
  status / prepare / start / binary PCM / finalize → partial|final|done

On macOS < 26 (or without SpeechAnalyzer), reports state=unsupported.
Set JARVIS_SPEECH_MOCK=1 to accept audio and emit a fixed transcript for contract tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import sys

logger = logging.getLogger("apple_speech_helper")


def _macos_major() -> int | None:
    if sys.platform != "darwin":
        return None
    version = platform.mac_ver()[0]
    if not version:
        return None
    try:
        return int(version.split(".", 1)[0])
    except ValueError:
        return None


def current_status(*, mock: bool) -> dict:
    if mock:
        return {"type": "status", "ready": True, "state": "ready", "detail": "Mock Apple Speech helper"}
    major = _macos_major()
    if major is None or major < 26:
        return {
            "type": "status",
            "ready": False,
            "state": "unsupported",
            "detail": "On-device Speech requires macOS 26+ and the signed Swift helper.",
        }
    return {
        "type": "status",
        "ready": False,
        "state": "unavailable",
        "detail": "Build and launch JARV1SSpeechHelper.app for SpeechAnalyzer.",
    }


class Session:
    def __init__(self, websocket, *, token: str, mock: bool) -> None:
        self.websocket = websocket
        self.token = token
        self.mock = mock
        self.active = False
        self.bytes_seen = 0

    def authorized(self, payload: dict) -> bool:
        if not self.token:
            return True
        return payload.get("token") == self.token

    async def send(self, payload: dict) -> None:
        await self.websocket.send(json.dumps(payload))

    async def handle_text(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            await self.send({"type": "error", "detail": "Invalid JSON message"})
            return
        if not isinstance(payload, dict):
            await self.send({"type": "error", "detail": "Invalid JSON message"})
            return
        if not self.authorized(payload):
            await self.send({"type": "error", "detail": "Unauthorized"})
            return

        msg_type = str(payload.get("type") or "").lower()
        if msg_type == "status":
            await self.send(current_status(mock=self.mock))
        elif msg_type == "prepare":
            await self.send(current_status(mock=self.mock))
        elif msg_type == "start":
            status = current_status(mock=self.mock)
            if not status["ready"]:
                await self.send({"type": "error", "detail": status.get("detail") or "Not ready"})
                return
            self.active = True
            self.bytes_seen = 0
            await self.send({"type": "started"})
        elif msg_type == "finalize":
            if self.mock and self.active:
                text = "hello jarvis"
                await self.send({"type": "final", "text": text})
            await self.send({"type": "done"})
            self.active = False
        elif msg_type == "cancel":
            self.active = False
        else:
            await self.send({"type": "error", "detail": f"Unknown message type: {msg_type}"})

    async def handle_binary(self, data: bytes) -> None:
        if not self.active:
            return
        self.bytes_seen += len(data)
        if self.mock and self.bytes_seen >= 3200:  # ~100ms at 16kHz mono s16
            await self.send({"type": "partial", "text": "hello"})


async def handler(websocket, *, token: str, mock: bool) -> None:
    session = Session(websocket, token=token, mock=mock)
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                await session.handle_binary(message)
            else:
                await session.handle_text(message)
    finally:
        session.active = False


async def main_async(host: str, port: int, token: str, mock: bool) -> None:
    import websockets

    async with websockets.serve(
        lambda ws: handler(ws, token=token, mock=mock),
        host,
        port,
        max_size=8 * 1024 * 1024,
    ):
        logger.info("Apple Speech helper (python) listening on ws://%s:%s/asr mock=%s", host, port, mock)
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser(description="JARV1S Apple Speech helper (protocol stub)")
    parser.add_argument("--host", default=os.environ.get("JARVIS_SPEECH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_SPEECH_PORT", "9091")))
    parser.add_argument("--token", default=os.environ.get("JARVIS_SPEECH_TOKEN", ""))
    parser.add_argument(
        "--mock",
        action="store_true",
        default=os.environ.get("JARVIS_SPEECH_MOCK", "").lower() in {"1", "true", "yes"},
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main_async(args.host, args.port, args.token, args.mock))


if __name__ == "__main__":
    main()
