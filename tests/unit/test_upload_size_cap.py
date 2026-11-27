"""Task 05 — upload bytes capped before full unbounded buffering."""

from __future__ import annotations

import asyncio

import pytest

from app.modules.radiology.upload_validation import (
    UploadTooLargeError,
    read_upload_with_size_cap,
)


class _FakeUpload:
    """Minimal UploadFile stand-in with chunked async read."""

    def __init__(self, data: bytes, filename: str = "sample.dcm") -> None:
        self.filename = filename
        self._data = data
        self._offset = 0
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        if size < 0:
            chunk = self._data[self._offset :]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    async def close(self) -> None:
        self.closed = True


def test_read_within_limit_returns_all_bytes() -> None:
    payload = b"DICM" + (b"x" * 100)
    upload = _FakeUpload(payload)
    data = asyncio.run(read_upload_with_size_cap(upload, max_bytes=10 * 1024 * 1024))
    assert data == payload
    assert upload.closed is False


def test_read_one_byte_over_limit_raises_domain_error() -> None:
    max_bytes = 1024
    upload = _FakeUpload(b"y" * (max_bytes + 1), filename="big.dcm")

    async def _run() -> None:
        await read_upload_with_size_cap(upload, max_bytes=max_bytes)

    with pytest.raises(UploadTooLargeError) as exc_info:
        asyncio.run(_run())
    assert "File too large" in str(exc_info.value)
    assert upload.closed is True


def test_read_does_not_buffer_far_past_limit() -> None:
    """Cap detection must stop near max_bytes+1, not after a huge body."""
    max_bytes = 2048
    upload = _FakeUpload(b"z" * (50 * 1024 * 1024), filename="huge.dcm")

    async def _run() -> None:
        await read_upload_with_size_cap(upload, max_bytes=max_bytes)

    with pytest.raises(UploadTooLargeError):
        asyncio.run(_run())
    # Offset should only have advanced slightly past the cap (one read window).
    assert upload._offset <= max_bytes + 64 * 1024
