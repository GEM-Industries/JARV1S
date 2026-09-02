#!/usr/bin/env bash
# Download and verify pinned Kokoro TTS assets for the Host bundle / local helper.
# Downloads land in a persistent cache so host rebuilds (which wipe DEST) skip re-fetch.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
MANIFEST="$DESKTOP/local-tts/manifest.json"
OUT_DIR="${1:-$DESKTOP/local-tts}"
# Persist outside resources/host (wiped every rebuild). Prefer the repo local-tts
# dir so existing verified assets are reused; override with JARVIS_LOCAL_TTS_CACHE.
CACHE_DIR="${JARVIS_LOCAL_TTS_CACHE:-$DESKTOP/local-tts}"

if [[ ! -f "$MANIFEST" ]]; then
  echo "missing local TTS manifest: $MANIFEST" >&2
  exit 1
fi

mkdir -p "$CACHE_DIR" "$OUT_DIR"
cp "$MANIFEST" "$OUT_DIR/manifest.json"

python3 - "$MANIFEST" "$CACHE_DIR" "$OUT_DIR" <<'PY'
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

manifest_path = Path(sys.argv[1])
cache_dir = Path(sys.argv[2])
out_dir = Path(sys.argv[3])
raw = json.loads(manifest_path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


for asset in raw["assets"]:
    name = asset["name"]
    url = asset["url"]
    expected = asset["sha256"].lower()
    cached = cache_dir / name
    dest = out_dir / name

    if (
        dest.is_file()
        and cached.is_file()
        and dest.stat().st_size == cached.stat().st_size
        and dest.stat().st_mtime >= cached.stat().st_mtime
    ):
        print(f"ok {name}")
        continue

    if cached.is_file() and sha256_file(cached) == expected:
        print(f"cache hit {name}")
    else:
        if cached.is_file():
            print(f"re-download {name} (hash mismatch)")
            cached.unlink()
        print(f"download {name}")
        partial = cached.with_suffix(cached.suffix + ".partial")
        urllib.request.urlretrieve(url, partial)
        digest = sha256_file(partial)
        if digest != expected:
            partial.unlink(missing_ok=True)
            raise SystemExit(f"sha256 mismatch for {name}: got {digest}, expected {expected}")
        partial.replace(cached)
        print(f"verified {name}")

    if dest.resolve() != cached.resolve():
        shutil.copy2(cached, dest)
    print(f"ok {name}")
PY

echo "Local TTS assets ready at $OUT_DIR"
