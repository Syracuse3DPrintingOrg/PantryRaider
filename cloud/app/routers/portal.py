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

import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import ratelimit, usage
from ..config import CLOUD_VERSION, settings
from ..deps import (ACCOUNT_DISABLED_MESSAGE, SESSION_COOKIE, client_ip,
                    cookie_account, get_db, is_admin, utc_now_iso)
from ..email import base_url, email_configured, send_email
from ..models import (Account, AuthSession, CommunityRecipe, EmailToken,
                      Instance, RecoveryCode, TotpChallenge, UsageLedger)
from ..security import (email_is_disposable, generate_totp_secret,
                        hash_password, new_token, otpauth_uri,
                        password_problem, token_hash, totp_verify,
                        verify_password)
from .accounts import (DISPOSABLE_EMAIL_MESSAGE, _issue_session, _valid_email,
                       authenticate, consume_totp, replace_recovery_codes)
from .. import turnstile
from .oauth_google import enabled as google_enabled

router = APIRouter(include_in_schema=False)

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Plans as people see them. Keys are the internal plan names in config,
# plus the "expired" state quota_state reports when nothing is active.
PLAN_LABELS = {"trial": "Free trial", "basic": "Cloud Basic",
               "premium": "Premium", "expired": "Trial ended"}


def _with_ref(url: str, account_id: int) -> str:
    """Append the account id as Stripe's client_reference_id so the webhook
    (checkout.session.completed) can attribute the payment to this account,
    plus prefilled_email so the customer is recognized. Stripe Payment Links
    pass both query params straight through to the session."""
    if not url or not account_id:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}client_reference_id={account_id}"


def checkout_options(account_id: int = 0) -> list[dict]:
    """The plan buttons the account page can honestly offer: one per
    configured Stripe Checkout link, or a single Subscribe button when only
    the plain link is set. Each link carries the account's client_reference_id
    so the webhook knows who paid. Empty means billing is not live yet."""
    per_price = [
        {"label": "Cloud Basic, $10 a year",
         "url": settings.stripe_checkout_url_basic_year},
        {"label": "Premium, $3 a month",
         "url": settings.stripe_checkout_url_premium_month},
        {"label": "Premium, $30 a year",
         "url": settings.stripe_checkout_url_premium_year},
    ]
    options = [{"label": o["label"], "url": _with_ref(o["url"], account_id)}
               for o in per_price if o["url"]]
    if not options and settings.stripe_checkout_url:
        options = [{"label": "Subscribe",
                    "url": _with_ref(settings.stripe_checkout_url, account_id)}]
    return options

# Post/redirect/get notices for the account page, keyed by the ?m= code so
# a refresh never re-submits a form.
_NOTICES = {
    "password-changed": "Your password has been updated.",
    "password-set": "Your password is set. You can now log in with it too.",
    "kitchen-removed": "Kitchen removed. That device can no longer use your account.",
    "verification-sent": "Verification email on its way. Check your inbox.",
    "twofa-disabled": "Two-factor authentication is off.",
}
_ACCOUNT_ERRORS = {
    "password-wrong": "Your current password did not match.",
    "password-weak": "Your new password is too short or too easy to guess.",
    "password-mismatch": "The new passwords did not match.",
    "verify-throttled": "You just asked for a verification email. Give it a "
                        "minute before trying again.",
    "twofa-bad": "That code or password did not match. Two-factor "
                 "authentication is still on.",
}
# Notices shown on the login page after a redirect there, keyed by ?m=.
_LOGIN_NOTICES = {
    "password-reset": "Your password has been reset. Log in with your new "
                      "password.",
    "google-no-account": "No Forager account was found for that Google "
                         "address. Use Sign up below to create one first.",
}


def _client(request: Request) -> str:
    return client_ip(request)


def _mint_email_token(db: Session, account_id: int, purpose: str) -> str:
    """Create a single-use email token and return the plaintext to link.

    Stored hashed with a purpose tag and an expiry; the same table backs both
    password resets and verification (see EmailToken)."""
    token = new_token("prv" if purpose == "verify" else "prr")
    if purpose == "verify":
        ttl = timedelta(days=settings.email_verify_ttl_days)
    else:
        ttl = timedelta(hours=settings.password_reset_ttl_hours)
    expires = datetime.now(timezone.utc) + ttl
    db.add(EmailToken(token_hash=token_hash(token), account_id=account_id,
                      purpose=purpose,
                      expires_at=expires.isoformat(timespec="seconds"),
                      created_at=utc_now_iso()))
    db.commit()
    return token


def _valid_email_token(db: Session, token: str, purpose: str) -> EmailToken | None:
    """The live token row for this plaintext and purpose, or None if unknown,
    already used, or expired. Does not mark it used; the caller does that once
    the action succeeds."""
    if not token:
        return None
    row = db.query(EmailToken).filter_by(
        token_hash=token_hash(token), purpose=purpose).first()
    if not row or row.used or row.expires_at < utc_now_iso():
        return None
    return row


def send_verification(db: Session, account: Account) -> bool:
    """Email a fresh verification link, if outgoing email is configured.

    Called at signup and from the account page's resend button. Silent no-op
    (returns False) when email is dark, so a signup never depends on it."""
    if not email_configured():
        return False
    token = _mint_email_token(db, account.id, "verify")
    link = f"{base_url()}/verify?token={token}"
    text = (
        "Welcome to Forager!\n\n"
        "Please confirm this is your email address by opening this link:\n"
        f"{link}\n\n"
        "If you did not create a Forager account, you can ignore this email."
    )
    return send_email(account.email, "Confirm your email for Forager", text)


def _send_password_reset(db: Session, account: Account) -> bool:
    """Email a fresh password-reset link. Assumes the caller already checked
    email_configured()."""
    token = _mint_email_token(db, account.id, "reset")
    link = f"{base_url()}/reset?token={token}"
    text = (
        "We got a request to reset the password for your Forager account.\n\n"
        "Choose a new password here:\n"
        f"{link}\n\n"
        f"This link works for the next hour and can be used once. If you did "
        "not ask to reset your password, you can ignore this email and your "
        "password stays the same."
    )
    return send_email(account.email, "Reset your Forager password", text)


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


# --- Two-factor sign-in (TOTP) ---

# The half-finished-login cookie: it names a pending TotpChallenge row and
# nothing more, so on its own it cannot reach any signed-in page. Short-lived
# on purpose; a user who wanders off is never logged in.
TOTP_PENDING_COOKIE = "forager_totp_pending"
_TOTP_CHALLENGE_TTL_MINUTES = 10


def _totp_qr_svg(uri: str) -> str:
    """Render an otpauth URI as an inline SVG QR code (no Pillow needed, so
    nothing heavy rides along just to draw a square)."""
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")


def _issue_totp_challenge(db: Session, account_id: int, return_url: str = "",
                          device_name: str = "") -> RedirectResponse:
    """Password (or Google) was right, but the account has 2FA on: stash a
    short-lived pending challenge and send the browser to the code page. No
    real session exists until a correct code redeems it.

    return_url and device_name are set only for the Google app-return flow
    (flow=app): when present, redeeming the code sends the browser back to the
    app with a provision code instead of starting a portal session."""
    token = new_token("prt")
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=_TOTP_CHALLENGE_TTL_MINUTES)
    db.add(TotpChallenge(token_hash=token_hash(token), account_id=account_id,
                         expires_at=expires.isoformat(timespec="seconds"),
                         created_at=utc_now_iso(),
                         return_url=return_url or "",
                         device_name=(device_name or "")[:120]))
    db.commit()
    resp = RedirectResponse("/login/2fa", status_code=303)
    resp.set_cookie(TOTP_PENDING_COOKIE, token,
                    max_age=_TOTP_CHALLENGE_TTL_MINUTES * 60,
                    httponly=True, samesite="lax", secure=settings.cookie_secure)
    return resp


def _pending_challenge(db: Session, request: Request) -> TotpChallenge | None:
    """The live pending challenge behind the cookie, or None when missing,
    expired, or unknown."""
    token = request.cookies.get(TOTP_PENDING_COOKIE, "")
    if not token:
        return None
    row = db.query(TotpChallenge).filter_by(token_hash=token_hash(token)).first()
    if not row or row.expires_at < utc_now_iso():
        return None
    return row


@router.get("/")
def landing(request: Request, account: Account | None = Depends(cookie_account)):
    return templates.TemplateResponse(request, "landing.html",
                                      {"signed_in": account is not None,
                                       "is_admin": is_admin(account)})


@router.get("/pricing")
def pricing_page(request: Request,
                 account: Account | None = Depends(cookie_account)):
    """The plans as a real page of their own, so the marketing content is
    navigable and links cleanly from the header and footer."""
    return templates.TemplateResponse(request, "pricing.html",
                                      {"signed_in": account is not None,
                                       "is_admin": is_admin(account)})


@router.get("/features")
def features_page(request: Request,
                  account: Account | None = Depends(cookie_account)):
    """How Forager works, plus what each plan turns on. Reuses the landing
    sections."""
    return templates.TemplateResponse(request, "features.html",
                                      {"signed_in": account is not None,
                                       "is_admin": is_admin(account)})


@router.get("/status")
def status_page(request: Request,
                account: Account | None = Depends(cookie_account)):
    """A public-safe health page: the service is up and which version is
    live. Deliberately free of any account or usage data, so it is safe to
    show signed out."""
    return templates.TemplateResponse(request, "status.html",
                                      {"signed_in": account is not None,
                                       "is_admin": is_admin(account),
                                       "version": CLOUD_VERSION})


@router.get("/signup")
def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html",
                                      {"google": google_enabled(),
                                       "turnstile_site_key": settings.turnstile_site_key
                                       if turnstile.enabled() else "",
                                       # Carried from the app's signup link into
                                       # a hidden field so the one-trial-per-
                                       # install rule can see this install.
                                       "install_key": request.query_params.get(
                                           "install_key", "")})


@router.post("/signup")
def signup_submit(request: Request,
                  email: str = Form(""),
                  password: str = Form(""),
                  confirm_password: str = Form(""),
                  website: str = Form(""),
                  install_key: str = Form(""),
                  cf_turnstile_response: str = Form("", alias="cf-turnstile-response"),
                  db: Session = Depends(get_db)):
    def retry(error: str, status: int = 400):
        return templates.TemplateResponse(
            request, "signup.html",
            {"error": error, "email": email, "google": google_enabled(),
             "install_key": install_key,
             "turnstile_site_key": settings.turnstile_site_key
             if turnstile.enabled() else ""},
            status_code=status)

    # Honeypot: a hidden field no person can see, so only a form-stuffing bot
    # fills it. Refuse with a generic error and create nothing, without
    # hinting that the trap was tripped.
    if website.strip():
        return retry("Something went wrong. Please try again.")
    # Signup fails closed: if the human-check cannot be verified (Cloudflare
    # unreachable), block rather than wave a possible bot through to a new
    # account. A real person can try again in a moment.
    if not turnstile.verify(cf_turnstile_response, _client(request),
                            fail_open=False):
        return retry("Please complete the challenge and try again.")
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
    # The 30-day trial starts the moment the account exists, limited to one per
    # install when the app forwarded its install key on the signup link.
    usage.grant_trial(db, account.id, account.created_at, install_key=install_key)
    # A confirmation email if outgoing mail is set up; signup does not wait on
    # it and never fails because of it.
    send_verification(db, account)
    return _start_session(db, account.id)


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html",
        {"google": google_enabled(),
         "email_enabled": email_configured(),
         "notice": _LOGIN_NOTICES.get(request.query_params.get("m", ""))})


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
    if account.totp_enabled:
        # The password was right, but 2FA gates the session: no cookie yet.
        return _issue_totp_challenge(db, account.id)
    return _start_session(db, account.id)


@router.get("/login/2fa")
def login_2fa_page(request: Request, db: Session = Depends(get_db)):
    """The second-factor prompt, reached only after a correct password when
    the account has 2FA on. Without a live pending challenge it just sends the
    browser back to the start of login."""
    if not _pending_challenge(db, request):
        return _login_redirect()
    return templates.TemplateResponse(request, "login_2fa.html", {})


@router.post("/login/2fa")
def login_2fa_submit(request: Request, code: str = Form(""),
                     db: Session = Depends(get_db)):
    def retry(error: str, status: int = 401):
        return templates.TemplateResponse(
            request, "login_2fa.html", {"error": error}, status_code=status)

    challenge = _pending_challenge(db, request)
    if not challenge:
        return _login_redirect()
    account = db.get(Account, challenge.account_id)
    if not account or account.disabled:
        return _login_redirect()
    # Rate-limit both the source IP and the target account, so neither a
    # single IP nor a run at one account can grind through codes.
    if (not ratelimit.allow(f"login2fa-ip:{_client(request)}",
                            settings.login_rate_per_minute)
            or not ratelimit.allow(f"login2fa-acct:{account.id}",
                                   settings.login_rate_per_minute)):
        return retry("Too many attempts. Wait a minute and try again.", 429)
    if not consume_totp(db, account, code):
        # One generic failure for a wrong TOTP code or a wrong/spent recovery
        # code; it never says which, or whether the account exists.
        return retry("That code did not match. Try again.")
    # Correct. An app-return challenge (Google flow=app on a 2FA account) sends
    # the browser back to the app with a fresh provision code, not into a portal
    # session. A plain portal login gets the real session.
    # device_name rides along in the challenge for symmetry with the cookie
    # state; the app still names its own kitchen when it redeems the code.
    return_url = (challenge.return_url or "").strip()
    db.delete(challenge)
    db.commit()
    from .oauth_google import _mint_provision_code, _safe_return_url  # avoids a cycle
    if return_url and _safe_return_url(return_url):
        provision_code = _mint_provision_code(db, account.id)
        sep = "&" if "?" in return_url else "?"
        resp = RedirectResponse(f"{return_url}{sep}code={provision_code}",
                                status_code=303)
        resp.delete_cookie(TOTP_PENDING_COOKIE)
        return resp
    resp = _start_session(db, account.id)
    resp.delete_cookie(TOTP_PENDING_COOKIE)
    return resp


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
    # The account's own community submissions, newest first, shown back to
    # them with a friendly status label so they can see what has been shared
    # and what is still waiting on review.
    _status_labels = {"approved": "Shared", "pending": "Pending review",
                      "hidden": "Hidden", "rejected": "Not accepted"}
    my_recipes = [
        {"title": r.title, "status": r.status,
         "status_label": _status_labels.get(r.status, r.status.title())}
        for r in (db.query(CommunityRecipe)
                  .filter_by(submitter_account_id=account.id)
                  .order_by(CommunityRecipe.created_at.desc()).all())]
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
        "checkout_options": checkout_options(account.id),
        "manage_url": settings.stripe_portal_url,
        "has_password": bool(account.password_hash),
        "google_linked": account.auth_provider == "google",
        "recipe_upload_authorized": bool(account.recipe_upload_authorized),
        "my_recipes": my_recipes,
        "totp_enabled": bool(account.totp_enabled),
        # The verify banner only shows when email is set up and this address
        # is not confirmed yet; it is purely advisory, nothing is blocked.
        "email_enabled": email_configured(),
        "email_verified": bool(account.email_verified),
        "notice": _NOTICES.get(request.query_params.get("m", "")),
        "error": _ACCOUNT_ERRORS.get(request.query_params.get("e", "")),
    })


@router.post("/account/verify/resend")
def resend_verification(request: Request,
                        account: Account | None = Depends(cookie_account),
                        db: Session = Depends(get_db)):
    """Send another verification email to the signed-in account. Rate-limited
    per account, a no-op when email is off or the address is already
    confirmed."""
    if not account:
        return _login_redirect()
    if not email_configured() or account.email_verified:
        return RedirectResponse("/account", status_code=303)
    if not ratelimit.allow(f"verify-resend:{account.id}",
                           settings.resend_verification_rate_per_minute):
        return RedirectResponse("/account?e=verify-throttled", status_code=303)
    send_verification(db, account)
    return RedirectResponse("/account?m=verification-sent", status_code=303)


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


@router.get("/account/2fa/setup")
def totp_setup_page(request: Request,
                    account: Account | None = Depends(cookie_account),
                    db: Session = Depends(get_db)):
    """Show the enrollment step: a QR code (and the secret to type by hand)
    plus a field to confirm the first code. Nothing is saved here, so leaving
    the page half-done never touches the account."""
    if not account:
        return _login_redirect()
    if account.totp_enabled:
        return RedirectResponse("/account", status_code=303)
    secret = generate_totp_secret()
    uri = otpauth_uri(secret, account.email)
    return templates.TemplateResponse(request, "totp_setup.html", {
        "secret": secret,
        "qr_svg": _totp_qr_svg(uri),
    })


@router.post("/account/2fa/enable")
def totp_enable(request: Request, secret: str = Form(""), code: str = Form(""),
                account: Account | None = Depends(cookie_account),
                db: Session = Depends(get_db)):
    """Confirm the first code against the pending secret. Only a correct code
    turns 2FA on, saves the secret, and reveals the recovery codes once."""
    if not account:
        return _login_redirect()
    if account.totp_enabled:
        return RedirectResponse("/account", status_code=303)

    def retry(error: str):
        # Keep the same pending secret so the QR the person already scanned
        # stays valid; a wrong code never resets the setup.
        return templates.TemplateResponse(request, "totp_setup.html", {
            "secret": secret,
            "qr_svg": _totp_qr_svg(otpauth_uri(secret, account.email)),
            "error": error,
        }, status_code=400)

    if not secret:
        return RedirectResponse("/account", status_code=303)
    if not totp_verify(secret, code):
        return retry("That code did not match. Check your authenticator app "
                     "and try again.")
    account.totp_secret = secret
    account.totp_enabled = 1
    db.commit()
    codes = replace_recovery_codes(db, account.id)
    return templates.TemplateResponse(request, "totp_recovery.html", {
        "codes": codes, "regenerated": False,
    })


@router.post("/account/2fa/disable")
def totp_disable(request: Request, credential: str = Form(""),
                 account: Account | None = Depends(cookie_account),
                 db: Session = Depends(get_db)):
    """Turn 2FA off. Requires proof it is really the owner: a current code
    from the authenticator app (or a recovery code), or the account password.
    A Google account with no password confirms with a code."""
    if not account:
        return _login_redirect()
    if not account.totp_enabled:
        return RedirectResponse("/account", status_code=303)
    credential = credential.strip()
    ok = bool(account.password_hash) and verify_password(
        credential, account.password_hash)
    if not ok:
        ok = consume_totp(db, account, credential)
    if not ok:
        return RedirectResponse("/account?e=twofa-bad", status_code=303)
    account.totp_enabled = 0
    account.totp_secret = ""
    db.query(RecoveryCode).filter_by(account_id=account.id).delete()
    db.commit()
    return RedirectResponse("/account?m=twofa-disabled", status_code=303)


@router.post("/account/2fa/recovery/regenerate")
def totp_regenerate_recovery(request: Request,
                             account: Account | None = Depends(cookie_account),
                             db: Session = Depends(get_db)):
    """Mint a fresh set of recovery codes and invalidate the old set. Shown
    once, like at enrollment."""
    if not account:
        return _login_redirect()
    if not account.totp_enabled:
        return RedirectResponse("/account", status_code=303)
    codes = replace_recovery_codes(db, account.id)
    return templates.TemplateResponse(request, "totp_recovery.html", {
        "codes": codes, "regenerated": True,
    })


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


# --- Forgotten password ---

@router.get("/forgot")
def forgot_page(request: Request):
    return templates.TemplateResponse(request, "forgot.html", {"sent": False})


@router.post("/forgot")
def forgot_submit(request: Request,
                  email: str = Form(""),
                  website: str = Form(""),
                  db: Session = Depends(get_db)):
    """Start a password reset. Always answers the same, whether or not the
    email belongs to an account, so the page never reveals who has signed up.
    A reset link is only actually sent for a real account when email is set
    up."""
    def sent():
        return templates.TemplateResponse(request, "forgot.html", {"sent": True})

    # Honeypot: a bot fills the hidden field. Answer like a normal success and
    # send nothing.
    if website.strip():
        return sent()
    if not ratelimit.allow(f"forgot:{_client(request)}",
                           settings.forgot_rate_per_minute):
        return templates.TemplateResponse(
            request, "forgot.html",
            {"sent": False,
             "error": "Too many attempts. Wait a minute and try again."},
            status_code=429)
    addr = email.strip().lower()
    account = db.query(Account).filter_by(email=addr).first()
    if account and email_configured():
        _send_password_reset(db, account)
    return sent()


@router.get("/reset")
def reset_page(request: Request, token: str = "",
               db: Session = Depends(get_db)):
    row = _valid_email_token(db, token, "reset")
    return templates.TemplateResponse(request, "reset.html",
                                      {"token": token, "valid": bool(row)})


@router.post("/reset")
def reset_submit(request: Request,
                 token: str = Form(""),
                 new_password: str = Form(""),
                 confirm_password: str = Form(""),
                 db: Session = Depends(get_db)):
    row = _valid_email_token(db, token, "reset")
    if not row:
        return templates.TemplateResponse(request, "reset.html",
                                          {"token": token, "valid": False})

    def retry(error: str):
        return templates.TemplateResponse(
            request, "reset.html",
            {"token": token, "valid": True, "error": error}, status_code=400)

    account = db.get(Account, row.account_id)
    if not account:
        return templates.TemplateResponse(request, "reset.html",
                                          {"token": token, "valid": False})
    problem = password_problem(new_password, account.email)
    if problem:
        return retry(problem)
    if new_password != confirm_password:
        return retry("The passwords did not match.")
    # Set the new password (a Google-only account gets its first one this way),
    # burn the token, and sign out every existing session: a reset must lock
    # out whoever might have been in the account.
    account.password_hash = hash_password(new_password)
    row.used = 1
    db.query(AuthSession).filter_by(account_id=account.id).delete()
    db.commit()
    return RedirectResponse("/login?m=password-reset", status_code=303)


# --- Email verification ---

@router.get("/verify")
def verify_page(request: Request, token: str = "",
                account: Account | None = Depends(cookie_account),
                db: Session = Depends(get_db)):
    row = _valid_email_token(db, token, "verify")
    ctx = {"signed_in": account is not None, "is_admin": is_admin(account)}
    if not row:
        return templates.TemplateResponse(request, "verify.html",
                                          {**ctx, "verified": False})
    target = db.get(Account, row.account_id)
    if target:
        target.email_verified = 1
        row.used = 1
        db.commit()
    return templates.TemplateResponse(request, "verify.html",
                                      {**ctx, "verified": True})
