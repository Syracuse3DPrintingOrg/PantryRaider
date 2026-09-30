"""Pure tests for password hashing, tokens, and Stripe signature checks."""
import hashlib
import hmac

from app.security import (
    hash_password,
    new_pairing_code,
    new_token,
    token_hash,
    verify_password,
    verify_stripe_signature,
)


def test_password_roundtrip():
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)


def test_password_hashes_are_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_password_tolerates_garbage():
    assert not verify_password("x", "")
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "md5$1$2$3$zz$zz")


def test_token_prefix_and_hash():
    token = new_token("prc")
    assert token.startswith("prc_")
    assert token_hash(token) == hashlib.sha256(token.encode()).hexdigest()


def test_pairing_code_shape():
    code = new_pairing_code()
    assert len(code) == 8
    assert "O" not in code and "0" not in code and "1" not in code


def _sign(payload: bytes, secret: str, ts: int) -> str:
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload,
                   hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_stripe_signature_valid():
    payload = b'{"id": "evt_1"}'
    header = _sign(payload, "whsec_test", ts=1000)
    assert verify_stripe_signature(payload, header, "whsec_test", now=1100)


def test_stripe_signature_wrong_secret():
    payload = b'{"id": "evt_1"}'
    header = _sign(payload, "whsec_other", ts=1000)
    assert not verify_stripe_signature(payload, header, "whsec_test", now=1100)


def test_stripe_signature_stale_timestamp():
    payload = b'{"id": "evt_1"}'
    header = _sign(payload, "whsec_test", ts=1000)
    assert not verify_stripe_signature(payload, header, "whsec_test", now=5000)


def test_stripe_signature_malformed_header():
    assert not verify_stripe_signature(b"{}", "", "whsec_test", now=0)
    assert not verify_stripe_signature(b"{}", "nonsense", "whsec_test", now=0)
