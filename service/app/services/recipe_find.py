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
    "we", "on", "screen", "put", "from", "in", "pantry", "raider",
    # "Let's cook ...", "I want to make ..."
    "s", "want", "would", "like", "going", "gonna", "start", "making", "cooking",
    # "Just lasagna", "only the soup please", "actually, the chili one"
    "just", "only", "actually", "really", "one", "ones", "instead", "um", "uh",
    "thanks", "thank", "you",
})
MIN_SCORE = 0.62
# Below a match but close enough to offer: "did you mean".
SUGGEST_SCORE = 0.48
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
    # ("key line pie" for key lime pie.) A short word has to be nearly right;
    # a longer one may be a letter or two off.
    hits = 0.0
    for word in q:
        best = max((difflib.SequenceMatcher(None, word, other).ratio() for other in n),
                   default=0.0)
        hits += best if best >= (0.7 if len(word) >= 4 else 0.99) else 0.0
    by_word = hits / len(q)
    whole = difflib.SequenceMatcher(None, " ".join(q), " ".join(n)).ratio()
    return round(min(0.83, max(by_word * 0.82, whole * 0.8)), 4)


def rank(query, recipes, floor: float = MIN_SCORE) -> list[dict]:
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
        if s >= floor:
            out.append({"slug": slug, "title": name, "score": s})
    out.sort(key=lambda r: (-r["score"], len(r["title"]), r["title"].lower()))
    return out


def choose(query, recipes) -> dict:
    """{"result": "shown", "recipe": {...}} for one clear match,
    {"result": "ambiguous", "candidates": [...]} when several fit about as
    well, or {"result": "not_found"}."""
    near = rank(query, recipes, SUGGEST_SCORE)
    ranked = [r for r in near if r["score"] >= MIN_SCORE]
    if not ranked:
        if not near:
            return {"result": "not_found"}
        # Nothing fits well, but something is close: offer it rather than
        # leave a misheard name at "not found".
        return {"result": "ambiguous",
                "candidates": [{"slug": r["slug"], "title": r["title"]} for r in near[:3]]}
    top = ranked[0]
    close = [r for r in ranked if top["score"] - r["score"] < CLOSE]
    exact = [r for r in ranked if r["score"] >= 1.0]
    if len(exact) == 1 or len(close) == 1:
        return {"result": "shown", "recipe": {"slug": top["slug"], "title": top["title"]}}
    return {"result": "ambiguous",
            "candidates": [{"slug": r["slug"], "title": r["title"]}
                           for r in close[:MAX_CANDIDATES]]}


# --- An answer to "which one?" -------------------------------------------------

_ORDINALS = {
    "first": 0, "1st": 0, "one": 0, "1": 0,
    "second": 1, "2nd": 1, "two": 1, "2": 1,
    "third": 2, "3rd": 2, "three": 2, "3": 2,
    "fourth": 3, "4th": 3, "four": 3, "4": 3,
    "fifth": 4, "5th": 4, "five": 4, "5": 4,
    "last": -1, "final": -1,
}
# Words in an answer that do not tell the candidates apart.
_ANSWER_FILLER = _FILLER | frozenset({
    "one", "ones", "number", "option", "choice", "pick", "go", "with", "want",
    "mean", "meant", "said", "it", "is", "that", "s", "yes", "yeah", "please",
    "like", "would", "try", "use", "cooking", "making", "called", "named", "about",
    "other", "kind", "version", "just", "only", "go", "lets", "let",
})


def _answer_words(text) -> list[str]:
    return [_stem(w) for w in _plain(text).split() if w not in _ANSWER_FILLER]


def resolve_choice(answer, candidates) -> dict | None:
    """Which of the candidates last offered an answer means: an ordinal ("the
    second one", "number two", "the last one") or words only one of them has
    ("the best-ever one", "the soup"). {"slug", "title"} or None when the
    answer does not pick exactly one, so the caller searches again. Pure."""
    rows = [c for c in (candidates or []) if isinstance(c, dict) and c.get("slug")]
    if not rows:
        return None
    words = _plain(answer).split()
    if not words:
        return None
    # An ordinal said on its own ("the second one", "number 2", "two").
    rest = [w for w in words if w not in ("the", "number", "option", "one", "choice",
                                         "recipe", "please", "pick", "i", "want", "go",
                                         "with", "s", "that", "let", "lets", "do")]
    if len(rest) == 1 and rest[0] in _ORDINALS:
        idx = _ORDINALS[rest[0]]
        if -len(rows) <= idx < len(rows):
            hit = rows[idx]
            return {"slug": hit["slug"], "title": hit["title"]}
        return None
    asked = _answer_words(answer)
    if not asked:
        return None

    def hits(c: dict) -> float:
        names = tokens(c.get("title"), drop_filler=False)
        total = 0.0
        for w in asked:
            best = max((difflib.SequenceMatcher(None, w, n).ratio() for n in names),
                       default=0.0)
            total += 1.0 if best >= (0.8 if len(w) >= 4 else 0.99) else 0.0
        return total

    scored = [(hits(c), c) for c in rows]
    top = max(s for s, _ in scored)
    if top <= 0:
        return None
    best = [c for s, c in scored if s == top]
    if len(best) > 1:
        # "Key lime pie" between "Key Lime Pie" and "Best-Ever Key Lime Pie":
        # the one whose name is exactly what was said.
        exact = [c for c in best if set(tokens(c.get("title"), drop_filler=False))
                 == set(asked)]
        best = exact
    if len(best) != 1:
        return None
    return {"slug": best[0]["slug"], "title": best[0]["title"]}


def join_titles(titles) -> str:
    """"A", "A and B", "A, B and C" for a spoken list."""
    titles = [str(t) for t in titles if t]
    if len(titles) <= 1:
        return "".join(titles)
    return ", ".join(titles[:-1]) + " and " + titles[-1]
