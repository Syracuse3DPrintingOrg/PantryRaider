"""Outgoing email for the portal (password resets, verification).

Deliberately stdlib only (smtplib + email.message), no dependency: the
whole feature is dark until CLOUD_SMTP_HOST and CLOUD_SMTP_FROM are set, the
same all-or-nothing gating as Google sign-in. Production points these at
Resend over SMTP (smtp.resend.com).

The message-building and gating helpers are pure, so they unit-test without
a network; the one impure step (opening the SMTP connection) goes through
``_connect``, which tests monkeypatch with a fake transport. send_email
never raises to its caller: a mail server that is down or misconfigured must
not turn a signup or a reset request into a 500, so failures are logged and
reported as False.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import settings

logger = logging.getLogger("forager.email")


def redact_address(to: str) -> str:
    """A log-safe form of an email address: the first three characters of the
    local part, then ***, then the domain ("dan***@example.com"). Enough to
    correlate a delivery problem with a support request without writing the
    full address into the log."""
    local, _, domain = (to or "").partition("@")
    kept = local[:3]
    return f"{kept}***@{domain}" if domain else f"{kept}***"


def email_configured() -> bool:
    """Whether outgoing email is set up. A host and a from address are the
    minimum; the username and password are optional (some relays authenticate
    by IP). Everything email-dependent checks this first."""
    return bool(settings.smtp_host and settings.smtp_from)


def base_url() -> str:
    """The public origin to build absolute links from, without a trailing
    slash. Reset and verification emails must carry full URLs, since the
    person reading them is not on the site yet."""
    return settings.public_base_url.rstrip("/")


def build_message(to: str, subject: str, text_body: str,
                  html_body: str | None = None) -> EmailMessage:
    """Assemble the outgoing message. Pure: no connection, no send.

    Always sends a plain-text body; when an HTML body is given the message
    becomes multipart/alternative so a mail client can show either."""
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


def _connect() -> smtplib.SMTP:
    """Open a connection to the configured mail server.

    STARTTLS on the submission port by default (587); with
    CLOUD_SMTP_STARTTLS off it dials TLS-from-the-start instead (SMTP_SSL,
    typically 465). Tests monkeypatch this to a fake transport."""
    timeout = settings.smtp_timeout_seconds
    if settings.smtp_starttls:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                            timeout=timeout)
        smtp.starttls()
        return smtp
    return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                            timeout=timeout)


def send_email(to: str, subject: str, text_body: str,
               html_body: str | None = None) -> bool:
    """Send one message. Returns True on success, False on any failure or
    when email is not configured. Never raises to the caller."""
    if not email_configured():
        return False
    msg = build_message(to, subject, text_body, html_body)
    try:
        smtp = _connect()
        try:
            if settings.smtp_user and settings.smtp_password:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        finally:
            smtp.quit()
        return True
    except Exception as exc:  # noqa: BLE001 - mail must never crash a request
        # The recipient is redacted: a warning log must not become a store of
        # full email addresses.
        logger.warning("Failed to send email to %s: %s", redact_address(to), exc)
        return False
