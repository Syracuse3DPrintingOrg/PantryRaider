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
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import ratelimit, usage
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, current_account,
                    current_instance, get_db, utc_now_iso, client_ip)
from ..models import Account, Instance, PairingCode
from ..security import new_pairing_code, new_token, token_hash
from .accounts import authenticate

router = APIRouter(prefix="/v1", tags=["instances"])


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
    inst, token = _create_instance(db, account.id, payload.device_name)
    state = usage.quota_state(db, account.id, usage.month_key())
    return {
        "instance_token": token,
        "instance_id": inst.id,
        "account_email": account.email,
        # trial / basic / premium / expired; days remaining rides along
        # while the plan is the trial (null otherwise).
        "plan": state["plan"],
        "trial_days_left": state["trial_days_left"],
        "quota": state["quota"],
        "month_used": state["used"],
        # Reserved for the hosted-tunnel follow-up: once WireGuard tunnels
        # exist, provisioning will suggest the install's public URL here.
        # Shaped now so the app-side contract does not change later.
        "suggested_public_url": None,
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
    if not row or row.redeemed or row.expires_at < utc_now_iso():
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
    account = db.get(Account, inst.account_id)
    return {"instance_id": inst.id, "name": inst.name,
            "account_email": account.email if account else "",
            "entitlement": state}


@router.delete("/instance")
def revoke_instance(inst: Instance = Depends(current_instance),
                    db: Session = Depends(get_db)):
    """Self-revoke: the app's Unlink calls this with its own token before
    forgetting it, so the credential dies on the server too."""
    db.delete(inst)
    db.commit()
    return {"revoked": True}
