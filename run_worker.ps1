# Start Celery worker for Empowerx (Windows: use solo pool).
# Usage (from repo root, venv active):
#   .\run_worker.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\Activate.ps1")) {
    Write-Error ".venv not found. Create it with: python -m venv .venv"
}

& .\.venv\Scripts\Activate.ps1
Write-Host "Starting Celery worker (queue=radiology) ..."
celery -A app.workers.celery_app.celery_app worker --loglevel=INFO --pool=solo -Q radiology,celery
