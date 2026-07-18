"""Instance provisioning, pairing, and the instance status endpoints.

The primary path is one-step provisioning: the app signs in with the
account's email and password and gets an instance token back in a single
call, so a non-technical user never handles a code. Pairing codes remain
as the advanced path (a code minted in the portal, typed into the app).
Either way the install ends up with a long-lived instance token, shown
once and stored hashed; from then on the install dials out with the
token and the cloud never reaches in.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ratelimit, usage
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, current_account,
                    current_instance, get_db, utc_now_iso, client_ip)
from ..models import Account, Instance, PairingCode
from ..security import new_pairing_code, new_token, token_hash
from .accounts import authenticate, totp_gate

router = APIRouter(prefix="/v1", tags=["instances"])


class VerifyLoginRequest(BaseModel):
    email: str
    password: str
    # A code from the authenticator app (or a recovery code). Optional and
    # ignored unless the linked account has two-factor sign-in turned on.
    totp: str = ""


@router.post("/instance/verify-login")
def verify_login(payload: VerifyLoginRequest, request: Request,
                 inst: Instance = Depends(current_instance),
                 db: Session = Depends(get_db)):
    """Confirm that an email + password (and 2FA, if on) belong to the SAME
    account this instance is linked to.

    The app calls this so a customer can sign in to the local install with
    their Forager credentials. It authenticates against the linked account
    only: a valid login for some OTHER account is refused exactly like a wrong
    password, so the endpoint cannot be used to probe which emails exist. The
    linked account's own two-factor requirement is honoured here, so the cloud
    is the enforcer for the "sign in with Forager" path.

    Shares the login rate-limit surface, keyed per instance so one kitchen's
    guessing cannot lock out another's.
    """
    if not ratelimit.allow(f"verify-login:{inst.id}", settings.login_rate_per_minute):
        raise HTTPException(429, detail="Too many attempts, try again in a minute")
    account, locked = authenticate(db, payload.email, payload.password)
    if locked:
        raise HTTPException(429, detail=locked)
    # Wrong credentials, a disabled account, or the right password for an
    # account that is not the one this instance is linked to all collapse to
    # one answer, so a caller learns nothing it did not already know.
    if not account or account.disabled or account.id != inst.account_id:
        return JSONResponse(status_code=401, content={"error": "invalid_credentials"})
    gate = totp_gate(db, account, payload.totp)
    if gate:
        # Password accepted; the second factor is missing or wrong. The app
        # reads the error to know whether to prompt for a code or say it failed.
        return JSONResponse(status_code=401, content={"error": gate})
    return {"ok": True, "account_email": account.email}


class VerifyUnlockRequest(BaseModel):
    # The single-use unlock code the Google flow delivered to the device's
    # login screen (flow=unlock, purpose="unlock").
    code: str = ""


@router.post("/instance/verify-unlock")
def verify_unlock(payload: VerifyUnlockRequest, request: Request,
                  inst: Instance = Depends(current_instance),
                  db: Session = Depends(get_db)):
    """Confirm that a Google sign-in (an unlock code from flow=unlock) belongs
    to the SAME account this instance is linked to.

    This backs the device login page's "Sign in with Google" button, the
    sibling of verify-login for accounts that have no Forager password. The
    device's own bearer token is the binding: an unlock code is worthless
    without the token of the instance whose account minted it, and unlike
    /v1/pairing/redeem nothing is created here, no instance and no credential,
    the reply is only "this Google sign-in is (not) your linked account".

    The code burns on FIRST presentation, before the account comparison, so a
    mismatch cannot be retried elsewhere. Telling "account_mismatch" apart
    from "invalid_code" is safe here: the caller is the linked device itself,
    the reply names no email, and codes are single-use, short-lived, and rate
    limited per instance.
    """
    if not ratelimit.allow(f"verify-unlock:{inst.id}",
                           settings.login_rate_per_minute):
        raise HTTPException(429, detail="Too many attempts, try again in a minute")
    row = db.query(PairingCode).filter_by(
        code_hash=token_hash(payload.code.strip().upper())).first()
    # Unknown, wrong flavour (a linking code), already spent, or expired all
    # collapse to one answer.
    if (not row or row.purpose != "unlock" or row.redeemed
            or row.expires_at < utc_now_iso()):
        return JSONResponse(status_code=401, content={"error": "invalid_code"})
    row.redeemed = 1
    db.commit()
    owner = db.get(Account, row.account_id)
    if not owner or owner.disabled:
        return JSONResponse(status_code=401, content={"error": "invalid_code"})
    if row.account_id != inst.account_id:
        # A real Google sign-in, but not the account this device is linked to.
        return JSONResponse(status_code=401,
                            content={"error": "account_mismatch"})
    return {"ok": True, "account_email": owner.email}


def _create_instance(db: Session, account_id: int, name: str) -> tuple[Instance, str]:
    """Mint an instance and its token. The returned token is the only copy;
    the database keeps its hash."""
    token = new_token("prc")
    inst = Instance(token_hash=token_hash(token), account_id=account_id,
                    name=name.strip()[:120], created_at=utc_now_iso())
    db.add(inst)
    db.commit()
    return inst, token


class ProvisionRequest(BaseModel):
    email: str
    password: str
    device_name: str = ""
    # A code from the authenticator app (or a recovery code). Optional and
    # ignored unless the account has two-factor sign-in turned on.
    totp: str = ""


@router.post("/instances/provision")
def provision_instance(payload: ProvisionRequest, request: Request,
                       db: Session = Depends(get_db)):
    """One-step linking: account credentials in, instance token out.

    This is what the app's "sign in" flow calls, so the user only ever
    types the email and password they created on the portal. Shares the
    login rate-limit window because it is the same password-guessing
    surface."""
    client = client_ip(request)
    if not ratelimit.allow(f"login:{client}", settings.login_rate_per_minute):
        raise HTTPException(429, detail="Too many login attempts, try again in a minute")
    account, locked = authenticate(db, payload.email, payload.password)
    if locked:
        raise HTTPException(429, detail=locked)
    if not account:
        raise HTTPException(401, detail="Invalid email or password")
    if account.disabled:
        raise HTTPException(403, detail=ACCOUNT_DISABLED_MESSAGE)
    gate = totp_gate(db, account, payload.totp)
    if gate:
        # Password accepted, second factor missing or wrong. The app reads the
        # error to know whether to prompt for a code or say it was wrong.
        return JSONResponse(status_code=401, content={"error": gate})
    inst, token = _create_instance(db, account.id, payload.device_name)
    state = usage.quota_state(db, account.id, usage.month_key())
    return {
        "instance_token": token,
        "instance_id": inst.id,
        "account_email": account.email,
        # trial / basic / premium / expired; days remaining rides along
        # while the plan is the trial (null otherwise). plan_label is the
        # same plan as a person reads it ("Trial until <date>", "Premium").
        "plan": state["plan"],
        "plan_label": state["plan_label"],
        "trial_days_left": state["trial_days_left"],
        "quota": state["quota"],
        "month_used": state["used"],
        # The install's remote-access URL once a tunnel is enabled, else null.
        # A freshly provisioned instance has none; it appears after the app
        # calls /v1/tunnel/enable.
        "suggested_public_url": inst.public_url or None,
    }


@router.post("/pairing/code")
def create_pairing_code(account: Account = Depends(current_account),
                        db: Session = Depends(get_db)):
    code = new_pairing_code()
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.pairing_code_ttl_minutes)
    expires_at = expires.isoformat(timespec="seconds")
    db.add(PairingCode(code_hash=token_hash(code), account_id=account.id,
                       expires_at=expires_at, created_at=utc_now_iso()))
    db.commit()
    return {"code": code, "expires_at": expires_at}


class RedeemRequest(BaseModel):
    code: str
    name: str = ""


@router.post("/pairing/redeem")
def redeem_pairing_code(payload: RedeemRequest, db: Session = Depends(get_db)):
    row = db.query(PairingCode).filter_by(
        code_hash=token_hash(payload.code.strip().upper())).first()
    # Only a "link" code mints an instance: an unlock code (verify-unlock's
    # flavour) is refused here exactly like an unknown one, so the two kinds
    # can never be swapped.
    if (not row or row.purpose != "link" or row.redeemed
            or row.expires_at < utc_now_iso()):
        # One message for unknown, used, and expired: a probe learns nothing.
        raise HTTPException(400, detail="Invalid or expired pairing code")
    owner = db.get(Account, row.account_id)
    if owner and owner.disabled:
        raise HTTPException(403, detail=ACCOUNT_DISABLED_MESSAGE)
    row.redeemed = 1
    inst, token = _create_instance(db, row.account_id, payload.name)
    # The only time the token crosses the wire; the database keeps its hash.
    return {"instance_token": token, "instance_id": inst.id}


@router.get("/instance/me")
def instance_me(inst: Instance = Depends(current_instance),
                db: Session = Depends(get_db)):
    """Entitlement status and quota remaining, for the app's settings page.

    account_email lets the app show which account the install is linked to,
    not just the instance's own name."""
    state = usage.quota_state(db, inst.account_id, usage.month_key())
    # The app renders "active" as "this account can use Forager right now"
    # (its settings pane and vision test both read it), so it must be true
    # for an admin comp and the running trial, not just a Stripe plan; a
    # comped account otherwise gets told it has no subscription. plan_label
    # and source ride along inside state so the app can show the plan as a
    # person reads it ("Complimentary", "Trial until <date>", "Premium").
    entitlement = {**state, "active": usage.has_active_access(state)}
    account = db.get(Account, inst.account_id)
    return {"instance_id": inst.id, "name": inst.name,
            "account_email": account.email if account else "",
            # Whether the linked account has two-factor sign-in turned on. The
            # app's "expose to the internet" gate reads it: a device linked to a
            # 2FA account already has a second factor available for outside
            # logins, so it may enable remote access without local device 2FA.
            # Derived from the account, no schema change.
            "account_2fa": bool(account.totp_enabled) if account else False,
            # The install's remote-access URL once a tunnel is enabled, else
            # null; the app reads it to show and link its own public address.
            "public_url": inst.public_url or None,
            "entitlement": entitlement}


@router.delete("/instance")
def revoke_instance(inst: Instance = Depends(current_instance),
                    db: Session = Depends(get_db)):
    """Self-revoke: the app's Unlink calls this with its own token before
    forgetting it, so the credential dies on the server too."""
    db.delete(inst)
    db.commit()
    return {"revoked": True}
