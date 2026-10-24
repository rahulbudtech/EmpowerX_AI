# Empowerx

Medical AI backend — **radiology-first** (pathology / dermatology scaffolds reserved).

FastAPI API → Redis/Celery queue → DICOM preprocess → Gemini findings → Postgres.

---

## Stack

| Layer | Tech |
|--------|------|
| API | FastAPI + Uvicorn |
| Queue | Celery + Redis (`radiology` queue) |
| DB | PostgreSQL + SQLAlchemy + Alembic |
| DICOM | pydicom + pylibjpeg |
| LLM | Google Gemini (`google-genai`) |
| Python | 3.11–3.12 |

---

## Folder structure

```text
Empowerx/
├── app/
│   ├── main.py                 # FastAPI entry
│   ├── api/
│   │   ├── router.py           # Mounts route modules
│   │   └── routes/
│   │       ├── health.py
│   │       ├── radiology.py    # Active: DICOM analyze + findings
│   │       ├── pathology.py    # Stub
│   │       └── dermatology.py  # Stub
│   ├── core/
│   │   ├── config.py           # Settings from .env
│   │   └── logging.py
│   ├── infrastructure/
│   │   ├── database.py         # Postgres session
│   │   └── redis.py
│   ├── modules/
│   │   ├── radiology/          # Domain logic (thin routes call here)
│   │   │   ├── preprocess.py   # DICOM → uint8 X-ray + safe metadata
│   │   │   ├── llm.py          # Gemini findings
│   │   │   ├── service.py      # Enqueue + worker orchestration + retries
│   │   │   ├── repository.py   # DB read/write
│   │   │   ├── models.py       # ORM (radiology_findings)
│   │   │   ├── schemas.py      # Pydantic request/response
│   │   │   ├── storage.py      # Local upload + preprocessed PNG paths
│   │   │   └── prompts/        # findings.txt (exact system prompt)
│   │   ├── pathology/          # Scaffold only
│   │   └── dermatology/        # Scaffold only
│   └── workers/
│       ├── celery_app.py
│       └── tasks/
│           └── radiology_tasks.py
├── migrations/                 # Alembic
├── unit_testing/               # Smoke / policy scripts (not formal pytest yet)
│   └── input/                  # Drop sample .dcm files here (gitignored)
├── unit_testing_results/       # Script outputs (gitignored)
├── uploads/                    # Staged DICOMs (UPLOAD_DIR, gitignored)
├── preprocessed/               # X-ray PNGs (PREPROCESS_IMAGE_DIR, gitignored)
├── tests/                      # Reserved for pytest
├── scripts/
├── Dockerfile.dev / .prod      # Empty stubs — owned by DevOps
├── Dockerfile.worker.dev/.prod
├── docker-compose.dev.yml / .prod.yml
├── run_backend.sh
├── run_worker.sh / run_worker.ps1
├── requirements.txt
├── pyproject.toml
├── alembic.ini
├── .env.example
└── README.md
```

**Architecture rule:** routes stay thin; business logic lives in `app/modules/<domain>/`.

---

## Prerequisites

- Python **3.11+** (and &lt; 3.13)
- **PostgreSQL** running locally (or reachable URL)
- **Redis** for Celery broker
- Gemini API key

---

## Local setup

```bash
# From repo root
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS / Linux / Git Bash
source .venv/bin/activate

pip install -r requirements.txt
pip install -e .

cp .env.example .env
# Edit .env — set GEMINI_API_KEY, DATABASE_URL, REDIS_URL
```

### Environment (`.env`)

| Variable | Purpose |
|----------|---------|
| `GEMINI_API_KEY` | Gemini access |
| `LLM_MODEL` | Model id (e.g. `gemini-3.6-flash`) |
| `DATABASE_URL` | Postgres SQLAlchemy URL |
| `REDIS_URL` | Redis (Celery broker default) |
| `UPLOAD_DIR` | Staged DICOM files (default `uploads`) |
| `PREPROCESS_IMAGE_DIR` | Preprocessed X-ray PNGs (default `preprocessed`) |
| `MAX_DICOM_FILES_PER_REQUEST` | Max files per upload (default `3`) |
| `MAX_DICOM_FILE_SIZE_MB` | Max size per DICOM (default `10`) |

API and Celery worker must share the **same disk** for `UPLOAD_DIR` / `PREPROCESS_IMAGE_DIR` until S3 is wired.

### Database migrations

```bash
alembic upgrade head
```

---

## Run (local / development)

**Terminal 1 — API**

```bash
bash run_backend.sh
# or: uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

**Terminal 2 — Celery worker**

```bash
# Windows PowerShell
.\run_worker.ps1

# macOS / Linux / Git Bash
bash run_worker.sh
```

- API: http://127.0.0.1:8000  
- Swagger: http://127.0.0.1:8000/docs  

Docker files (`*.dev` / `*.prod`) are **placeholders for DevOps** — not required for local venv runs.

---

## Pipeline (worker)

1. Load staged DICOM from `UPLOAD_DIR`
2. Preprocess → grayscale uint8 + PHI-stripped metadata  
3. Save PNG → `PREPROCESS_IMAGE_DIR/{finding_id}.png`
4. Gemini → structured `RadiologyReport` JSON
5. Persist to `radiology_findings` (Postgres)

`case_id` is taken from DICOM **AccessionNumber** when present (not the upload folder UUID).

---

## Database notes

Table: `radiology_findings`

Notable columns (some reserved for later wiring):

- `report` — AI structured findings JSON  
- `preprocess_image_url` — local PNG path (S3 later)  
- `dicom_file_url` — reserved for DICOM S3 link  
- `specialist_override_text` — reserved for full free-text specialist override  
- `edited_report` / `edited_at` — reserved for structured specialist edits  

---

## License / PHI

Treat DICOM uploads and preprocessed images as **sensitive**. Sample inputs under `unit_testing/input/` and runtime folders `uploads/` / `preprocessed/` are gitignored — do not commit PHI.
