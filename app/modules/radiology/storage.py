"""
Local file staging for Celery workers (DICOM uploads + preprocessed X-rays).

API / worker share the same disk via UPLOAD_DIR and PREPROCESS_IMAGE_DIR.
Replace with S3 later without changing the task contract (store path/URL string).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

from app.core.config import Settings, get_settings


def _safe_filename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^\w.\-]+", "_", base).strip("._")
    return cleaned or "upload.dcm"


def save_upload_bytes(
    data: bytes,
    *,
    finding_id: uuid.UUID,
    filename: str | None,
    settings: Settings | None = None,
) -> Path:
    """Persist upload under ``{UPLOAD_DIR}/{finding_id}/{filename}``."""
    cfg = settings or get_settings()
    dest_dir = Path(cfg.upload_dir) / str(finding_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_filename(filename or "upload.dcm")
    dest.write_bytes(data)
    return dest.resolve()


def save_preprocessed_image(
    image_u8: np.ndarray,
    *,
    finding_id: uuid.UUID,
    settings: Settings | None = None,
) -> Path:
    """
    Persist display X-ray PNG under ``{PREPROCESS_IMAGE_DIR}/{finding_id}.png``.

    Returns absolute path; store that string in ``preprocess_image_url`` until S3.
    """
    if image_u8.ndim != 2:
        raise ValueError(f"Expected 2D uint8 image, got shape {image_u8.shape}")

    cfg = settings or get_settings()
    dest_dir = Path(cfg.preprocess_image_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{finding_id}.png"
    Image.fromarray(image_u8, mode="L").save(dest, format="PNG", optimize=False)
    return dest.resolve()
