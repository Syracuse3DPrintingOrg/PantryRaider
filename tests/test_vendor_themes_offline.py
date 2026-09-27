"""The vendored themes must not reach out to Google Fonts.

Bootswatch ships its themes with an @import of a Google Fonts stylesheet. The
app's own Content-Security-Policy blocks that request, so leaving it in only
logs a CSP violation on every page and still falls back to the next font in
the stack. The app is meant to work fully offline, so the vendored copies keep
no external font references at all.
"""

from pathlib import Path

THEMES_DIR = Path(__file__).resolve().parent.parent / "service" / "app" / "static" / "vendor" / "themes"

BLOCKED_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")


def test_themes_dir_exists():
    assert THEMES_DIR.is_dir()
    assert any(THEMES_DIR.glob("*.css"))


def test_no_theme_references_google_fonts():
    offenders = []
    for path in sorted(THEMES_DIR.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for host in BLOCKED_HOSTS:
            if host in text:
                offenders.append(f"{path.name}: {host}")
    assert not offenders, "vendored themes reference Google Fonts: " + ", ".join(offenders)


def test_bootswatch_themes_keep_no_remote_import():
    for name in ("cyborg.min.css", "darkly.min.css", "flatly.min.css"):
        text = (THEMES_DIR / name).read_text(encoding="utf-8")
        assert "@import" not in text, name
        # The Bootswatch license banner and the Bootstrap body stay intact.
        assert text.startswith('@charset "UTF-8";/*!'), name
        assert "Licensed under MIT" in text, name
