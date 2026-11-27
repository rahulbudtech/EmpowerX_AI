"""
Upload-time DICOM validation (API / enqueue).

Keeps extension + content checks out of the orchestration service so
rules stay testable and reusable. Content check matches pydicom
``is_dicom`` (Part 10 preamble + ``DICM`` at offset 128).
"""

from __future__ import annotations

from typing import Protocol

ALLOWED_DICOM_EXTENSIONS: tuple[str, ...] = (".dcm", ".dicom")
_UPLOAD_READ_CHUNK = 64 * 1024


class UploadTooLargeError(Exception):
    """Upload exceeded the configured byte cap (maps to HTTP 413)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class AsyncBinaryUpload(Protocol):
    """Minimal async upload surface (FastAPI ``UploadFile`` satisfies this)."""

    filename: str | None

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...


def is_allowed_dicom_filename(name: str | None) -> bool:
    """True when filename ends with an allowed DICOM extension."""
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in ALLOWED_DICOM_EXTENSIONS)


def looks_like_dicom_bytes(data: bytes) -> bool:
    """True when bytes have a DICOM Part 10 ``DICM`` marker (same as ``is_dicom``)."""
    return len(data) >= 132 and data[128:132] == b"DICM"


async def read_upload_with_size_cap(
    upload: AsyncBinaryUpload,
    *,
    max_bytes: int,
) -> bytes:
    """
    Read an upload in chunks; stop once size exceeds ``max_bytes``.

    Never buffers an unbounded body. Oversize raises ``UploadTooLargeError``.
    """
    name = upload.filename or "upload.dcm"
    buf = bytearray()
    while True:
        # Read at most enough to cross the cap by one chunk boundary.
        remaining_budget = max_bytes + 1 - len(buf)
        if remaining_budget <= 0:
            break
        chunk = await upload.read(min(_UPLOAD_READ_CHUNK, remaining_budget))
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            await upload.close()
            raise UploadTooLargeError(
                f"File too large: {name}. "
                f"Maximum is {max_bytes // (1024 * 1024)} MB per DICOM."
            )
    return bytes(buf)
