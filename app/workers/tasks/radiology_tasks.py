"""
Celery tasks for radiology (dedicated ``radiology`` queue).

Keep tasks thin: open a DB session, call module service, close session.
Retry policy: transient → up to max_retries; permanent → fail once.
"""

from __future__ import annotations

import logging
import uuid

from celery.exceptions import MaxRetriesExceededError

from app.infrastructure.database import SessionLocal
from app.modules.radiology.service import (
    FindingNotFoundError,
    PermanentPipelineError,
    TransientPipelineError,
    process_queued_dicom,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# 1 initial attempt + 2 retries = 3 tries total; 15s between attempts.
_MAX_RETRIES = 2
_RETRY_DELAY_S = 15


@celery_app.task(
    bind=True,
    name="radiology.analyze_dicom",
    queue="radiology",
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_DELAY_S,
    soft_time_limit=600,
    time_limit=720,
)
def analyze_dicom_task(
    self,
    finding_id: str,
    file_path: str,
    case_id: str | None = None,
) -> dict:
    """
    Background job: preprocess DICOM → LLM findings → update Postgres row.

    Args:
        finding_id: UUID string of radiology_findings.id
        file_path: Absolute path (or future S3 URI) to the staged DICOM
        case_id: Optional provisional case id
    """
    fid = uuid.UUID(finding_id)
    db = SessionLocal()
    try:
        process_queued_dicom(
            db,
            finding_id=fid,
            file_path=file_path,
            case_id=case_id,
        )
        return {"finding_id": finding_id, "status": "completed"}
    except FindingNotFoundError as exc:
        logger.error("Finding missing finding_id=%s: %s", finding_id, exc)
        return {"finding_id": finding_id, "status": "failed", "error": str(exc)}
    except PermanentPipelineError as exc:
        # Service already marked the row ``failed``.
        logger.error(
            "Permanent failure finding_id=%s (no retry): %s",
            finding_id,
            exc,
        )
        return {"finding_id": finding_id, "status": "failed", "error": str(exc)}
    except TransientPipelineError as exc:
        return _retry_or_fail(
            self,
            db,
            fid=fid,
            finding_id=finding_id,
            exc=exc,
            reason="transient",
        )
    except Exception as exc:
        logger.exception("Unexpected worker error finding_id=%s", finding_id)
        return _retry_or_fail(
            self,
            db,
            fid=fid,
            finding_id=finding_id,
            exc=exc,
            reason="unexpected",
        )
    finally:
        db.close()


def _retry_or_fail(
    task,
    db,
    *,
    fid: uuid.UUID,
    finding_id: str,
    exc: BaseException,
    reason: str,
) -> dict:
    """Celery retry while attempts remain; mark failed when exhausted."""
    attempt = int(getattr(task.request, "retries", 0) or 0) + 1
    max_attempts = _MAX_RETRIES + 1
    logger.warning(
        "%s failure finding_id=%s attempt=%s/%s: %s",
        reason.capitalize(),
        finding_id,
        attempt,
        max_attempts,
        exc,
    )
    try:
        raise task.retry(exc=exc)
    except MaxRetriesExceededError:
        from app.modules.radiology.repository import mark_finding_failed

        message = f"{exc} (exhausted {max_attempts} attempts)"
        try:
            mark_finding_failed(db, fid, error_message=message)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not mark finding failed after retries finding_id=%s",
                finding_id,
            )
        logger.error(
            "Retries exhausted finding_id=%s after %s attempts",
            finding_id,
            max_attempts,
        )
        return {
            "finding_id": finding_id,
            "status": "failed",
            "error": message,
        }
