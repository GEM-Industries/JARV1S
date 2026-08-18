#!/usr/bin/env bash
# Minimal host bundle placeholder so Tauri dev/check succeeds before full runtime build.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DEST="$ROOT/apps/desktop/resources/host"

if [[ -x "$DEST/runtime/python/bin/python3" ]]; then
  if ! head -1 "$DEST/runtime/python/bin/python3" | grep -q '^#!/bin/sh'; then
    exit 0
  fi
fi

mkdir -p "$DEST/backend" "$DEST/frontend-dist" "$DEST/runtime/python/bin"

cp "$ROOT/backend/main.py" "$DEST/backend/main.py"

if [[ -f "$ROOT/frontend/dist/index.html" ]]; then
  cp -R "$ROOT/frontend/dist/." "$DEST/frontend-dist/"
else
  echo '<!doctype html><html><body>JARV1S</body></html>' >"$DEST/frontend-dist/index.html"
fi

cp "$ROOT/docker-compose.yml" "$DEST/docker-compose.yml"

cat >"$DEST/runtime-bundle.json" <<EOF
{"runtime_bundle":"stub","frontend_build":"stub","release_channel":"internal"}
EOF

cat >"$DEST/runtime/python/bin/python3" <<'EOF'
#!/bin/sh
echo "Packaged runtime not built. Run task desktop:build-runtime or task desktop:dogfood." >&2
exit 1
EOF
chmod +x "$DEST/runtime/python/bin/python3"
