#!/bin/bash
# Restore the previous successful release. Does not change the database.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export EMPOWERX_DEPLOY_ROOT="$ROOT"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

load_config
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

invoke_activate switch "$target"
invoke_pm2 reload-ai
export EMPOWERX_AI_HEALTH_PHASE=rollback
"$ROOT/bin/healthcheck.sh"
write_state current "$target"
if [[ -n "$current" && "$current" != "$target" ]]; then
  write_state previous "$current"
fi
log "rollback succeeded $target"
