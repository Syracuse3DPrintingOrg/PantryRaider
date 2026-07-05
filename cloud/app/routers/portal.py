"""The Forager web portal: server-rendered pages for people.

The JSON API under /v1 is what the app talks to; these routes are the
human side (landing page, signup, login, account). Same accounts and the
same session tokens as the bearer flow, carried in an HttpOnly cookie so
a browser can hold one. The cookie is SameSite=Lax, which keeps
cross-site form posts out of the state-changing routes here.

Copy on these pages is written for someone who is not technical. The
portal says "kitchen" where the API says instance, and never mentions
tokens or APIs; removing a kitchen quietly revokes its credential.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import ratelimit, usage
from ..config import settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, SESSION_COOKIE, client_ip,
                    cookie_account, get_db, is_admin, utc_now_iso)
from ..models import Account, AuthSession, Instance, UsageLedger
from ..security import (email_is_disposable, hash_password, password_problem,
                        token_hash, verify_password)
from .accounts import (DISPOSABLE_EMAIL_MESSAGE, _issue_session, _valid_email,
                       authenticate)
from .oauth_google import enabled as google_enabled

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Plans as people see them. Keys are the internal plan names in config,
# plus the "expired" state quota_state reports when nothing is active.
PLAN_LABELS = {"trial": "Free trial", "basic": "Cloud Basic",
               "premium": "Premium", "expired": "Trial ended"}


def checkout_options() -> list[dict]:
    """The plan buttons the account page can honestly offer: one per
    configured Stripe Checkout link, or a single Subscribe button when only
    the plain link is set. Empty means billing is not live yet."""
    per_price = [
        {"label": "Cloud Basic, $10 a year",
         "url": settings.stripe_checkout_url_basic_year},
        {"label": "Premium, $3 a month",
         "url": settings.stripe_checkout_url_premium_month},
        {"label": "Premium, $30 a year",
         "url": settings.stripe_checkout_url_premium_year},
    ]
    options = [o for o in per_price if o["url"]]
    if not options and settings.stripe_checkout_url:
        options = [{"label": "Subscribe", "url": settings.stripe_checkout_url}]
    return options

# Post/redirect/get notices for the account page, keyed by the ?m= code so
# a refresh never re-submits a form.
_NOTICES = {
    "password-changed": "Your password has been updated.",
    "password-set": "Your password is set. You can now log in with it too.",
    "kitchen-removed": "Kitchen removed. That device can no longer use your account.",
}
_ACCOUNT_ERRORS = {
    "password-wrong": "Your current password did not match.",
    "password-weak": "Your new password is too short or too easy to guess.",
    "password-mismatch": "The new passwords did not match.",
}


def _client(request: Request) -> str:
    return client_ip(request)


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _start_session(db: Session, account_id: int) -> RedirectResponse:
    """Log the browser in: mint a session and carry it in the cookie."""
    token = _issue_session(db, account_id)
    resp = RedirectResponse("/account", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token,
                    max_age=settings.session_ttl_hours * 3600,
                    httponly=True, samesite="lax",
                    secure=settings.cookie_secure)
    return resp


@router.get("/")
def landing(request: Request, account: Account | None = Depends(cookie_account)):
    return templates.TemplateResponse(request, "landing.html",
                                      {"signed_in": account is not None,
                                       "is_admin": is_admin(account)})


@router.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html",
                                      {"google": google_enabled()})


@router.post("/signup")
def signup_submit(request: Request,
                  email: str = Form(""),
                  password: str = Form(""),
                  confirm_password: str = Form(""),
                  website: str = Form(""),
                  db: Session = Depends(get_db)):
    def retry(error: str, status: int = 400):
        return templates.TemplateResponse(
            request, "signup.html",
            {"error": error, "email": email, "google": google_enabled()},
            status_code=status)

    # Honeypot: a hidden field no person can see, so only a form-stuffing bot
    # fills it. Refuse with a generic error and create nothing, without
    # hinting that the trap was tripped.
    if website.strip():
        return retry("Something went wrong. Please try again.")
    if not ratelimit.allow(f"signup:{_client(request)}",
                           settings.signup_rate_per_minute):
        return retry("Too many attempts. Wait a minute and try again.", 429)
    email = email.strip().lower()
    if not _valid_email(email):
        return retry("Enter a valid email address.")
    if email_is_disposable(email):
        return retry(DISPOSABLE_EMAIL_MESSAGE)
    problem = password_problem(password, email)
    if problem:
        return retry(problem)
    if password != confirm_password:
        return retry("The passwords did not match.")
    if db.query(Account).filter_by(email=email).first():
        return retry("An account with that email already exists. "
                     "Try logging in instead.", 409)
    account = Account(email=email, password_hash=hash_password(password),
                      created_at=utc_now_iso())
    db.add(account)
    db.commit()
    # The 30-day trial starts the moment the account exists.
    usage.grant_trial(db, account.id, account.created_at)
    return _start_session(db, account.id)


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html",
                                      {"google": google_enabled()})


@router.post("/login")
def login_submit(request: Request,
                 email: str = Form(""),
                 password: str = Form(""),
                 website: str = Form(""),
                 db: Session = Depends(get_db)):
    def retry(error: str, status: int = 401):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": error, "email": email, "google": google_enabled()},
            status_code=status)

    # Honeypot: only a bot fills the hidden field. Refuse with the same
    # generic failure a wrong password shows, authenticating no one.
    if website.strip():
        return retry("That email and password did not match.")
    if not ratelimit.allow(f"login:{_client(request)}",
                           settings.login_rate_per_minute):
        return retry("Too many attempts. Wait a minute and try again.", 429)
    account, locked = authenticate(db, email, password)
    if locked:
        return retry(locked, 429)
    if not account:
        # One message for both cases, so login does not confirm which
        # emails have accounts.
        return retry("That email and password did not match.")
    if account.disabled:
        return retry(ACCOUNT_DISABLED_MESSAGE, 403)
    return _start_session(db, account.id)


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        db.query(AuthSession).filter_by(token_hash=token_hash(token)).delete()
        db.commit()
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/account")
def account_page(request: Request,
                 account: Account | None = Depends(cookie_account),
                 db: Session = Depends(get_db)):
    if not account:
        return _login_redirect()
    mk = usage.month_key()
    state = usage.quota_state(db, account.id, mk)
    scans = (db.query(UsageLedger)
             .filter_by(account_id=account.id, month_key=mk).count())
    kitchens = (db.query(Instance).filter_by(account_id=account.id)
                .order_by(Instance.created_at).all())
    percent = 0
    if state["quota"] > 0:
        percent = min(100, round(state["used"] * 100 / state["quota"]))
    return templates.TemplateResponse(request, "account.html", {
        "signed_in": True,
        "is_admin": is_admin(account),
        "email": account.email,
        "plan": state["plan"],
        "plan_label": PLAN_LABELS.get(state["plan"], state["plan"].title()),
        "plan_active": state["active"],
        "entitled": state["entitled"],
        "trial_days_left": state["trial_days_left"],
        "percent": percent,
        "over_quota": state["over_quota"],
        "scans": scans,
        "kitchens": kitchens,
        "checkout_options": checkout_options(),
        "manage_url": settings.stripe_checkout_url,
        "has_password": bool(account.password_hash),
        "notice": _NOTICES.get(request.query_params.get("m", "")),
        "error": _ACCOUNT_ERRORS.get(request.query_params.get("e", "")),
    })


@router.post("/account/password")
def change_password(request: Request,
                    current_password: str = Form(""),
                    new_password: str = Form(""),
                    confirm_password: str = Form(""),
                    account: Account | None = Depends(cookie_account),
                    db: Session = Depends(get_db)):
    if not account:
        return _login_redirect()

    def back(code: str, param: str = "e"):
        return RedirectResponse(f"/account?{param}={code}", status_code=303)

    # An account created with Google sign-in has no password yet; setting
    # its first one has no current password to check.
    had_password = bool(account.password_hash)
    if had_password and not verify_password(current_password,
                                            account.password_hash):
        return back("password-wrong")
    if password_problem(new_password, account.email):
        return back("password-weak")
    if new_password != confirm_password:
        return back("password-mismatch")
    account.password_hash = hash_password(new_password)
    db.commit()
    return back("password-changed" if had_password else "password-set", "m")


@router.post("/account/kitchens/{kitchen_id}/remove")
def remove_kitchen(kitchen_id: int, request: Request,
                   account: Account | None = Depends(cookie_account),
                   db: Session = Depends(get_db)):
    """Delete the instance row, which revokes its credential: the next
    request that device makes is refused. Scoped to the signed-in account,
    so nobody can remove someone else's kitchen by guessing ids."""
    if not account:
        return _login_redirect()
    inst = db.query(Instance).filter_by(id=kitchen_id,
                                        account_id=account.id).first()
    if inst:
        db.delete(inst)
        db.commit()
    return RedirectResponse("/account?m=kitchen-removed", status_code=303)
