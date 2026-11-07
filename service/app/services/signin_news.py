"""The sign-in page's one-time note about how signing in changed.

People who used this kitchen before the change (FoodAssistant-9g6os and
friends) see a short note on the sign-in page saying what is new. It is
decided once, on the first start of the release that brought it: an install
that already had a password shows it for 30 days, a brand-new install never
does. Each browser stops showing it after it is closed or after one sign-in
(static/js/pages/login.js).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

DAYS = 30


def seed(settings, now: datetime | None = None) -> None:
    if settings.signin_news_checked:
        return
    now = now or datetime.now(timezone.utc)
    changes = {"signin_news_checked": True}
    if settings.auth_password:
        changes["signin_news_until"] = (now + timedelta(days=DAYS)).isoformat(timespec="seconds")
    settings.save(changes)


def showing(settings, now: datetime | None = None) -> bool:
    until = settings.signin_news_until
    if not until:
        return False
    try:
        end = datetime.fromisoformat(until)
    except ValueError:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) < end
