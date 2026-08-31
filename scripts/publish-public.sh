#!/usr/bin/env bash
# Export a sanitized snapshot of this private tree to GEM-Industries/JARV1S.
# Default is dry-run (rsync + scan + git status). Pass --push to commit and push.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${JARVIS_PUBLIC_REPO:-$ROOT/../JARV1S-public}"
REMOTE="${JARVIS_PUBLIC_REMOTE:-https://github.com/GEM-Industries/JARV1S.git}"
PUSH=0

for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    -h|--help)
      echo "Usage: $0 [--push]"
      echo "  JARVIS_PUBLIC_REPO   destination clone (default: $ROOT/../JARV1S-public)"
      echo "  JARVIS_PUBLIC_REMOTE remote URL (default: $REMOTE)"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$ROOT/.publicignore" ]]; then
  echo "Missing $ROOT/.publicignore" >&2
  exit 1
fi

ensure_dest() {
  if [[ -d "$DEST/.git" ]]; then
    return
  fi
  mkdir -p "$(dirname "$DEST")"
  if git ls-remote "$REMOTE" HEAD >/dev/null 2>&1; then
    echo "Cloning $REMOTE -> $DEST"
    git clone "$REMOTE" "$DEST"
  else
    echo "Initializing empty public clone at $DEST"
    git init -b main "$DEST"
    git -C "$DEST" remote add origin "$REMOTE"
  fi
}

assert_clean_staging() {
  local hits
  hits="$(rg -n -I \
    -g '!.git/**' \
    -g '!**/publish-public.sh' \
    -e 'geoffmccosker' \
    -e 'gemblaney' \
    -e '/home/geoff' \
    "$DEST" || true)"
  if [[ -n "$hits" ]]; then
    echo "Refuse: staging tree still contains personal markers:" >&2
    echo "$hits" >&2
    exit 1
  fi

  local secrets
  secrets="$(find "$DEST" \( -name '.env' -o -name '*.pem' -o -name '*.p12' -o -name 'id_rsa' \) \
    ! -path '*/.git/*' 2>/dev/null || true)"
  if [[ -n "$secrets" ]]; then
    echo "Refuse: secret-looking filenames in staging:" >&2
    echo "$secrets" >&2
    exit 1
  fi
}

ensure_dest

echo "Rsync $ROOT -> $DEST"
rsync -a --delete \
  --exclude '.git/' \
  --filter=':- .gitignore' \
  --exclude-from "$ROOT/.publicignore" \
  "$ROOT/" "$DEST/"

# rsync's gitignore filter does not honor git negation (!*.wav).
while IFS= read -r -d '' f; do
  case "$f" in
    training/*) continue ;;
  esac
  mkdir -p "$DEST/$(dirname "$f")"
  cp "$ROOT/$f" "$DEST/$f"
done < <(git -C "$ROOT" ls-files -z '*.wav')

# Keep the private dogfood owner id out of the public snapshot.
config_py="$DEST/backend/core/config.py"
if ! grep -q 'DEFAULT_USER_ID: str = "geoff"' "$config_py"; then
  echo "Refuse: expected private DEFAULT_USER_ID=geoff in $config_py" >&2
  exit 1
fi
perl -i -pe 's/DEFAULT_USER_ID: str = "geoff"/DEFAULT_USER_ID: str = "local"/' "$config_py"

assert_clean_staging

echo "Staging file count: $(git -C "$DEST" ls-files --others --exclude-standard | wc -l | tr -d ' ') untracked after rsync (plus existing index)"
git -C "$DEST" add -A
git -C "$DEST" status --short | head -50
local_count="$(git -C "$DEST" status --short | wc -l | tr -d ' ')"
echo "Changed paths: $local_count"

if [[ "$PUSH" -eq 0 ]]; then
  echo "Dry-run complete. Re-run with --push to commit and push to $REMOTE"
  git -C "$DEST" reset -q
  exit 0
fi

if git -C "$DEST" diff --cached --quiet && git -C "$DEST" diff --quiet; then
  echo "Nothing to commit."
  git -C "$DEST" push -u origin HEAD
  exit 0
fi

if git -C "$DEST" rev-parse --verify HEAD >/dev/null 2>&1; then
  MSG="Sync from private"
else
  MSG="Initial public snapshot"
fi

git -C "$DEST" commit -m "$MSG"
git -C "$DEST" push -u origin HEAD
echo "Pushed $MSG to $REMOTE"
