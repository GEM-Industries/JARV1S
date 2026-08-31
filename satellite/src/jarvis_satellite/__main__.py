"""Command line entry point for the JARV1S satellite."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import struct
import sys
from pathlib import Path

from .audio import AlsaProcessAudioIO, AudioIO, print_devices, pyaudio_available
from .client import SatelliteClient
from .config import DEFAULT_CONFIG_PATH, build_parser, load_config
from .pair import PairError, pair_and_write


def _pcm_stats(audio: bytes) -> tuple[int, int]:
    samples = len(audio) // 2
    if samples == 0:
        return 0, 0
    values = struct.unpack("<" + "h" * samples, audio[: samples * 2])
    peak = max(abs(value) for value in values)
    rms = int(math.sqrt(sum(value * value for value in values) / samples))
    return rms, peak


async def _dry_run_audio(config) -> None:
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=10)
    ended = asyncio.Event()
    backend = config.audio_backend
    if backend == "auto":
        backend = "pyaudio" if pyaudio_available() else "alsa"
    audio_cls = AudioIO if backend == "pyaudio" else AlsaProcessAudioIO
    audio = audio_cls(config, mic_queue=queue, on_playback_end=ended.set)
    audio.start()
    try:
        print("Audio dry run started. Capturing microphone for 3 seconds...")
        chunks = 0
        max_rms = 0
        max_peak = 0
        deadline = asyncio.get_running_loop().time() + 3
        while asyncio.get_running_loop().time() < deadline:
            chunk = await asyncio.wait_for(queue.get(), timeout=1)
            rms, peak = _pcm_stats(chunk)
            max_rms = max(max_rms, rms)
            max_peak = max(max_peak, peak)
            chunks += 1
        print(f"Captured {chunks} microphone chunks. max_rms={max_rms} max_peak={max_peak}")
    finally:
        audio.close()


def _pair_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis-satellite pair",
        description="Pair this speaker with JARV1S using a Rooms setup code.",
    )
    parser.add_argument("code", help="One-time pairing code from Rooms on the Mac")
    parser.add_argument(
        "--url",
        help="Host WebSocket URL (wss://…/api/v1/ws). Required on first pair.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state-dir", type=Path)
    return parser


def run_pair(argv: list[str]) -> int:
    args = _pair_parser().parse_args(argv)
    if sys.platform == "darwin" and not os.environ.get("JARVIS_SATELLITE_ALLOW_LOCAL_PAIR"):
        print(
            "Pairing runs on the room speaker.\n"
            "From this Mac: Rooms → Connect speaker, or task sat:pair -- CODE",
            file=sys.stderr,
        )
        return 2
    try:
        node_id = pair_and_write(
            code=args.code,
            url=args.url,
            config_path=args.config,
            state_dir=args.state_dir,
        )
    except (PairError, ValueError) as exc:
        print(f"Pairing failed: {exc}", file=sys.stderr)
        return 1
    print(f"Paired as {node_id}. Restart the speaker if it is already running.")
    return 0


async def async_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "pair":
        return run_pair(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list_devices:
        print_devices()
        return 0

    if args.dry_run_audio:
        await _dry_run_audio(config)
        return 0

    client = SatelliteClient(config, config_path=args.config)
    try:
        await client.run()
    except KeyboardInterrupt:
        await client.stop()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
