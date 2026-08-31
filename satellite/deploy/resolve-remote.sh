#!/usr/bin/env bash
# Resolve SATELLITE_USER@SATELLITE_HOST, trying common dogfood accounts.
set -euo pipefail

SATELLITE_HOST="${SATELLITE_HOST:-jarvis-satellite-1.local}"

if [ -n "${SATELLITE_USER:-}" ]; then
  printf '%s@%s\n' "${SATELLITE_USER}" "${SATELLITE_HOST}"
  exit 0
fi

for user in geoff pi "${USER:-}"; do
  [ -z "${user}" ] && continue
  if ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new \
    "${user}@${SATELLITE_HOST}" true >/dev/null 2>&1; then
    printf '%s@%s\n' "${user}" "${SATELLITE_HOST}"
    exit 0
  fi
done

echo "Could not SSH to ${SATELLITE_HOST} as geoff, pi, or ${USER:-}." >&2
echo "Set SATELLITE_USER (the Linux account on the speaker)." >&2
exit 1
