"""Radiology HTTP routes — thin layer only. Business logic lives in modules/radiology/."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import require_analyze_caller, require_service_api_key_optional_user
from app.core.config import get_settings
from app.infrastructure.database import get_db
from app.modules.auth.schemas import AccessTokenClaims
from app.modules.radiology.schemas import (
    DicomAnalyzeBatchQueuedResponse,
    FindingsDetailResponse,
    LlmSettingsResponse,
    LlmSettingsUpdateRequest,
)
from app.modules.radiology.service import (
    FindingNotFoundError,
    InvalidLlmSettingsError,
    InvalidUploadError,
    RadiologyServiceError,
    UploadFilePayload,
    enqueue_dicom_batch,
    get_findings,
    get_llm_settings,
    update_llm_settings,
)
from app.modules.radiology.upload_validation import (
    UploadTooLargeError,
    read_upload_with_size_cap,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/radiology", tags=["radiology"])


@router.post(
    "/dicom/analyze",
    response_model=DicomAnalyzeBatchQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue 1-3 DICOM analyze jobs (max 10 MB each); poll findings by id",
)
async def analyze_dicom(
    files: list[UploadFile] = File(
        ...,
        description="1 to 3 DICOM files (.dcm / .dicom), max 10 MB each",
    ),
    case_id: str = Form(
        ...,
        description="Case id from the frontend (applied to all files in the batch)",
    ),
    db: Session = Depends(get_db),
    caller: AccessTokenClaims = Depends(require_analyze_caller),
) -> DicomAnalyzeBatchQueuedResponse:
    """Accept uploads → queue one job per file → return job ids immediately."""
    logger.info(
        "dicom analyze accepted user_id=%s facility_id=%s role=%s type=%s",
        caller.user_id,
        caller.facility_id,
        caller.role,
        caller.type,
    )
    cfg = get_settings()
    max_files = cfg.max_dicom_files_per_request
    max_bytes = cfg.max_dicom_file_size_mb * 1024 * 1024

    if len(files) > max_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many files: got {len(files)}, maximum is {max_files}.",
        )

    payloads: list[UploadFilePayload] = []
    try:
        for upload in files:
            data = await read_upload_with_size_cap(upload, max_bytes=max_bytes)
            payloads.append(UploadFilePayload(data=data, filename=upload.filename))
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    finally:
        for upload in files:
            await upload.close()

    try:
        return enqueue_dicom_batch(payloads, db=db, case_id=case_id)
    except InvalidUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except RadiologyServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get(
    "/findings/{finding_id}",
    response_model=FindingsDetailResponse,
    summary="Get findings job status / report by id",
)
def read_findings(
    finding_id: uuid.UUID,
    db: Session = Depends(get_db),
    _caller: AccessTokenClaims | None = Depends(require_service_api_key_optional_user),
) -> FindingsDetailResponse:
    """Poll until status is completed or failed. Node worker: X-API-Key only."""
    try:
        return get_findings(db, finding_id)
    except FindingNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.get(
    "/llm-settings",
    response_model=LlmSettingsResponse,
    summary="Get primary/fallback LLM selection and allowed options",
)
def read_llm_settings(
    db: Session = Depends(get_db),
    _caller: AccessTokenClaims = Depends(require_analyze_caller),
) -> LlmSettingsResponse:
    """DB selection when saved; otherwise env defaults. Includes dropdown options."""
    return get_llm_settings(db)


@router.put(
    "/llm-settings",
    response_model=LlmSettingsResponse,
    summary="Save primary/fallback LLM selection",
)
def write_llm_settings(
    body: LlmSettingsUpdateRequest,
    db: Session = Depends(get_db),
    _caller: AccessTokenClaims = Depends(require_analyze_caller),
) -> LlmSettingsResponse:
    """Persist singleton settings; subsequent analyze jobs use this until changed."""
    try:
        return update_llm_settings(db, body)
    except InvalidLlmSettingsError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
