"""Share-time audit of a community recipe: brand names and copied-text risk.

The ground rules this module encodes, and why it advises instead of censoring:

- A recipe as such (the ingredient list and the basic directions) is not
  protected by copyright. People have always traded recipes and the law leaves
  room for exactly that, so a shared recipe is never treated as a problem to
  be caught. Nothing here polices cooking.
- What CAN be protected is a publisher's creative prose: a book's exact
  wording, the story above the recipe. So the only thing the audit refuses
  outright is text carrying an explicit lifted-from-a-publisher line, the kind
  ("reprinted with permission") that only turns up in copied publisher text.
- Trademark law does not forbid naming the product you are imitating. Calling
  a recipe a "copycat", "Brand-style", or "inspired by" a brand is the honest,
  compliant form, and the guidance steers people there. A brand name standing
  alone as the recipe's own name earns a nudge, never a rejection.

audit_recipe() is the public surface: pure, no I/O, no database. It returns
{"level": "ok" | "advise" | "block", "findings": [...]}, each finding a dict
with "code", "severity", and a user-forward "message". Severity "note"
findings are context stored for a later review (a flag, a takedown notice)
and never raise the level.
"""
from __future__ import annotations

import re

LEVEL_OK = "ok"
LEVEL_ADVISE = "advise"
LEVEL_BLOCK = "block"
# Stored with a finding but never raised to the recipe's level: a signal worth
# keeping for a human review, not worth bothering the sharer about.
SEVERITY_NOTE = "note"

# Brand names that read as a recipe's identity when they stand alone in a
# title. To extend, add a name: matching is case-insensitive, word-boundary,
# apostrophe-optional (Wendys and Wendy's both match), hyphen and space
# interchangeable in multi-word names, and a trailing plural or possessive is
# accepted (Oreos, Oreo's). Names that are also plain food words (Chipotle,
# Spam, Goldfish) are left out on purpose so a real ingredient never trips
# the nudge.
BRAND_NAMES: tuple[str, ...] = (
    # Restaurants and chains.
    "Applebee's", "Arby's", "Burger King", "Cheesecake Factory",
    "Chick-fil-A", "Cinnabon", "Cracker Barrel", "Domino's", "Dunkin",
    "Five Guys", "IHOP", "In-N-Out", "KFC", "Krispy Kreme", "Little Caesars",
    "McDonald's", "Olive Garden", "Outback Steakhouse", "P.F. Chang's",
    "Panda Express", "Panera", "Papa John's", "Pizza Hut", "Popeyes",
    "Raising Cane's", "Red Lobster", "Shake Shack", "Starbucks", "Taco Bell",
    "Texas Roadhouse", "Wendy's", "White Castle", "Wingstop",
    # Packaged and grocery brands.
    "Big Mac", "Biscoff", "Bisquick", "Butterfinger", "Cheerios", "Cheetos",
    "Cheez-It", "Cool Whip", "Doritos", "Eggo", "Ghirardelli",
    "Hamburger Helper", "Heinz", "Hellmann's", "Hidden Valley", "Jell-O",
    "Kit Kat", "Kool-Aid", "M&M", "Nutella", "Nutter Butter", "Oreo",
    "Pop-Tart", "Pringles", "Reese's", "Rice Krispies", "Ritz", "Snickers",
    "Toll House", "Twinkie", "Twix", "Velveeta",
)

# Lines that only appear in text copied out of a published source. These are
# the one thing the audit refuses; everything else is at most a nudge.
PUBLISHER_COPY_PHRASES: tuple[str, ...] = (
    "reprinted with permission",
    "used with permission",
    "excerpted from",
    "all rights reserved",
    "may not be reproduced",
)

# Recipe publishers whose address inside step text suggests the steps were
# pasted from their page (a copied source line rides along with copied text).
# A link is a signal, not proof, so it is noted for review, never punished.
RECIPE_PUBLISHER_DOMAINS: tuple[str, ...] = (
    "allrecipes.com", "bbcgoodfood.com", "bonappetit.com",
    "cooking.nytimes.com", "delish.com", "epicurious.com", "food52.com",
    "foodandwine.com", "foodnetwork.com", "kingarthurbaking.com",
    "marthastewart.com", "seriouseats.com", "simplyrecipes.com",
    "tasteofhome.com", "thekitchn.com",
)

# A single step this long reads like a pasted passage, not a written step.
# Copied publisher prose is where copyright actually bites, so it is worth a
# stored note; a long-winded home cook is not a violation, so never more.
LONG_STEP_CHARS = 600

_APOSTROPHES = "['’]"


def _compile_brand(brand: str) -> re.Pattern:
    """A case-insensitive, word-boundary pattern for one brand name.

    Apostrophes become optional, spaces and hyphens match each other, and an
    optional trailing plural or possessive is accepted, so "wendys style",
    "Wendy's", "Oreos", and "Pop Tarts" all resolve to their brands while
    "boreo" and "ritzy" never match."""
    words = [w for w in re.split(r"[\s\-]+", brand) if w]
    body = r"[\s\-]+".join(
        re.escape(word).replace("'", f"{_APOSTROPHES}?") for word in words)
    return re.compile(
        rf"(?<![A-Za-z0-9]){body}(?:{_APOSTROPHES}?s)?(?![A-Za-z0-9])",
        re.IGNORECASE)


_BRAND_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (brand, _compile_brand(brand)) for brand in BRAND_NAMES)

# The nominative markers: "copycat" or "inspired" anywhere in the title, or
# "style" hanging directly off the brand ("Oreo-style", "Wendy's style").
_COPYCAT_RE = re.compile(r"(?<![A-Za-z0-9])copy[\s\-]?cat(?![A-Za-z0-9])",
                         re.IGNORECASE)
_INSPIRED_RE = re.compile(r"(?<![A-Za-z0-9])inspired(?![A-Za-z0-9])",
                          re.IGNORECASE)
_STYLE_AFTER_RE = re.compile(r"^[\s\-]+style(?![A-Za-z0-9])", re.IGNORECASE)


def _has_nominative_marker(title: str, match: re.Match) -> bool:
    """Whether this brand mention in the title is the compliant, nominative
    form: a copycat or inspired-by title, or the brand followed by "style"."""
    if _COPYCAT_RE.search(title) or _INSPIRED_RE.search(title):
        return True
    return bool(_STYLE_AFTER_RE.match(title[match.end():]))


def _brand_message(brand: str) -> str:
    return (f'"{brand}" looks like a brand name used as this recipe\'s own '
            f'name. If this is your version of theirs, a title like '
            f'"Copycat {brand}" or "{brand}-style" says so honestly, and '
            f'that is the naming we encourage. If {brand} is simply a '
            f'store-bought ingredient in the dish, the title is fine as it '
            f'is.')


def audit_recipe(title: str, description: str = "",
                 steps: list[str] | None = None) -> dict:
    """Audit one recipe about to be shared. Pure; safe on any input.

    Returns {"level": ..., "findings": [...]}. Only a brand name standing
    alone as the title's identity yields "advise"; "block" is reserved for
    an explicit publisher-copy line. Softer copy-risk signals (a publisher
    link inside a step, one unusually long step) are recorded as "note"
    findings and leave the level alone."""
    title = str(title or "")
    description = str(description or "")
    steps = [str(step) for step in (steps or [])]
    findings: list[dict] = []

    # (a) A brand name as the recipe's own identity: the title only. A brand
    # in the description or a step names a product being used, which is plain
    # nominative use and not flagged at all.
    for brand, pattern in _BRAND_PATTERNS:
        match = pattern.search(title)
        if not match or _has_nominative_marker(title, match):
            continue
        findings.append({
            "code": "brand_identity",
            "severity": LEVEL_ADVISE,
            "brand": brand,
            "message": _brand_message(brand),
        })

    # (b) An explicit publisher-copy line anywhere in the text: the one thing
    # refused, because it only rides along with copied publisher prose.
    everything = "\n".join([title, description, *steps]).lower()
    for phrase in PUBLISHER_COPY_PHRASES:
        if phrase in everything:
            findings.append({
                "code": "publisher_copy",
                "severity": LEVEL_BLOCK,
                "match": phrase,
                "message": (f'This includes the line "{phrase}", which '
                            "normally appears only in text copied from a "
                            "published source. Please write the recipe in "
                            "your own words and share it again."),
            })

    # (b) Softer paste signals, noted for a later human review and never
    # raised to the sharer: a recipe-publisher link inside step text, and a
    # single step long enough to read as a pasted passage.
    seen_domains: set[str] = set()
    long_step_noted = False
    for step in steps:
        low = step.lower()
        for domain in RECIPE_PUBLISHER_DOMAINS:
            if domain in low and domain not in seen_domains:
                seen_domains.add(domain)
                findings.append({
                    "code": "publisher_link",
                    "severity": SEVERITY_NOTE,
                    "match": domain,
                    "message": (f"A step links to {domain}. If the steps came "
                                "from that page, rewrite them in your own "
                                "words; the credit line is the place to say "
                                "where a recipe came from."),
                })
        if len(step) >= LONG_STEP_CHARS and not long_step_noted:
            long_step_noted = True
            findings.append({
                "code": "long_step",
                "severity": SEVERITY_NOTE,
                "message": ("One step runs unusually long for a single step. "
                            "Long pasted passages are where copied text tends "
                            "to hide; short steps in your own words are "
                            "always safe."),
            })

    return {"level": _level(findings), "findings": findings}


def _level(findings: list[dict]) -> str:
    """The recipe's overall level: the worst real severity. Notes never
    raise it."""
    severities = {finding["severity"] for finding in findings}
    if LEVEL_BLOCK in severities:
        return LEVEL_BLOCK
    if LEVEL_ADVISE in severities:
        return LEVEL_ADVISE
    return LEVEL_OK


def guidance(audit: dict) -> list[str]:
    """The advise-level messages, the ones worth handing back to the sharer
    alongside a successful share."""
    return [finding["message"] for finding in audit["findings"]
            if finding["severity"] == LEVEL_ADVISE]


def block_message(audit: dict) -> str:
    """The first block finding's message, or "" when nothing blocks."""
    for finding in audit["findings"]:
        if finding["severity"] == LEVEL_BLOCK:
            return finding["message"]
    return ""
