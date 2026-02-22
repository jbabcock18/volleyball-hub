#!/bin/zsh
set -euo pipefail

ROOT="/Users/jackbabcock/Desktop/tournament-hub"
PYTHON_BIN="$ROOT/.venv/bin/python"
CURL_BIN="/usr/bin/curl"
DATA_FILE="$ROOT/data/tournaments.json"
LOCK_DIR="$ROOT/data/local_push.lock"
TOKEN_FILE="$ROOT/data/push_token"
ALT_TOKEN_FILE="$HOME/.config/tournament-hub/push_token"
PUSH_URL="${TOURNAMENT_HUB_PUSH_URL:-https://volleyball-hub.onrender.com/api/push-cache}"
PUSH_TOKEN="${TOURNAMENT_HUB_PUSH_TOKEN:-}"

if [[ -z "$PUSH_TOKEN" && -f "$TOKEN_FILE" ]]; then
  PUSH_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
fi
if [[ -z "$PUSH_TOKEN" && -f "$ALT_TOKEN_FILE" ]]; then
  PUSH_TOKEN="$(tr -d '\r\n' < "$ALT_TOKEN_FILE")"
fi

if [[ -z "$PUSH_TOKEN" ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] missing push token; set TOURNAMENT_HUB_PUSH_TOKEN or create $TOKEN_FILE"
  exit 1
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] push already running; exiting"
  exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$ROOT"

"$PYTHON_BIN" "$ROOT/scripts/refresh_cache.py"

"$CURL_BIN" -fsS -X POST "$PUSH_URL" \
  -H "Authorization: Bearer $PUSH_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "@$DATA_FILE" >/dev/null

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] scrape+push complete"
