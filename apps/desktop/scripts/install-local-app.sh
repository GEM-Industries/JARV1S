#!/usr/bin/env bash
# Replace the local /Applications dogfood app with the latest unsigned build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
BUILT_APP="$ROOT/apps/desktop/src-tauri/target/release/bundle/macos/JARV1S.app"
INSTALL_APP="${JARVIS_INSTALL_APP_PATH:-/Applications/JARV1S.app}"
APP_ID="dev.jarv1s.host"
APP_NAME="JARV1S"
APP_EXECUTABLE="$(defaults read "$BUILT_APP/Contents/Info" CFBundleExecutable)"
APP_PROCESS_PATTERN="$INSTALL_APP/Contents/MacOS/$APP_EXECUTABLE"

app_is_running() {
  pgrep -f "$APP_PROCESS_PATTERN" >/dev/null 2>&1
}

if [[ ! -d "$BUILT_APP" ]]; then
  echo "Built app not found: $BUILT_APP" >&2
  echo "Run task desktop:build before installing locally." >&2
  exit 1
fi

echo "Quitting running $APP_NAME app if needed..."
osascript -e "tell application id \"$APP_ID\" to quit" >/dev/null 2>&1 || true
osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true

for _ in {1..20}; do
  if ! app_is_running; then
    break
  fi
  sleep 0.5
done

if app_is_running; then
  echo "$APP_NAME is still running. Please quit it and retry." >&2
  exit 1
fi

HOST_PATTERN="$INSTALL_APP/Contents/Resources/host"
PID_FILE="$(mktemp)"
if pgrep -f "$HOST_PATTERN" >"$PID_FILE" 2>/dev/null; then
  echo "Stopping stale packaged backend processes..."
  while IFS= read -r pid; do
    kill "$pid" 2>/dev/null || true
  done <"$PID_FILE"
  sleep 1
  while IFS= read -r pid; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done <"$PID_FILE"
fi
rm -f "$PID_FILE"

echo "Installing $BUILT_APP -> $INSTALL_APP"
mkdir -p "$(dirname "$INSTALL_APP")"
TMP_APP="${INSTALL_APP}.installing"
rm -rf "$TMP_APP"
ditto "$BUILT_APP" "$TMP_APP"
rm -rf "$INSTALL_APP"
mv "$TMP_APP" "$INSTALL_APP"

# Locally built unsigned apps should launch without Gatekeeper quarantine prompts.
xattr -dr com.apple.quarantine "$INSTALL_APP" >/dev/null 2>&1 || true

echo "Opening $INSTALL_APP"
open "$INSTALL_APP"
