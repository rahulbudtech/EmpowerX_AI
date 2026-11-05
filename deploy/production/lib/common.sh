#!/bin/bash
# Production-only helpers. Independent of staging deploy/lib/common.sh.

redact_stream() {
  sed -E \
    -e 's#://[^[:space:]/@]+:[^[:space:]@]+@#://***:***@#g' \
    -e 's#(DATABASE_URL|REDIS_URL|GEMINI_API_KEY|GOOGLE_API_KEY|CELERY_BROKER_URL|CELERY_RESULT_BACKEND|PASSWORD|SECRET|TOKEN|PGPASSWORD)=[^[:space:]]*#\1=***#g'
}

log() {
  local msg line
  msg="$(printf '%s\n' "$*" | redact_stream)"
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) ${msg}"
  printf '%s\n' "$line" >&2
  if [[ -n "${EMPOWERX_AI_LOG_FILE:-}" ]]; then
    printf '%s\n' "$line" >> "$EMPOWERX_AI_LOG_FILE"
  fi
}

die() {
  local code="$1"
  shift
  log "ERROR: $*"
  exit "$code"
}

assert_sha() {
  local value="$1"
  [[ "${#value}" -eq 40 ]] || return 1
  case "$value" in
    *[!0-9a-f]*) return 1 ;;
  esac
  return 0
}

sha256_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  else
    shasum -a 256 "$file" | awk '{print $1}'
  fi
}

prod_test_mode() {
  [[ "${EMPOWERX_AI_PROD_TEST:-}" == "1" ]]
}

load_prod_config() {
  if prod_test_mode; then
    [[ -n "${EMPOWERX_AI_LIVE_DIR:-}" ]] || die 4 "missing EMPOWERX_AI_LIVE_DIR"
    [[ -n "${EMPOWERX_AI_STATE_DIR:-}" ]] || die 4 "missing EMPOWERX_AI_STATE_DIR"
  else
    [[ -f /etc/empowerx-ai-prod/deploy.env ]] || die 4 "missing /etc/empowerx-ai-prod/deploy.env"
    # shellcheck disable=SC1091
    source /etc/empowerx-ai-prod/deploy.env
  fi
  EMPOWERX_AI_LIVE_DIR="${EMPOWERX_AI_LIVE_DIR:-/var/www/empowerx/EmpowerX_AI}"
  EMPOWERX_AI_STATE_DIR="${EMPOWERX_AI_STATE_DIR:-/var/lib/empowerx-ai-prod}"
  EMPOWERX_AI_HEALTH_URL="${EMPOWERX_AI_HEALTH_URL:-http://127.0.0.1:8002/health}"
  EMPOWERX_AI_API_PM2="${EMPOWERX_AI_API_PM2:-empowerx-ai-api}"
  EMPOWERX_AI_WORKER_PM2="${EMPOWERX_AI_WORKER_PM2:-empowerx-ai-worker}"
  EMPOWERX_AI_MIN_FREE_KB="${EMPOWERX_AI_MIN_FREE_KB:-1048576}"
  EMPOWERX_AI_PG_DUMP="${EMPOWERX_AI_PG_DUMP:-pg_dump}"
  EMPOWERX_AI_PM2="${EMPOWERX_AI_PM2:-}"
  export EMPOWERX_AI_LIVE_DIR EMPOWERX_AI_STATE_DIR EMPOWERX_AI_HEALTH_URL
  export EMPOWERX_AI_API_PM2 EMPOWERX_AI_WORKER_PM2 EMPOWERX_AI_MIN_FREE_KB
  export EMPOWERX_AI_PG_DUMP EMPOWERX_AI_PM2
}

python_bin() {
  printf '%s\n' "${EMPOWERX_AI_PYTHON:-python3}"
}

check_disk() {
  local avail min
  min="${EMPOWERX_AI_MIN_FREE_KB:-1048576}"
  avail="$(df -Pk "$EMPOWERX_AI_STATE_DIR" | awk 'NR==2 {print $4}')"
  [[ -n "$avail" ]] || return 1
  [[ "$avail" -ge "$min" ]]
}

acquire_lock() {
  local lockdir="$EMPOWERX_AI_STATE_DIR/locks/deploy.lock"
  local tries="${EMPOWERX_AI_LOCK_TRIES:-600}"
  local i=0 holder
  mkdir -p "$EMPOWERX_AI_STATE_DIR/locks"
  while [[ "$i" -lt "$tries" ]]; do
    if mkdir "$lockdir" 2>/dev/null; then
      printf '%s\n' "$$" > "$lockdir/pid"
      LOCK_DIR="$lockdir"
      export LOCK_DIR
      return 0
    fi
    holder=""
    if [[ -f "$lockdir/pid" ]]; then
      holder="$(cat "$lockdir/pid" 2>/dev/null || true)"
    fi
    if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
      rm -rf "$lockdir"
    else
      i=$((i + 1))
      sleep "${EMPOWERX_AI_LOCK_SLEEP:-0.1}"
    fi
  done
  die 1 "another deployment holds the lock"
}

release_lock() {
  case "${LOCK_DIR:-}" in
    */locks/deploy.lock) rm -rf "$LOCK_DIR" ;;
  esac
}

read_state() {
  local file="$EMPOWERX_AI_STATE_DIR/state/$1"
  [[ -f "$file" ]] || return 1
  tr -d '[:space:]' < "$file"
}

write_state() {
  mkdir -p "$EMPOWERX_AI_STATE_DIR/state"
  printf '%s\n' "$2" > "$EMPOWERX_AI_STATE_DIR/state/$1"
}

assert_persistent() {
  local name path
  for name in .env uploads preprocessed; do
    path="$EMPOWERX_AI_LIVE_DIR/$name"
    if [[ -L "$path" ]]; then
      die 4 "refusing to deploy because $name is a symlink"
    fi
    if [[ ! -e "$path" ]]; then
      die 4 "missing persistent path $name"
    fi
  done
}
