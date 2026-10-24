"""
Radiology ORM models (provisional schema — product schema TBD).

Table: radiology_findings — one row per analyze job (queued → completed/failed).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database import Base


class FindingsStatus:
    """Allowed values for radiology_findings.status (Celery lifecycle)."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class RadiologyFinding(Base):
    """Persisted DICOM findings job + report."""

    __tablename__ = "radiology_findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    case_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    modality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=FindingsStatus.QUEUED,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)

    # Null while queued/processing; filled on completed.
    # Canonical key order when set:
    # technical_metadata → findings → clinical_impression → recommendations
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    edited_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    preprocess_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    preprocess_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Schema only for now — S3 upload + specialist override APIs not wired yet.
    dicom_file_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    specialist_override_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
