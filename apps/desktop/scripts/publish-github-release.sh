#!/usr/bin/env bash
# Publish a local signed desktop release to GitHub.
#
# Private (default): beta audience on this clone's origin. Versioned DMG plus
# the rolling updater channel. Apple/updater secrets stay here.
# Public: promote the same already-signed DMG to GEM-Industries. No rebuild,
# no updater channel (beta auto-updates must not leak to the public feed).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
BUNDLE="${BUNDLE:-$DESKTOP/src-tauri/target/aarch64-apple-darwin/release/bundle}"
CHANNEL="${JARVIS_RELEASE_CHANNEL:-internal}"
PUBLIC_REPO="${JARVIS_PUBLIC_RELEASE_REPO:-GEM-Industries/JARV1S}"
CHANGELOG_FILE="${JARVIS_CHANGELOG:-$ROOT/CHANGELOG.md}"
AUDIENCE="private"
PRINT_CHANGELOG=0

usage() {
  cat <<EOF
Usage: $0 [--private|--public]

  --private  Beta audience (default). Private origin: versioned DMG + rolling
             updater channel (${CHANNEL}). Does not touch ${PUBLIC_REPO}.
  --public   Promote the already-signed DMG to ${PUBLIC_REPO}. No rebuild and
             no updater channel. Run \`task public:publish -- --push\` first so
             the public tag lands on matching sanitized source.

Release notes come from CHANGELOG.md for this version. GitHub also attaches
Source code zip/tar from the tag; install the DMG, not those archives.
EOF
}

extract_changelog_section() {
  local version="$1"
  local file="$CHANGELOG_FILE"
  if [[ ! -f "$file" ]]; then
    echo "Missing changelog: $file" >&2
    exit 1
  fi
  local section
  section="$(awk -v ver="$version" '
    BEGIN { hdr = "## [" ver "]" }
    index($0, hdr) == 1 { grab = 1 }
    grab && NR > 1 && /^## \[/ && index($0, hdr) != 1 { exit }
    grab && /^\[[^]]+\]:/ { exit }
    grab { print }
  ' "$file")"
  if [[ -z "$section" ]]; then
    echo "CHANGELOG.md has no ## [${version}] section" >&2
    exit 1
  fi
  printf '%s\n' "$section" | awk '
    { lines[n++] = $0 }
    END {
      while (n > 0 && lines[n-1] ~ /^[[:space:]]*$/) n--
      for (i = 0; i < n; i++) print lines[i]
    }
  '
}

build_release_notes() {
  local audience="$1"
  local sha="$2"
  local notes=""
  if [[ "$audience" == "private" ]]; then
    notes="Private beta (invite-only)."$'\n\n'
  fi
  notes+="${CHANGELOG_SECTION}"$'\n\n'
  notes+="macOS 14+ Apple Silicon."$'\n\n'
  notes+="Install the \`JARV1S_${VERSION}_aarch64.dmg\` (open it and copy JARV1S to Applications). Ignore GitHub's Source code zip/tar unless you are building from source."$'\n\n'
  notes+="SHA-256: \`${sha}\`"
  printf '%s\n' "$notes"
}

for arg in "$@"; do
  case "$arg" in
    --private) AUDIENCE="private" ;;
    --public) AUDIENCE="public" ;;
    --print-changelog) PRINT_CHANGELOG=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

VERSION="$(node -p "JSON.parse(require('fs').readFileSync('$DESKTOP/src-tauri/tauri.conf.json','utf8')).version")"
CHANGELOG_SECTION="$(extract_changelog_section "$VERSION")"

if [[ "$PRINT_CHANGELOG" == 1 ]]; then
  printf '%s\n' "$CHANGELOG_SECTION"
  exit 0
fi

if [[ ! -d "$BUNDLE/macos" ]]; then
  echo "Release bundle not found: $BUNDLE/macos" >&2
  echo "Run task desktop:release:local first." >&2
  exit 1
fi

TAG="v${VERSION}"
DMG="$BUNDLE/macos/JARV1S_${VERSION}_aarch64.dmg"
UPDATER_TAR="$BUNDLE/macos/JARV1S.app.tar.gz"
UPDATER_SIG="${UPDATER_TAR}.sig"
MANIFEST="$BUNDLE/latest.json"
if [[ "$CHANNEL" != "internal" ]]; then
  MANIFEST="$BUNDLE/latest-${CHANNEL}.json"
fi

[[ -f "$DMG" ]] || { echo "DMG not found for version ${VERSION}: $BUNDLE/macos" >&2; exit 1; }

if [[ "$AUDIENCE" == "private" ]]; then
  [[ -f "$UPDATER_TAR" ]] || { echo "Updater archive not found in $BUNDLE/macos" >&2; exit 1; }
  [[ -f "$UPDATER_SIG" ]] || { echo "Updater signature not found: $UPDATER_SIG" >&2; exit 1; }
  [[ -f "$MANIFEST" ]] || { echo "Updater manifest not found in $BUNDLE" >&2; exit 1; }
fi

SHA256="$(shasum -a 256 "$DMG" | awk '{print $1}')"
DMG_ASSET="${DMG}#Download for macOS (Apple Silicon)"
PRIVATE_NOTES="$(build_release_notes private "$SHA256")"
PUBLIC_NOTES="$(build_release_notes public "$SHA256")"

publish_dmg() {
  local repo="$1"
  local notes="$2"
  local mode="$3"

  echo "Publishing ${TAG} to ${repo}..."
  if gh release view "$TAG" --repo "$repo" >/dev/null 2>&1; then
    if [[ "$mode" == "private" ]]; then
      gh release edit "$TAG" --repo "$repo" \
        --title "JARV1S ${VERSION}" \
        --notes "$notes" \
        --prerelease
    else
      gh release edit "$TAG" --repo "$repo" \
        --title "JARV1S ${VERSION}" \
        --notes "$notes" \
        --prerelease=false \
        --latest
    fi
    gh release upload "$TAG" --repo "$repo" "$DMG_ASSET" --clobber
    return
  fi

  if [[ "$mode" == "private" ]]; then
    gh release create "$TAG" --repo "$repo" \
      --title "JARV1S ${VERSION}" \
      --notes "$notes" \
      --target "$HEAD_SHA" \
      --prerelease \
      --verify-tag \
      "$DMG_ASSET"
  else
    gh release create "$TAG" --repo "$repo" \
      --title "JARV1S ${VERSION}" \
      --notes "$notes" \
      --target main \
      --latest \
      "$DMG_ASSET"
  fi
}

publish_updater_channel() {
  local repo="$1"

  if gh release view "$CHANNEL" --repo "$repo" >/dev/null 2>&1; then
    gh release edit "$CHANNEL" --repo "$repo" \
      --title "JARV1S updater channel" \
      --notes "Rolling updater files for private beta installs." \
      --prerelease
    gh release upload "$CHANNEL" --repo "$repo" \
      "$UPDATER_TAR" \
      "$UPDATER_SIG" \
      "$MANIFEST" \
      --clobber
  else
    gh release create "$CHANNEL" --repo "$repo" \
      --title "JARV1S updater channel" \
      --notes "Rolling updater files for private beta installs." \
      --prerelease \
      "$UPDATER_TAR" \
      "$UPDATER_SIG" \
      "$MANIFEST"
  fi
}

ensure_local_tag() {
  local head
  head="$(git -C "$ROOT" rev-parse HEAD)"
  if git -C "$ROOT" rev-parse "$TAG" >/dev/null 2>&1; then
    if [[ "$(git -C "$ROOT" rev-parse "${TAG}^{commit}")" != "$head" ]]; then
      echo "Tag ${TAG} exists at a different commit than HEAD." >&2
      exit 1
    fi
    return
  fi
  echo "Creating tag ${TAG} on HEAD..."
  git -C "$ROOT" tag -a "$TAG" -m "Release ${VERSION}"
}

HEAD_SHA="$(git -C "$ROOT" rev-parse HEAD)"

if [[ "$AUDIENCE" == "public" ]]; then
  echo "Promoting ${TAG} DMG to ${PUBLIC_REPO} (no updater channel)..."
  publish_dmg "$PUBLIC_REPO" "$PUBLIC_NOTES" public
  PUBLIC_CLONE="${JARVIS_PUBLIC_REPO:-$ROOT/../JARV1S-public}"
  if [[ -d "$PUBLIC_CLONE/.git" ]]; then
    git -C "$PUBLIC_CLONE" tag -f "$TAG" HEAD
    git -C "$PUBLIC_CLONE" push origin "$TAG" --force
    echo "Public tag ${TAG} -> $(git -C "$PUBLIC_CLONE" rev-parse --short HEAD)"
  fi
  echo "DMG: https://github.com/${PUBLIC_REPO}/releases/download/${TAG}/$(basename "$DMG")"
  exit 0
fi

ensure_local_tag
PRIVATE_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

# gh release create publishes the tag with the assets. Do not git-push the
# tag first: that starts CI before the DMG exists.
publish_dmg "$PRIVATE_REPO" "$PRIVATE_NOTES" private
publish_updater_channel "$PRIVATE_REPO"
git -C "$ROOT" fetch origin "refs/tags/${TAG}:refs/tags/${TAG}" 2>/dev/null || true
echo "DMG: https://github.com/${PRIVATE_REPO}/releases/download/${TAG}/$(basename "$DMG")"
