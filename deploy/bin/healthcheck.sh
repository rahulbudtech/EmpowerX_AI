#!/bin/bash
# Verify the AI API, its database, and the Celery worker.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

load_config

curl_bin() {
  if production_mode; then
    printf '%s\n' /usr/bin/curl
  else
    printf '%s\n' "${EMPOWERX_AI_CURL:-curl}"
  fi
}

parse_health() {
  local body="$1" py
  py="$(python_bin)"
  "$py" - "$body" <<'PY'
import json, sys
raw = open(sys.argv[1], "r", encoding="utf-8").read(4096)
try:
    data = json.loads(raw)
except Exception:
    raise SystemExit(1)
if data.get("status") == "ok" and data.get("redis") == "ok":
    raise SystemExit(0)
raise SystemExit(1)
PY
}

wait_for_url() {
  local url="$1" label="$2"
  local deadline curl_max interval body code_file rc http CURL
  local last_rc=1
  CURL="$(curl_bin)"
  curl_max="${EMPOWERX_AI_CURL_MAX_TIME:-5}"
  interval="${EMPOWERX_AI_HEALTH_INTERVAL:-2}"
  deadline=$((SECONDS + ${EMPOWERX_AI_HEALTH_TIMEOUT:-40}))
  while [[ "$SECONDS" -lt "$deadline" ]]; do
    body="$(mktemp)"
    set +e
    http="$("$CURL" --silent --show-error --max-time "$curl_max" --output "$body" --write-out '%{http_code}' "$url" 2>/dev/null)"
    rc=$?
    set -e
    last_rc="$rc"
    if [[ "$rc" -eq 0 && "$http" == "200" ]] && parse_health "$body"; then
      rm -f "$body"
      log "$label health is ok"
      return 0
    fi
    rm -f "$body"
    sleep "$interval"
  done
  if [[ "$last_rc" -eq 28 ]]; then
    log "$label health check timed out"
  else
    log "$label FastAPI health check failed"
  fi
  return 1
}

if ! wait_for_url "$EMPOWERX_AI_HEALTH_URL" "local"; then
  exit 20
fi

if [[ "$EMPOWERX_AI_REQUIRE_PUBLIC_HEALTH" == "yes" ]]; then
  if ! wait_for_url "$EMPOWERX_AI_PUBLIC_HEALTH_URL" "public"; then
    exit 20
  fi
fi

set +e
invoke_runtime check-database
db_rc=$?
set -e
if [[ "$db_rc" -ne 0 ]]; then
  log "PostgreSQL connectivity check failed"
  exit 21
fi
log "PostgreSQL connectivity check passed"

worker_attempts="${EMPOWERX_AI_WORKER_CHECK_ATTEMPTS:-5}"
worker_delay="${EMPOWERX_AI_WORKER_CHECK_DELAY:-3}"
worker_rc=1
worker_attempt=1
while [[ "$worker_attempt" -le "$worker_attempts" ]]; do
  set +e
  invoke_runtime check-worker
  worker_rc=$?
  set -e
  if [[ "$worker_rc" -eq 0 ]]; then
    log "Celery worker responded"
    exit 0
  fi
  if [[ "$worker_attempt" -lt "$worker_attempts" ]]; then
    log "Celery worker check attempt ${worker_attempt}/${worker_attempts} failed; retrying"
    sleep "$worker_delay"
  fi
  worker_attempt=$((worker_attempt + 1))
done
log "Celery worker check failed"
exit 22
