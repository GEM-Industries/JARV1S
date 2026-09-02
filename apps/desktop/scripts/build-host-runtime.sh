#!/usr/bin/env bash
# Build app-owned Host runtime assets for Tauri bundle (arm64 macOS first).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DEST="$ROOT/apps/desktop/resources/host"
PYTHON_VERSION="${JARVIS_PYTHON_VERSION:-3.12}"
ARCH="$(uname -m)"

echo "Building Host runtime into $DEST"
mkdir -p "$DEST/backend" "$DEST/frontend-dist" "$DEST/runtime"

RUNTIME_STAMP="$(
  cat "$ROOT/backend/uv.lock" "$ROOT/.python-version" "$ROOT/apps/desktop/services/versions.json" \
    | shasum -a 256 | awk '{print $1}'
)"
STAMP_FILE="$DEST/.runtime-stamp"
PYTHON_BIN="$DEST/runtime/python/bin/python3"
REUSE_PYTHON=0
if [[ -x "$PYTHON_BIN" && -f "$STAMP_FILE" && "$(cat "$STAMP_FILE")" == "$RUNTIME_STAMP" ]]; then
  REUSE_PYTHON=1
  echo "Reusing bundled Python (uv.lock / Python / Mongo pin unchanged)"
else
  rm -rf "$DEST/runtime"
  mkdir -p "$DEST/runtime"
fi

echo "Building bundled MongoDB service binary..."
bash "$(dirname "$0")/build-service-binaries.sh"

if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
  echo "Building frontend..."
  (cd "$ROOT/frontend" && npm ci && npm run build)
fi
mkdir -p "$DEST/frontend-dist"
rsync -a --delete "$ROOT/frontend/dist/" "$DEST/frontend-dist/"

echo "Copying backend source..."
mkdir -p "$DEST/backend"
rsync -a --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'tests' \
  --exclude '.pytest_cache' \
  --exclude '.mypy_cache' \
  --exclude '.ruff_cache' \
  --exclude '.cache' \
  --exclude 'graphify-out' \
  --exclude 'evals' \
  --exclude 'htmlcov' \
  --exclude '.coverage' \
  --exclude 'logs' \
  --exclude '.grimp_cache' \
  --exclude 'uv.lock' \
  "$ROOT/backend/" "$DEST/backend/"

cp "$ROOT/docker-compose.yml" "$DEST/docker-compose.yml"

if [[ "$REUSE_PYTHON" == "1" ]]; then
  PYTHON_BIN_DIR="$DEST/runtime/python/bin"
else
echo "Installing relocatable Python baseline..."
PYTHON_STAGE="$(mktemp -d)"
trap 'rm -rf "$PYTHON_STAGE"' EXIT
uv python install "$PYTHON_VERSION" --install-dir "$PYTHON_STAGE"

# uv lays out cpython-<ver>-<platform>/ under the install dir, plus metadata
# (.temp, .lock) and an absolute-path symlink. Ship only the distribution tree.
PYTHON_DIST="$(find "$PYTHON_STAGE" -maxdepth 1 -type d -name 'cpython-*' | head -1)"
if [[ -z "$PYTHON_DIST" || ! -x "$PYTHON_DIST/bin/python3" ]]; then
  echo "Python distribution not found under $PYTHON_STAGE" >&2
  exit 1
fi
rm -rf "$DEST/runtime/python"
mv "$PYTHON_DIST" "$DEST/runtime/python"
rm -rf "$PYTHON_STAGE"
trap - EXIT

PYTHON_BIN_DIR="$DEST/runtime/python/bin"
PYTHON_BIN="$PYTHON_BIN_DIR/python3"
# Flatten bin/: keep a single real python3. Tauri dereferences symlinks when
# bundling, so leftover interpreter links become duplicate ~17MB copies, and
# pip*/config scripts carry absolute build-tree shebangs.
REAL_PY=""
for candidate in "$PYTHON_BIN_DIR"/python3.*; do
  [[ -f "$candidate" && "$candidate" != *-config ]] || continue
  REAL_PY="$candidate"
  break
done
if [[ -z "$REAL_PY" || ! -x "$REAL_PY" ]]; then
  echo "Versioned python3.* binary not found under $PYTHON_BIN_DIR" >&2
  exit 1
fi
for entry in "$PYTHON_BIN_DIR"/*; do
  [[ "$(basename "$entry")" == "$(basename "$REAL_PY")" ]] && continue
  rm -rf "$entry"
done
mv "$REAL_PY" "$PYTHON_BIN"
chmod +x "$PYTHON_BIN"

REQ_FILE="$DEST/runtime/requirements.txt"
echo "Exporting locked dependencies..."
(cd "$ROOT/backend" && uv export --frozen --no-dev --no-editable --no-hashes -o "$REQ_FILE.tmp")
grep -v '^\.$' "$REQ_FILE.tmp" >"$REQ_FILE"
rm -f "$REQ_FILE.tmp"

echo "Installing locked dependencies into bundled Python..."
uv pip install --python "$PYTHON_BIN" --break-system-packages -r "$REQ_FILE"

# Drop console-script wrappers; they embed absolute shebangs and are unused
# (the Host launches via `python3 -m uvicorn`).
for entry in "$PYTHON_BIN_DIR"/*; do
  [[ "$(basename "$entry")" == "python3" ]] && continue
  rm -rf "$entry"
done
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python3 missing after bin prune" >&2
  exit 1
fi
echo "$RUNTIME_STAMP" > "$STAMP_FILE"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Bundled python3 missing at $PYTHON_BIN" >&2
  exit 1
fi

echo "Installing openWakeWord ONNX resource models..."
"$PYTHON_BIN" -c "from openwakeword.utils import download_models; download_models(['silero_vad'])"

OWW_MODELS_DIR="$("$PYTHON_BIN" -c "import openwakeword, os; print(os.path.join(os.path.dirname(openwakeword.__file__), 'resources', 'models'))")"
for required_model in embedding_model.onnx melspectrogram.onnx silero_vad.onnx; do
  if [[ ! -f "$OWW_MODELS_DIR/$required_model" ]]; then
    echo "Missing required openWakeWord model: $OWW_MODELS_DIR/$required_model" >&2
    exit 1
  fi
done

JARVIS_WAKEWORD_MODEL="$DEST/backend/resources/models/wakeword/Jarvis.onnx"
if [[ ! -f "$JARVIS_WAKEWORD_MODEL" ]]; then
  echo "Missing Jarvis wakeword model: $JARVIS_WAKEWORD_MODEL" >&2
  exit 1
fi
"$PYTHON_BIN" - "$JARVIS_WAKEWORD_MODEL" <<'PY'
import sys
from openwakeword.model import Model

Model(wakeword_models=[sys.argv[1]], inference_framework="onnx", vad_threshold=0.4)
PY

JARVIS_SPEAKER_MODEL="$DEST/backend/resources/models/speaker/nemo_en_titanet_small.onnx"
if [[ ! -f "$JARVIS_SPEAKER_MODEL" ]]; then
  echo "Missing speaker verifier model: $JARVIS_SPEAKER_MODEL" >&2
  exit 1
fi
PYTHONPATH="$DEST/backend" "$PYTHON_BIN" - <<'PY'
from core.voice.wakeword.factory import build_default_wake_verifiers
from core.voice.wakeword.verifiers import AcceptAllWakeVerifier

verifiers = build_default_wake_verifiers(owner_id=None)
assert len(verifiers) == 1
assert isinstance(verifiers[0], AcceptAllWakeVerifier)
PY

GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo local)"
PYTHON_VER="$("$PYTHON_BIN" --version 2>&1 | awk '{print $2}')"
HOST_VERSION="$(awk -F '"' '/^version = / { print $2; exit }' "$ROOT/backend/pyproject.toml")"
RELEASE_CHANNEL="${JARVIS_RELEASE_CHANNEL:-internal}"
if [[ "$RELEASE_CHANNEL" != "internal" && "$RELEASE_CHANNEL" != "beta" ]]; then
  echo "Unsupported JARVIS_RELEASE_CHANNEL: $RELEASE_CHANNEL" >&2
  exit 1
fi

BUNDLE_JSON="$DEST/runtime-bundle.json"
python3 - "$BUNDLE_JSON" "$HOST_VERSION" "$GIT_SHA" "$PYTHON_VER" "$ARCH" "$RELEASE_CHANNEL" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
desired = {
    "host_version": sys.argv[2],
    "runtime_bundle": sys.argv[3],
    "frontend_build": sys.argv[3],
    "python": sys.argv[4],
    "arch": sys.argv[5],
    "service_provider": "bundled",
    "release_channel": sys.argv[6],
}
current = {}
if path.is_file():
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        current = {}
if all(current.get(key) == value for key, value in desired.items()):
    sys.exit(0)
desired["built_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
path.write_text(json.dumps(desired, indent=2) + "\n", encoding="utf-8")
PY

cp "$ROOT/apps/desktop/THIRD_PARTY_NOTICES.md" "$DEST/THIRD_PARTY_NOTICES.md"

OAUTH_SRC="${JARVIS_PRODUCT_OAUTH_FILE:-$ROOT/apps/desktop/resources/product_oauth.json}"
if [[ -f "$OAUTH_SRC" ]]; then
  cp "$OAUTH_SRC" "$DEST/product_oauth.json"
  echo "Bundled product OAuth identity"
else
  echo "No product_oauth.json — Google connect will use Advanced until CI supplies it"
fi

echo "Building Apple Speech helper..."
HELPER_DEST="$ROOT/apps/desktop/resources/helpers"
bash "$ROOT/apps/desktop/apple-stt-helper/scripts/build.sh" "$HELPER_DEST"
bash "$ROOT/apps/desktop/apple-stt-helper/scripts/smoke.sh" \
  "$HELPER_DEST/JARV1SSpeechHelper.app/Contents/MacOS/JARV1SSpeechHelper" \
  "$PYTHON_BIN"

echo "Packaging managed Ollama helper..."
bash "$ROOT/apps/desktop/scripts/build-ollama-helper.sh" "$HELPER_DEST/Ollama"
cp "$ROOT/apps/desktop/managed-llm/manifest.json" \
  "$DEST/backend/core/setup/managed_llm_manifest.json"

echo "Packaging local Kokoro TTS assets..."
bash "$ROOT/apps/desktop/scripts/build-local-tts-assets.sh" "$DEST/local-tts"
if [[ "$REUSE_PYTHON" == "1" && -f "$DEST/local-tts/kokoro-v1.0.int8.onnx" ]]; then
  echo "Skipping Kokoro smoke (Python runtime unchanged)"
else
  # Fail the release build if the helper cannot synthesize with bundled deps/assets.
  JARVIS_TTS_ASSETS_DIR="$DEST/local-tts" "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import numpy as np
from kokoro_onnx import Kokoro

assets = Path(__import__("os").environ["JARVIS_TTS_ASSETS_DIR"])
kokoro = Kokoro(str(assets / "kokoro-v1.0.int8.onnx"), str(assets / "voices-v1.0.bin"))
samples, sr = kokoro.create("Hello.", voice="af_heart", speed=1.0, lang="en-us")
audio = np.asarray(samples, dtype=np.float32)
assert int(sr) == 24000, sr
assert audio.size > 0 and np.isfinite(audio).all()
print(f"Kokoro smoke ok samples={audio.size} sr={sr}")
PY
fi

if [[ "$REUSE_PYTHON" == "1" ]]; then
  echo "Skipping bundled service smoke test (Python runtime unchanged)"
else
  echo "Running bundled service smoke test..."
  bash "$(dirname "$0")/smoke-services.sh"
fi

echo "Host runtime ready at $DEST"
