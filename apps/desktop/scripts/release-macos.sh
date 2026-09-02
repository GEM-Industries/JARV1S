#!/usr/bin/env bash
# arm64 macOS release pipeline for Phase 1b technical beta.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
IDENTITY="${APPLE_SIGNING_IDENTITY:-}"

if [[ -z "$IDENTITY" ]]; then
  echo "APPLE_SIGNING_IDENTITY is required for desktop:release. Use task desktop:build for unsigned local builds." >&2
  exit 1
fi

cd "$DESKTOP"

if [[ -n "${APPLE_API_KEY:-}" && -z "${APPLE_API_KEY_PATH:-}" ]]; then
  mkdir -p "$DESKTOP/src-tauri/target"
  export APPLE_API_KEY_PATH="$DESKTOP/src-tauri/target/notary-api-key.p8"
  printf '%s' "$APPLE_API_KEY" >"$APPLE_API_KEY_PATH"
fi

if [[ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" && -z "${TAURI_SIGNING_PRIVATE_KEY_PATH:-}" ]]; then
  echo "TAURI_SIGNING_PRIVATE_KEY or TAURI_SIGNING_PRIVATE_KEY_PATH is required for updater artifacts." >&2
  exit 1
fi

SIGNING_PROBE="$(mktemp)"
trap 'rm -f "$SIGNING_PROBE"' EXIT
printf 'jarvis-updater-signing-probe' >"$SIGNING_PROBE"
if ! ./node_modules/.bin/tauri signer sign "$SIGNING_PROBE" >/dev/null; then
  echo "Updater key validation failed before build. Check TAURI_SIGNING_PRIVATE_KEY_PASSWORD." >&2
  exit 1
fi
rm -f "$SIGNING_PROBE"
trap - EXIT

if [[ "${JARVIS_UPDATER_ONLY:-}" == "1" ]]; then
  BUNDLE_DIR="$DESKTOP/src-tauri/target/aarch64-apple-darwin/release/bundle"
  APP_PATH="$(find "$BUNDLE_DIR/macos" -maxdepth 1 -name '*.app' -print -quit)"
  VERSION="$(node -p "JSON.parse(require('fs').readFileSync('$DESKTOP/src-tauri/tauri.conf.json','utf8')).version")"
  DMG_PATH="$BUNDLE_DIR/macos/JARV1S_${VERSION}_aarch64.dmg"

  [[ -d "$APP_PATH" ]] || { echo "Existing release app not found in $BUNDLE_DIR/macos" >&2; exit 1; }
  [[ -f "$DMG_PATH" ]] || { echo "Existing release DMG not found: $DMG_PATH" >&2; exit 1; }
  APP_NAME="$(basename "$APP_PATH")"
  UPDATER_TAR="$BUNDLE_DIR/macos/${APP_NAME}.tar.gz"
  spctl --assess --type execute --verbose=4 "$APP_PATH"
  spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG_PATH"
  echo "Reusing the existing notarized app and DMG."
else
if ! command -v create-dmg >/dev/null 2>&1; then
  echo "Installing create-dmg via Homebrew..."
  brew install create-dmg
fi

echo "Syncing app version from backend..."
node scripts/sync-app-version.mjs

echo "Building Host runtime assets..."
bash scripts/build-host-runtime.sh

echo "Building Tauri app..."
# Sign outside `tauri build`: nested Helpers (Ollama metallibs/dylibs) must be
# signed inside-out first, then the top-level .app — see sign-nested-binaries.sh.
env \
  -u APPLE_SIGNING_IDENTITY \
  -u APPLE_API_KEY \
  -u APPLE_API_KEY_PATH \
  -u APPLE_API_KEY_ID \
  -u APPLE_API_ISSUER \
  -u APPLE_ID \
  -u APPLE_PASSWORD \
  -u APPLE_TEAM_ID \
  npm run tauri build -- --target aarch64-apple-darwin --bundles app

BUNDLE_DIR="$DESKTOP/src-tauri/target/aarch64-apple-darwin/release/bundle"
APP_PATH="$(find "$BUNDLE_DIR/macos" -maxdepth 1 -name '*.app' -print -quit)"

if [[ ! -d "$APP_PATH" ]]; then
  echo "Built .app not found in $BUNDLE_DIR/macos" >&2
  exit 1
fi

echo "Staging Host runtime into the app bundle..."
bash scripts/bundle-host-resources.sh "$APP_PATH"

VERSION="$(node -p "JSON.parse(require('fs').readFileSync('$DESKTOP/src-tauri/tauri.conf.json','utf8')).version")"
DMG_PATH="$BUNDLE_DIR/macos/JARV1S_${VERSION}_aarch64.dmg"
APP_ZIP="$BUNDLE_DIR/macos/JARV1S_${VERSION}_aarch64.zip"
APP_NAME="$(basename "$APP_PATH")"
UPDATER_TAR="$BUNDLE_DIR/macos/${APP_NAME}.tar.gz"

echo "Signing nested Python/native binaries..."
bash scripts/sign-nested-binaries.sh "$APP_PATH" "$IDENTITY"

echo "Running signed service smoke test..."
MONGOD="$APP_PATH/Contents/Resources/host/services/mongodb/bin/mongod" \
PYTHON="$APP_PATH/Contents/Resources/host/runtime/python/bin/python3" \
  bash scripts/smoke-services.sh

echo "Signing top-level app bundle..."
codesign --force --options runtime --timestamp \
  --entitlements "$DESKTOP/src-tauri/Entitlements.plist" \
  --sign "$IDENTITY" "$APP_PATH"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

notarytool_submit() {
  local artifact="$1"
  if [[ "${JARVIS_SKIP_NOTARIZATION:-}" == "1" ]]; then
    echo "Skipping notarization for $artifact because JARVIS_SKIP_NOTARIZATION=1"
    return
  fi

  if [[ -n "${APPLE_API_KEY_PATH:-}" && -n "${APPLE_API_KEY_ID:-}" && -n "${APPLE_API_ISSUER:-}" ]]; then
    xcrun notarytool submit "$artifact" \
      --key "$APPLE_API_KEY_PATH" \
      --key-id "$APPLE_API_KEY_ID" \
      --issuer "$APPLE_API_ISSUER" \
      --wait
  elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
    xcrun notarytool submit "$artifact" \
      --apple-id "$APPLE_ID" \
      --password "$APPLE_PASSWORD" \
      --team-id "$APPLE_TEAM_ID" \
      --wait
  else
    echo "Notarization credentials are required. Set APPLE_API_KEY_PATH/APPLE_API_KEY_ID/APPLE_API_ISSUER or APPLE_ID/APPLE_PASSWORD/APPLE_TEAM_ID." >&2
    exit 1
  fi
}

echo "Notarizing and stapling app..."
rm -f "$APP_ZIP"
ditto -c -k --keepParent "$APP_PATH" "$APP_ZIP"
notarytool_submit "$APP_ZIP"
xcrun stapler staple "$APP_PATH"
xcrun stapler validate "$APP_PATH"

echo "Creating signed DMG..."
rm -f "$DMG_PATH"

# Retina background: Finder picks the right resolution from a hidpi TIFF.
DMG_BG="$DESKTOP/src-tauri/target/dmg-background.tiff"
tiffutil -cathidpicheck \
  "$DESKTOP/src-tauri/dmg/background.png" \
  "$DESKTOP/src-tauri/dmg/background@2x.png" \
  -out "$DMG_BG"

# create-dmg copies the contents of its source folder into the volume.
DMG_STAGING="$(mktemp -d)"
trap 'rm -rf "$DMG_STAGING"' EXIT
ditto "$APP_PATH" "$DMG_STAGING/$APP_NAME"

set +e
create-dmg \
  --volname "JARV1S" \
  --volicon "$DESKTOP/src-tauri/icons/icon.icns" \
  --background "$DMG_BG" \
  --window-pos 200 120 \
  --window-size 660 400 \
  --icon-size 128 \
  --icon "$APP_NAME" 165 175 \
  --hide-extension "$APP_NAME" \
  --app-drop-link 495 175 \
  --format UDZO \
  --hdiutil-quiet \
  "$DMG_PATH" "$DMG_STAGING"
DMG_STATUS=$?
set -e

rm -rf "$DMG_STAGING"
trap - EXIT

# create-dmg exits 2 when only the cosmetic volume-icon step fails.
if [[ $DMG_STATUS -ne 0 && $DMG_STATUS -ne 2 ]]; then
  echo "create-dmg failed with status $DMG_STATUS" >&2
  exit "$DMG_STATUS"
fi
if [[ ! -f "$DMG_PATH" ]]; then
  echo "create-dmg did not produce $DMG_PATH" >&2
  exit 1
fi
codesign --force --timestamp --sign "$IDENTITY" "$DMG_PATH"
codesign --verify --verbose=2 "$DMG_PATH"

echo "Notarizing and stapling DMG..."
notarytool_submit "$DMG_PATH"
xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"
spctl --assess --type execute --verbose=4 "$APP_PATH"
spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG_PATH"
fi

echo "Creating signed updater artifact..."
find "$BUNDLE_DIR/macos" -maxdepth 1 \( -name '*.app.tar.gz' -o -name '*.app.tar.gz.sig' \) -delete
tar -czf "$UPDATER_TAR" -C "$(dirname "$APP_PATH")" "$APP_NAME"
./node_modules/.bin/tauri signer sign "$UPDATER_TAR"
RELEASE_CHANNEL="${JARVIS_RELEASE_CHANNEL:-internal}"
UPDATE_BASE_URL="${JARVIS_UPDATE_BASE_URL:-https://github.com/GTS-html77/JARV1S/releases/download/$RELEASE_CHANNEL}"
node scripts/generate-update-manifest.mjs "$BUNDLE_DIR" "$VERSION" "$RELEASE_CHANNEL" "$UPDATE_BASE_URL"

echo "Release artifacts in $BUNDLE_DIR"
