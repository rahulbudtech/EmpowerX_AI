"""
Upload-time DICOM validation (API / enqueue).

Keeps extension + content checks out of the orchestration service so
rules stay testable and reusable. Content check matches pydicom
``is_dicom`` (Part 10 preamble + ``DICM`` at offset 128).
"""

from __future__ import annotations

ALLOWED_DICOM_EXTENSIONS: tuple[str, ...] = (".dcm", ".dicom")


def is_allowed_dicom_filename(name: str | None) -> bool:
    """True when filename ends with an allowed DICOM extension."""
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in ALLOWED_DICOM_EXTENSIONS)


def looks_like_dicom_bytes(data: bytes) -> bool:
    """True when bytes have a DICOM Part 10 ``DICM`` marker (same as ``is_dicom``)."""
    return len(data) >= 132 and data[128:132] == b"DICM"
