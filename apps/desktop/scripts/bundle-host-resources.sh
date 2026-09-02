#!/usr/bin/env bash
# Copy the packaged Host runtime into a built .app.
#
# Tauri's bundle.resources walks/copies every file during cargo build.rs.
# Host + Ollama are ~2.5GB, so they are staged here once after `tauri build`
# instead of being listed in tauri.conf.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOST_SRC="$ROOT/apps/desktop/resources/host"
OLLAMA_SRC="$ROOT/apps/desktop/resources/helpers/Ollama"
APP_PATH="${1:-}"

if [[ -z "$APP_PATH" ]]; then
  for candidate in \
    "$ROOT/apps/desktop/src-tauri/target/release/bundle/macos/JARV1S.app" \
    "$ROOT/apps/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/JARV1S.app"
  do
    if [[ -d "$candidate" ]]; then
      APP_PATH="$candidate"
      break
    fi
  done
fi

if [[ -z "$APP_PATH" || ! -d "$APP_PATH" ]]; then
  echo "Built app not found. Pass the .app path or run tauri build first." >&2
  exit 1
fi

if [[ ! -x "$HOST_SRC/runtime/python/bin/python3" ]]; then
  echo "Host runtime missing at $HOST_SRC (run npm run build:host-runtime first)." >&2
  exit 1
fi
if [[ ! -x "$OLLAMA_SRC/ollama" ]]; then
  echo "Ollama helper missing at $OLLAMA_SRC (run npm run build:host-runtime first)." >&2
  exit 1
fi

clone_or_copy() {
  local src="$1"
  local dest="$2"
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  if cp -cR "$src" "$dest" 2>/dev/null; then
    return
  fi
  ditto "$src" "$dest"
}

RESOURCES="$APP_PATH/Contents/Resources"
echo "Staging Host runtime into $APP_PATH"
clone_or_copy "$HOST_SRC" "$RESOURCES/host"
clone_or_copy "$OLLAMA_SRC" "$RESOURCES/ollama-runtime"

if [[ ! -x "$RESOURCES/host/runtime/python/bin/python3" ]]; then
  echo "Staged python3 missing under $RESOURCES/host" >&2
  exit 1
fi
if [[ ! -x "$RESOURCES/ollama-runtime/ollama" ]]; then
  echo "Staged ollama missing under $RESOURCES/ollama-runtime" >&2
  exit 1
fi
echo "Host runtime staged"
