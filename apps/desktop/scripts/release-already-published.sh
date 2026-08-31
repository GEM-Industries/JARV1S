#!/usr/bin/env bash
# Exit 0 if GitHub already has the versioned DMG for this tag (local publish
# won the race). Exit 1 if CI should build.
#
# If the release exists but the DMG is still uploading, wait up to
# JARVIS_RELEASE_ASSET_WAIT_SECS (default 1800) so a tag-triggered workflow
# does not start a second notarization.
set -euo pipefail

TAG="${1:-}"
if [[ -z "$TAG" || "$TAG" == -h || "$TAG" == --help ]]; then
  echo "Usage: $0 <tag>" >&2
  echo "  Exit 0: skip CI rebuild. Exit 1: CI should sign and publish." >&2
  exit 2
fi

WAIT_SECS="${JARVIS_RELEASE_ASSET_WAIT_SECS:-1800}"
SLEEP_SECS="${JARVIS_RELEASE_ASSET_POLL_SECS:-20}"

has_dmg() {
  gh release view "$TAG" --json assets --jq '.assets[].name' 2>/dev/null \
    | grep -Eq '^JARV1S_.*_aarch64\.dmg$'
}

if ! gh release view "$TAG" >/dev/null 2>&1; then
  echo "No GitHub Release for ${TAG}; CI should build."
  exit 1
fi

if has_dmg; then
  echo "${TAG} already has a signed DMG; skip rebuild."
  exit 0
fi

echo "${TAG} exists without a DMG; waiting up to ${WAIT_SECS}s for a local upload..."
deadline=$((SECONDS + WAIT_SECS))
while (( SECONDS < deadline )); do
  sleep "$SLEEP_SECS"
  if has_dmg; then
    echo "${TAG} DMG appeared; skip rebuild."
    exit 0
  fi
done

echo "${TAG} still has no DMG after ${WAIT_SECS}s; CI should build."
exit 1
