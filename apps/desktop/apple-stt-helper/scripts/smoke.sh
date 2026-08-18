#!/usr/bin/env bash
set -euo pipefail

HELPER_BIN="${1:?Usage: smoke.sh <helper-binary> <python-binary>}"
PYTHON_BIN="${2:?Usage: smoke.sh <helper-binary> <python-binary>}"

"$PYTHON_BIN" - "$HELPER_BIN" <<'PY'
import asyncio
import json
import os
import socket
import sys

import websockets


async def main() -> None:
    helper_bin = sys.argv[1]
    token = "jarvis-speech-smoke"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = {
        **os.environ,
        "JARVIS_SPEECH_HOST": "127.0.0.1",
        "JARVIS_SPEECH_PORT": str(port),
        "JARVIS_SPEECH_TOKEN": token,
    }
    process = await asyncio.create_subprocess_exec(
        helper_bin,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        url = f"ws://127.0.0.1:{port}/asr"
        for _ in range(40):
            if process.returncode is not None:
                _, stderr = await process.communicate()
                raise RuntimeError(f"Speech helper exited: {stderr.decode().strip()}")
            try:
                async with websockets.connect(url, open_timeout=0.25) as ws:
                    await ws.send(json.dumps({"type": "status", "token": token}))
                    payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                    if payload.get("type") != "status" or payload.get("state") not in {
                        "ready",
                        "needs_permission",
                        "needs_assets",
                        "unavailable",
                        "unsupported",
                    }:
                        raise RuntimeError(f"Invalid speech helper status: {payload}")
                    print(f"Apple Speech helper smoke ok state={payload['state']}")
                    return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.05)
        raise RuntimeError("Apple Speech helper did not answer a status request")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


asyncio.run(main())
PY
