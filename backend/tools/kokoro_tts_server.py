#!/usr/bin/env python3
"""Supervised Kokoro TTS helper for JARV1S Host.

Loopback WebSocket contract:
  status / warm / speak → binary pcm_f32le@24kHz frames → done

Closing the speak WebSocket cancels delivery; the helper discards late inference.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("kokoro_tts_server")

SAMPLE_RATE = 24000
TRANSPORT_FRAME_MS = 80
FRAME_SAMPLES = SAMPLE_RATE * TRANSPORT_FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 4  # float32
DEFAULT_VOICE = "af_heart"


def _client_gone(websocket) -> bool:
    return getattr(websocket, "close_code", None) is not None


def _status(ready: bool, state: str, detail: str) -> dict:
    return {"type": "status", "ready": ready, "state": state, "detail": detail}


class Engine:
    def __init__(self, *, assets_dir: Path) -> None:
        self.assets_dir = assets_dir
        self.model_path = assets_dir / "kokoro-v1.0.int8.onnx"
        self.voices_path = assets_dir / "voices-v1.0.bin"
        self._kokoro = None
        self._lock = threading.Lock()
        self._warmed = False
        self._failure: str | None = None

    def status(self) -> dict:
        if not self._assets_present():
            return _status(False, "unavailable", f"Bundled Kokoro assets missing under {self.assets_dir}")
        if self._failure:
            return _status(False, "unavailable", self._failure)
        # Present assets mean the model can load on demand; warming only removes
        # first-utterance latency, so it is not a readiness precondition.
        return _status(True, "ready", "Kokoro TTS ready" if self._warmed else "Kokoro TTS available")

    def warm(self, *, voice: str) -> dict:
        try:
            self.synthesize("Hello.", voice=voice, speed=1.0)
            self._warmed = True
            self._failure = None
        except Exception as exc:
            self._failure = f"Could not warm Kokoro: {exc}"
            logger.exception("Kokoro warm failed")
        return self.status()

    def _assets_present(self) -> bool:
        return self.model_path.is_file() and self.voices_path.is_file()

    def _ensure_loaded(self) -> None:
        if self._kokoro is not None:
            return
        if not self._assets_present():
            raise FileNotFoundError(f"Kokoro assets missing under {self.assets_dir}")
        from kokoro_onnx import Kokoro

        self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
        logger.info("Loaded Kokoro model from %s", self.model_path)

    def synthesize(self, text: str, *, voice: str, speed: float) -> bytes:
        """Synthesize one utterance to raw pcm_f32le. Blocking; call off the loop."""
        self._ensure_loaded()
        assert self._kokoro is not None
        with self._lock:
            samples, sample_rate = self._kokoro.create(
                text,
                voice=voice,
                speed=speed,
                lang="en-us",
            )
        if int(sample_rate) != SAMPLE_RATE:
            raise RuntimeError(f"Unexpected Kokoro sample rate {sample_rate}")
        import numpy as np

        return np.asarray(samples, dtype=np.float32).tobytes()


class Session:
    """One client connection. Messages are handled in order, so a connection can
    never interleave two utterances."""

    def __init__(self, websocket, *, token: str, engine: Engine) -> None:
        self.websocket = websocket
        self.token = token
        self.engine = engine

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
            payload = None
        if not isinstance(payload, dict):
            await self.send({"type": "error", "detail": "Invalid JSON message"})
            return
        if not self.authorized(payload):
            await self.send({"type": "error", "detail": "Unauthorized"})
            return

        msg_type = payload.get("type")
        if msg_type == "status":
            await self.send(self.engine.status())
        elif msg_type == "warm":
            voice = str(payload.get("voice") or DEFAULT_VOICE)
            await self.send(await asyncio.to_thread(self.engine.warm, voice=voice))
        elif msg_type == "speak":
            await self._speak(payload)
        else:
            await self.send({"type": "error", "detail": f"Unknown message type: {msg_type}"})

    async def _speak(self, payload: dict) -> None:
        utterance_id = str(payload.get("utterance_id") or "")
        text = str(payload.get("text") or "").strip()
        if not text:
            await self.send({"type": "done", "utterance_id": utterance_id})
            return

        voice = str(payload.get("voice") or DEFAULT_VOICE)
        speed = float(payload.get("speed") or 1.0)
        try:
            pcm = await asyncio.to_thread(self.engine.synthesize, text, voice=voice, speed=speed)
            for offset in range(0, len(pcm), FRAME_BYTES):
                await self.websocket.send(pcm[offset : offset + FRAME_BYTES])
            await self.send({"type": "done", "utterance_id": utterance_id})
        except Exception as exc:
            if _client_gone(self.websocket):
                # Closing the socket is how the Host cancels; drop the late result.
                logger.debug("Discarded cancelled utterance %s", utterance_id)
                return
            logger.exception("Speak failed")
            with contextlib.suppress(Exception):
                await self.send({"type": "error", "detail": str(exc), "utterance_id": utterance_id})


async def _handler(websocket, *, token: str, engine: Engine):
    session = Session(websocket, token=token, engine=engine)
    try:
        async for message in websocket:
            if isinstance(message, str):
                await session.handle_text(message)
    except Exception:
        logger.debug("Client disconnected", exc_info=True)


async def main_async(host: str, port: int, token: str, assets_dir: Path) -> None:
    import websockets

    engine = Engine(assets_dir=assets_dir)
    logger.info("Starting Kokoro TTS helper on ws://%s:%s assets=%s", host, port, assets_dir)

    async def handler(websocket):
        await _handler(websocket, token=token, engine=engine)

    async with websockets.serve(handler, host, port, max_size=8 * 1024 * 1024):
        await asyncio.Future()


def resolve_assets_dir(explicit: str | None) -> Path:
    """The supervisor (packaged) and Taskfile (dev) both inject the assets directory;
    the checkout path only covers running this script by hand."""
    configured = explicit or os.environ.get("JARVIS_TTS_ASSETS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "apps" / "desktop" / "local-tts"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="JARV1S Kokoro TTS helper")
    parser.add_argument("--host", default=os.environ.get("JARVIS_TTS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("JARVIS_TTS_PORT", "9092")))
    parser.add_argument("--token", default=os.environ.get("JARVIS_TTS_TOKEN", ""))
    parser.add_argument("--assets-dir", default=os.environ.get("JARVIS_TTS_ASSETS_DIR"))
    args = parser.parse_args()
    assets_dir = resolve_assets_dir(args.assets_dir)
    asyncio.run(main_async(args.host, args.port, args.token, assets_dir))


if __name__ == "__main__":
    main()
