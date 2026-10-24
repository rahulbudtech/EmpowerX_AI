"""
Radiology persistence — DB read/write only (no LLM / preprocess).

Reusable from API service and Celery workers.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.modules.radiology.models import FindingsStatus, RadiologyFinding
from app.modules.radiology.schemas import RadiologyReport


def create_queued_finding(
    db: Session,
    *,
    finding_id: uuid.UUID,
    provider: str,
    case_id: str | None = None,
    source_file: str | None = None,
) -> RadiologyFinding:
    """Insert a job row in ``queued`` state (report null until worker finishes)."""
    row = RadiologyFinding(
        id=finding_id,
        case_id=case_id,
        source_file=source_file,
        modality=None,
        status=FindingsStatus.QUEUED,
        provider=provider,
        report=None,
        error_message=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def mark_finding_processing(db: Session, finding_id: uuid.UUID) -> RadiologyFinding | None:
    row = get_finding_by_id(db, finding_id)
    if row is None:
        return None
    row.status = FindingsStatus.PROCESSING
    row.error_message = None
    db.commit()
    db.refresh(row)
    return row


def mark_finding_completed(
    db: Session,
    finding_id: uuid.UUID,
    *,
    report: RadiologyReport,
    modality: str | None = None,
    case_id: str | None = None,
    source_file: str | None = None,
    preprocess_metadata: dict | None = None,
    preprocess_image_url: str | None = None,
) -> RadiologyFinding | None:
    row = get_finding_by_id(db, finding_id)
    if row is None:
        return None
    row.status = FindingsStatus.COMPLETED
    row.report = report.to_storage_dict()
    row.error_message = None
    if modality is not None:
        row.modality = modality
    if case_id is not None:
        row.case_id = case_id
    if source_file is not None:
        row.source_file = source_file
    if preprocess_metadata is not None:
        row.preprocess_metadata = preprocess_metadata
    if preprocess_image_url is not None:
        row.preprocess_image_url = preprocess_image_url
    db.commit()
    db.refresh(row)
    return row


def mark_finding_failed(
    db: Session,
    finding_id: uuid.UUID,
    *,
    error_message: str,
) -> RadiologyFinding | None:
    row = get_finding_by_id(db, finding_id)
    if row is None:
        return None
    row.status = FindingsStatus.FAILED
    row.error_message = error_message[:4000]
    db.commit()
    db.refresh(row)
    return row


def save_completed_finding(
    db: Session,
    *,
    report: RadiologyReport,
    provider: str,
    case_id: str | None = None,
    source_file: str | None = None,
    modality: str | None = None,
) -> RadiologyFinding:
    """Insert an already-completed findings row (sync / scripts)."""
    row = RadiologyFinding(
        case_id=case_id,
        source_file=source_file,
        modality=modality,
        status=FindingsStatus.COMPLETED,
        provider=provider,
        report=report.to_storage_dict(),
        error_message=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_finding_by_id(db: Session, finding_id: uuid.UUID) -> RadiologyFinding | None:
    """Fetch one findings row by primary key."""
    return db.get(RadiologyFinding, finding_id)
