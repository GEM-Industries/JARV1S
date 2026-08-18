#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR="${1:-}"
if [[ -z "$BUNDLE_DIR" || ! -d "$BUNDLE_DIR" ]]; then
  echo "Usage: $0 <tauri-bundle-directory>" >&2
  exit 1
fi

MACOS_DIR="$BUNDLE_DIR/macos"
APP_CANDIDATES=("$MACOS_DIR"/*.app)
DMG_CANDIDATES=("$MACOS_DIR"/*.dmg)
MANIFEST_CANDIDATES=("$BUNDLE_DIR"/latest*.json)

APP="${APP_CANDIDATES[0]:-}"
DMG="${DMG_CANDIDATES[0]:-}"
if [[ ! -d "$APP" || ! -f "$DMG" ]]; then
  echo "Expected one .app and one .dmg under $MACOS_DIR" >&2
  exit 1
fi

codesign --verify --deep --strict --verbose=2 "$APP"
spctl --assess --type execute --verbose=2 "$APP"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
xcrun stapler validate "$APP"
xcrun stapler validate "$DMG"

MOUNT_POINT="$(mktemp -d)"
hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MOUNT_POINT" >/dev/null
trap 'hdiutil detach "$MOUNT_POINT" -quiet || true' EXIT
if [[ ! -L "$MOUNT_POINT/Applications" ]]; then
  echo "DMG is missing the /Applications drop link required for drag-to-install" >&2
  exit 1
fi

APP_ON_VOLUME="$(find "$MOUNT_POINT" -maxdepth 1 -name '*.app' -print -quit)"
PYTHON_ON_VOLUME="${APP_ON_VOLUME}/Contents/Resources/host/runtime/python/bin/python3"
if [[ ! -x "$PYTHON_ON_VOLUME" ]]; then
  echo "DMG app is missing relocatable Python at $PYTHON_ON_VOLUME" >&2
  exit 1
fi
# Read-only mount: proves the interpreter + deps work without build-tree paths
# and without writing into the app bundle.
"$PYTHON_ON_VOLUME" -c "import encodings, fastapi, uvicorn, motor"

hdiutil detach "$MOUNT_POINT" -quiet
trap - EXIT

if [[ ! -f "${MANIFEST_CANDIDATES[0]:-}" ]]; then
  echo "Updater manifest missing from $BUNDLE_DIR" >&2
  exit 1
fi

node - "${MANIFEST_CANDIDATES[@]}" <<'NODE'
const fs = require('node:fs')
for (const path of process.argv.slice(2)) {
  const manifest = JSON.parse(fs.readFileSync(path, 'utf8'))
  const platform = manifest.platforms?.['darwin-aarch64']
  if (!/^\d+\.\d+\.\d+/.test(manifest.version || '')) {
    throw new Error(`${path}: invalid version`)
  }
  if (!platform?.signature || !/^https:\/\//.test(platform.url || '')) {
    throw new Error(`${path}: missing signature or HTTPS artifact URL`)
  }
}
NODE

echo "Signed artifact integrity checks passed."
echo "Still required on clean hardware: install, microphone, phone wss://, revocation, update, and soak."
