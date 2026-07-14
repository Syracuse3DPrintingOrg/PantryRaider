"""Guard against duplicate top-level `let`/`const`/`class` declarations across
static JS files that load together on the same page.

Classic (non-module) `<script>` tags share ONE global lexical scope. If two
files loaded on the same page both declare the same top-level `let`/`const`/
`class`, the second file throws a SyntaxError at parse time, which kills
every function defined in it (and anything after it). This is exactly what
happened in the /setup wizard: hardware.js and wizard.js both declared a
top-level `let _pairingPollTimer`, so wizard.js never parsed and its
`wizNext` function was undefined, silently breaking the wizard's Next button.

This test statically extracts top-level declarations from each JS file and
asserts no identifier is declared at top level by two files that share a
page, using the actual `<script src=...>` tags in setup.html and base.html
as the source of truth for what loads together.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_STATIC_JS = _ROOT / "service" / "app" / "static" / "js"
_TEMPLATES = _ROOT / "service" / "app" / "templates"

# Keywords that, appearing just before a bare identifier, mean a regex
# literal may legally follow a `/` (as opposed to a division operator).
_REGEX_CONTEXT_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
}

_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def extract_top_level_declarations(source: str) -> dict[str, int]:
    """Return {identifier: 1-based line number} for top-level let/const/class
    declarations in a classic (non-module) script.

    "Top level" means brace depth 0 (outside every function/block/class
    body). This is a line-based / character-scan parser, not a real JS
    parser: it tracks brace depth while skipping over strings, template
    literals, comments, and regex literals so their contents never affect
    depth or get mistaken for declarations. Good enough for this codebase's
    plain, unminified style; it is not a general JS tokenizer.
    """
    declarations: dict[str, int] = {}
    depth = 0
    i = 0
    n = len(source)
    line = 1
    # Category of the last significant token, used to disambiguate a `/` as
    # division (after a value) vs. the start of a regex literal (elsewhere).
    prev_is_value = False
    pending_keyword = None  # 'let' | 'const' | 'class' awaiting the name

    def count_newlines(text: str) -> int:
        return text.count("\n")

    while i < n:
        c = source[i]

        # Line comment
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            j = source.find("\n", i)
            j = n if j == -1 else j
            line += count_newlines(source[i:j])
            i = j
            prev_is_value = False
            continue

        # Block comment
        if c == "/" and i + 1 < n and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
            line += count_newlines(source[i:j])
            i = j
            prev_is_value = False
            continue

        # String literals
        if c in ("'", '"', "`"):
            quote = c
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == quote:
                    j += 1
                    break
                j += 1
            else:
                j = n
            line += count_newlines(source[i:j])
            i = j
            prev_is_value = True
            continue

        # Regex literal vs. division: only treat `/` as a regex start when
        # the previous significant token was not a value (identifier,
        # number, string, `)`, `]`) — the standard JS lexer heuristic.
        if c == "/" and not prev_is_value:
            j = i + 1
            in_class = False
            while j < n:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    j += 1
                    break
                elif ch == "\n":
                    # Not actually a regex (unterminated on this line); bail
                    # out and treat the `/` as ordinary punctuation instead.
                    j = -1
                    break
                j += 1
            if j != -1 and j <= n:
                # Consume trailing flags.
                while j < n and source[j].isalpha():
                    j += 1
                line += count_newlines(source[i:j])
                i = j
                prev_is_value = True
                continue
            # Fall through: treat as plain punctuation below.

        if c == "{":
            depth += 1
            i += 1
            prev_is_value = False
            pending_keyword = None
            continue
        if c == "}":
            depth -= 1
            i += 1
            prev_is_value = False
            pending_keyword = None
            continue
        if c in "()[],;:=+-*/%<>!&|?~^":
            i += 1
            prev_is_value = False
            continue
        if c == "\n":
            line += 1
            i += 1
            continue
        if c.isspace():
            i += 1
            continue

        m = _IDENT_RE.match(source, i)
        if m:
            word = m.group(0)
            if depth == 0 and word in ("let", "const", "class") and pending_keyword is None:
                pending_keyword = word
            elif pending_keyword is not None:
                # First identifier after the keyword is the declared name
                # (destructuring patterns are not used at top level in this
                # codebase, so a bare identifier is all we need to handle).
                declarations.setdefault(word, line)
                pending_keyword = None
                prev_is_value = True
                i = m.end()
                continue
            else:
                prev_is_value = word not in _REGEX_CONTEXT_KEYWORDS
            i = m.end()
            continue

        if c.isdigit():
            j = i
            while j < n and (source[j].isalnum() or source[j] in "._"):
                j += 1
            i = j
            prev_is_value = True
            continue

        # Any other punctuation.
        i += 1
        prev_is_value = False

    return declarations


def _scripts_for_page(template_name: str) -> list[str]:
    """Parse `<script src="...">` tags out of a template, in document order,
    keeping only same-origin static/js/*.js files (skip vendor bundles and
    anything outside static/js, which is not first-party app code)."""
    html = (_TEMPLATES / template_name).read_text()
    srcs = re.findall(r'<script\s+src="([^"]+)"', html)
    out = []
    for src in srcs:
        path = src.split("?", 1)[0]
        if not path.startswith("static/js/"):
            continue
        rel = path[len("static/js/"):]
        out.append(rel)
    return out


def _all_js_files() -> dict[str, Path]:
    return {
        str(p.relative_to(_STATIC_JS)): p
        for p in _STATIC_JS.rglob("*.js")
    }


def test_no_collision_regression_hardware_wizard():
    # The bug that motivated this test: hardware.js and wizard.js both
    # declared a top-level `let _pairingPollTimer`. Confirm the fix landed.
    hardware = extract_top_level_declarations((_STATIC_JS / "setup" / "hardware.js").read_text())
    wizard = extract_top_level_declarations((_STATIC_JS / "setup" / "wizard.js").read_text())
    assert "_pairingPollTimer" not in hardware
    assert "_pairingPollTimer" in wizard  # still owned by wizard.js
    assert set(hardware) & set(wizard) == set()


def test_extractor_finds_known_declarations():
    # Sanity check the parser against files with regex literals, template
    # interpolation, and escaped quotes near the string/regex boundary
    # (hardware.js has all three, which is what tripped up a naive brace
    # counter during development of this test).
    decls = extract_top_level_declarations((_STATIC_JS / "setup" / "hardware.js").read_text())
    assert decls == {"_devicePairingPollTimer": 367}

    decls = extract_top_level_declarations((_STATIC_JS / "setup" / "wizard.js").read_text())
    assert set(decls) == {
        "_wizStep", "_WIZ_TOTAL", "_installMode", "_pairingPollTimer", "_grocyWatchActive",
    }


def test_no_top_level_declaration_collisions_on_shared_pages():
    files = _all_js_files()
    pages = {
        "setup.html": _scripts_for_page("setup.html"),
        "base.html": _scripts_for_page("base.html"),
    }

    for page, scripts in pages.items():
        assert scripts, f"expected {page} to load at least one static/js script"
        owners: dict[str, tuple[str, int]] = {}
        collisions = []
        for rel in scripts:
            path = files.get(rel)
            assert path is not None, f"{page} references missing script static/js/{rel}"
            decls = extract_top_level_declarations(path.read_text())
            for name, line in decls.items():
                if name in owners:
                    other_rel, other_line = owners[name]
                    collisions.append(
                        f"`{name}` declared at top level in both "
                        f"{other_rel}:{other_line} and {rel}:{line} "
                        f"(both load on {page})"
                    )
                else:
                    owners[name] = (rel, line)
        assert not collisions, "\n".join(collisions)
