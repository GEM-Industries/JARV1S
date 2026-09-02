#!/usr/bin/env bash
# Package a pinned Ollama CLI/runtime payload for the JARV1S desktop Host.
# Downloads the official darwin tarball (checksum-verified) into resources/helpers/Ollama/.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
MANIFEST="$DESKTOP/managed-llm/manifest.json"
OUT_DIR="${1:-$DESKTOP/resources/helpers/Ollama}"
CACHE_DIR="${JARVIS_OLLAMA_CACHE:-$DESKTOP/.cache/ollama}"

if [[ ! -f "$MANIFEST" ]]; then
  echo "Missing managed LLM manifest: $MANIFEST" >&2
  exit 1
fi

RUNTIME_VERSION="$(python3 -c "import json; print(json.load(open('$MANIFEST'))['runtime_version'])")"
RUNTIME_URL="$(python3 -c "import json; print(json.load(open('$MANIFEST'))['runtime_source'])")"
RUNTIME_SHA="$(python3 -c "import json; print(json.load(open('$MANIFEST'))['runtime_sha256'])")"

if [[ -x "$OUT_DIR/ollama" && -f "$OUT_DIR/manifest.json" ]]; then
  EXISTING_VERSION="$(python3 -c "import json; print(json.load(open('$OUT_DIR/manifest.json')).get('runtime_version',''))")"
  if [[ "$EXISTING_VERSION" == "$RUNTIME_VERSION" ]]; then
    echo "Reusing packaged Ollama helper (version=$RUNTIME_VERSION)"
    exit 0
  fi
fi

ARCHIVE_NAME="ollama-darwin.tgz"
ARCHIVE_PATH="$CACHE_DIR/$RUNTIME_VERSION/$ARCHIVE_NAME"
mkdir -p "$(dirname "$ARCHIVE_PATH")" "$OUT_DIR"

need_download=1
if [[ -f "$ARCHIVE_PATH" ]]; then
  actual="$(shasum -a 256 "$ARCHIVE_PATH" | awk '{print $1}')"
  if [[ "$actual" == "$RUNTIME_SHA" ]]; then
    need_download=0
  else
    echo "Cached Ollama archive checksum mismatch; re-downloading"
    rm -f "$ARCHIVE_PATH"
  fi
fi

if [[ "$need_download" == "1" ]]; then
  echo "Downloading Ollama $RUNTIME_VERSION..."
  curl -fL --retry 3 --retry-delay 2 -o "$ARCHIVE_PATH.partial" "$RUNTIME_URL"
  mv "$ARCHIVE_PATH.partial" "$ARCHIVE_PATH"
  actual="$(shasum -a 256 "$ARCHIVE_PATH" | awk '{print $1}')"
  if [[ "$actual" != "$RUNTIME_SHA" ]]; then
    echo "Ollama archive SHA-256 mismatch (expected $RUNTIME_SHA, got $actual)" >&2
    rm -f "$ARCHIVE_PATH"
    exit 1
  fi
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -xzf "$ARCHIVE_PATH" -C "$STAGE"

# Official darwin tarball is a flat payload: ollama + llama-server + libs + mlx_metal_*.
# Preserve that layout; do not flatten subdirectory trees (mlx_metal_v3/v4).
PAYLOAD_ROOT="$STAGE"
if [[ ! -x "$STAGE/ollama" ]]; then
  if [[ -x "$STAGE/bin/ollama" ]]; then
    PAYLOAD_ROOT="$STAGE"
    # Some archives nest under bin/; lift siblings when present.
    if [[ ! -e "$STAGE/llama-server" && -e "$STAGE/bin/llama-server" ]]; then
      PAYLOAD_ROOT="$STAGE/bin"
    fi
  else
    FOUND="$(find "$STAGE" -type f -name ollama -perm -111 | head -1 || true)"
    if [[ -z "$FOUND" ]]; then
      echo "ollama binary not found in archive" >&2
      exit 1
    fi
    PAYLOAD_ROOT="$(dirname "$FOUND")"
  fi
fi
if [[ ! -x "$PAYLOAD_ROOT/ollama" ]]; then
  echo "ollama binary not found in archive" >&2
  exit 1
fi
if [[ ! -x "$PAYLOAD_ROOT/llama-server" ]]; then
  echo "llama-server binary not found next to ollama (required for inference)" >&2
  exit 1
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
# Copy the full payload (binaries, dylibs, metallibs, mlx_metal_* trees).
cp -R "$PAYLOAD_ROOT"/. "$OUT_DIR/"
chmod +x "$OUT_DIR/ollama" "$OUT_DIR/llama-server"
if [[ -x "$OUT_DIR/llama-quantize" ]]; then
  chmod +x "$OUT_DIR/llama-quantize"
fi

cp "$MANIFEST" "$OUT_DIR/manifest.json"

# License notices for redistribution.
NOTICE="$OUT_DIR/THIRD_PARTY_NOTICES.txt"
{
  echo "Ollama $RUNTIME_VERSION"
  echo "License: MIT — https://github.com/ollama/ollama/blob/main/LICENSE"
  echo "Source: https://github.com/ollama/ollama"
  echo
  echo "This payload is packaged for local-only use by the JARV1S desktop Host."
} >"$NOTICE"

if command -v codesign >/dev/null 2>&1; then
  # Ad-hoc sign for local packaging; release signing uses sign-nested-binaries.sh.
  find "$OUT_DIR" -type f \( \
      -name ollama -o -name llama-server -o -name llama-quantize \
      -o -name '*.dylib' -o -name '*.so' -o -name '*.metallib' \
    \) -print0 \
    | while IFS= read -r -d '' file; do
        codesign --force --sign - --identifier "dev.jarv1s.host.ollama" "$file" 2>/dev/null || true
      done
fi

echo "Packaged Ollama helper at $OUT_DIR (version=$RUNTIME_VERSION)"
