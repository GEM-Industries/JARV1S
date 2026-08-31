#!/usr/bin/env bash
# Pair the Raspberry Pi speaker from this Mac: task sat:pair -- CODE
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CODE="${1:-}"
shift || true
URL=""
if [ "${1:-}" = "--url" ]; then
  URL="${2:-}"
elif [ -n "${1:-}" ]; then
  URL="$1"
fi

if [ -z "${CODE}" ]; then
  echo "Usage: task sat:pair -- CODE [--url wss://host/api/v1/ws]" >&2
  exit 1
fi

REMOTE="$("${SCRIPT_DIR}/resolve-remote.sh")"
echo "Pairing via ${REMOTE}"
ssh "${REMOTE}" bash -s -- "${CODE}" "${URL}" <<'EOF'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
code="$1"
url="${2:-}"
if [ -n "$url" ]; then
  jarvis-satellite pair "$code" --url "$url"
else
  jarvis-satellite pair "$code"
fi
EOF
ssh "${REMOTE}" "systemctl --user restart jarvis-satellite"
echo "Restarted jarvis-satellite on ${REMOTE}"
