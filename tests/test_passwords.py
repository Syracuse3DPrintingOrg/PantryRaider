"""Hashing the web-UI password and kiosk PIN at rest (FoodAssistant-ufwz)."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.passwords import hash_secret, verify_secret, looks_hashed  # noqa: E402


def test_hash_is_salted_and_self_describing():
    h = hash_secret("hunter2")
    assert looks_hashed(h) and h.startswith("scrypt$")
    # Salt makes two hashes of the same secret differ.
    assert hash_secret("hunter2") != h
    assert not looks_hashed("hunter2")


def test_verify_round_trip():
    h = hash_secret("Correct Horse Battery Staple")
    assert verify_secret("Correct Horse Battery Staple", h) is True
    assert verify_secret("wrong", h) is False


def test_empty_inputs_never_verify():
    assert hash_secret("") == ""
    assert verify_secret("", "anything") is False
    assert verify_secret("x", "") is False
    assert verify_secret("", "") is False


def test_legacy_plaintext_still_verifies():
    # An install that predates hashing has a plaintext value on disk; it must
    # keep working until the next save upgrades it.
    assert verify_secret("1234", "1234") is True
    assert verify_secret("1234", "9999") is False


def test_corrupt_hash_does_not_crash():
    assert verify_secret("x", "scrypt$bogus") is False


def test_save_hashes_password_and_pin_on_disk(tmp_path, monkeypatch):
    import json
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    # Captured so the mutation save() applies to the global object is restored,
    # keeping other tests' auth_password/kiosk_pin defaults intact.
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "kiosk_pin", "", raising=False)
    settings.save({"auth_password": "plaintext-pw", "kiosk_pin": "4242"})
    on_disk = json.loads((tmp_path / "settings.json").read_text())
    # The raw secret is never written; a scrypt hash is.
    assert on_disk["auth_password"].startswith("scrypt$")
    assert "plaintext-pw" not in on_disk["auth_password"]
    assert on_disk["kiosk_pin"].startswith("scrypt$")
    # The live object verifies against the supplied plaintext.
    assert verify_secret("plaintext-pw", settings.auth_password) is True
    assert verify_secret("4242", settings.kiosk_pin) is True
    # A re-save with the already-hashed value does not double-hash.
    h = settings.auth_password
    settings.save({"auth_password": h})
    assert settings.auth_password == h
