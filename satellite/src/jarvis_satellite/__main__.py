"""Command line entry point for the JARV1S satellite."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import sys

from .audio import AlsaProcessAudioIO, AudioIO, print_devices, pyaudio_available
from .client import SatelliteClient
from .config import build_parser, load_config


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


async def async_main(argv: list[str] | None = None) -> int:
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

    client = SatelliteClient(config)
    try:
        await client.run()
    except KeyboardInterrupt:
        await client.stop()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
