#!/usr/bin/env bash
# Publish a local desktop release to GitHub Releases.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
BUNDLE="${BUNDLE:-$DESKTOP/src-tauri/target/aarch64-apple-darwin/release/bundle}"
CHANNEL="${JARVIS_RELEASE_CHANNEL:-internal}"

if [[ ! -d "$BUNDLE/macos" ]]; then
  echo "Release bundle not found: $BUNDLE/macos" >&2
  echo "Run task desktop:release:local first." >&2
  exit 1
fi

VERSION="$(node -p "JSON.parse(require('fs').readFileSync('$DESKTOP/src-tauri/tauri.conf.json','utf8')).version")"
TAG="v${VERSION}"
DMG="$BUNDLE/macos/JARV1S_${VERSION}_aarch64.dmg"
UPDATER_TAR="$BUNDLE/macos/JARV1S.app.tar.gz"
UPDATER_SIG="${UPDATER_TAR}.sig"
MANIFEST="$BUNDLE/latest.json"
if [[ "$CHANNEL" != "internal" ]]; then
  MANIFEST="$BUNDLE/latest-${CHANNEL}.json"
fi

[[ -f "$DMG" ]] || { echo "DMG not found for version ${VERSION}: $BUNDLE/macos" >&2; exit 1; }
[[ -f "$UPDATER_TAR" ]] || { echo "Updater archive not found in $BUNDLE/macos" >&2; exit 1; }
[[ -f "$UPDATER_SIG" ]] || { echo "Updater signature not found: $UPDATER_SIG" >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "Updater manifest not found in $BUNDLE" >&2; exit 1; }

SHA256="$(shasum -a 256 "$DMG" | awk '{print $1}')"
DMG_ASSET="${DMG}#Download for macOS (Apple Silicon)"
NOTES="$(cat <<EOF
Private technical beta for Apple Silicon Macs running macOS 14 or later.

Download the macOS installer below, open it, and copy JARV1S to Applications.

SHA-256: \`${SHA256}\`
EOF
)"

if ! git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Creating tag ${TAG} on HEAD..."
  git tag -a "$TAG" -m "Release ${VERSION}"
fi

echo "Pushing tag ${TAG}..."
git push origin "$TAG"

echo "Publishing GitHub release ${TAG}..."
if gh release view "$TAG" >/dev/null 2>&1; then
  gh release edit "$TAG" \
    --title "JARV1S ${VERSION}" \
    --notes "$NOTES" \
    --prerelease=false \
    --latest
  gh release upload "$TAG" "$DMG_ASSET" --clobber
else
  gh release create "$TAG" \
    --title "JARV1S ${VERSION}" \
    --notes "$NOTES" \
    --latest \
    "$DMG_ASSET"
fi

if gh release view "$CHANNEL" >/dev/null 2>&1; then
  gh release edit "$CHANNEL" \
    --title "JARV1S updater channel" \
    --notes "Rolling updater files used by installed apps." \
    --prerelease
  gh release upload "$CHANNEL" \
    "$UPDATER_TAR" \
    "$UPDATER_SIG" \
    "$MANIFEST" \
    --clobber
else
  gh release create "$CHANNEL" \
    --title "JARV1S updater channel" \
    --notes "Rolling updater files used by installed apps." \
    --prerelease \
    "$UPDATER_TAR" \
    "$UPDATER_SIG" \
    "$MANIFEST"
fi

echo "Published ${TAG} to GitHub Releases."
echo "DMG: https://github.com/GTS-html77/JARV1S/releases/download/${TAG}/$(basename "$DMG")"
