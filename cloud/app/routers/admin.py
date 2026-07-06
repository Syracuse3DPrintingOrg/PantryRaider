"""The Forager admin panel: operator-only pages under /admin.

Access is an email allowlist (CLOUD_ADMIN_EMAILS) checked against the
signed-in portal session. Everyone else, signed in or not, gets a 404,
the same answer as a route that does not exist, so the panel never
advertises itself. Unlike the subscriber portal these pages are for the
operator and may use technical words (tokens, instances, Stripe ids).

Every mutation here writes an admin_actions row: the account detail page
shows the target's trail and the overview shows the latest actions
globally.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import usage
from ..config import PLAN_QUOTAS, settings
from ..deps import cookie_account, get_db, is_admin, utc_now_iso
from ..models import (Account, AdminAction, AuthSession, Entitlement,
                      Instance, Subscription, UsageLedger)

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# The overview lists at most this many accounts; the search box narrows the
# rest. Real pagination can come when the list actually outgrows this.
ACCOUNT_LIST_CAP = 500

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def require_admin(account: Account | None = Depends(cookie_account)) -> Account:
    """The signed-in admin, or a 404. Not a 403: to anyone off the
    allowlist, /admin does not exist."""
    if not is_admin(account):
        raise HTTPException(404, detail="Not found")
    return account


def _log(db: Session, admin: Account, action: str, account_id: int,
         detail: str = "") -> None:
    db.add(AdminAction(admin_email=admin.email, action=action,
                       account_id=account_id, detail=detail[:255],
                       created_at=utc_now_iso()))
    db.commit()


def _target(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, detail="Not found")
    return account


def _back(account_id: int) -> RedirectResponse:
    return RedirectResponse(f"/admin/accounts/{account_id}", status_code=303)


def _grouped(rows) -> dict:
    return {key: value for key, value in rows}


@router.get("/admin")
def admin_overview(request: Request, q: str = "",
                   admin: Account = Depends(require_admin),
                   db: Session = Depends(get_db)):
    mk = usage.month_key()

    total_accounts = db.query(func.count(Account.id)).scalar() or 0
    total_kitchens = db.query(func.count(Instance.id)).scalar() or 0
    # Paid subscriptions only: neither comped grants nor signup trials are
    # revenue, so count only the paid sources (Stripe, or a legacy blank
    # source that predates the column).
    active_subs = (db.query(Entitlement)
                   .filter(Entitlement.status == "active",
                           Entitlement.source.in_(("stripe", "")))
                   .count())
    month_tokens = int(db.query(func.coalesce(func.sum(UsageLedger.tokens), 0))
                       .filter(UsageLedger.month_key == mk).scalar() or 0)
    est_cost = month_tokens / 1_000_000 * settings.gemini_cost_per_million_tokens

    query = db.query(Account)
    q = q.strip()
    if q:
        query = query.filter(Account.email.contains(q.lower()))
    accounts = (query.order_by(Account.id.desc())
                .limit(ACCOUNT_LIST_CAP + 1).all())
    capped = len(accounts) > ACCOUNT_LIST_CAP
    accounts = accounts[:ACCOUNT_LIST_CAP]
    ids = [a.id for a in accounts]

    tokens_by, kitchens_by, seen_by, session_by, ents = {}, {}, {}, {}, {}
    if ids:
        tokens_by = _grouped(
            db.query(UsageLedger.account_id, func.sum(UsageLedger.tokens))
            .filter(UsageLedger.month_key == mk,
                    UsageLedger.account_id.in_(ids))
            .group_by(UsageLedger.account_id).all())
        kitchens_by = _grouped(
            db.query(Instance.account_id, func.count(Instance.id))
            .filter(Instance.account_id.in_(ids))
            .group_by(Instance.account_id).all())
        seen_by = _grouped(
            db.query(Instance.account_id, func.max(Instance.last_seen_at))
            .filter(Instance.account_id.in_(ids))
            .group_by(Instance.account_id).all())
        session_by = _grouped(
            db.query(AuthSession.account_id, func.max(AuthSession.created_at))
            .filter(AuthSession.account_id.in_(ids))
            .group_by(AuthSession.account_id).all())
        ents = {e.account_id: e for e in
                db.query(Entitlement)
                .filter(Entitlement.account_id.in_(ids)).all()}

    rows = []
    for a in accounts:
        ent = ents.get(a.id)
        plan = ent.plan if usage.entitlement_active(ent) else "expired"
        # Last seen: the freshest signal we track, a portal login or a
        # paired install checking in; a brand-new account falls back to
        # its creation time.
        last_seen = max(seen_by.get(a.id) or "", session_by.get(a.id) or "")
        rows.append({
            "id": a.id,
            "email": a.email,
            "auth": a.auth_provider or "password",
            "verified": bool(a.email_verified),
            "plan": plan,
            "comp": bool(ent and ent.source == "comp"
                         and usage.entitlement_active(ent)),
            "tokens": int(tokens_by.get(a.id) or 0),
            "kitchens": int(kitchens_by.get(a.id) or 0),
            "created": a.created_at,
            "last_seen": last_seen or a.created_at,
            "disabled": bool(a.disabled),
        })

    recent = (db.query(AdminAction).order_by(AdminAction.id.desc())
              .limit(20).all())
    action_emails = {a.id: a.email for a in
                     db.query(Account).filter(
                         Account.id.in_({r.account_id for r in recent})).all()
                     } if recent else {}

    return templates.TemplateResponse(request, "admin.html", {
        "signed_in": True,
        "q": q,
        "totals": {
            "accounts": total_accounts,
            "kitchens": total_kitchens,
            "active_subs": active_subs,
            "month_tokens": month_tokens,
            "est_cost": f"{est_cost:,.2f}",
            "month": mk,
        },
        "rows": rows,
        "capped": capped,
        "cap": ACCOUNT_LIST_CAP,
        "recent": recent,
        "action_emails": action_emails,
    })


@router.get("/admin/stats")
def admin_stats(request: Request,
                admin: Account = Depends(require_admin),
                db: Session = Depends(get_db)):
    """A high-level numbers panel for the operator: totals plus a per-plan
    breakdown of where accounts currently sit. Reuses the same counting and
    entitlement-resolution the overview and quota gates use, so the figures
    line up with the rest of the panel. Read-only; no per-account data."""
    mk = usage.month_key()

    total_accounts = db.query(func.count(Account.id)).scalar() or 0
    verified_accounts = (db.query(func.count(Account.id))
                         .filter(Account.email_verified == 1).scalar() or 0)
    total_kitchens = db.query(func.count(Instance.id)).scalar() or 0
    # Paid subscriptions only, exactly as the overview counts them: Stripe or
    # a legacy blank source, never comps or trials.
    active_subs = (db.query(Entitlement)
                   .filter(Entitlement.status == "active",
                           Entitlement.source.in_(("stripe", "")))
                   .count())
    month_tokens = int(db.query(func.coalesce(func.sum(UsageLedger.tokens), 0))
                       .filter(UsageLedger.month_key == mk).scalar() or 0)
    est_cost = month_tokens / 1_000_000 * settings.gemini_cost_per_million_tokens

    # Per-plan breakdown: the plan that governs each account right now, using
    # the same resolver the AI proxy and account page read, so a trial that
    # sits under a paid plan is not double-counted. An account with no active
    # entitlement lands in "expired".
    ents_by_account: dict[int, list] = {}
    for e in db.query(Entitlement).all():
        ents_by_account.setdefault(e.account_id, []).append(e)
    plan_counts = {"trial": 0, "basic": 0, "premium": 0, "expired": 0}
    accounts_with_ents = set(ents_by_account)
    for rows in ents_by_account.values():
        ent = usage.resolve_entitlement(rows)
        plan = ent.plan if ent else "expired"
        plan_counts[plan] = plan_counts.get(plan, 0) + 1
    # Accounts that never got an entitlement row read as expired too.
    plan_counts["expired"] += total_accounts - len(accounts_with_ents)
    plan_breakdown = [{"plan": p, "count": plan_counts.get(p, 0)}
                      for p in ("trial", "basic", "premium", "expired")]

    return templates.TemplateResponse(request, "admin_stats.html", {
        "signed_in": True,
        "totals": {
            "accounts": total_accounts,
            "verified": verified_accounts,
            "kitchens": total_kitchens,
            "active_subs": active_subs,
            "month_tokens": month_tokens,
            "est_cost": f"{est_cost:,.2f}",
            "month": mk,
        },
        "plan_breakdown": plan_breakdown,
    })


@router.get("/admin/accounts/{account_id}")
def admin_account_page(account_id: int, request: Request,
                       admin: Account = Depends(require_admin),
                       db: Session = Depends(get_db)):
    account = _target(db, account_id)
    mk = usage.month_key()
    state = usage.quota_state(db, account.id, mk)
    ent = db.query(Entitlement).filter_by(account_id=account.id).first()
    kitchens = (db.query(Instance).filter_by(account_id=account.id)
                .order_by(Instance.created_at).all())
    subs = (db.query(Subscription).filter_by(account_id=account.id)
            .order_by(Subscription.id.desc()).all())
    by_month = (db.query(UsageLedger.month_key,
                         func.sum(UsageLedger.tokens),
                         func.count(UsageLedger.id))
                .filter(UsageLedger.account_id == account.id)
                .group_by(UsageLedger.month_key)
                .order_by(UsageLedger.month_key.desc())
                .limit(6).all())
    trail = (db.query(AdminAction).filter_by(account_id=account.id)
             .order_by(AdminAction.id.desc()).all())

    return templates.TemplateResponse(request, "admin_account.html", {
        "signed_in": True,
        "account": account,
        "state": state,
        "ent": ent,
        "ent_active": usage.entitlement_active(ent),
        "kitchens": kitchens,
        "subs": subs,
        "by_month": [{"month": m, "tokens": int(t or 0), "requests": int(c or 0)}
                     for m, t, c in by_month],
        "trail": trail,
    })


@router.post("/admin/accounts/{account_id}/disable")
def disable_account(account_id: int,
                    admin: Account = Depends(require_admin),
                    db: Session = Depends(get_db)):
    account = _target(db, account_id)
    account.disabled = 1
    db.commit()
    # A disabled account loses remote access too: tear down any live tunnels
    # so a kill-switched kitchen stops being reachable from the internet.
    from .tunnel import disable_tunnel_for_account
    disable_tunnel_for_account(db, account.id)
    _log(db, admin, "disable", account.id)
    return _back(account.id)


@router.post("/admin/accounts/{account_id}/enable")
def enable_account(account_id: int,
                   admin: Account = Depends(require_admin),
                   db: Session = Depends(get_db)):
    account = _target(db, account_id)
    account.disabled = 0
    db.commit()
    _log(db, admin, "enable", account.id)
    return _back(account.id)


@router.post("/admin/accounts/{account_id}/comp")
def comp_account(account_id: int,
                 expires_on: str = Form(...),
                 admin: Account = Depends(require_admin),
                 db: Session = Depends(get_db)):
    """Grant a premium entitlement on the house, until the chosen date.

    Never overwrites a Stripe-sourced entitlement: a paying subscriber has
    nothing to comp, and the webhook owns that row."""
    account = _target(db, account_id)
    if not _DATE_RE.match(expires_on.strip()):
        raise HTTPException(400, detail="Expiry must be a YYYY-MM-DD date")
    ent = db.query(Entitlement).filter_by(account_id=account.id).first()
    if ent and ent.source == "stripe" and ent.status == "active":
        raise HTTPException(409, detail="Account has an active Stripe "
                                        "subscription; nothing to comp")
    if not ent:
        ent = Entitlement(account_id=account.id)
        db.add(ent)
    ent.plan = "premium"
    ent.status = "active"
    ent.monthly_token_quota = PLAN_QUOTAS["premium"]
    ent.source = "comp"
    # Comps run through the end of the chosen day (UTC).
    ent.expires_at = f"{expires_on.strip()}T23:59:59+00:00"
    ent.updated_at = utc_now_iso()
    db.commit()
    _log(db, admin, "comp", account.id, f"premium until {expires_on.strip()}")
    return _back(account.id)


@router.post("/admin/accounts/{account_id}/comp/expire")
def expire_comp(account_id: int,
                admin: Account = Depends(require_admin),
                db: Session = Depends(get_db)):
    account = _target(db, account_id)
    ent = db.query(Entitlement).filter_by(account_id=account.id,
                                          source="comp").first()
    if ent:
        ent.status = "inactive"
        ent.updated_at = utc_now_iso()
        db.commit()
        _log(db, admin, "expire-comp", account.id, f"was {ent.plan}")
    return _back(account.id)


@router.post("/admin/accounts/{account_id}/kitchens/{kitchen_id}/revoke")
def revoke_kitchen(account_id: int, kitchen_id: int,
                   admin: Account = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """Delete the instance row, killing its credential, exactly like the
    owner's own Remove button on the portal."""
    account = _target(db, account_id)
    inst = db.query(Instance).filter_by(id=kitchen_id,
                                        account_id=account.id).first()
    if inst:
        name = inst.name or f"instance {inst.id}"
        db.delete(inst)
        db.commit()
        _log(db, admin, "revoke-kitchen", account.id, name)
    return _back(account.id)
