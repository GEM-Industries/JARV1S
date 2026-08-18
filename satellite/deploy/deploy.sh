#!/usr/bin/env bash
set -euo pipefail

SATELLITE_HOST="${SATELLITE_HOST:-jarvis-satellite-1.local}"
SATELLITE_USER="${SATELLITE_USER:-pi}"
REMOTE_ROOT="${REMOTE_ROOT:-/home/${SATELLITE_USER}/.jarvis-satellite}"
REMOTE_APP="${REMOTE_ROOT}/app"
SERVICE_NAME="${SERVICE_NAME:-jarvis-satellite}"
SATELLITE_WRITE_CONFIG="${SATELLITE_WRITE_CONFIG:-0}"

default_brain_host() {
  local name=""
  if command -v scutil >/dev/null 2>&1; then
    name="$(scutil --get LocalHostName 2>/dev/null || true)"
  fi
  if [ -z "${name}" ]; then
    name="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
    name="${name%%.*}"
  fi
  if [ -n "${name}" ]; then
    printf "%s.local" "${name}"
  else
    printf "localhost"
  fi
}

SATELLITE_BRAIN_HOST="${SATELLITE_BRAIN_HOST:-$(default_brain_host)}"
SATELLITE_BACKEND_URL="${SATELLITE_BACKEND_URL:-ws://${SATELLITE_BRAIN_HOST}:8000/api/v1/ws}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SATELLITE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd -- "${SATELLITE_DIR}/.." && pwd)"
FRONTEND_SOUNDS_DIR="${REPO_ROOT}/frontend/public/sounds"
WAKEWORD_MODEL="${REPO_ROOT}/backend/resources/models/wakeword/Jarvis.onnx"
SATELLITE_EDGE_WAKEWORD="${SATELLITE_EDGE_WAKEWORD:-0}"
REMOTE="${SATELLITE_USER}@${SATELLITE_HOST}"

echo "Deploying ${SATELLITE_DIR} to ${REMOTE}:${REMOTE_APP}"
ssh "${REMOTE}" "mkdir -p '${REMOTE_APP}' '${REMOTE_ROOT}/models'"
EDGE_WAKE_ENABLED="${SATELLITE_EDGE_WAKEWORD}"
if [ "${EDGE_WAKE_ENABLED}" != "1" ] \
  && [ "${SATELLITE_WRITE_CONFIG}" != "1" ] \
  && ssh "${REMOTE}" "grep -Eq '^edge_wakeword[[:space:]]*=[[:space:]]*true' '${REMOTE_ROOT}/config.toml' 2>/dev/null"; then
  EDGE_WAKE_ENABLED="1"
fi
rsync -az --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  "${SATELLITE_DIR}/" "${REMOTE}:${REMOTE_APP}/"
if [ -d "${FRONTEND_SOUNDS_DIR}" ]; then
  ssh "${REMOTE}" "mkdir -p '${REMOTE_APP}/src/jarvis_satellite/assets/sounds'"
  rsync -az --delete --include '*.wav' --exclude '*' "${FRONTEND_SOUNDS_DIR}/" "${REMOTE}:${REMOTE_APP}/src/jarvis_satellite/assets/sounds/"
fi
if [ "${EDGE_WAKE_ENABLED}" = "1" ]; then
  if [ ! -f "${WAKEWORD_MODEL}" ]; then
    echo "Missing wakeword model: ${WAKEWORD_MODEL}" >&2
    exit 1
  fi
  rsync -az "${WAKEWORD_MODEL}" "${REMOTE}:${REMOTE_ROOT}/models/Jarvis.onnx"
fi

ssh "${REMOTE}" "if ! command -v uv >/dev/null 2>&1 && [ ! -x \"\$HOME/.local/bin/uv\" ]; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi"

ssh "${REMOTE}" "if [ '${SATELLITE_WRITE_CONFIG}' = '1' ] || [ ! -f '${REMOTE_ROOT}/config.toml' ]; then cat > '${REMOTE_ROOT}/config.toml' <<'EOF'
backend_url = \"${SATELLITE_BACKEND_URL}\"
timezone = \"Australia/Sydney\"
node_id = \"jarvis-satellite-1\"
node_label = \"Bedroom Satellite\"
capabilities = [\"mic\", \"speaker\"]
audio_backend = \"alsa\"
input_device = \"plughw:Array,0\"
output_device = \"plughw:Array,0\"
input_channels = 2
input_channel_index = 1
playback_channels = 2
tts_end_timeout_s = 2.0
# Local safety override only; the normal Tool cues toggle is owner-wide on the host.
tool_cues_enabled = true
log_level = \"INFO\"
EOF
fi"

if [ "${SATELLITE_EDGE_WAKEWORD}" = "1" ]; then
  ssh "${REMOTE}" "python3 - '${REMOTE_ROOT}/config.toml' '${REMOTE_ROOT}/models/Jarvis.onnx'" <<'PY'
import re
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
model_path = sys.argv[2]
text = config_path.read_text()

def set_value(key: str, value: str) -> None:
    global text
    pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
    replacement = f"{key} = {value}"
    text, count = re.subn(pattern, replacement, text)
    if count == 0:
        text = f"{text.rstrip()}\n{replacement}\n"

set_value("edge_wakeword", "true")
set_value("wakeword_model_path", f'"{model_path}"')
set_value("wake_preroll_seconds", "3.0")
config_path.write_text(text)
PY
fi
UV_SYNC_EXTRAS=""
if [ "${EDGE_WAKE_ENABLED}" = "1" ]; then
  UV_SYNC_EXTRAS="--extra wakeword"
fi
ssh "${REMOTE}" "export PATH=\"\$HOME/.local/bin:\$PATH\" && cd '${REMOTE_APP}' && uv sync ${UV_SYNC_EXTRAS}"
ssh "${REMOTE}" "mkdir -p '/home/${SATELLITE_USER}/.config/systemd/user' && cp '${REMOTE_APP}/deploy/jarvis-satellite.service' '/home/${SATELLITE_USER}/.config/systemd/user/${SERVICE_NAME}.service'"
ssh "${REMOTE}" "systemctl --user daemon-reload && systemctl --user enable '${SERVICE_NAME}.service' && systemctl --user restart '${SERVICE_NAME}.service'"

echo "Deployed. Follow logs with:"
echo "ssh ${REMOTE} 'journalctl --user -u ${SERVICE_NAME} -f'"
if [ "${EDGE_WAKE_ENABLED}" = "1" ]; then
  echo "Edge wakeword enabled (model under ${REMOTE_ROOT}/models/Jarvis.onnx)."
fi
