#!/usr/bin/env bash
# Inside-out codesign for embedded Mach-O binaries before signing the top-level .app.
set -euo pipefail

APP_PATH="${1:?Usage: sign-nested-binaries.sh <path/to/JARV1S.app> [signing-identity]}"
IDENTITY="${2:-${APPLE_SIGNING_IDENTITY:-}}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONGOD_ENTITLEMENTS="$SCRIPT_DIR/mongod-entitlements.plist"
SPEECH_HELPER_ENTITLEMENTS="$SCRIPT_DIR/speech-helper-entitlements.plist"
OLLAMA_HELPER_ENTITLEMENTS="$SCRIPT_DIR/ollama-helper-entitlements.plist"

if [[ -z "$IDENTITY" ]]; then
  echo "APPLE_SIGNING_IDENTITY or second argument required" >&2
  exit 1
fi

sign_file() {
  local file="$1"
  local entitlements="${2:-}"
  if [[ -n "$entitlements" ]]; then
    codesign --force --options runtime --timestamp \
      --entitlements "$entitlements" \
      --sign "$IDENTITY" "$file"
  else
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$file"
  fi
}

is_macho() {
  local description
  description="$(file -b "$1" 2>/dev/null || true)"
  [[ "$description" == *"Mach-O"* ]]
}

sign_tree() {
  local root="$1"
  [[ -d "$root" ]] || return 0
  while IFS= read -r -d '' file; do
    if ! is_macho "$file"; then
      continue
    fi
    if [[ "$file" == *"/services/mongodb/bin/mongod" ]]; then
      sign_file "$file" "$MONGOD_ENTITLEMENTS"
    else
      sign_file "$file"
    fi
    codesign --verify --verbose=2 "$file"
  done < <(find "$root" -type f -print0)
}

sign_tree "$APP_PATH/Contents/Resources/host/runtime"
sign_tree "$APP_PATH/Contents/Resources/host/services"

HELPER_APP="$APP_PATH/Contents/Helpers/JARV1SSpeechHelper.app"
if [[ -d "$HELPER_APP" ]]; then
  # Inside-out: binary first, then the helper .app bundle.
  HELPER_BIN="$HELPER_APP/Contents/MacOS/JARV1SSpeechHelper"
  if [[ -f "$HELPER_BIN" ]]; then
    sign_file "$HELPER_BIN" "$SPEECH_HELPER_ENTITLEMENTS"
  fi
  sign_file "$HELPER_APP" "$SPEECH_HELPER_ENTITLEMENTS"
  codesign --verify --verbose=2 "$HELPER_APP"
fi

OLLAMA_DIR="$APP_PATH/Contents/Resources/ollama-runtime"
# Legacy Helpers paths from early managed-LLM builds.
if [[ ! -d "$OLLAMA_DIR" && -d "$APP_PATH/Contents/Helpers/ollama-runtime" ]]; then
  OLLAMA_DIR="$APP_PATH/Contents/Helpers/ollama-runtime"
fi
if [[ ! -d "$OLLAMA_DIR" && -d "$APP_PATH/Contents/Helpers/Ollama" ]]; then
  OLLAMA_DIR="$APP_PATH/Contents/Helpers/Ollama"
fi
if [[ -d "$OLLAMA_DIR" ]]; then
  while IFS= read -r -d '' file; do
    base="$(basename "$file")"
    if is_macho "$file" || [[ "$base" == *.metallib ]]; then
      case "$base" in
        ollama|llama-server|llama-quantize)
          sign_file "$file" "$OLLAMA_HELPER_ENTITLEMENTS"
          ;;
        *)
          sign_file "$file"
          ;;
      esac
      codesign --verify --verbose=2 "$file"
    fi
  done < <(find "$OLLAMA_DIR" -type f -print0)
fi

echo "Nested binaries signed under $APP_PATH"
