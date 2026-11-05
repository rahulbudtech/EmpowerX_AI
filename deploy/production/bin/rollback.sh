#!/bin/bash
# Restore the previous production application release. Does not change the database.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

load_prod_config
umask 027
mkdir -p "$EMPOWERX_AI_STATE_DIR/logs" "$EMPOWERX_AI_STATE_DIR/locks"
acquire_lock
trap 'release_lock' EXIT
export EMPOWERX_AI_LOG_FILE="$EMPOWERX_AI_STATE_DIR/logs/rollback-$(date -u +%Y%m%dT%H%M%SZ).log"

target="$(read_state previous || true)"
[[ -n "$target" ]] || die 7 "no previous release is recorded"
assert_sha "$target" || die 2 "invalid previous release"
current="$(read_state current || true)"
log "manual rollback to $target"

backup=""
if [[ -n "$current" && -d "$EMPOWERX_AI_STATE_DIR/backups/code/$current-previous" ]]; then
  backup="$EMPOWERX_AI_STATE_DIR/backups/code/$current-previous"
fi
if [[ -z "$backup" ]]; then
  die 7 "previous application snapshot is missing"
fi

rsync -a --checksum \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude 'uploads' \
  --exclude 'preprocessed' \
  --exclude '.venv' \
  --exclude '.git' \
  "$backup"/ "$EMPOWERX_AI_LIVE_DIR"/

if [[ -x "$EMPOWERX_AI_STATE_DIR/venvs/$target/bin/python" ]]; then
  ln -sfn "$EMPOWERX_AI_STATE_DIR/venvs/$target" "$EMPOWERX_AI_LIVE_DIR/.venv"
elif [[ -x "$EMPOWERX_AI_STATE_DIR/venvs/baseline/bin/python" ]]; then
  ln -sfn "$EMPOWERX_AI_STATE_DIR/venvs/baseline" "$EMPOWERX_AI_LIVE_DIR/.venv"
fi
printf '%s\n' "$target" > "$EMPOWERX_AI_LIVE_DIR/.deploy-sha"

"$ROOT/vps/empowerx-ai-prod" reload-ai
export EMPOWERX_AI_EXPECTED_SHA="$target"
export EMPOWERX_AI_HEALTH_PHASE=rollback
"$ROOT/bin/healthcheck.sh"
write_state current "$target"
if [[ -n "$current" && "$current" != "$target" ]]; then
  write_state previous "$current"
fi
log "rollback succeeded $target"
log "database schema was not reverted"
