#!/usr/bin/env bash
# Fetch and stage bundled MongoDB for the packaged desktop app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
VERSIONS_FILE="$DESKTOP/services/versions.json"
DEST="$DESKTOP/resources/host/services"
CACHE="$DESKTOP/.cache/service-binaries"
ARCH="$(uname -m)"

if [[ "$ARCH" != "arm64" ]]; then
  echo "Bundled service binaries are only built for macOS arm64 in Phase 1b (found: $ARCH)." >&2
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Bundled service binaries are only built on macOS." >&2
  exit 1
fi

read_json() {
  python3 - "$VERSIONS_FILE" "$1" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
path = sys.argv[2].split(".")
value = data
for key in path:
    value = value[key]
print(value)
PY
}

verify_sha256() {
  local file="$1"
  local expected="$2"
  if [[ -z "$expected" ]]; then
    echo "Missing pinned sha256 for $file" >&2
    exit 1
  fi
  local actual
  actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "Checksum mismatch for $file" >&2
    echo " expected: $expected" >&2
    echo "   actual: $actual" >&2
    exit 1
  fi
}

fetch() {
  local url="$1"
  local output="$2"
  mkdir -p "$(dirname "$output")"
  if [[ ! -f "$output" ]]; then
    echo "Downloading $url"
    curl -fsSL "$url" -o "$output"
  fi
}

MONGO_VERSION="$(read_json mongodb.version)"
MONGO_URL="$(read_json mongodb.download_url)"
MONGO_SHA="$(read_json mongodb.sha256)"
MIN_MACOS="$(read_json minimum_macos)"

rm -rf "$DEST"
mkdir -p "$CACHE" "$DEST/mongodb/bin"

MONGO_ARCHIVE="$CACHE/mongodb-macos-arm64-${MONGO_VERSION}.tgz"
fetch "$MONGO_URL" "$MONGO_ARCHIVE"
verify_sha256 "$MONGO_ARCHIVE" "$MONGO_SHA"

MONGO_EXTRACT="$CACHE/mongodb-${MONGO_VERSION}"
rm -rf "$MONGO_EXTRACT"
mkdir -p "$MONGO_EXTRACT"
tar -xzf "$MONGO_ARCHIVE" -C "$MONGO_EXTRACT" --strip-components=1
install -m 755 "$MONGO_EXTRACT/bin/mongod" "$DEST/mongodb/bin/mongod"

for license_file in LICENSE-Community.txt THIRD-PARTY-NOTICES MPL-2; do
  if [[ -f "$MONGO_EXTRACT/$license_file" ]]; then
    cp "$MONGO_EXTRACT/$license_file" "$DEST/mongodb/"
  fi
done

GIT_SHA="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo local)"
cat >"$DEST/services-bundle.json" <<EOF
{
  "mongodb_version": "${MONGO_VERSION}",
  "service_provider": "bundled",
  "arch": "${ARCH}",
  "minimum_macos": "${MIN_MACOS}",
  "runtime_bundle": "${GIT_SHA}",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

echo "Service binaries ready at $DEST"
