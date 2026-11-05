#!/bin/bash
# Verify the production AI API, database, and Celery worker.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

load_prod_config

parse_health() {
  local body="$1"
  "$(python_bin)" - "$body" <<'PY'
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

CURL="${EMPOWERX_AI_CURL:-curl}"
curl_max="${EMPOWERX_AI_CURL_MAX_TIME:-5}"
interval="${EMPOWERX_AI_HEALTH_INTERVAL:-2}"
deadline=$((SECONDS + ${EMPOWERX_AI_HEALTH_TIMEOUT:-40}))
last_rc=1
healthy=0
while [[ "$SECONDS" -lt "$deadline" ]]; do
  body="$(mktemp)"
  set +e
  http="$("$CURL" --silent --show-error --max-time "$curl_max" --output "$body" --write-out '%{http_code}' "$EMPOWERX_AI_HEALTH_URL" 2>/dev/null)"
  rc=$?
  set -e
  last_rc="$rc"
  if [[ "$rc" -eq 0 && "$http" == "200" ]] && parse_health "$body"; then
    rm -f "$body"
    healthy=1
    log "local health is ok"
    break
  fi
  rm -f "$body"
  sleep "$interval"
done
if [[ "$healthy" -ne 1 ]]; then
  if [[ "$last_rc" -eq 28 ]]; then
    log "local health check timed out"
    exit 20
  fi
  log "local FastAPI health check failed"
  exit 20
fi

check_commit() {
  local expected actual
  expected="${EMPOWERX_AI_EXPECTED_SHA:-}"
  [[ -n "$expected" ]] || return 0
  assert_sha "$expected" || return 1
  [[ -f "$EMPOWERX_AI_LIVE_DIR/.deploy-sha" ]] || return 1
  actual="$(tr -d '[:space:]' < "$EMPOWERX_AI_LIVE_DIR/.deploy-sha")"
  [[ "$actual" == "$expected" ]]
}

check_pm2_ai() {
  local pm2_bin
  if prod_test_mode; then
    pm2_bin="${EMPOWERX_PM2:-pm2}"
  else
    pm2_bin="${EMPOWERX_AI_PM2:-$(command -v pm2 || true)}"
  fi
  [[ -n "$pm2_bin" ]] || return 1
  "$pm2_bin" jlist | "$(python_bin)" -c '
import json, os, sys
live, api, worker = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.stderr.write("pm2 process list is unreadable\n")
    raise SystemExit(1)
if isinstance(data, dict):
    data = data.get("processes", [])
wanted = (api, worker)
counts = {name: 0 for name in wanted}
live_real = os.path.realpath(live)
for proc in data:
    name = proc.get("name")
    if name not in counts:
        continue
    counts[name] += 1
    env = proc.get("pm2_env") or {}
    status = str(env.get("status") or proc.get("status") or "")
    if status and status != "online":
        sys.stderr.write("%s is not online\n" % name)
        raise SystemExit(1)
    cwd = str(env.get("pm_cwd") or "")
    if cwd and os.path.realpath(cwd) != live_real:
        sys.stderr.write("%s is not running from the live directory\n" % name)
        raise SystemExit(1)
for name in wanted:
    if counts[name] != 1:
        sys.stderr.write("expected exactly one %s process, found %s\n" % (name, counts[name]))
        raise SystemExit(1)
' "$EMPOWERX_AI_LIVE_DIR" "$EMPOWERX_AI_API_PM2" "$EMPOWERX_AI_WORKER_PM2"
}

check_database() {
  if prod_test_mode; then
    printf 'check-database\n' >> "$EMPOWERX_AI_STATE_DIR/stub-runtime/calls"
    mode="$(cat "$EMPOWERX_AI_STATE_DIR/stub-runtime/database-mode" 2>/dev/null || echo ok)"
    phase="${EMPOWERX_AI_HEALTH_PHASE:-deploy}"
    if [[ "$mode" == "fail-deploy" && "$phase" == "deploy" ]]; then
      return 1
    fi
    return 0
  fi
  (
    cd "$EMPOWERX_AI_LIVE_DIR"
    "$EMPOWERX_AI_LIVE_DIR/.venv/bin/python" - <<'PY'
import sys
try:
    from sqlalchemy import text
    from app.infrastructure.database import engine
    with engine.connect() as conn:
        value = conn.execute(text("SELECT 1")).scalar()
    if value != 1:
        raise RuntimeError("probe")
except Exception as exc:
    sys.stderr.write("database connectivity check failed: {0}\n".format(type(exc).__name__))
    raise SystemExit(1)
PY
  )
}

check_worker() {
  if prod_test_mode; then
    mkdir -p "$EMPOWERX_AI_STATE_DIR/stub-runtime"
    printf 'check-worker\n' >> "$EMPOWERX_AI_STATE_DIR/stub-runtime/calls"
    mode="$(cat "$EMPOWERX_AI_STATE_DIR/stub-runtime/worker-mode" 2>/dev/null || echo ok)"
    phase="${EMPOWERX_AI_HEALTH_PHASE:-deploy}"
    count_file="$EMPOWERX_AI_STATE_DIR/stub-runtime/worker-count"
    n=0
    [[ -f "$count_file" ]] && n="$(cat "$count_file")"
    n=$((n + 1))
    printf '%s\n' "$n" > "$count_file"
    if [[ "$mode" == "fail-deploy" && "$phase" == "deploy" ]]; then
      return 1
    fi
    return 0
  fi
  set +e
  out="$(
    cd "$EMPOWERX_AI_LIVE_DIR" &&
    "$EMPOWERX_AI_LIVE_DIR/.venv/bin/celery" -A app.workers.celery_app.celery_app inspect ping --timeout 15 2>&1
  )"
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]] && printf '%s\n' "$out" | grep -q "pong"; then
    return 0
  fi
  printf '%s\n' "$out" | redact_stream >&2
  return 1
}

if [[ -n "${EMPOWERX_AI_EXPECTED_SHA:-}" ]]; then
  if ! check_commit; then
    log "deployed commit does not match the intended production SHA"
    exit 23
  fi
  log "live directory is running the intended commit"
fi

pm2_attempts="${EMPOWERX_AI_PM2_CHECK_ATTEMPTS:-5}"
pm2_delay="${EMPOWERX_AI_PM2_CHECK_DELAY:-2}"
pm2_attempt=1
pm2_ok=0
while [[ "$pm2_attempt" -le "$pm2_attempts" ]]; do
  if check_pm2_ai; then
    pm2_ok=1
    break
  fi
  if [[ "$pm2_attempt" -lt "$pm2_attempts" ]]; then
    sleep "$pm2_delay"
  fi
  pm2_attempt=$((pm2_attempt + 1))
done
if [[ "$pm2_ok" -ne 1 ]]; then
  log "PM2 AI process check failed"
  exit 24
fi
log "PM2 has exactly one AI API and one AI worker"

if ! check_database; then
  log "PostgreSQL connectivity check failed"
  exit 21
fi
log "PostgreSQL connectivity check passed"

worker_attempts="${EMPOWERX_AI_WORKER_CHECK_ATTEMPTS:-5}"
worker_delay="${EMPOWERX_AI_WORKER_CHECK_DELAY:-3}"
worker_attempt=1
while [[ "$worker_attempt" -le "$worker_attempts" ]]; do
  if check_worker; then
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
