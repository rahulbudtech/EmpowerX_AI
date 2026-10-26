#!/bin/bash
# Run the deployment test suite on a developer machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
cd "$REPO"

bash "$ROOT/bin/run-static-checks.sh"

PY="${EMPOWERX_AI_PYTHON:-python3.12}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY="python3"
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  printf '%s\n' "Python 3.12 is required for local deployment checks" >&2
  exit 1
fi

VENV="$REPO/.tooling-venv"
if [[ ! -x "$VENV/bin/pytest" ]]; then
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install pytest
fi

set +e
output="$("$VENV/bin/pytest" tests/deploy -q --tb=short)"
status=$?
set -e
printf '%s\n' "$output"
if [[ "$status" -ne 0 ]]; then
  exit "$status"
fi
if printf '%s\n' "$output" | grep -E 'skipped|no tests ran|collected 0 items' >/dev/null; then
  printf '%s\n' "Refusing to treat skipped or absent tests as success" >&2
  exit 1
fi

if "$PY" -c 'import fastapi, celery' >/dev/null 2>&1; then
  "$PY" -m compileall -f -q app migrations
  "$PY" -c 'import app.main, app.workers.celery_app'
  printf '%s\n' "application import check passed"
else
  printf '%s\n' "application import check not run: runtime dependencies are not installed in this interpreter"
fi
