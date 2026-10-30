#!/bin/bash
# Activate a previously received EmpowerX AI release.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export EMPOWERX_DEPLOY_ROOT="$ROOT"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$ROOT/lib/release.sh"

SHA="${1:-}"
MIGRATE="no"
if [[ "${2:-}" == "migrate=yes" ]]; then
  MIGRATE="yes"
elif [[ -n "${2:-}" ]]; then
  die 2 "unknown deploy argument"
fi
assert_sha "$SHA" || die 2 "invalid commit sha"

load_config
assert_persistent
umask 027
mkdir -p \
  "$EMPOWERX_AI_STATE_DIR/incoming" \
  "$EMPOWERX_AI_STATE_DIR/releases" \
  "$EMPOWERX_AI_STATE_DIR/logs" \
  "$EMPOWERX_AI_STATE_DIR/locks" \
  "$EMPOWERX_AI_STATE_DIR/state" \
  "$EMPOWERX_AI_STATE_DIR/catalog" \
  "$EMPOWERX_AI_STATE_DIR/venvs" \
  "$EMPOWERX_AI_STATE_DIR/backups"

acquire_lock
trap 'release_lock' EXIT
rm -f "$EMPOWERX_AI_STATE_DIR/state/live-mutated"

LOG="$EMPOWERX_AI_STATE_DIR/logs/deploy-$(date -u +%Y%m%dT%H%M%SZ)-$SHA.log"
export EMPOWERX_AI_LOG_FILE="$LOG"
log "starting deploy $SHA"

check_disk || die 4 "insufficient disk space"

archive="$EMPOWERX_AI_STATE_DIR/incoming/$SHA.tar.gz"
sumfile="$EMPOWERX_AI_STATE_DIR/incoming/$SHA.sha256"
[[ -f "$archive" && -f "$sumfile" ]] || die 3 "missing deployment artifact"
[[ -r "$archive" && -r "$sumfile" ]] || die 4 "deployment artifact is not readable"

dest="$EMPOWERX_AI_STATE_DIR/releases/$SHA"
if [[ -e "$dest" ]]; then
  die 3 "release directory already exists"
fi

outer="$(tr -d '[:space:]' < "$sumfile")"
py="$(python_bin)"
if ! "$py" "$ROOT/lib/validate_archive.py" deploy-bundle \
  --archive "$archive" \
  --checksum "$outer" \
  --expected-commit "$SHA" \
  --dest "$dest" \
  --require-branch bud_development; then
  safe_rm_release "$dest" || true
  die 3 "deployment artifact was rejected"
fi

if ! "$py" "$ROOT/lib/scan_migrations.py" "$dest/migrations"; then
  safe_rm_release "$dest" || true
  die 6 "migration scan failed"
fi

if ! build_release "$dest"; then
  safe_rm_release "$dest" || true
  die 5 "dependency installation failed"
fi

set +e
mig_out="$(invoke_runtime check-migrations "$SHA" 2>&1)"
mig_rc=$?
set -e
printf '%s\n' "$mig_out" | while IFS= read -r line; do
  [[ -n "$line" ]] && log "migration: $line"
done

if [[ "$mig_rc" -eq 10 ]]; then
  if [[ "$MIGRATE" != "yes" ]]; then
    safe_rm_release "$dest" || true
    die 6 "pending migrations require explicit approval"
  fi
  if ! invoke_migrate apply "$SHA"; then
    safe_rm_release "$dest" || true
    die 6 "approved migration was not applied"
  fi
elif [[ "$mig_rc" -ne 0 ]]; then
  safe_rm_release "$dest" || true
  die 6 "could not determine migration status"
else
  log "no pending migrations"
fi

ROLLBACK_SHA="$(read_state current || true)"
if [[ -z "$ROLLBACK_SHA" ]]; then
  ROLLBACK_SHA="$(read_state baseline || true)"
fi

do_rollback() {
  [[ -n "${ROLLBACK_SHA:-}" ]] || return 1
  log "restoring release $ROLLBACK_SHA"
  invoke_activate switch "$ROLLBACK_SHA" || return 1
  invoke_pm2 reload-ai || return 1
  EMPOWERX_AI_HEALTH_PHASE=rollback "$ROOT/bin/healthcheck.sh"
}

activate_rc=0
set +e
invoke_activate switch "$SHA"
activate_rc=$?
set -e
if [[ -f "$EMPOWERX_AI_STATE_DIR/state/live-mutated" ]]; then
  ACTIVATED=1
else
  ACTIVATED=0
fi
if [[ "$activate_rc" -ne 0 ]]; then
  if [[ "$ACTIVATED" == "1" ]]; then
    if do_rollback; then
      log "rolled back to $ROLLBACK_SHA"
      exit 8
    fi
    die 9 "rollback failed after activation error"
  fi
  die 7 "activation failed"
fi

if ! invoke_pm2 reload-ai; then
  if do_rollback; then
    log "rolled back to $ROLLBACK_SHA after process reload failure"
    exit 8
  fi
  die 9 "rollback failed after process reload error"
fi

set +e
EMPOWERX_AI_HEALTH_PHASE=deploy "$ROOT/bin/healthcheck.sh"
health_rc=$?
set -e
if [[ "$health_rc" -ne 0 ]]; then
  log "health verification failed"
  if do_rollback; then
    log "rolled back to $ROLLBACK_SHA"
    exit 8
  fi
  die 9 "rollback failed after health verification"
fi

if [[ -n "$ROLLBACK_SHA" && "$ROLLBACK_SHA" != "$SHA" ]]; then
  write_state previous "$ROLLBACK_SHA"
fi
write_state current "$SHA"
prune_releases || true
log "deploy succeeded $SHA"
exit 0
