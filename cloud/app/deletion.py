"""Self-serve account deletion: the full cascade in one place.

Deleting a Forager account removes everything that identifies the person:
credentials and sessions, second-factor material, paired kitchens and their
tunnels, cloud backups (rows and files), usage history, billing rows, and
share links. Two categories deliberately survive:

- Published community recipes keep their content but are anonymized (the
  account link nulled, the attribution rewritten), the same way published
  content normally outlives its author.
- trial_claims (keyed to the install, not the person) and admin_actions
  (the audit trail, including the row recording this deletion) stay; both
  reference the account by bare integer, never by email.

Billing comes first and is the one step that can refuse: any live Stripe
subscription is cancelled immediately, and if Stripe cannot confirm the
cancellation the deletion aborts, so a deleted account can never keep
paying. All row deletions happen in one transaction; backup files on disk
are removed only after that transaction commits, so a crash mid-way never
strands rows pointing at deleted files.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from . import stripe_api
from .config import settings
from .deps import utc_now_iso
from .models import (Account, AdminAction, AuthSession, CloudBackup,
                     CommunityRecipe, EmailToken, Entitlement, Instance,
                     PairingCode, RecipeRating, RecipeReport, RecoveryCode,
                     SharedRecipe, SharedRecipeReport, Subscription,
                     TotpChallenge, TunnelPeer, UsageLedger,
                     WebAuthnChallenge, WebAuthnCredential)

logger = logging.getLogger("forager.deletion")

# What the confirm page and the API tell the owner when Stripe cannot
# confirm the cancellation: the account is left untouched rather than
# deleted with a live subscription still billing.
BILLING_ABORT_MESSAGE = (
    "Your subscription could not be cancelled right now, so nothing was "
    "deleted. Please try again in a few minutes, or contact "
    "support@pantryraider.app and we will take care of it.")

# The attribution a deleted member's community recipes carry from then on.
ANONYMOUS_ATTRIBUTION = "a former member"

# Stripe statuses with nothing left to cancel.
_DEAD_SUB_STATUSES = {"canceled", "incomplete_expired", ""}


class DeletionBlocked(Exception):
    """The deletion could not proceed safely; nothing was changed. The
    message is user-facing."""


def _cancel_billing(db: Session, account_id: int) -> None:
    """Cancel every live Stripe subscription immediately, or refuse.

    Immediate (not at period end) on purpose: an account that no longer
    exists cannot use the remaining period, and the industry norm for
    deletion is to stop billing on the spot. Aborts the whole deletion when
    Stripe cannot confirm, so a paying subscription is never orphaned."""
    subs = db.query(Subscription).filter_by(account_id=account_id).all()
    for sub in subs:
        if not sub.stripe_subscription_id or sub.status in _DEAD_SUB_STATUSES:
            continue
        try:
            stripe_api.cancel_now(sub.stripe_subscription_id)
        except stripe_api.StripeApiError as exc:
            raise DeletionBlocked(BILLING_ABORT_MESSAGE) from exc
        sub.status = "canceled"
        sub.cancel_at_period_end = 0
        sub.updated_at = utc_now_iso()


def _anonymize_community_recipes(db: Session, account_id: int) -> int:
    """Keep published recipe content, sever the link to the person."""
    now = utc_now_iso()
    recipes = (db.query(CommunityRecipe)
               .filter_by(submitter_account_id=account_id).all())
    for recipe in recipes:
        recipe.submitter_account_id = None
        recipe.attribution = ANONYMOUS_ATTRIBUTION
        recipe.updated_at = now
    return len(recipes)


def _delete_ratings_and_reports(db: Session, account_id: int) -> None:
    """Remove the account's ratings and flags, keeping each recipe's
    denormalized totals honest by recomputing them from what remains."""
    rated_ids = {r.recipe_id for r in
                 db.query(RecipeRating).filter_by(account_id=account_id)}
    db.query(RecipeRating).filter_by(account_id=account_id).delete()
    for recipe_id in rated_ids:
        recipe = db.get(CommunityRecipe, recipe_id)
        if not recipe:
            continue
        rows = db.query(RecipeRating).filter_by(recipe_id=recipe_id).all()
        recipe.rating_count = len(rows)
        recipe.rating_sum = sum(int(r.stars or 0) for r in rows)
    flagged_ids = {r.recipe_id for r in
                   db.query(RecipeReport).filter_by(account_id=account_id)}
    db.query(RecipeReport).filter_by(account_id=account_id).delete()
    for recipe_id in flagged_ids:
        recipe = db.get(CommunityRecipe, recipe_id)
        if recipe:
            recipe.report_count = (db.query(RecipeReport)
                                   .filter_by(recipe_id=recipe_id).count())


def _delete_shares(db: Session, account_id: int) -> None:
    """Remove the account's share links (revoking them for anyone holding
    the URL), their reports, the account's own flags elsewhere, and its
    address on shares other members aimed at it."""
    own_ids = [s.id for s in db.query(SharedRecipe)
               .filter_by(owner_account_id=account_id)]
    if own_ids:
        (db.query(SharedRecipeReport)
         .filter(SharedRecipeReport.share_id.in_(own_ids))
         .delete(synchronize_session=False))
        (db.query(SharedRecipe)
         .filter(SharedRecipe.id.in_(own_ids))
         .delete(synchronize_session=False))
    # Flags this member filed on other people's shares carry their account
    # id in reporter_key; those go too. The shares' report_count is a
    # running tally of distinct reporters and stays as applied.
    (db.query(SharedRecipeReport)
     .filter_by(reporter_key=f"acct:{account_id}")
     .delete(synchronize_session=False))
    for share in (db.query(SharedRecipe)
                  .filter_by(recipient_account_id=account_id).all()):
        share.recipient_account_id = None


def _collect_backup_files(db: Session, account_id: int) -> list[Path]:
    base = Path(settings.backup_storage_dir) / str(int(account_id))
    return [base / row.filename
            for row in db.query(CloudBackup).filter_by(account_id=account_id)
            if row.filename]


def delete_account(db: Session, account: Account) -> None:
    """Delete the account and everything tied to it. Raises DeletionBlocked
    (and changes nothing) when a live subscription cannot be cancelled.

    The caller has already re-authenticated the owner; this only does the
    work. Tunnel teardown talks to the VPS agent (best-effort, outside the
    transaction); every row change below rides one transaction, and backup
    files are unlinked only after it commits."""
    account_id = account.id

    # 1. Stop the money first; a refusal here aborts before anything changes.
    _cancel_billing(db, account_id)
    # Write the status update out now: the bulk deletes below bypass the
    # session, and a dirty Subscription object flushing after its row is
    # already gone would fail the commit.
    db.flush()

    # 2. Tear down remote-access tunnels (network calls, own commits).
    #    Local import: routers import this module's neighbors, so importing a
    #    router at module load would be a cycle.
    from .routers.tunnel import disable_tunnel_for_account
    disable_tunnel_for_account(db, account_id)

    # 3. Everything row-shaped, one transaction.
    backup_files = _collect_backup_files(db, account_id)
    _anonymize_community_recipes(db, account_id)
    _delete_ratings_and_reports(db, account_id)
    _delete_shares(db, account_id)
    for model in (AuthSession, EmailToken, RecoveryCode, TotpChallenge,
                  WebAuthnCredential, PairingCode, CloudBackup, UsageLedger,
                  Subscription, Entitlement, TunnelPeer):
        db.query(model).filter_by(account_id=account_id).delete(
            synchronize_session=False)
    # WebAuthn challenges carry the account as a bare integer (no FK).
    db.query(WebAuthnChallenge).filter_by(account_id=account_id).delete(
        synchronize_session=False)
    db.query(Instance).filter_by(account_id=account_id).delete(
        synchronize_session=False)
    db.delete(db.get(Account, account_id))
    # The audit record of the deletion itself: the bare account id and the
    # fact, no email and no personal payload. trial_claims stay untouched so
    # the install cannot mint a fresh trial.
    db.add(AdminAction(admin_email="", action="account-delete",
                       account_id=account_id, detail="self-serve",
                       created_at=utc_now_iso()))
    db.commit()

    # 4. Files last, after the rows are truly gone.
    for path in backup_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # a stubborn file must not resurrect the account
    try:
        (Path(settings.backup_storage_dir) / str(int(account_id))).rmdir()
    except OSError:
        pass  # missing or not empty; either way nothing personal remains

    logger.info("Account %s deleted (self-serve): %d backup files removed",
                account_id, len(backup_files))
