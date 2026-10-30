"""
Radiology orchestration: enqueue (API) + process (Celery worker).

API stays thin: validate → stage file → DB queued row → Celery delay.
Worker calls ``process_queued_dicom`` for preprocess → LLM → DB update.
Token usage is recorded best-effort and never fails the analyze job.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.modules.radiology.llm import (
    FindingsLLMError,
    FindingsLLMTransientError,
    generate_findings_report,
)
from app.modules.radiology.preprocess import (
    DicomPreprocessError,
    PreprocessResult,
    preprocess_dicom_bytes,
    preprocess_dicom_path,
)
from app.modules.radiology.repository import (
    accumulate_token_usage,
    create_queued_finding,
    get_finding_by_id,
    mark_finding_completed,
    mark_finding_failed,
    mark_finding_processing,
)
from app.modules.radiology.schemas import (
    DicomAnalyzeBatchQueuedResponse,
    DicomAnalyzeQueuedResponse,
    FindingsDetailResponse,
    LlmTokenUsage,
    RadiologyReport,
    SafeDicomMetadata,
)
from app.modules.radiology.storage import save_preprocessed_image, save_upload_bytes
from app.modules.radiology.token_cost import estimate_cost_usd
from app.modules.radiology.upload_validation import (
    ALLOWED_DICOM_EXTENSIONS,
    is_allowed_dicom_filename,
    looks_like_dicom_bytes,
)

logger = logging.getLogger(__name__)


class RadiologyServiceError(Exception):
    """Domain error for the radiology analyze pipeline."""


class InvalidUploadError(RadiologyServiceError):
    """Client upload is missing or empty / over limits (maps to HTTP 400)."""


class FindingNotFoundError(RadiologyServiceError):
    """No findings row for the given id (maps to HTTP 404)."""


class PermanentPipelineError(RadiologyServiceError):
    """Bad data / config — do not Celery-retry; mark finding failed."""

    def __init__(
        self,
        message: str,
        *,
        token_usage: LlmTokenUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.token_usage = token_usage


class TransientPipelineError(RadiologyServiceError):
    """Flaky infra / LLM — Celery may retry; keep finding as processing."""

    def __init__(
        self,
        message: str,
        *,
        token_usage: LlmTokenUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.token_usage = token_usage


@dataclass(slots=True)
class AnalyzeResult:
    """In-memory analyze output (scripts / unit tests)."""

    report: RadiologyReport
    metadata: SafeDicomMetadata
    preprocess: PreprocessResult
    provider: str
    usage: LlmTokenUsage | None = None


@dataclass(slots=True)
class UploadFilePayload:
    """One uploaded DICOM already read into memory (route → service)."""

    data: bytes
    filename: str | None


def _validate_one_dicom_upload(item: UploadFilePayload, *, max_bytes: int) -> None:
    """Reject empty, oversized, wrong extension, or non-DICOM content."""
    name = item.filename or "upload.dcm"

    if not item.data:
        raise InvalidUploadError(f"Empty file upload: {name}")
    if len(item.data) > max_bytes:
        raise InvalidUploadError(
            f"File too large: {name} "
            f"({len(item.data)} bytes). Maximum is {max_bytes // (1024 * 1024)} MB per DICOM."
        )
    if not is_allowed_dicom_filename(name):
        allowed = ", ".join(ALLOWED_DICOM_EXTENSIONS)
        raise InvalidUploadError(
            f"Unsupported file type: {name}. Only DICOM uploads are allowed ({allowed})."
        )
    if not looks_like_dicom_bytes(item.data):
        raise InvalidUploadError(
            f"File is not a valid DICOM: {name}. "
            "Expected a DICOM Part 10 file (.dcm / .dicom)."
        )


def _validate_upload_batch(
    files: list[UploadFilePayload],
    *,
    settings: Settings,
) -> None:
    max_files = settings.max_dicom_files_per_request
    max_bytes = settings.max_dicom_file_size_mb * 1024 * 1024

    if not files:
        raise InvalidUploadError("At least one DICOM file is required.")
    if len(files) > max_files:
        raise InvalidUploadError(
            f"Too many files: got {len(files)}, maximum is {max_files}."
        )

    for item in files:
        _validate_one_dicom_upload(item, max_bytes=max_bytes)


def _persist_token_usage_best_effort(
    db: Session,
    *,
    finding_id: uuid.UUID,
    usage: LlmTokenUsage | None,
    case_id: str | None,
    settings: Settings,
) -> None:
    """
    Upsert token usage; log and swallow failures so analyze is never blocked.
    """
    if usage is None:
        return
    try:
        cost = estimate_cost_usd(usage, settings.gemini_token_cost_rates())
        accumulate_token_usage(
            db,
            finding_id=finding_id,
            usage=usage,
            estimated_cost_usd=cost,
            case_id=case_id,
        )
    except Exception:  # noqa: BLE001 — usage must not fail the job
        logger.exception(
            "Failed to persist radiology token usage finding_id=%s case_id=%s",
            finding_id,
            case_id,
        )


def enqueue_dicom_analyze(
    data: bytes,
    *,
    db: Session,
    filename: str | None = None,
    case_id: str | None = None,
    settings: Settings | None = None,
) -> DicomAnalyzeQueuedResponse:
    """
    Stage one DICOM, create ``queued`` row, push Celery task.

    Prefer ``enqueue_dicom_batch`` from the API (applies count/size limits).
    """
    cfg = settings or get_settings()
    max_bytes = cfg.max_dicom_file_size_mb * 1024 * 1024
    _validate_one_dicom_upload(
        UploadFilePayload(data=data, filename=filename),
        max_bytes=max_bytes,
    )

    source_name = filename or "upload.dcm"
    finding_id = uuid.uuid4()
    file_path = save_upload_bytes(data, finding_id=finding_id, filename=source_name)

    row = create_queued_finding(
        db,
        finding_id=finding_id,
        provider=cfg.llm_provider,
        case_id=case_id,
        source_file=source_name,
    )

    # Late import avoids circular import at module load (celery → tasks → service).
    from app.workers.tasks.radiology_tasks import analyze_dicom_task

    analyze_dicom_task.delay(str(row.id), str(file_path), None)
    logger.info("Enqueued radiology.analyze_dicom finding_id=%s path=%s", row.id, file_path)

    return DicomAnalyzeQueuedResponse(
        id=row.id,
        case_id=row.case_id,
        source_file=row.source_file,
        provider=row.provider,
        status=row.status,
    )


def enqueue_dicom_batch(
    files: list[UploadFilePayload],
    *,
    db: Session,
    case_id: str | None = None,
    settings: Settings | None = None,
) -> DicomAnalyzeBatchQueuedResponse:
    """
    API entry: validate 1-3 DICOMs (max 10 MB each) -> enqueue one Celery job per file.
    """
    cfg = settings or get_settings()
    _validate_upload_batch(files, settings=cfg)

    jobs = [
        enqueue_dicom_analyze(
            item.data,
            db=db,
            filename=item.filename,
            case_id=case_id,
            settings=cfg,
        )
        for item in files
    ]
    return DicomAnalyzeBatchQueuedResponse(jobs=jobs)


def process_queued_dicom(
    db: Session,
    *,
    finding_id: uuid.UUID,
    file_path: str | Path,
    case_id: str | None = None,
    settings: Settings | None = None,
) -> None:
    """
    Worker entry: preprocess → LLM → mark completed.

    Permanent errors → mark ``failed`` and raise ``PermanentPipelineError``.
    Transient errors → leave status ``processing`` and raise
    ``TransientPipelineError`` so Celery can retry (failed only after max retries).

    When the LLM ran, token usage is accumulated even if the job fails or retries.
    """
    cfg = settings or get_settings()
    path = Path(file_path)

    row = mark_finding_processing(db, finding_id)
    if row is None:
        raise FindingNotFoundError(f"Findings not found: {finding_id}")

    resolved_case_id = case_id or row.case_id

    try:
        # case_id comes from DICOM AccessionNumber via preprocess (not upload folder).
        try:
            pre = preprocess_dicom_path(
                path,
                use_dicom_window=True,
                case_id=None,
            )
        except FileNotFoundError as exc:
            raise PermanentPipelineError(str(exc)) from exc
        except DicomPreprocessError as exc:
            raise PermanentPipelineError(str(exc)) from exc

        resolved_case_id = pre.metadata.case_id or resolved_case_id

        # Local PNG until S3 — same disk as worker/API via PREPROCESS_IMAGE_DIR.
        image_path = save_preprocessed_image(
            pre.image_u8,
            finding_id=finding_id,
            settings=cfg,
        )

        result = _run_findings(pre, settings=cfg)
        mark_finding_completed(
            db,
            finding_id,
            report=result.report,
            modality=result.metadata.modality,
            case_id=result.metadata.case_id,
            source_file=result.metadata.source_file or row.source_file,
            preprocess_metadata=result.metadata.model_dump(mode="json"),
            preprocess_image_url=str(image_path),
        )
        _persist_token_usage_best_effort(
            db,
            finding_id=finding_id,
            usage=result.usage,
            case_id=result.metadata.case_id or resolved_case_id,
            settings=cfg,
        )
        logger.info(
            "Completed radiology analyze finding_id=%s image=%s",
            finding_id,
            image_path,
        )
    except TransientPipelineError as exc:
        logger.warning(
            "Transient radiology failure finding_id=%s (Celery may retry)",
            finding_id,
            exc_info=True,
        )
        _persist_token_usage_best_effort(
            db,
            finding_id=finding_id,
            usage=exc.token_usage,
            case_id=resolved_case_id,
            settings=cfg,
        )
        raise
    except PermanentPipelineError as exc:
        logger.exception("Permanent radiology failure finding_id=%s", finding_id)
        mark_finding_failed(db, finding_id, error_message=str(exc))
        _persist_token_usage_best_effort(
            db,
            finding_id=finding_id,
            usage=exc.token_usage,
            case_id=resolved_case_id,
            settings=cfg,
        )
        raise
    except FindingNotFoundError:
        raise
    except Exception as exc:
        # Unexpected worker/runtime blip — treat as transient; task marks failed if exhausted.
        logger.exception("Unexpected radiology error finding_id=%s", finding_id)
        raise TransientPipelineError(str(exc)) from exc


def get_findings(db: Session, finding_id: uuid.UUID) -> FindingsDetailResponse:
    """Load a findings job row for the API (report may be null while in progress)."""
    row = get_finding_by_id(db, finding_id)
    if row is None:
        raise FindingNotFoundError(f"Findings not found: {finding_id}")

    report = None
    if row.report is not None:
        report = RadiologyReport.model_validate(row.report)

    return FindingsDetailResponse(
        id=row.id,
        case_id=row.case_id,
        source_file=row.source_file,
        modality=row.modality,
        status=row.status,
        provider=row.provider,
        report=report,
        preprocess_image_url=row.preprocess_image_url,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def analyze_dicom_path(
    path: str | Path,
    *,
    use_dicom_window: bool = True,
    case_id: str | None = None,
    settings: Settings | None = None,
) -> AnalyzeResult:
    """Full pipeline from a DICOM path on disk (no DB write)."""
    cfg = settings or get_settings()
    try:
        pre = preprocess_dicom_path(
            path,
            use_dicom_window=use_dicom_window,
            case_id=case_id,
        )
    except FileNotFoundError as exc:
        raise PermanentPipelineError(str(exc)) from exc
    except DicomPreprocessError as exc:
        raise PermanentPipelineError(str(exc)) from exc

    return _run_findings(pre, settings=cfg)


def analyze_dicom_bytes(
    data: bytes,
    *,
    source_name: str | None = None,
    use_dicom_window: bool = True,
    case_id: str | None = None,
    settings: Settings | None = None,
) -> AnalyzeResult:
    """Full pipeline from upload bytes (no DB write)."""
    cfg = settings or get_settings()
    try:
        pre = preprocess_dicom_bytes(
            data,
            source_name=source_name,
            use_dicom_window=use_dicom_window,
            case_id=case_id,
        )
    except DicomPreprocessError as exc:
        raise PermanentPipelineError(str(exc)) from exc

    return _run_findings(pre, settings=cfg)


def _run_findings(pre: PreprocessResult, *, settings: Settings) -> AnalyzeResult:
    try:
        llm_result = generate_findings_report(
            pre.image_u8,
            pre.metadata,
            settings=settings,
        )
    except FindingsLLMTransientError as exc:
        raise TransientPipelineError(str(exc), token_usage=exc.usage) from exc
    except FindingsLLMError as exc:
        raise PermanentPipelineError(str(exc), token_usage=exc.usage) from exc

    return AnalyzeResult(
        report=llm_result.report,
        metadata=pre.metadata,
        preprocess=pre,
        provider=settings.llm_provider,
        usage=llm_result.usage,
    )
