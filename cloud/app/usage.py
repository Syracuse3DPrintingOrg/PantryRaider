"""Per-account monthly token accounting over the usage ledger.

The per-account counterpart of the app's local usage tracker
(service/app/services/usage.py); the month-key and quota semantics match it
on purpose so the app can surface cloud quota errors exactly like its local
budget gate. Duplicated rather than imported: cloud/ shares nothing at
import time with service/.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import (EXPIRED_PLAN, PLAN_QUOTAS, PREMIUM_PLAN, TRIAL_DAYS,
                     TRIAL_PLAN)
from .models import Entitlement, TrialClaim, UsageLedger

# What a customer sees when their install has already spent its one free trial
# on an earlier account. User-forward: it names the device, not tokens or
# instances, and points at the way forward (subscribe).
TRIAL_ALREADY_USED_MESSAGE = (
    "This device already used its free trial. You can subscribe to keep going."
)

# The ledger kind a pending reservation carries until it is reconciled to the
# real token count. It is a normal ledger row (so month_total already counts
# it against the running quota the moment it is written), overwritten in place
# with the real kind once the provider answers, or deleted if the call fails.
RESERVE_KIND = "reserve"


def month_key(now=None) -> str:
    """Current 'YYYY-MM' key in UTC. ``now`` injectable for tests."""
    if now is None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def month_total(db: Session, account_id: int, mk: str) -> int:
    total = (
        db.query(func.coalesce(func.sum(UsageLedger.tokens), 0))
        .filter(UsageLedger.account_id == account_id,
                UsageLedger.month_key == mk)
        .scalar()
    )
    return int(total or 0)


def record(db: Session, account_id: int, instance_id: int, tokens: int,
           kind: str, mk: str, created_at: str) -> None:
    """Append a ledger row. No-op for zero or negative counts."""
    if not tokens or tokens < 0:
        return
    db.add(UsageLedger(account_id=account_id, instance_id=instance_id,
                       month_key=mk, tokens=int(tokens), kind=kind,
                       created_at=created_at))
    db.commit()


def _lock_account(db: Session, account_id: int) -> None:
    """Serialize the gate-and-reserve critical section per account.

    On Postgres (production) a transaction-scoped advisory lock keyed on the
    account id makes the quota check and the reservation insert atomic against
    other requests for the same account: a concurrent burst cannot all read
    "room left" before any of them records usage. The lock releases on commit
    or rollback, so it never outlives the reservation write.

    SQLite (the test database) has no advisory locks and no real concurrency
    to defend against here, so this is a no-op there. The burst protection is
    therefore Postgres-only, which is the production database; the reservation
    itself (below) still closes the check-to-record gap for the interleavings
    the test suite exercises deterministically.
    """
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
               {"key": int(account_id)})


def gate_and_reserve(db: Session, account_id: int, instance_id: int, mk: str,
                     estimate: int, created_at: str):
    """Check the quota and, if there is room, reserve an estimated cost.

    Runs under the per-account advisory lock so the check and the reservation
    insert cannot interleave with another request for the same account. The
    reservation is a live ledger row: once it is committed, every later gate
    for the account sees it in month_total, so N concurrent requests with room
    for fewer than N can no longer all pass before any usage is recorded.

    Returns ``(state, reservation_id)``. ``reservation_id`` is None when the
    account is refused (no active entitlement, or already over quota); the
    caller raises the existing 402 from ``state``. Reconcile the reservation to
    the real cost with :func:`reconcile_reservation` on success, or drop it with
    :func:`release_reservation` on any failure, so a reservation never becomes
    phantom usage.
    """
    _lock_account(db, account_id)
    state = quota_state(db, account_id, mk)
    if not has_active_access(state) or state["over_quota"]:
        # Nothing written; commit only to release the advisory lock promptly.
        db.commit()
        return state, None
    row = UsageLedger(account_id=account_id, instance_id=instance_id,
                      month_key=mk, tokens=max(1, int(estimate)),
                      kind=RESERVE_KIND, created_at=created_at)
    db.add(row)
    db.flush()  # assign the primary key while still inside the locked txn
    reservation_id = row.id
    db.commit()  # persist the reservation and release the advisory lock
    return state, reservation_id


def reconcile_reservation(db: Session, reservation_id: int, tokens: int,
                          kind: str, created_at: str) -> None:
    """Settle a reservation to the real token count once the call succeeds.

    Overwrites the reserved row in place (no second row, so ledger counts stay
    exact) with the actual cost and task kind. A zero or negative real cost
    means nothing to charge, so the reservation is dropped entirely, matching
    :func:`record`'s no-op-on-zero behavior."""
    row = db.get(UsageLedger, reservation_id)
    if row is None:
        return
    if not tokens or tokens < 0:
        db.delete(row)
    else:
        row.tokens = int(tokens)
        row.kind = kind
        row.created_at = created_at
    db.commit()


def release_reservation(db: Session, reservation_id: int) -> None:
    """Drop a reservation whose call never billed (upstream error or refusal),
    so the estimate never lingers as phantom usage."""
    row = db.get(UsageLedger, reservation_id)
    if row is not None:
        db.delete(row)
        db.commit()


def entitlement_active(ent, now_iso: str | None = None) -> bool:
    """Whether an entitlement row counts right now. Status must be active,
    and a row with a hard expiry (trials and comped plans) must not be
    past it."""
    if not ent or ent.status != "active":
        return False
    if ent.expires_at:
        if now_iso is None:
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if ent.expires_at < now_iso:
            return False
    return True


# Resolution order when an account holds more than one entitlement row: an
# active paid plan wins over an active comp, which wins over the signup
# trial. Rows written before the source column exists ("") are Stripe rows.
_SOURCE_PRIORITY = {"stripe": 0, "": 0, "comp": 1, "trial": 2}
_PAID_SOURCES = {"stripe", ""}

# Human plan names for the status payloads the app and portal render.
_PLAN_LABELS = {TRIAL_PLAN: "Free trial", "basic": "Cloud Basic",
                PREMIUM_PLAN: "Premium"}
NO_PLAN_LABEL = "No active plan"


def has_active_access(state: dict) -> bool:
    """One authoritative answer to "may this account use Forager right now".

    True for an active (or trialing) Stripe subscription, an unexpired
    admin-granted comp, or the running signup trial: any governing
    entitlement resolve_entitlement accepts. The AI gate and the status
    payload the app renders both read this, so a comped or trialing account
    can never pass one and fail the other.
    """
    return bool(state.get("entitled"))


def plan_label(ent, now=None) -> str:
    """The plan as a person should read it: "Complimentary" for an admin
    comp, "Trial until <date>" while the signup trial runs, the plan's
    marketing name for a paid subscription, and "No active plan" when
    nothing is active."""
    if ent is None:
        return NO_PLAN_LABEL
    if ent.source == "comp":
        return "Complimentary"
    if ent.source == "trial":
        try:
            expires = datetime.fromisoformat(ent.expires_at)
        except (TypeError, ValueError):
            return _PLAN_LABELS[TRIAL_PLAN]
        return (f"Trial until {expires.strftime('%B')} "
                f"{expires.day}, {expires.year}")
    return _PLAN_LABELS.get(ent.plan, ent.plan.title() if ent.plan
                            else "Subscribed")


def resolve_entitlement(rows, now_iso: str | None = None):
    """The entitlement row that governs the account right now, or None."""
    active = [e for e in rows if entitlement_active(e, now_iso)]
    if not active:
        return None
    return min(active, key=lambda e: _SOURCE_PRIORITY.get(e.source, 0))


def premium_active(db: Session, account_id: int, now_iso: str | None = None) -> bool:
    """Whether the account holds an active *Premium* entitlement right now.

    The gate for the cloud backup feature. It is stricter than ``entitled``:
    the signup trial (which carries the premium quota but the plan name
    "trial") and the smaller "basic" plan are NOT premium, so neither can use
    cloud backup. Only a governing entitlement whose plan is "premium" counts,
    whether it came from Stripe or an admin comp.
    """
    rows = db.query(Entitlement).filter_by(account_id=account_id).all()
    ent = resolve_entitlement(rows, now_iso)
    return ent is not None and ent.plan == PREMIUM_PLAN


def grant_trial(db: Session, account_id: int, created_at: str,
                install_key: str | None = None) -> dict:
    """The automatic signup trial: 30 days of the premium quota, expiry
    derived at creation time so no cron job is needed. Reuses the same
    expires_at machinery as comped plans.

    When ``install_key`` is given (the app's opaque per-install id), the trial
    is limited to one per install: the first account created from a given
    install claims it, and any later account created from the same install is
    refused a fresh trial so one person cannot farm trial after trial by
    signing up repeatedly. A missing key (an older app that does not send one)
    keeps the original always-grant behavior, so nothing breaks.

    Returns ``{"granted": bool, "reason": str}``. ``reason`` is a customer-ready
    sentence the caller can surface when a trial is refused (empty when
    granted). The account is created either way; a refused install simply
    starts with no trial (expired plan, zero quota) and can subscribe.
    """
    key = (install_key or "").strip()
    if key:
        # Record the claim before writing the entitlement. The unique
        # constraint on install_key is the race guard: two concurrent
        # first-claims for the same key both try to insert, one commits and one
        # raises IntegrityError, so a trial is granted exactly once. This
        # mirrors the Stripe-event idempotency fix.
        db.add(TrialClaim(install_key=key, account_id=account_id,
                          created_at=created_at))
        try:
            db.flush()
        except IntegrityError:
            # This install already spent its trial on an earlier account. Drop
            # the losing insert and grant nothing; the account still exists.
            db.rollback()
            return {"granted": False, "reason": TRIAL_ALREADY_USED_MESSAGE}
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=TRIAL_DAYS)
    db.add(Entitlement(account_id=account_id, plan=TRIAL_PLAN,
                       status="active",
                       monthly_token_quota=PLAN_QUOTAS[TRIAL_PLAN],
                       source="trial",
                       expires_at=expires.isoformat(timespec="seconds"),
                       updated_at=created_at))
    db.commit()
    return {"granted": True, "reason": ""}


def trial_days_left(ent, now=None) -> int:
    """Whole days until a trial entitlement expires (counting a partial
    day as a day, so a fresh trial reads 30 and expiry day reads 1)."""
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(ent.expires_at)
    except (TypeError, ValueError):
        return 0
    remaining = expires - now
    return max(0, remaining.days + (1 if remaining.seconds or remaining.microseconds else 0))


def quota_state(db: Session, account_id: int, mk: str) -> dict:
    """Entitlement + usage snapshot for gates and the status endpoints.

    Resolution order: active paid plan > active comp > active trial >
    nothing. With nothing active the plan reads "expired" and the quota is
    zero: Forager is trial-then-paid, there is no free tier underneath.
    "active" keeps its original meaning (an active paid entitlement
    exists); "entitled" says whether anything, trial included, is active,
    and has_active_access over this dict is the one gate every consumer
    shares. "plan_label" and "source" ride along so status payloads can
    show a person what governs their account without re-deriving it.
    """
    rows = db.query(Entitlement).filter_by(account_id=account_id).all()
    ent = resolve_entitlement(rows)
    used = month_total(db, account_id, mk)
    if ent:
        plan = ent.plan
        quota = int(ent.monthly_token_quota)
    else:
        plan = EXPIRED_PLAN
        quota = 0
    days_left = trial_days_left(ent) if ent and ent.source == "trial" else None
    return {
        "active": ent is not None and ent.source in _PAID_SOURCES,
        "entitled": ent is not None,
        "plan": plan,
        "plan_label": plan_label(ent),
        "source": (ent.source or "stripe") if ent else "",
        "trial_days_left": days_left,
        "quota": quota,
        "used": used,
        "remaining": max(0, quota - used),
        "over_quota": quota > 0 and used >= quota,
        "month": mk,
    }
