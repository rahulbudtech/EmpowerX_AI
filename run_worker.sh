#!/usr/bin/env bash
# Start Celery worker (Linux/macOS/Git Bash).
# Usage: bash run_worker.sh

set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/Scripts/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/Scripts/activate"
elif [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
else
  echo "ERROR: .venv not found"
  exit 1
fi

echo "Starting Celery worker (queue=radiology) ..."
exec celery -A app.workers.celery_app.celery_app worker --loglevel=INFO -Q radiology,celery
