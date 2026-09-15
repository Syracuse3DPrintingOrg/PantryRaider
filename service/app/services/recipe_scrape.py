"""In-process recipe URL import via the recipe-scrapers library (FoodAssistant-zwwe).

This replaces Mealie's /recipes/create/url role: recipe-scrapers is the very
library Mealie wraps, so running it in-process keeps the same site coverage
without the API version dance. The wrapper fetches the page itself (with
ordinary browser headers, since bot-protected sites refuse a bare client) and
reduces the scrape to the same normalized parsed-recipe dict
recipes_import.parse_recipe_file produces, so the save path is shared.

Failure contract: every problem raises RecipeScrapeError with a user-facing
message. Callers keep the existing fallback ladder (browser fetch plus LLM
extraction) for pages the scraper cannot read, so a scrape failure is a
stepping stone, never a dead end.

The library import is lazy so the app (and the test suite) never needs
recipe-scrapers installed just to import this module.
"""
from __future__ import annotations

import httpx

# Present as an ordinary modern browser: plain client User-Agents get 403/404
# from bot-protected recipe sites (same rationale as the LLM-fallback fetch).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_FETCH_TIMEOUT = 20.0


class RecipeScrapeError(Exception):
    """Raised with a user-facing message when a URL cannot be scraped."""


def _call(scraper, method: str):
    """Read one scraper field, treating any per-field error as 'not provided'.

    recipe-scrapers raises per-field (NotImplemented, SchemaOrgException, ...)
    when a site omits a field; a missing description or time must never sink a
    scrape that found the name and ingredients."""
    try:
        return getattr(scraper, method)()
    except Exception:  # noqa: BLE001 - any missing field reads as None
        return None


def _time_text(minutes) -> str:
    """Render the scraper's total_time (minutes as a number) as display text."""
    try:
        n = int(minutes)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    hours, mins = divmod(n, 60)
    if hours and mins:
        return f"{hours} hr {mins} min"
    if hours:
        return f"{hours} hr"
    return f"{n} minutes"


def parse_html(html: str, url: str) -> dict:
    """Scrape recipe HTML into the normalized parsed-recipe dict. No network.

    Tries the site-specific scraper first, then wild mode (generic schema.org
    extraction) for sites the library has no dedicated scraper for. Raises
    RecipeScrapeError when no recipe can be read at all."""
    try:
        from recipe_scrapers import scrape_html
    except ImportError as exc:
        raise RecipeScrapeError(
            "The recipe reader is not installed on this server yet. "
            "Update the app image, then try again.") from exc

    scraper = None
    try:
        scraper = scrape_html(html, org_url=url, supported_only=False)
    except TypeError:
        # Older library versions call generic extraction "wild_mode".
        try:
            scraper = scrape_html(html, org_url=url, wild_mode=True)
        except Exception:  # noqa: BLE001
            scraper = None
    except Exception:  # noqa: BLE001 - unreadable page, fall through
        scraper = None
    if scraper is None:
        raise RecipeScrapeError("Could not read a recipe from that page.")

    name = str(_call(scraper, "title") or "").strip()
    ingredients = [str(i).strip() for i in (_call(scraper, "ingredients") or [])
                   if str(i or "").strip()]
    steps = _call(scraper, "instructions_list")
    if not steps:
        blob = str(_call(scraper, "instructions") or "")
        steps = [s.strip() for s in blob.splitlines() if s.strip()]
    steps = [str(s).strip() for s in steps or [] if str(s or "").strip()]

    if not name or not ingredients:
        raise RecipeScrapeError("Could not read a recipe from that page.")

    return {
        "name": name,
        "description": str(_call(scraper, "description") or "").strip(),
        "servings": str(_call(scraper, "yields") or "").strip(),
        "total_time": _time_text(_call(scraper, "total_time")),
        "ingredients": ingredients,
        "instructions": steps,
        "source": url,
        "source_url": str(_call(scraper, "canonical_url") or "").strip() or url,
        "image": str(_call(scraper, "image") or "").strip() or None,
    }


async def scrape_url(url: str) -> dict:
    """Fetch a webpage and scrape it into the normalized parsed-recipe dict.

    Raises RecipeScrapeError with a clean, user-facing message on any failure
    (unreachable site, blocked request, or a page with no readable recipe)."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise RecipeScrapeError("Enter a full URL starting with http:// or https://")
    from . import egress
    try:
        # SSRF-guarded, pinned client: recipes come from the public web, so a URL
        # (or a redirect) that resolves to loopback / link-local / a private LAN
        # address is refused at connect time (FoodAssistant-wa3g).
        async with egress.guarded_async_client(timeout=_FETCH_TIMEOUT,
                                               follow_redirects=True) as client:
            resp = await client.get(url, headers=_BROWSER_HEADERS)
            resp.raise_for_status()
            html = resp.text
    except egress.BlockedHostError as exc:
        raise RecipeScrapeError(
            "That address points at this device, not a recipe site.") from exc
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (404, 410):
            raise RecipeScrapeError(
                "That page could not be found. Check the link points to a "
                "single recipe, not a recipe list or search page.") from exc
        if code in (401, 403):
            raise RecipeScrapeError(
                "That site blocked the request. Try copying the recipe text "
                "into an import instead.") from exc
        raise RecipeScrapeError(
            "That site returned an error, so the recipe could not be read.") from exc
    except httpx.HTTPError as exc:
        raise RecipeScrapeError(
            "Could not reach that site. Check the link and your connection.") from exc

    if not (html or "").strip():
        raise RecipeScrapeError("That page was empty, so there was no recipe to read.")
    return parse_html(html, str(resp.url) or url)
