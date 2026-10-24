#!/usr/bin/env bash
# Start the Empowerx FastAPI backend (local dev).
# Usage (from repo root, Git Bash / WSL / macOS / Linux):
#   bash run_backend.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Activate venv (Windows: Scripts/, Unix: bin/)
if [[ -f ".venv/Scripts/activate" ]]; then
  # Git Bash on Windows
  # shellcheck disable=SC1091
  source ".venv/Scripts/activate"
elif [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
else
  echo "ERROR: .venv not found. Create it with: python -m venv .venv"
  exit 1
fi

echo "Using Python: $(python --version)"
echo "Starting backend on http://127.0.0.1:8000 ..."

# --reload for local development; remove in production
exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
