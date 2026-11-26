"""Find a recipe from what someone said ("the lasagna recipe").

Pure matching over a list of {"slug", "name"} rows, so it works the same on
the native library and on Mealie and needs no network to test. ``choose``
answers one of three ways: one clear match, a short list to pick from, or
nothing.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

# Words people say around the name that are not part of it.
_FILLER = frozenset({
    "a", "an", "the", "my", "our", "your", "that", "this", "some", "please",
    "recipe", "recipes", "for", "of", "show", "me", "us", "open", "pull", "up",
    "find", "display", "make", "cook", "let", "lets", "to", "how", "do", "i",
    "we", "on", "screen", "put",
})
MIN_SCORE = 0.62
# Two matches this close together are a question, not an answer.
CLOSE = 0.08
MAX_CANDIDATES = 5


def _plain(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("es") and word[-3] in "sxzh":
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text, drop_filler: bool = True) -> list[str]:
    """The words that matter in a name or a request, lowercased, accents and
    plurals folded."""
    words = _plain(text).split()
    if drop_filler:
        kept = [w for w in words if w not in _FILLER]
        # A recipe really called "The Recipe" still has to match something.
        words = kept or words
    return [_stem(w) for w in words]


def score(query, name) -> float:
    """How well a spoken request fits a recipe name, 0 to 1."""
    q = tokens(query)
    n = tokens(name, drop_filler=False)
    if not q or not n:
        return 0.0
    if q == n or q == tokens(name):
        return 1.0
    qs, ns = set(q), set(n)
    if qs <= ns:
        # Every word asked for is in the name. A shorter name is the closer
        # fit: "lasagna" is nearer "Classic Lasagna" than a five-word title.
        return 0.84 + 0.12 * (len(qs) / len(ns))
    # Speech to text gets words slightly wrong: match word by word, loosely.
    hits = 0.0
    for word in q:
        best = max((difflib.SequenceMatcher(None, word, other).ratio() for other in n),
                   default=0.0)
        hits += best if best >= 0.8 else 0.0
    by_word = hits / len(q)
    whole = difflib.SequenceMatcher(None, " ".join(q), " ".join(n)).ratio()
    return round(min(0.83, max(by_word * 0.8, whole * 0.8)), 4)


def rank(query, recipes) -> list[dict]:
    """Recipes that fit, best first: [{"slug", "title", "score"}]."""
    out = []
    seen = set()
    for row in recipes or []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        name = str(row.get("name") or row.get("title") or "").strip()
        if not slug or not name or slug in seen:
            continue
        seen.add(slug)
        s = score(query, name)
        if s >= MIN_SCORE:
            out.append({"slug": slug, "title": name, "score": s})
    out.sort(key=lambda r: (-r["score"], len(r["title"]), r["title"].lower()))
    return out


def choose(query, recipes) -> dict:
    """{"result": "shown", "recipe": {...}} for one clear match,
    {"result": "ambiguous", "candidates": [...]} when several fit about as
    well, or {"result": "not_found"}."""
    ranked = rank(query, recipes)
    if not ranked:
        return {"result": "not_found"}
    top = ranked[0]
    close = [r for r in ranked if top["score"] - r["score"] < CLOSE]
    exact = [r for r in ranked if r["score"] >= 1.0]
    if len(exact) == 1 or len(close) == 1:
        return {"result": "shown", "recipe": {"slug": top["slug"], "title": top["title"]}}
    return {"result": "ambiguous",
            "candidates": [{"slug": r["slug"], "title": r["title"]}
                           for r in close[:MAX_CANDIDATES]]}


def join_titles(titles) -> str:
    """"A", "A and B", "A, B and C" for a spoken list."""
    titles = [str(t) for t in titles if t]
    if len(titles) <= 1:
        return "".join(titles)
    return ", ".join(titles[:-1]) + " and " + titles[-1]
