"""Read a page together with the scripts it loads.

The Content-Security-Policy refuses inline script, so every page keeps its
JavaScript in files under static/js/ (static/js/pages/<page>.js for the page
itself) rather than in an inline <script> block. Tests that look for a
function or a handler on a page read the template (or the rendered HTML) plus
the app's own scripts it loads, so a check holds no matter which side of the
move the code lives on. Vendored libraries are left out.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "service" / "app"
TEMPLATES = APP / "templates"
STATIC = APP / "static"

_PAGE_SRC = re.compile(r'<script[^>]*\ssrc="static/(js/(?!vendor/)[^"?]+\.js)')


def with_page_scripts(html: str) -> str:
    """The markup followed by the source of every app script it loads."""
    parts = [html]
    for rel in dict.fromkeys(_PAGE_SRC.findall(html)):
        parts.append((STATIC / rel).read_text())
    return "\n".join(parts)


def template_source(name: str) -> str:
    """A template's source plus the page scripts it loads."""
    return with_page_scripts((TEMPLATES / name).read_text())
