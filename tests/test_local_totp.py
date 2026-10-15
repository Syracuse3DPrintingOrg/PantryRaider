"""The app's own local two-factor helpers (FoodAssistant-x1ty).

Pure stdlib TOTP plus the recovery-code hashing and single-use burn. The TOTP
side is pinned to the RFC 6238 published vectors so a regression in the HMAC
math is caught without a clock or a network.
"""
import base64
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from app import totp as t

# The RFC 6238 test secret: ASCII "12345678901234567890" in base32.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_totp_matches_rfc6238_vectors():
    assert t.totp_now(RFC_SECRET, for_time=59) == "287082"
    assert t.totp_now(RFC_SECRET, for_time=1111111109) == "081804"


def test_totp_verify_accepts_current_and_skew_window():
    secret = t.generate_totp_secret()
    assert t.totp_verify(secret, t.totp_now(secret, for_time=10_000), now=10_000)
    prev = t.totp_now(secret, for_time=10_000 - 30)
    nxt = t.totp_now(secret, for_time=10_000 + 30)
    assert t.totp_verify(secret, prev, now=10_000, window=1)
    assert t.totp_verify(secret, nxt, now=10_000, window=1)
    far = t.totp_now(secret, for_time=10_000 - 60)
    assert not t.totp_verify(secret, far, now=10_000, window=1)


def test_totp_verify_rejects_wrong_malformed_and_empty_secret():
    secret = t.generate_totp_secret()
    current = t.totp_now(secret, for_time=10_000)
    wrong = f"{(int(current) + 1) % 1_000_000:06d}"
    assert not t.totp_verify(secret, wrong, now=10_000)
    assert not t.totp_verify(secret, "", now=10_000)
    assert not t.totp_verify(secret, "12345", now=10_000)
    assert not t.totp_verify(secret, "abcdef", now=10_000)
    # No secret means 2FA is off: verification can never pass.
    assert not t.totp_verify("", current, now=10_000)


def test_otpauth_uri_carries_issuer_and_label():
    uri = t.otpauth_uri("ABC234", "kitchen-pi", issuer="Pantry Raider")
    assert uri.startswith("otpauth://totp/Pantry%20Raider:kitchen-pi?")
    assert "secret=ABC234" in uri
    assert "issuer=Pantry+Raider" in uri
    assert "period=30" in uri and "digits=6" in uri


def test_recovery_codes_readable_and_normalise():
    codes = t.generate_recovery_codes()
    assert len(codes) == 10
    for c in codes:
        assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", c)
        assert not (set("O01I") & set(c))
    assert t.normalize_recovery_code("abcd-2345") == "ABCD2345"
    assert t.normalize_recovery_code(" ab cd 23 45 ") == "ABCD2345"


def test_recovery_codes_hash_verify_and_single_use():
    codes = ["ABCD-2345", "WXYZ-6789"]
    hashed = t.hash_recovery_codes(codes)
    assert len(hashed) == 2
    # The stored form is a salted hash, never the plaintext.
    assert all(h.startswith("scrypt$") for h in hashed)
    # A correct code matches with or without the dash and burns exactly one hash.
    matched, remaining = t.consume_recovery_code("abcd2345", hashed)
    assert matched and len(remaining) == 1
    # The same code cannot be used again against the reduced list.
    again, remaining2 = t.consume_recovery_code("ABCD-2345", remaining)
    assert not again and len(remaining2) == 1
    # A wrong code leaves the list untouched.
    miss, same = t.consume_recovery_code("0000-0000", remaining)
    assert not miss and len(same) == 1
