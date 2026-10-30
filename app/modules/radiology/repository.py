"""
Radiology persistence — DB read/write only (no LLM / preprocess).

Reusable from API service and Celery workers.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.radiology.models import (
    FindingsStatus,
    RadiologyFinding,
    RadiologyTokenUsage,
)
from app.modules.radiology.schemas import LlmTokenUsage, RadiologyReport


def _as_int(value: int | None) -> int:
    return int(value or 0)


def _as_money(value: Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(value)


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


def get_token_usage_by_finding_id(
    db: Session,
    finding_id: uuid.UUID,
) -> RadiologyTokenUsage | None:
    """Fetch token usage row for a finding (at most one)."""
    return db.execute(
        select(RadiologyTokenUsage).where(RadiologyTokenUsage.finding_id == finding_id)
    ).scalar_one_or_none()


def accumulate_token_usage(
    db: Session,
    *,
    finding_id: uuid.UUID,
    usage: LlmTokenUsage,
    estimated_cost_usd: Decimal,
    case_id: str | None = None,
    llm_calls_delta: int = 1,
) -> RadiologyTokenUsage:
    """
    Insert or accumulate token usage for a finding job.

    Retries add to counters so spent tokens are not lost when Celery retries.
    """
    row = get_token_usage_by_finding_id(db, finding_id)
    cost_delta = Decimal(estimated_cost_usd)

    if row is None:
        row = RadiologyTokenUsage(
            finding_id=finding_id,
            case_id=case_id,
            llm_model=usage.llm_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            total_tokens=usage.total_tokens,
            llm_calls=max(0, llm_calls_delta),
            estimated_cost_usd=cost_delta,
        )
        db.add(row)
    else:
        if case_id is not None:
            row.case_id = case_id
        if usage.llm_model:
            row.llm_model = usage.llm_model
        row.input_tokens = _as_int(row.input_tokens) + usage.input_tokens
        row.output_tokens = _as_int(row.output_tokens) + usage.output_tokens
        row.reasoning_tokens = _as_int(row.reasoning_tokens) + usage.reasoning_tokens
        row.cached_input_tokens = (
            _as_int(row.cached_input_tokens) + usage.cached_input_tokens
        )
        row.total_tokens = _as_int(row.total_tokens) + usage.total_tokens
        row.llm_calls = _as_int(row.llm_calls) + max(0, llm_calls_delta)
        row.estimated_cost_usd = _as_money(row.estimated_cost_usd) + cost_delta

    db.commit()
    db.refresh(row)
    return row
