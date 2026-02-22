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
RESPONSE_FILE="$ROOT/data/push_response.json"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

if [[ -z "$PUSH_TOKEN" && -f "$TOKEN_FILE" ]]; then
  PUSH_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
fi
if [[ -z "$PUSH_TOKEN" && -f "$ALT_TOKEN_FILE" ]]; then
  PUSH_TOKEN="$(tr -d '\r\n' < "$ALT_TOKEN_FILE")"
fi

if [[ -z "$PUSH_TOKEN" ]]; then
  log "missing push token; set TOURNAMENT_HUB_PUSH_TOKEN or create $TOKEN_FILE"
  exit 1
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "push already running; exiting"
  exit 0
fi
cleanup() {
  local exit_code=$?
  rmdir "$LOCK_DIR" 2>/dev/null || true
  if [[ $exit_code -ne 0 ]]; then
    log "job failed (exit=$exit_code)"
  fi
}
trap cleanup EXIT

cd "$ROOT"

log "job started"
"$PYTHON_BIN" "$ROOT/scripts/refresh_cache.py"
if [[ ! -s "$DATA_FILE" ]]; then
  log "missing or empty $DATA_FILE after refresh"
  exit 1
fi

http_code="$("$CURL_BIN" -sS -o "$RESPONSE_FILE" -w "%{http_code}" -X POST "$PUSH_URL" \
  -H "Authorization: Bearer $PUSH_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary "@$DATA_FILE")"

response_compact="$(tr '\n' ' ' < "$RESPONSE_FILE" | sed 's/[[:space:]]\+/ /g' | cut -c1-400)"
if [[ "$http_code" != 2* ]]; then
  log "push failed: http=$http_code response=$response_compact"
  exit 1
fi

log "push succeeded: http=$http_code response=$response_compact"
log "job complete"
