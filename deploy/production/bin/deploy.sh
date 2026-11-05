#!/bin/bash
# Deploy one production commit onto the live AI directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

SHA="${1:-}"
assert_sha "$SHA" || die 2 "invalid commit sha"

load_prod_config
assert_persistent
umask 027
mkdir -p \
  "$EMPOWERX_AI_STATE_DIR/incoming" \
  "$EMPOWERX_AI_STATE_DIR/releases" \
  "$EMPOWERX_AI_STATE_DIR/logs" \
  "$EMPOWERX_AI_STATE_DIR/locks" \
  "$EMPOWERX_AI_STATE_DIR/state" \
  "$EMPOWERX_AI_STATE_DIR/venvs" \
  "$EMPOWERX_AI_STATE_DIR/backups/code" \
  "$EMPOWERX_AI_STATE_DIR/backups/migrations"

acquire_lock
trap 'release_lock' EXIT

LOG="$EMPOWERX_AI_STATE_DIR/logs/deploy-$(date -u +%Y%m%dT%H%M%SZ)-$SHA.log"
export EMPOWERX_AI_LOG_FILE="$LOG"
log "starting production deploy $SHA"

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
  --require-branch production; then
  rm -rf "$dest"
  die 3 "deployment artifact was rejected"
fi

build_venv() {
  local release="$1" venv live_py
  venv="$EMPOWERX_AI_STATE_DIR/venvs/$SHA"
  if [[ -e "$venv" ]]; then
    log "release virtualenv already exists"
    return 5
  fi
  live_py="$EMPOWERX_AI_LIVE_DIR/.venv/bin/python"
  if [[ ! -x "$live_py" ]]; then
    live_py="$py"
  fi
  if prod_test_mode; then
    if [[ -f "$EMPOWERX_AI_STATE_DIR/fail-deps" ]]; then
      log "dependency installation failed"
      return 5
    fi
    mkdir -p "$venv/bin"
    printf '%s\n' '#!/bin/bash' 'exit 0' > "$venv/bin/python"
    chmod +x "$venv/bin/python"
    printf '%s\n' '#!/bin/bash' 'exit 0' > "$venv/bin/pip"
    chmod +x "$venv/bin/pip"
    printf '%s\n' '#!/bin/bash' 'exit 0' > "$venv/bin/alembic"
    chmod +x "$venv/bin/alembic"
    printf '%s\n' '#!/bin/bash' 'exit 0' > "$venv/bin/celery"
    chmod +x "$venv/bin/celery"
    return 0
  fi
  if ! "$live_py" -m venv "$venv"; then
    log "virtualenv creation failed"
    return 5
  fi
  if ! "$venv/bin/pip" install --upgrade pip >/dev/null; then
    log "dependency installation failed"
    return 5
  fi
  if ! "$venv/bin/pip" install -r "$release/requirements.txt"; then
    log "dependency installation failed"
    return 5
  fi
  if ! (cd "$release" && "$venv/bin/python" -c 'import app.main, app.workers.celery_app'); then
    log "application import failed after dependency installation"
    return 5
  fi
  return 0
}

if ! build_venv "$dest"; then
  rm -rf "$dest" "$EMPOWERX_AI_STATE_DIR/venvs/$SHA"
  die 5 "dependency installation failed"
fi

MIGRATED=0
apply_migrations() {
  local release="$1" venvpy out rc head_line backup
  venvpy="$EMPOWERX_AI_STATE_DIR/venvs/$SHA/bin/python"
  if prod_test_mode; then
    rc="$(cat "$EMPOWERX_AI_STATE_DIR/stub-runtime/migrations.exit" 2>/dev/null || printf '%s' 0)"
    if [[ "$rc" == "0" ]]; then
      log "no pending migrations"
      return 0
    fi
    if [[ "$rc" == "10" ]]; then
      if [[ -f "$EMPOWERX_AI_STATE_DIR/stub-runtime/migrate.exit" ]]; then
        log "migration execution failed"
        return 6
      fi
      printf 'apply %s\n' "$SHA" >> "$EMPOWERX_AI_STATE_DIR/stub-runtime/calls"
      MIGRATED=1
      log "applied pending forward migrations"
      return 0
    fi
    log "migration execution failed"
    return 6
  fi
  set +e
  out="$("$venvpy" "$ROOT/lib/migrate.py" check --release "$release" --env-file "$EMPOWERX_AI_LIVE_DIR/.env" 2>&1)"
  rc=$?
  set -e
  printf '%s\n' "$out" | redact_stream >&2
  if [[ "$rc" -eq 0 ]]; then
    log "no pending migrations"
    return 0
  fi
  [[ "$rc" -eq 10 ]] || {
    log "migration execution failed"
    return 6
  }
  head_line="$(printf '%s\n' "$out" | awk -F= '/^head=/{print $2; exit}')"
  backup="$EMPOWERX_AI_STATE_DIR/backups/migrations/$(date -u +%Y%m%dT%H%M%SZ)-$head_line.dump"
  log "applying pending forward migrations"
  if ! "$venvpy" "$ROOT/lib/migrate.py" upgrade \
    --release "$release" \
    --env-file "$EMPOWERX_AI_LIVE_DIR/.env" \
    --backup "$backup" \
    --expect-head "$head_line"; then
    log "migration execution failed"
    return 6
  fi
  MIGRATED=1
  log "applied pending forward migrations $head_line"
  return 0
}

if ! apply_migrations "$dest"; then
  rm -rf "$dest" "$EMPOWERX_AI_STATE_DIR/venvs/$SHA"
  die 6 "migration execution failed"
fi

snapshot_code() {
  local dest_dir="$1"
  mkdir -p "$dest_dir"
  rsync -a --checksum \
    --exclude '.env' \
    --exclude '.env.local' \
    --exclude 'uploads' \
    --exclude 'preprocessed' \
    --exclude '.venv' \
    --exclude '.git' \
    "$EMPOWERX_AI_LIVE_DIR"/ "$dest_dir"/
}

switch_live() {
  rsync -a --checksum \
    --exclude '.env' \
    --exclude '.env.local' \
    --exclude 'uploads' \
    --exclude 'preprocessed' \
    --exclude '.venv' \
    --exclude '.git' \
    "$dest"/ "$EMPOWERX_AI_LIVE_DIR"/
  if [[ -L "$EMPOWERX_AI_LIVE_DIR/.venv" ]]; then
    ln -sfn "$EMPOWERX_AI_STATE_DIR/venvs/$SHA" "$EMPOWERX_AI_LIVE_DIR/.venv"
  elif [[ -d "$EMPOWERX_AI_LIVE_DIR/.venv" ]]; then
    if [[ ! -d "$EMPOWERX_AI_STATE_DIR/venvs/baseline" ]]; then
      mv "$EMPOWERX_AI_LIVE_DIR/.venv" "$EMPOWERX_AI_STATE_DIR/venvs/baseline"
    else
      rm -rf "$EMPOWERX_AI_LIVE_DIR/.venv"
    fi
    ln -sfn "$EMPOWERX_AI_STATE_DIR/venvs/$SHA" "$EMPOWERX_AI_LIVE_DIR/.venv"
  else
    ln -sfn "$EMPOWERX_AI_STATE_DIR/venvs/$SHA" "$EMPOWERX_AI_LIVE_DIR/.venv"
  fi
  printf '%s\n' "$SHA" > "$EMPOWERX_AI_LIVE_DIR/.deploy-sha"
}

PREVIOUS="$(read_state current || true)"
snapshot_code "$EMPOWERX_AI_STATE_DIR/backups/code/$SHA-previous"
if [[ -n "$PREVIOUS" ]]; then
  write_state previous "$PREVIOUS"
fi

reload_ai() {
  if prod_test_mode; then
    printf 'reload-ai\n' >> "$EMPOWERX_AI_STATE_DIR/pm2-calls"
    "$ROOT/vps/empowerx-ai-prod" reload-ai
    return $?
  fi
  "$ROOT/vps/empowerx-ai-prod" reload-ai
}

do_rollback() {
  local prev snapshot
  snapshot="$EMPOWERX_AI_STATE_DIR/backups/code/$SHA-previous"
  [[ -d "$snapshot" ]] || return 1
  prev="$(read_state previous || true)"
  [[ -n "$prev" ]] || prev="$(read_state baseline || true)"
  log "restoring previous application code"
  rsync -a --checksum \
    --exclude '.env' \
    --exclude '.env.local' \
    --exclude 'uploads' \
    --exclude 'preprocessed' \
    --exclude '.venv' \
    --exclude '.git' \
    "$snapshot"/ "$EMPOWERX_AI_LIVE_DIR"/
  if [[ -n "$prev" && -x "$EMPOWERX_AI_STATE_DIR/venvs/$prev/bin/python" ]]; then
    ln -sfn "$EMPOWERX_AI_STATE_DIR/venvs/$prev" "$EMPOWERX_AI_LIVE_DIR/.venv"
  elif [[ -x "$EMPOWERX_AI_STATE_DIR/venvs/baseline/bin/python" ]]; then
    ln -sfn "$EMPOWERX_AI_STATE_DIR/venvs/baseline" "$EMPOWERX_AI_LIVE_DIR/.venv"
  fi
  if [[ -n "$prev" && "$prev" != "baseline" ]]; then
    printf '%s\n' "$prev" > "$EMPOWERX_AI_LIVE_DIR/.deploy-sha"
  else
    rm -f "$EMPOWERX_AI_LIVE_DIR/.deploy-sha"
  fi
  reload_ai || return 1
  if [[ -n "$prev" && "$prev" != "baseline" ]]; then
    EMPOWERX_AI_EXPECTED_SHA="$prev" EMPOWERX_AI_HEALTH_PHASE=rollback "$ROOT/bin/healthcheck.sh"
  else
    unset EMPOWERX_AI_EXPECTED_SHA
    EMPOWERX_AI_HEALTH_PHASE=rollback "$ROOT/bin/healthcheck.sh"
  fi
}

switch_live
if ! reload_ai; then
  if do_rollback; then
    if [[ "$MIGRATED" == "1" ]]; then
      log "application code rolled back; database schema was not reverted"
    else
      log "rolled back to previous application code"
    fi
    exit 8
  fi
  die 9 "rollback failed after process reload error"
fi

set +e
export EMPOWERX_AI_EXPECTED_SHA="$SHA"
EMPOWERX_AI_HEALTH_PHASE=deploy "$ROOT/bin/healthcheck.sh"
health_rc=$?
set -e
if [[ "$health_rc" -ne 0 ]]; then
  log "health verification failed"
  if do_rollback; then
    if [[ "$MIGRATED" == "1" ]]; then
      log "application code rolled back; database schema was not reverted"
    else
      log "rolled back to previous application code"
    fi
    exit 8
  fi
  die 9 "rollback failed after health verification"
fi

write_state current "$SHA"
log "API and Celery worker are running production release $SHA"
log "deploy succeeded $SHA"
exit 0
