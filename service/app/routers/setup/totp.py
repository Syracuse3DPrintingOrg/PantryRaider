"""Local two-factor authentication for the app's own login (FoodAssistant-x1ty).

The device's own authenticator-app 2FA for the LOCAL password: generate a
secret and QR, confirm a code to turn it on (revealing recovery codes once),
and turn it back off (or regenerate the codes) after proving a current code
or the app password.
"""

import asyncio
from fastapi import Request
from pydantic import BaseModel

from ...config import settings, APP_NAME, device_hostname
from ...passwords import verify_secret
from ... import totp as local_totp
from ...services import auth_state
from ._common import router


class TOTPVerifyPayload(BaseModel):
    secret: str
    code: str
    # Only needed to replace 2FA that is already on: a current code, a
    # recovery code, or the app password.
    credential: str = ""


class TOTPCredentialPayload(BaseModel):
    # A live authenticator code, one of the recovery codes, or the app password:
    # any one proves the owner is present before a 2FA change.
    credential: str = ""


def _totp_account_label() -> str:
    """The label an authenticator app shows for this device's entry: the linked
    Forager account when available, otherwise the device hostname or the app
    name, so several kitchens are told apart in the app's list."""
    if settings.cloud_linked():
        # Best effort: the account email is only known after a cloud round-trip,
        # so fall back to the hostname rather than dial out on setup.
        return device_hostname() or APP_NAME
    return device_hostname() or APP_NAME


def _local_2fa_credential_ok(credential: str) -> bool:
    """Whether ``credential`` proves the owner may change 2FA: a live TOTP code,
    an unused recovery code, or the app (admin) password."""
    credential = (credential or "").strip()
    if not credential:
        return False
    secret = settings.local_totp_effective_secret()
    if secret and local_totp.totp_verify(secret, credential):
        return True
    matched, _remaining = local_totp.consume_recovery_code(
        credential, settings.local_totp_recovery)
    if matched:
        return True
    if settings.auth_password and verify_secret(credential, settings.auth_password):
        return True
    return False


@router.post("/totp/generate")
async def totp_generate():
    """Generate a fresh TOTP secret and its QR for enrollment. Nothing is saved
    until the user confirms a code at /totp/verify."""
    secret = local_totp.generate_totp_secret()
    uri = local_totp.otpauth_uri(secret, _totp_account_label(), APP_NAME)
    return {"secret": secret, "qr": local_totp.qr_data_uri(uri), "uri": uri}


@router.post("/totp/verify")
async def totp_verify(request: Request, payload: TOTPVerifyPayload):
    """Confirm the authenticator app is in sync, then turn on 2FA and mint the
    one-time recovery codes (shown here once, stored only as hashes).

    Replacing 2FA that is already on needs the same proof as turning it off,
    so a walk-up at an open settings page cannot move it to their own phone."""
    if settings.local_2fa_active() and not await asyncio.to_thread(
            _local_2fa_credential_ok, payload.credential):
        return {"ok": False,
                "error": "Enter a current code or your password to set up two-factor again."}
    if not local_totp.totp_verify(payload.secret, payload.code):
        return {"ok": False,
                "error": "Code did not match. Check your authenticator app clock."}
    codes = local_totp.generate_recovery_codes()
    hashed = await asyncio.to_thread(local_totp.hash_recovery_codes, codes)
    settings.save({
        "local_totp_secret": payload.secret,
        "local_totp_enabled": True,
        "local_totp_recovery": hashed,
        # Clear any legacy secret so there is one source of truth going forward.
        "totp_secret": "",
    })
    auth_state.sign_out_others(request.session)
    return {"ok": True, "recovery_codes": codes,
            "message": "Two-factor authentication is on. Save your recovery codes."}


@router.post("/totp/disable")
async def totp_disable(request: Request,
                       payload: TOTPCredentialPayload = TOTPCredentialPayload()):
    """Turn 2FA off after the owner proves a current code or the app password,
    so a walk-up at an open settings page cannot silently remove it."""
    if settings.local_2fa_active() and not await asyncio.to_thread(
            _local_2fa_credential_ok, payload.credential):
        return {"ok": False,
                "error": "Enter a current code or your password to turn off two-factor authentication."}
    settings.save({"local_totp_secret": "", "local_totp_enabled": False,
                   "local_totp_recovery": [], "totp_secret": ""})
    auth_state.sign_out_others(request.session)
    return {"ok": True, "message": "Two-factor authentication is off."}


@router.post("/totp/recovery")
async def totp_recovery(payload: TOTPCredentialPayload = TOTPCredentialPayload()):
    """Mint a fresh set of recovery codes (the old set stops working), after the
    owner proves a current code or the app password."""
    if not settings.local_2fa_active():
        return {"ok": False,
                "error": "Turn on two-factor authentication first."}
    if not await asyncio.to_thread(_local_2fa_credential_ok, payload.credential):
        return {"ok": False,
                "error": "Enter a current code or your password to make new recovery codes."}
    codes = local_totp.generate_recovery_codes()
    hashed = await asyncio.to_thread(local_totp.hash_recovery_codes, codes)
    settings.save({"local_totp_recovery": hashed})
    return {"ok": True, "recovery_codes": codes,
            "message": "New recovery codes. The old ones no longer work."}
