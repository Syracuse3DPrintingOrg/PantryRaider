"""Passkey (WebAuthn / FIDO2) helpers for the Forager account.

A thin, testable layer over py_webauthn. The pure pieces (the relying-party
id and origin derived from the request, the signature-counter replay check,
the base64url conversions) live here so the routes stay small and the security
rules can be unit-tested without a real authenticator. The two verify wrappers
are the seam the tests stub, standing in for a browser signing a challenge.

Relying-party id and origin: both are derived from the request rather than
hardcoded, so the same code works at forager.pantryraider.app in production and
at localhost in development. The app sits behind Caddy, which sets
X-Forwarded-Host and X-Forwarded-Proto; we honour those first and fall back to
the request's own host. The id is the host without its port; the origin is the
scheme and host together, and the browser's assertion is verified against it.
"""
from __future__ import annotations

from fastapi import Request
from webauthn import (base64url_to_bytes, generate_authentication_options,
                      generate_registration_options, options_to_json,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      AuthenticatorTransport,
                                      PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement,
                                      UserVerificationRequirement)

# The relying party as a person would recognise it, shown by the browser and
# the operating system's passkey prompt.
RP_NAME = "Forager"


def rp_id_and_origin(request: Request) -> tuple[str, str]:
    """The relying-party id and the expected origin for this request.

    Derived from the request so it is correct in both production
    (forager.pantryraider.app) and development (localhost), never a hardcoded
    single origin. Honours the proxy's forwarded host/scheme first (Caddy sets
    them), then the request's own Host header. The id is the bare host without
    a port; the origin is scheme + host, matching what the browser reports.
    """
    proto = (request.headers.get("x-forwarded-proto")
             or request.url.scheme or "https")
    proto = proto.split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc or "localhost")
    host = host.split(",")[0].strip()
    rp_id = host.split(":")[0]
    origin = f"{proto}://{host}"
    return rp_id, origin


def _transport_enums(transports: str) -> list[AuthenticatorTransport] | None:
    """Turn a stored comma-joined transport string back into the enums
    py_webauthn wants for an allow/exclude descriptor, dropping anything it
    does not recognise."""
    out: list[AuthenticatorTransport] = []
    for t in (transports or "").split(","):
        t = t.strip()
        if not t:
            continue
        try:
            out.append(AuthenticatorTransport(t))
        except ValueError:
            continue
    return out or None


def descriptor(credential_id_b64: str, transports: str = ""
               ) -> PublicKeyCredentialDescriptor:
    """A PublicKeyCredentialDescriptor for a stored credential, used to
    exclude a key already registered (so the same device is not added twice)
    and to let a specific key answer a sign-in."""
    return PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(credential_id_b64),
        transports=_transport_enums(transports))


def registration_options_json(rp_id: str, account_id: int, account_email: str,
                              exclude: list[PublicKeyCredentialDescriptor]
                              ) -> tuple[str, str]:
    """Build PublicKeyCredentialCreationOptions for a signed-in account adding
    a passkey. Returns (options_json, challenge_b64). The user handle is the
    account id, a stable per-account value; already-registered credentials are
    excluded so the browser will not enrol the same device twice."""
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=str(account_id).encode("ascii"),
        user_name=account_email,
        user_display_name=account_email,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED),
    )
    return options_to_json(opts), bytes_to_base64url(opts.challenge)


def authentication_options_json(rp_id: str,
                                allow: list[PublicKeyCredentialDescriptor]
                                ) -> tuple[str, str]:
    """Build PublicKeyCredentialRequestOptions for a sign-in. When ``allow`` is
    empty the ceremony is usernameless (any discoverable passkey for this site
    may answer); when it lists an account's credentials the browser is steered
    to those. Returns (options_json, challenge_b64)."""
    opts = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return options_to_json(opts), bytes_to_base64url(opts.challenge)


def sign_count_ok(stored: int, presented: int) -> bool:
    """Whether an assertion's signature counter is acceptable.

    The counter must advance on every use, so a value at or below the stored
    one means a cloned or replayed authenticator and the sign-in is refused.
    The one allowed exception is an authenticator that does not keep a counter
    at all: it always reports 0, so 0-against-0 is fine.
    """
    if presented == 0 and stored == 0:
        return True
    return presented > stored


# --- The verify seam ------------------------------------------------------
# These wrap py_webauthn's verification. The tests monkeypatch them to stand
# in for a real browser and authenticator; production calls straight through.

def verify_registration(credential: dict, expected_challenge: bytes,
                        rp_id: str, origin: str):
    """Verify an attestation against the stashed challenge and this request's
    rp id / origin. Raises on any mismatch (bad challenge, wrong origin,
    tampered attestation); the caller turns that into a user-forward error."""
    return verify_registration_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        require_user_verification=False,
    )


def verify_authentication(credential: dict, expected_challenge: bytes,
                          rp_id: str, origin: str, public_key: bytes,
                          sign_count: int):
    """Verify an assertion against the stashed challenge, this request's rp id
    / origin, and the stored public key. Raises on any mismatch."""
    return verify_authentication_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=public_key,
        credential_current_sign_count=sign_count,
        require_user_verification=False,
    )


__all__ = [
    "RP_NAME", "rp_id_and_origin", "descriptor", "registration_options_json",
    "authentication_options_json", "sign_count_ok", "verify_registration",
    "verify_authentication", "base64url_to_bytes", "bytes_to_base64url",
]
