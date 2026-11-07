"""Passkey sign-in for the device's admin (FoodAssistant-kc2cy).

A small software authenticator (ES256, attestation "none") stands in for a
phone or security key, so the real py_webauthn checks run end to end through
/setup/passkeys and /ui/login with no browser and no hardware.
"""
import base64
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

import pytest

# The slim test workflow installs only the core packages; the passkey checks
# need py_webauthn (and cbor2, which it brings).
cbor2 = pytest.importorskip("cbor2")
pytest.importorskip("webauthn")
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
from app.services import passkeys as pk  # noqa: E402

_PW = "kitchen-secret"
_HOST = "kitchen.example.com"
_ORIGIN = f"https://{_HOST}"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftKey:
    """One passkey: an ES256 key pair, a credential id and a counter."""

    def __init__(self, uv=True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.uv = uv

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"),
                            -3: n.y.to_bytes(32, "big")})

    def _flags(self, attested: bool) -> int:
        return 0x01 | (0x04 if self.uv else 0) | (0x40 if attested else 0)

    def create(self, options: dict, origin: str = _ORIGIN) -> dict:
        rp_id = options["rp"]["id"]
        client = json.dumps({"type": "webauthn.create", "challenge": options["challenge"],
                             "origin": origin}).encode()
        auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(True)])
                + struct.pack(">I", self.count) + b"\x00" * 16
                + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": _b64u(client), "attestationObject": _b64u(att),
                             "transports": ["internal"]},
                "clientExtensionResults": {}}

    def get(self, options: dict, origin: str = _ORIGIN, rp_id: str | None = None,
            bump: bool = True) -> dict:
        rp_id = rp_id or options["rpId"]
        if bump:
            self.count += 1
        client = json.dumps({"type": "webauthn.get", "challenge": options["challenge"],
                             "origin": origin}).encode()
        auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(False)])
                + struct.pack(">I", self.count))
        sig = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": _b64u(client), "authenticatorData": _b64u(auth),
                             "signature": _b64u(sig)},
                "clientExtensionResults": {}}


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "gk", raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret(_PW), raising=False)
    monkeypatch.setattr(settings, "viewer_password", "", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", False, raising=False)
    monkeypatch.setattr(settings, "local_totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "local_totp_recovery", [], raising=False)
    monkeypatch.setattr(settings, "local_passkeys", [], raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", False, raising=False)
    monkeypatch.setattr(settings, "cloud_instance_token", "", raising=False)
    # The name this kitchen answers to from outside, so the page is "home".
    monkeypatch.setattr(settings, "home_host_names", [_HOST], raising=False)
    from app.main import app
    pk.reset()
    try:
        yield TestClient(app, base_url=_ORIGIN)
    finally:
        os.chdir(cwd)


def _sign_in_password(client):
    r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                    headers={"origin": _ORIGIN}, follow_redirects=False)
    assert r.status_code == 303, r.text


def _add(client, key, credential=_PW, name="Phone"):
    begin = client.post("/setup/passkeys/register/begin",
                        json={"name": name, "credential": credential},
                        headers={"origin": _ORIGIN}).json()
    if not begin.get("ok"):
        return begin
    return client.post("/setup/passkeys/register/finish",
                       json={"credential": key.create(begin["options"])},
                       headers={"origin": _ORIGIN}).json()


def _passkey_sign_in(client, key, **kw):
    begin = client.post("/ui/login", data={"mode": "passkey_begin"},
                        headers={"origin": _ORIGIN}).json()
    assert begin["ok"], begin
    return client.post("/ui/login",
                       data={"mode": "passkey_finish",
                             "credential": json.dumps(key.get(begin["options"], **kw))},
                       headers={"origin": _ORIGIN})


# --- Where passkeys can work -----------------------------------------------

@pytest.mark.parametrize("origin,expected", [
    ("https://kitchen.example.com", ("kitchen.example.com", "https://kitchen.example.com")),
    ("https://kitchen.example.com:8443", ("kitchen.example.com", "https://kitchen.example.com:8443")),
    ("http://localhost:9284", ("localhost", "http://localhost:9284")),
    ("http://pr.local:9284", None),
    ("https://192.168.1.20", None),
    ("https://[::1]", None),
    ("null", None),
    ("", None),
])
def test_site_for(origin, expected):
    assert pk.site_for(origin) == expected


# --- Adding and signing in ---------------------------------------------------

def test_add_then_sign_in_with_passkey(client):
    _sign_in_password(client)
    key = SoftKey()
    done = _add(client, key)
    assert done["ok"], done
    assert [p["name"] for p in done["passkeys"]] == ["Phone"]
    assert "public_key" not in done["passkeys"][0]

    client.cookies.clear()
    assert client.get("/setup", follow_redirects=False).status_code != 200
    page = client.get("/ui/login")
    assert "Sign in with a passkey" in page.text
    r = _passkey_sign_in(client, key)
    assert r.json()["ok"], r.text
    assert client.get("/setup", follow_redirects=False).status_code == 200
    stored = settings.local_passkeys[0]
    assert stored["sign_count"] == 1 and stored["last_used"]


def test_adding_needs_the_password(client):
    _sign_in_password(client)
    d = _add(client, SoftKey(), credential="wrong")
    assert not d["ok"] and "password" in d["error"].lower()
    assert settings.local_passkeys == []


def test_adding_needs_a_secure_named_address(client):
    _sign_in_password(client)
    d = client.post("/setup/passkeys/register/begin",
                    json={"name": "x", "credential": _PW},
                    headers={"origin": "https://192.168.1.20"}).json()
    assert not d["ok"] and "HTTPS" in d["error"]


def test_key_that_skips_user_verification_is_refused(client):
    _sign_in_password(client)
    d = _add(client, SoftKey(uv=False))
    assert not d["ok"] and "check who you are" in d["error"]
    assert settings.local_passkeys == []


def test_passkey_works_from_outside_without_device_2fa(client, monkeypatch):
    _sign_in_password(client)
    key = SoftKey()
    assert _add(client, key)["ok"]
    client.cookies.clear()
    # Now the same name counts as outside: a bare password is refused there,
    # but a passkey is two factors on its own.
    monkeypatch.setattr(settings, "home_host_names", [], raising=False)
    monkeypatch.setattr(settings, "tunnel_enabled", True, raising=False)
    monkeypatch.setattr(settings, "qr_public_url", _ORIGIN, raising=False)
    r = client.post("/ui/login", data={"mode": "local", "password": _PW},
                    headers={"origin": _ORIGIN}, follow_redirects=False)
    assert r.status_code == 401
    r = _passkey_sign_in(client, key)
    assert r.json()["ok"], r.text


def test_passkey_skips_the_device_code(client, monkeypatch):
    from app import totp as local_totp
    _sign_in_password(client)
    key = SoftKey()
    assert _add(client, key)["ok"]
    monkeypatch.setattr(settings, "local_totp_secret", local_totp.generate_totp_secret(),
                        raising=False)
    monkeypatch.setattr(settings, "local_totp_enabled", True, raising=False)
    client.cookies.clear()
    assert _passkey_sign_in(client, key).json()["ok"]
    assert client.get("/setup", follow_redirects=False).status_code == 200


def test_replayed_counter_is_refused(client):
    _sign_in_password(client)
    key = SoftKey()
    assert _add(client, key)["ok"]
    client.cookies.clear()
    assert _passkey_sign_in(client, key).json()["ok"]
    client.cookies.clear()
    # A cloned key reports the counter it already used.
    r = _passkey_sign_in(client, key, bump=False)
    assert r.status_code == 401 and not r.json()["ok"]


def test_challenge_is_single_use(client):
    _sign_in_password(client)
    key = SoftKey()
    assert _add(client, key)["ok"]
    client.cookies.clear()
    begin = client.post("/ui/login", data={"mode": "passkey_begin"},
                        headers={"origin": _ORIGIN}).json()
    answer = json.dumps(key.get(begin["options"]))
    assert client.post("/ui/login", data={"mode": "passkey_finish", "credential": answer},
                       headers={"origin": _ORIGIN}).json()["ok"]
    client.cookies.clear()
    r = client.post("/ui/login", data={"mode": "passkey_finish", "credential": answer},
                    headers={"origin": _ORIGIN})
    assert r.status_code == 401


def test_passkey_from_another_site_is_refused(client):
    _sign_in_password(client)
    key = SoftKey()
    assert _add(client, key)["ok"]
    client.cookies.clear()
    begin = client.post("/ui/login", data={"mode": "passkey_begin"},
                        headers={"origin": _ORIGIN}).json()
    # Signed for a look-alike site's name.
    bad = key.get(begin["options"], rp_id="kitchen.example.co", origin="https://kitchen.example.co")
    r = client.post("/ui/login", data={"mode": "passkey_finish", "credential": json.dumps(bad)},
                    headers={"origin": _ORIGIN})
    assert r.status_code == 401
    assert client.get("/setup", follow_redirects=False).status_code != 200


def test_no_passkey_here_means_no_button(client):
    page = client.get("/ui/login")
    assert "Sign in with a passkey" not in page.text
    d = client.post("/ui/login", data={"mode": "passkey_begin"},
                    headers={"origin": _ORIGIN}).json()
    assert d == {"ok": False, "available": False}


def test_remove_passkey(client):
    _sign_in_password(client)
    key = SoftKey()
    done = _add(client, key)
    pid = done["passkeys"][0]["id"]
    d = client.post("/setup/passkeys/remove", json={"id": pid},
                    headers={"origin": _ORIGIN}).json()
    assert d["ok"] and d["passkeys"] == []
    assert not client.post("/setup/passkeys/remove", json={"id": pid},
                           headers={"origin": _ORIGIN}).json()["ok"]

