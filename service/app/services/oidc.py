"""Sign in with your own identity provider (OpenID Connect).

Optional and off until the admin fills in an issuer, a client id and a
client secret under Settings, Security, then links their own account. The
kitchen is a confidential client using the authorization code flow with
PKCE (S256). One account can sign in this way: the one whose subject (`sub`)
was saved when the admin linked it. Email is never used to decide who
someone is.

ID tokens are checked in full (RS256 signature against the provider's
published keys, issuer, audience, authorized party, expiry, issue time and
nonce) even though they arrive straight from the token endpoint over TLS.

A sign-in in progress waits here, in memory, under a random state value the
browser also holds in a short cookie. The app runs one worker
(FoodAssistant-n6mta), so memory is enough; a restart only means starting
the sign-in again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx

CALLBACK_PATH = "/auth/oidc/callback"
START_PATH = "/auth/oidc/start"
STATE_COOKIE = "pr_oidc"
SCOPES = "openid profile email"
FLOW_SECONDS = 600
CACHE_SECONDS = 3600
LEEWAY_SECONDS = 60
_MAX_PENDING = 64

NOT_READY_MSG = "Sign-in with your identity provider is not set up on this kitchen."
NO_ANSWER_MSG = "Your identity provider did not answer. Try again in a moment."
NOT_EXPECTED_MSG = "That address does not look like an identity provider. Check the issuer address."
TURNED_DOWN_MSG = "Your identity provider turned that sign-in down. Try again."
CHECK_FAILED_MSG = "That sign-in could not be checked. Try again."
EXPIRED_MSG = "That sign-in took too long or was already used. Try again."
WRONG_ACCOUNT_MSG = "That account is not the one linked to this kitchen."

# Tests swap in an httpx.MockTransport here.
transport: httpx.BaseTransport | None = None


class OidcError(Exception):
    """The sign-in could not be completed. The message is safe to show."""


# Settings -----------------------------------------------------------------


def configured(s) -> bool:
    return bool(s.oidc_issuer and s.oidc_client_id and s.oidc_client_secret)


def linked(s) -> bool:
    return configured(s) and bool(s.oidc_subject)


def only(s) -> bool:
    """Whether the identity provider is the one way in for people. The
    recovery sign-in (the device password, from home) still works."""
    return linked(s) and bool(s.oidc_only)


def display_name(s) -> str:
    name = (s.oidc_name or "").strip()
    return name or "your identity provider"


def normalize_issuer(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
        raise OidcError("The issuer must be an https:// address, such as https://id.example.com.")
    return value


def host_matches(s, host: str) -> bool:
    return bool(s.oidc_host) and (host or "").lower() == s.oidc_host.lower()


# Provider documents ---------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _client() -> httpx.Client:
    if transport is not None:
        return httpx.Client(transport=transport, timeout=10, follow_redirects=False)
    from . import tls
    return httpx.Client(verify=tls.default_context(), timeout=10, follow_redirects=False)


def _fetch(url: str) -> dict:
    last: Exception | None = None
    for _ in range(2):
        try:
            with _client() as c:
                r = c.get(url)
            if r.status_code == 200:
                doc = r.json()
                if isinstance(doc, dict):
                    return doc
            last = OidcError(f"status {r.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
    raise OidcError(NO_ANSWER_MSG) from last


def _cached(key: str, url: str, fresh: bool = False) -> dict:
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and hit[0] > time.time():
            return hit[1]
    doc = _fetch(url)
    with _lock:
        _cache[key] = (time.time() + CACHE_SECONDS, doc)
    return doc


def reset_cache() -> None:
    with _lock:
        _cache.clear()
        _pending.clear()


def discovery(issuer: str, fresh: bool = False) -> dict:
    issuer = issuer.rstrip("/")
    doc = _cached("discovery:" + issuer, issuer + "/.well-known/openid-configuration", fresh)
    if str(doc.get("issuer", "")).rstrip("/") != issuer:
        raise OidcError(NOT_EXPECTED_MSG)
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not str(doc.get(field, "")).startswith("https://"):
            raise OidcError(NOT_EXPECTED_MSG)
    return doc


# The flow -------------------------------------------------------------------

_pending: dict[str, dict] = {}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def begin(s, redirect_uri: str, *, link_sid: str = "", remember: bool = True) -> tuple[str, str]:
    """Start a sign-in (or, with link_sid, the admin linking their account
    from that signed-in session). Returns the provider address to send the
    browser to and the state value the browser keeps in a cookie."""
    if not configured(s):
        raise OidcError(NOT_READY_MSG)
    doc = discovery(s.oidc_issuer)
    verifier = _b64url(secrets.token_bytes(48))
    nonce = _b64url(secrets.token_bytes(24))
    state = _b64url(secrets.token_bytes(24))
    now = time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if v["expires"] <= now]:
            _pending.pop(k, None)
        while len(_pending) >= _MAX_PENDING:
            _pending.pop(next(iter(_pending)), None)
        _pending[state] = {"verifier": verifier, "nonce": nonce, "redirect_uri": redirect_uri,
                           "link_sid": link_sid, "remember": bool(remember),
                           "issuer": s.oidc_issuer, "client_id": s.oidc_client_id,
                           "expires": now + FLOW_SECONDS}
    query = urlencode({
        "response_type": "code",
        "client_id": s.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    })
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return f"{doc['authorization_endpoint']}{sep}{query}", state


def take(state: str, cookie: str) -> dict:
    """The waiting sign-in for this state, used up. The browser must hold
    the same state in its cookie, so a link to the callback sent to someone
    else cannot finish there."""
    if not state or not cookie or not secrets.compare_digest(state.encode(), cookie.encode()):
        raise OidcError(EXPIRED_MSG)
    with _lock:
        flow = _pending.pop(state, None)
    if not flow or flow["expires"] <= time.time():
        raise OidcError(EXPIRED_MSG)
    return flow


def finish(s, flow: dict, code: str, iss: str = "") -> dict:
    """Trade the code for an ID token and return its checked claims."""
    if flow["issuer"] != s.oidc_issuer or flow["client_id"] != s.oidc_client_id:
        raise OidcError(EXPIRED_MSG)
    # RFC 9207: a provider that names itself on the way back must name the
    # one this sign-in started with.
    if iss and iss.rstrip("/") != s.oidc_issuer.rstrip("/"):
        raise OidcError(CHECK_FAILED_MSG)
    if not code:
        raise OidcError(TURNED_DOWN_MSG)
    doc = discovery(s.oidc_issuer)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow["redirect_uri"],
        "code_verifier": flow["verifier"],
    }
    # client_secret_basic, with each part form-encoded first (RFC 6749 2.3.1).
    basic = base64.b64encode(
        f"{quote(s.oidc_client_id, safe='')}:{quote(s.oidc_client_secret, safe='')}".encode()).decode()
    try:
        with _client() as c:
            r = c.post(doc["token_endpoint"], data=form,
                       headers={"Authorization": "Basic " + basic, "Accept": "application/json"})
        body = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(NO_ANSWER_MSG) from exc
    token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(token, str):
        raise OidcError(TURNED_DOWN_MSG)
    claims = verify_id_token(s, token, flow["nonce"])
    if not str(claims.get("sub") or ""):
        raise OidcError(CHECK_FAILED_MSG)
    return claims


def subject_matches(s, claims: dict) -> bool:
    sub = str(claims.get("sub") or "")
    return bool(s.oidc_subject) and bool(sub) and secrets.compare_digest(
        sub.encode(), s.oidc_subject.encode())


def _public_key(s, kid: str | None):
    from cryptography.hazmat.primitives.asymmetric import rsa
    uri = discovery(s.oidc_issuer)["jwks_uri"]
    for fresh in (False, True):
        # The second pass refetches, for a key the provider rotated in since.
        keys = _cached("jwks:" + uri, uri, fresh=fresh).get("keys", [])
        for jwk in keys if isinstance(keys, list) else []:
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig":
                continue
            if kid is None or jwk.get("kid") == kid:
                try:
                    n = int.from_bytes(_unb64url(jwk["n"]), "big")
                    e = int.from_bytes(_unb64url(jwk["e"]), "big")
                    return rsa.RSAPublicNumbers(e, n).public_key()
                except (KeyError, TypeError, ValueError):
                    continue
    raise OidcError(CHECK_FAILED_MSG)


def verify_id_token(s, token: str, nonce: str) -> dict:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # an install whose packages lag its code
        raise OidcError(CHECK_FAILED_MSG) from exc
    bad = OidcError(CHECK_FAILED_MSG)
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        head = json.loads(_unb64url(head_b64))
        claims = json.loads(_unb64url(body_b64))
        signature = _unb64url(sig_b64)
    except (ValueError, TypeError) as exc:
        raise bad from exc
    # One algorithm, named here, never taken from the token.
    if not isinstance(head, dict) or head.get("alg") != "RS256" or not isinstance(claims, dict):
        raise bad
    try:
        _public_key(s, head.get("kid")).verify(
            signature, f"{head_b64}.{body_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError) as exc:
        raise bad from exc
    now = time.time()
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    try:
        expires = float(claims.get("exp", 0))
        issued = float(claims.get("iat", 0))
    except (TypeError, ValueError) as exc:
        raise bad from exc
    if (
        str(claims.get("iss", "")).rstrip("/") != s.oidc_issuer.rstrip("/")
        or s.oidc_client_id not in audiences
        or ("azp" in claims and claims.get("azp") != s.oidc_client_id)
        or (len(audiences) > 1 and claims.get("azp") != s.oidc_client_id)
        or expires < now - LEEWAY_SECONDS
        or issued > now + LEEWAY_SECONDS
        or not secrets.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode())
    ):
        raise bad
    return claims


def account_label(claims: dict) -> str:
    """A name to show for the linked account, for display only."""
    for key in ("preferred_username", "email", "name"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return ""
