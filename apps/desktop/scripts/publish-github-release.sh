#!/usr/bin/env bash
# Publish a local desktop release to GitHub Releases.
# Always publishes to this clone's origin (private canonical).
# Also attaches the DMG to GEM-Industries/JARV1S so the public repo has a download.
# Set JARVIS_PUBLIC_RELEASE_REPO= to skip the public mirror (CI does this).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
BUNDLE="${BUNDLE:-$DESKTOP/src-tauri/target/aarch64-apple-darwin/release/bundle}"
CHANNEL="${JARVIS_RELEASE_CHANNEL:-internal}"
PUBLIC_REPO="${JARVIS_PUBLIC_RELEASE_REPO-GEM-Industries/JARV1S}"

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
macOS 14+ Apple Silicon.

Open the DMG and copy JARV1S to Applications.

SHA-256: \`${SHA256}\`
EOF
)"

publish_dmg() {
  local repo="$1"
  shift

  echo "Publishing ${TAG} to ${repo}..."
  if gh release view "$TAG" --repo "$repo" >/dev/null 2>&1; then
    gh release edit "$TAG" --repo "$repo" \
      --title "JARV1S ${VERSION}" \
      --notes "$NOTES" \
      --prerelease=false \
      --latest
    gh release upload "$TAG" --repo "$repo" "$DMG_ASSET" --clobber
  else
    gh release create "$TAG" --repo "$repo" \
      --title "JARV1S ${VERSION}" \
      --notes "$NOTES" \
      --latest \
      "$@" \
      "$DMG_ASSET"
  fi
}

publish_updater_channel() {
  local repo="$1"

  if gh release view "$CHANNEL" --repo "$repo" >/dev/null 2>&1; then
    gh release edit "$CHANNEL" --repo "$repo" \
      --title "JARV1S updater channel" \
      --notes "Rolling updater files used by installed apps." \
      --prerelease
    gh release upload "$CHANNEL" --repo "$repo" \
      "$UPDATER_TAR" \
      "$UPDATER_SIG" \
      "$MANIFEST" \
      --clobber
  else
    gh release create "$CHANNEL" --repo "$repo" \
      --title "JARV1S updater channel" \
      --notes "Rolling updater files used by installed apps." \
      --prerelease \
      "$UPDATER_TAR" \
      "$UPDATER_SIG" \
      "$MANIFEST"
  fi
}

if ! git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Creating tag ${TAG} on HEAD..."
  git tag -a "$TAG" -m "Release ${VERSION}"
fi

echo "Pushing tag ${TAG}..."
git push origin "$TAG"

PRIVATE_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
publish_dmg "$PRIVATE_REPO"
publish_updater_channel "$PRIVATE_REPO"
echo "DMG: https://github.com/${PRIVATE_REPO}/releases/download/${TAG}/$(basename "$DMG")"

if [[ -n "$PUBLIC_REPO" && "$PUBLIC_REPO" != "$PRIVATE_REPO" ]]; then
  publish_dmg "$PUBLIC_REPO" --target main
  echo "DMG: https://github.com/${PUBLIC_REPO}/releases/download/${TAG}/$(basename "$DMG")"
fi
