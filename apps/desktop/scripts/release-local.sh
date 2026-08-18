#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jarv1s"
CONFIG_FILE="$CONFIG_DIR/release.env"
KEYCHAIN_SERVICE="dev.jarv1s.updater-signing"

store_updater_password() {
  local updater_password
  read -r -s -p "Tauri updater key password: " updater_password
  echo
  [[ -n "$updater_password" ]] || { echo "Updater key password is required." >&2; exit 1; }
  security add-generic-password -U \
    -a "$USER" \
    -s "$KEYCHAIN_SERVICE" \
    -w "$updater_password" >/dev/null
}

prompt() {
  local name="$1"
  local label="$2"
  local default="${3:-}"
  local value

  if [[ -n "$default" ]]; then
    read -r -p "$label [$default]: " value
    value="${value:-$default}"
  else
    read -r -p "$label: " value
  fi

  if [[ -z "$value" ]]; then
    echo "$label is required." >&2
    exit 1
  fi
  printf -v "$name" '%s' "$value"
}

setup_release() {
  local identity api_key_path api_key_id api_issuer updater_key_path
  local detected_identity=""
  local detected_api_key=""

  detected_identity="$(security find-identity -v -p codesigning 2>/dev/null | awk -F'"' '/Developer ID Application:/{print $2; exit}')"

  local candidates=("$HOME"/Downloads/AuthKey_*.p8)
  if [[ -f "${candidates[0]}" ]]; then
    detected_api_key="${candidates[0]}"
  fi

  echo "Configuring local JARV1S releases. These settings are saved to $CONFIG_FILE."
  prompt identity "Apple signing identity" "$detected_identity"
  prompt api_key_path "App Store Connect API key path" "$detected_api_key"
  [[ -f "$api_key_path" ]] || { echo "API key not found: $api_key_path" >&2; exit 1; }

  api_key_id="$(basename "$api_key_path")"
  api_key_id="${api_key_id#AuthKey_}"
  api_key_id="${api_key_id%.p8}"
  prompt api_key_id "App Store Connect API key ID" "$api_key_id"
  prompt api_issuer "App Store Connect issuer ID" "${APPLE_API_ISSUER:-}"
  prompt updater_key_path "Tauri updater private key path" "$HOME/.tauri/jarvis-updater.key"
  [[ -f "$updater_key_path" ]] || { echo "Updater key not found: $updater_key_path" >&2; exit 1; }

  if security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w >/dev/null 2>&1; then
    echo "Using the updater password already stored in macOS Keychain."
  else
    store_updater_password
  fi

  mkdir -p "$CONFIG_DIR"
  {
    printf 'APPLE_SIGNING_IDENTITY=%q\n' "$identity"
    printf 'APPLE_API_KEY_PATH=%q\n' "$api_key_path"
    printf 'APPLE_API_KEY_ID=%q\n' "$api_key_id"
    printf 'APPLE_API_ISSUER=%q\n' "$api_issuer"
    printf 'TAURI_SIGNING_PRIVATE_KEY_PATH=%q\n' "$updater_key_path"
  } >"$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
}

MODE="${1:-}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  setup_release
elif [[ "$MODE" == "--setup" ]]; then
  setup_release
fi

if [[ "$MODE" == "--retry-updater" ]]; then
  export JARVIS_UPDATER_ONLY=1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"
export APPLE_SIGNING_IDENTITY APPLE_API_KEY_PATH APPLE_API_KEY_ID APPLE_API_ISSUER
export TAURI_SIGNING_PRIVATE_KEY_PATH

[[ -f "$APPLE_API_KEY_PATH" ]] || { echo "API key not found: $APPLE_API_KEY_PATH. Run with --setup." >&2; exit 1; }
[[ -f "$TAURI_SIGNING_PRIVATE_KEY_PATH" ]] || { echo "Updater key not found: $TAURI_SIGNING_PRIVATE_KEY_PATH. Run with --setup." >&2; exit 1; }

TAURI_SIGNING_PRIVATE_KEY_PASSWORD="$(
  security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null
)" || {
  echo "Updater password is missing from Keychain. Run with --setup." >&2
  exit 1
}
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD

cd "$ROOT"
if [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1090
  source "$HOME/.nvm/nvm.sh"
  nvm use --silent
fi

task desktop:release
