"""Size limits on anything a stranger can send us.

Two layers, tested here: the request-wide ceiling that refuses an over-sized
body from its declared length before anything reads it, and read_capped, the
one helper every upload path reads through so a cap is enforced while the bytes
arrive rather than after they are all in memory.
"""
import asyncio

import pytest

from app import main
from app.config import settings
from app.main import max_request_bytes
from app.uploads import CHUNK_BYTES, UploadTooLarge, read_capped


# --- read_capped (pure) -----------------------------------------------------

class _FakeUpload:
    """Stands in for an UploadFile: hands out a body in chunks and records how
    much of it the reader actually pulled."""

    def __init__(self, total_bytes: int):
        self.remaining = total_bytes
        self.served = 0

    async def read(self, size: int = -1) -> bytes:
        take = self.remaining if size is None or size < 0 else min(size,
                                                                   self.remaining)
        self.remaining -= take
        self.served += take
        return b"x" * take


def test_read_capped_returns_a_file_under_the_cap():
    upload = _FakeUpload(200_000)
    assert asyncio.run(read_capped(upload, 1_000_000)) == b"x" * 200_000
    assert upload.remaining == 0


def test_read_capped_accepts_a_file_exactly_at_the_cap():
    upload = _FakeUpload(1_000)
    assert len(asyncio.run(read_capped(upload, 1_000))) == 1_000


def test_read_capped_stops_reading_once_the_cap_is_passed():
    # The point of the helper: a 50 MB body aimed at a 100 KB cap must cost one
    # chunk past the cap, not 50 MB of memory.
    upload = _FakeUpload(50 * 1024 * 1024)
    with pytest.raises(UploadTooLarge):
        asyncio.run(read_capped(upload, 100_000))
    assert upload.served <= 100_000 + CHUNK_BYTES


def test_read_capped_names_the_cap_it_enforced():
    upload = _FakeUpload(5_000)
    with pytest.raises(UploadTooLarge) as caught:
        asyncio.run(read_capped(upload, 1_000))
    assert caught.value.max_bytes == 1_000


def test_read_capped_handles_an_empty_upload():
    assert asyncio.run(read_capped(_FakeUpload(0), 1_000)) == b""


# --- The request-wide ceiling -----------------------------------------------

def test_the_ceiling_leaves_room_for_the_largest_real_upload(monkeypatch):
    # A kitchen's backup zip is the biggest thing anyone legitimately sends, so
    # the ceiling has to sit above that cap or Premium backup would break.
    monkeypatch.setattr(settings, "backup_max_bytes", 50_000_000)
    monkeypatch.setattr(settings, "recipe_upload_max_bytes", 8_000_000)
    monkeypatch.setattr(settings, "recipe_image_max_bytes", 5_000_000)
    assert max_request_bytes() > 50_000_000


def test_the_ceiling_rises_with_a_raised_cap(monkeypatch):
    monkeypatch.setattr(settings, "backup_max_bytes", 120_000_000)
    monkeypatch.setattr(settings, "recipe_upload_max_bytes", 8_000_000)
    monkeypatch.setattr(settings, "recipe_image_max_bytes", 5_000_000)
    assert max_request_bytes() > 120_000_000


def _shrink_ceiling(monkeypatch, ceiling: int) -> None:
    monkeypatch.setattr(main, "_BODY_HEADROOM_BYTES", 0)
    monkeypatch.setattr(settings, "backup_max_bytes", ceiling)
    monkeypatch.setattr(settings, "recipe_upload_max_bytes", ceiling)
    monkeypatch.setattr(settings, "recipe_image_max_bytes", ceiling)


def test_an_oversized_body_is_refused_before_the_route_runs(client, monkeypatch):
    # The webhook answers 400 for an unsigned body, so a 413 here proves the
    # request was turned away before the route (and before the body was read).
    _shrink_ceiling(monkeypatch, 1_000)
    resp = client.post("/v1/stripe/webhook", content=b"x" * 5_000)
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"]


def test_a_body_within_the_ceiling_reaches_the_route(client, monkeypatch):
    _shrink_ceiling(monkeypatch, 100_000)
    resp = client.post("/v1/stripe/webhook", content=b"x" * 5_000)
    assert resp.status_code == 400  # the route's own "unsigned" answer


def test_the_refusal_still_carries_the_security_headers(client, monkeypatch):
    # The header middleware has to stay outside the size guard, or a refused
    # request would come back without the app's browser hardening.
    _shrink_ceiling(monkeypatch, 1_000)
    resp = client.post("/v1/stripe/webhook", content=b"x" * 5_000)
    assert resp.status_code == 413
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]


def test_a_get_with_no_body_is_untouched(client, monkeypatch):
    _shrink_ceiling(monkeypatch, 1)
    assert client.get("/health").status_code == 200
