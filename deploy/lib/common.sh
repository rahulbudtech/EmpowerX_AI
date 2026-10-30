#!/bin/bash
# Shared helpers for EmpowerX AI deployment. Bash 3.2 compatible.

empowerx_deploy_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$here/.." && pwd
}

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

assert_revision() {
  local value="$1"
  [[ -n "$value" ]] || return 1
  case "$value" in
    *[!A-Za-z0-9_]*) return 1 ;;
  esac
  return 0
}

production_mode() {
  [[ -f /etc/empowerx-ai/production ]]
}

apply_security_posture() {
  if production_mode; then
    EMPOWERX_AI_TEST_MODE=0
    unset EMPOWERX_AI_ALLOW_TEST_MODE || true
    export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  fi
}

sha256_file() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  else
    shasum -a 256 "$file" | awk '{print $1}'
  fi
}

parse_config_file() {
  local file="$1" line key value
  [[ -f "$file" ]] || die 4 "missing deployment configuration"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    case "$line" in
      \#*) continue ;;
    esac
    if printf '%s\n' "$line" | grep -q '[;|&<>`$()]'; then
      die 4 "unsafe deployment configuration"
    fi
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" != "$line" ]] || die 4 "unsafe deployment configuration"
    case "$key" in
      EMPOWERX_AI_LIVE_DIR|EMPOWERX_AI_STATE_DIR|EMPOWERX_AI_PYTHON|EMPOWERX_AI_UV|EMPOWERX_AI_HEALTH_URL|EMPOWERX_AI_PUBLIC_HEALTH_URL|EMPOWERX_AI_REQUIRE_PUBLIC_HEALTH|EMPOWERX_AI_API_PM2|EMPOWERX_AI_WORKER_PM2|EMPOWERX_AI_BASELINE_SHA|EMPOWERX_AI_MIN_FREE_KB|EMPOWERX_AI_PG_DUMP|EMPOWERX_AI_ECOSYSTEM)
        printf -v "$key" '%s' "$value"
        export "$key"
        ;;
      *)
        die 4 "unexpected deployment configuration key"
        ;;
    esac
  done < "$file"
}

load_config() {
  apply_security_posture
  if production_mode; then
    parse_config_file /etc/empowerx-ai/deploy.env
  fi
  [[ -n "${EMPOWERX_AI_LIVE_DIR:-}" ]] || die 4 "missing EMPOWERX_AI_LIVE_DIR"
  [[ -n "${EMPOWERX_AI_STATE_DIR:-}" ]] || die 4 "missing EMPOWERX_AI_STATE_DIR"
  [[ -n "${EMPOWERX_AI_PYTHON:-}" ]] || die 4 "missing EMPOWERX_AI_PYTHON"
  [[ -n "${EMPOWERX_AI_UV:-}" ]] || die 4 "missing EMPOWERX_AI_UV"
  export EMPOWERX_AI_LIVE_DIR EMPOWERX_AI_STATE_DIR EMPOWERX_AI_PYTHON EMPOWERX_AI_UV
  EMPOWERX_AI_HEALTH_URL="${EMPOWERX_AI_HEALTH_URL:-http://127.0.0.1:8002/health}"
  EMPOWERX_AI_PUBLIC_HEALTH_URL="${EMPOWERX_AI_PUBLIC_HEALTH_URL:-https://ai.devempowerx.io/health}"
  EMPOWERX_AI_REQUIRE_PUBLIC_HEALTH="${EMPOWERX_AI_REQUIRE_PUBLIC_HEALTH:-yes}"
  EMPOWERX_AI_API_PM2="${EMPOWERX_AI_API_PM2:-empowerx-ai-api}"
  EMPOWERX_AI_WORKER_PM2="${EMPOWERX_AI_WORKER_PM2:-empowerx-ai-worker}"
  EMPOWERX_AI_MIN_FREE_KB="${EMPOWERX_AI_MIN_FREE_KB:-1048576}"
  export EMPOWERX_AI_HEALTH_URL EMPOWERX_AI_PUBLIC_HEALTH_URL EMPOWERX_AI_REQUIRE_PUBLIC_HEALTH
  export EMPOWERX_AI_API_PM2 EMPOWERX_AI_WORKER_PM2 EMPOWERX_AI_MIN_FREE_KB
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
  local name="$1"
  local file="$EMPOWERX_AI_STATE_DIR/state/$name"
  [[ -f "$file" ]] || return 1
  tr -d '[:space:]' < "$file"
}

write_state() {
  local name="$1" value="$2"
  mkdir -p "$EMPOWERX_AI_STATE_DIR/state"
  printf '%s\n' "$value" > "$EMPOWERX_AI_STATE_DIR/state/$name"
}

python_bin() {
  if production_mode; then
    printf '%s\n' "$EMPOWERX_AI_PYTHON"
  else
    printf '%s\n' "${EMPOWERX_AI_PYTHON:-python3}"
  fi
}

deploy_root() {
  if [[ -n "${EMPOWERX_DEPLOY_ROOT:-}" ]]; then
    printf '%s\n' "$EMPOWERX_DEPLOY_ROOT"
    return 0
  fi
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$here/.." && pwd
}

invoke_runtime() {
  local root
  root="$(deploy_root)"
  if production_mode; then
    sudo -n -u empowerx-ai /usr/local/sbin/empowerx-ai-runtime "$@"
  else
    bash "$root/vps/empowerx-ai-runtime" "$@"
  fi
}

invoke_activate() {
  local root
  root="$(deploy_root)"
  if production_mode; then
    sudo -n /usr/local/sbin/empowerx-ai-activate "$@"
  else
    bash "$root/vps/empowerx-ai-activate" "$@"
  fi
}

invoke_pm2() {
  local root
  root="$(deploy_root)"
  if production_mode; then
    sudo -n /usr/local/sbin/empowerx-ai-pm2 "$@"
  else
    bash "$root/vps/empowerx-ai-pm2" "$@"
  fi
}

invoke_migrate() {
  local root
  root="$(deploy_root)"
  if production_mode; then
    sudo -n /usr/local/sbin/empowerx-ai-migrate "$@"
  else
    bash "$root/vps/empowerx-ai-migrate" "$@"
  fi
}
