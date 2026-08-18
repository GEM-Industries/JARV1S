#!/usr/bin/env bash
# Smoke test bundled MongoDB and a relocated Python runtime with space-bearing paths.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SERVICES="$ROOT/apps/desktop/resources/host/services"
RUNTIME_SRC="${RUNTIME_SRC:-$ROOT/apps/desktop/resources/host/runtime}"
MONGOD="${MONGOD:-$SERVICES/mongodb/bin/mongod}"
PYTHON_SRC="${PYTHON:-$RUNTIME_SRC/python/bin/python3}"
MONGOD_PID=""

if [[ ! -x "$MONGOD" ]]; then
  echo "Missing mongod binary at $MONGOD (run task desktop:build-runtime or task desktop:doctor first)" >&2
  exit 1
fi
if [[ ! -x "$PYTHON_SRC" ]]; then
  echo "Missing bundled Python at $PYTHON_SRC (run task desktop:build-runtime or task desktop:doctor first)" >&2
  exit 1
fi

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/JARV1S service smoke.XXXXXX")"
DATA_DIR="$WORKDIR/mongo data"
RUN_DIR="$WORKDIR/run"
LOG_DIR="$WORKDIR/logs"
RELOCATED_RUNTIME="$WORKDIR/relocated runtime"
mkdir -p "$DATA_DIR" "$RUN_DIR" "$LOG_DIR"

# Copy the runtime away from the build tree so absolute pyvenv/home paths cannot
# silently keep working. Prefer a full runtime tree copy; fall back to copying
# just the python distribution when PYTHON points at a signed app binary.
if [[ -d "$RUNTIME_SRC/python" ]]; then
  mkdir -p "$RELOCATED_RUNTIME"
  cp -R "$RUNTIME_SRC/python" "$RELOCATED_RUNTIME/python"
  PYTHON="$RELOCATED_RUNTIME/python/bin/python3"
else
  PYTHON_DIST="$(cd "$(dirname "$PYTHON_SRC")/.." && pwd)"
  mkdir -p "$RELOCATED_RUNTIME"
  cp -R "$PYTHON_DIST" "$RELOCATED_RUNTIME/python"
  PYTHON="$RELOCATED_RUNTIME/python/bin/python3"
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "Relocated Python missing at $PYTHON" >&2
  exit 1
fi

cat >"$LOG_DIR/mongod.conf" <<EOF
storage:
  dbPath: "$DATA_DIR"
systemLog:
  destination: file
  path: "$LOG_DIR/mongod.log"
  logAppend: true
net:
  port: 0
  unixDomainSocket:
    enabled: true
    pathPrefix: "$RUN_DIR"
    filePermissions: 448
EOF

MONGO_SOCKET="$RUN_DIR/mongodb-0.sock"
"$MONGOD" --config "$LOG_DIR/mongod.conf" &
MONGOD_PID=$!

cleanup() {
  if [[ -n "$MONGOD_PID" ]]; then
    kill -TERM "$MONGOD_PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$MONGOD_PID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$MONGOD_PID" 2>/dev/null || true
    wait "$MONGOD_PID" 2>/dev/null || true
  fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  [[ -S "$MONGO_SOCKET" ]] && break
  kill -0 "$MONGOD_PID" 2>/dev/null || {
    echo "mongod exited before readiness" >&2
    tail -20 "$LOG_DIR/mongod.log" >&2 || true
    exit 1
  }
  sleep 1
done

if [[ ! -S "$MONGO_SOCKET" ]]; then
  echo "MongoDB socket did not appear under $RUN_DIR" >&2
  tail -20 "$LOG_DIR/mongod.log" >&2 || true
  exit 1
fi

MONGODB_URL="$("$PYTHON" - "$MONGO_SOCKET" <<'PY'
import sys
from urllib.parse import quote
print(f"mongodb://{quote(sys.argv[1], safe='')}")
PY
)"

for _ in $(seq 1 30); do
  if JARVIS_MONGO_URL="$MONGODB_URL" "$PYTHON" - <<'PY'
import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient

async def main():
    mongo = AsyncIOMotorClient(os.environ["JARVIS_MONGO_URL"], serverSelectionTimeoutMS=2000)
    try:
        await mongo.admin.command("ping")
    finally:
        mongo.close()

asyncio.run(main())
PY
  then
    echo "Service smoke test passed (MongoDB via relocated bundled Python)"
    exit 0
  fi
  sleep 1
done

echo "MongoDB ping failed" >&2
tail -20 "$LOG_DIR/mongod.log" >&2 || true
exit 1
