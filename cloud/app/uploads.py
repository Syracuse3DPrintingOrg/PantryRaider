"""Reading an uploaded file without its size becoming the attack.

Every upload path in Forager has a size cap, but a cap only protects the
service if it is enforced *before* the bytes are in memory. ``await
file.read()`` followed by ``len(data) > cap`` has already buffered whatever the
client chose to send, so the check fires after the damage is done.

``read_capped`` is the one way this app reads an upload: fixed-size chunks with
a running total, stopping the moment the cap is passed. The caller turns
:class:`UploadTooLarge` into whatever its own surface says (a 413 for the API,
a friendly message on the portal form), so the wording stays local to each
path while the reading rule stays in one place.
"""
from __future__ import annotations

from fastapi import UploadFile

# Read granularity. Small enough that a rejected upload never costs more than
# one chunk past its cap, large enough to keep the loop cheap on a real file.
CHUNK_BYTES = 64 * 1024


class UploadTooLarge(Exception):
    """The upload passed its cap. Carries the cap so a caller can name it."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"upload exceeds {max_bytes} bytes")
        self.max_bytes = max_bytes


async def read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """The upload's bytes, or :class:`UploadTooLarge` as soon as it passes
    ``max_bytes``. Never holds more than the cap plus one chunk in memory."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLarge(max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)
