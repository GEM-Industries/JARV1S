#!/usr/bin/env bash
# Build JARV1SSpeechHelper.app into apps/desktop/resources/helpers/
#
# Requires a macOS 26 SDK with SpeechAnalyzer (Xcode 26+; run `xcode-select -s`).
# The helper still gates AppleSpeechEngine behind `#available(macOS 26.0, *)`, so
# older *runtimes* fall back to UnsupportedSpeechEngine at launch.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP_ROOT="$(cd "$ROOT/.." && pwd)"
OUT_DIR="${1:-$DESKTOP_ROOT/resources/helpers}"
APP_NAME="JARV1SSpeechHelper.app"
APP_PATH="$OUT_DIR/$APP_NAME"
BIN_NAME="JARV1SSpeechHelper"
BUILD_DIR="$ROOT/.build"
LOG="$BUILD_DIR/swiftc.log"
mkdir -p "$OUT_DIR" "$BUILD_DIR"

HELPER_FRESH=1
if [[ -x "$APP_PATH/Contents/MacOS/$BIN_NAME" ]]; then
  HELPER_FRESH=0
  for src in "$ROOT"/Sources/*.swift "$ROOT/Info.plist"; do
    if [[ "$src" -nt "$APP_PATH/Contents/MacOS/$BIN_NAME" ]]; then
      HELPER_FRESH=1
      break
    fi
  done
fi
if [[ "$HELPER_FRESH" == "0" ]]; then
  echo "Reusing $APP_PATH"
  exit 0
fi

rm -rf "$APP_PATH"

SDK_PATH="$(xcrun --show-sdk-path 2>/dev/null || true)"
SPEECH_MODULE_DIR="$SDK_PATH/System/Library/Frameworks/Speech.framework/Modules/Speech.swiftmodule"
if [[ -z "$SDK_PATH" ]] || ! grep -q "SpeechAnalyzer" "$SPEECH_MODULE_DIR"/*.swiftinterface 2>/dev/null; then
  echo "SDK at ${SDK_PATH:-<none>} has no SpeechAnalyzer. Install Xcode 26+ and run:" >&2
  echo "  sudo xcode-select -s /Applications/Xcode.app/Contents/Developer" >&2
  exit 1
fi

BIN_OUT="$BUILD_DIR/$BIN_NAME"
if ! swiftc -O -target arm64-apple-macos14.0 -DJARVIS_HAS_SPEECH_ANALYZER \
  -o "$BIN_OUT" "$ROOT"/Sources/*.swift 2>"$LOG"; then
  echo "Apple Speech helper failed to compile:" >&2
  tail -40 "$LOG" >&2
  exit 1
fi

mkdir -p "$APP_PATH/Contents/MacOS"
cp "$BIN_OUT" "$APP_PATH/Contents/MacOS/$BIN_NAME"
chmod +x "$APP_PATH/Contents/MacOS/$BIN_NAME"
cp "$ROOT/Info.plist" "$APP_PATH/Contents/Info.plist"

if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - --identifier "dev.jarv1s.host.speech" "$APP_PATH" 2>/dev/null || true
fi

echo "Built $APP_PATH"
