"""Attribution guards for the code we did not write.

Three things drift and nobody notices until someone asks a licensing question:

1. The license is PolyForm Noncommercial, so no user-facing copy may call
   Pantry Raider "open source". Wording that describes the upstream projects
   (Grocy, Mealie, and friends) is exempt: those really are open source.
2. Every third-party bundle we serve from our own address has to carry its
   license, and the two Apache-2.0 ones ship no header upstream, so the text
   lives in a file beside them plus a banner on the bundle.
3. Open-Meteo publishes under CC BY 4.0, which wants a visible credit wherever
   the forecast is shown.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "service/app/templates"
VENDOR = ROOT / "service/app/static/vendor"
ESP_TOOLS = ROOT / "service/app/static/js/vendor/esp-web-tools"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"

# Copy that talks about the upstream projects, not about Pantry Raider itself.
UPSTREAM_CREDIT_LINES = (
    "built on top of a number of excellent open-source projects",
    "donating to the open-source projects listed below",
    "the open-source python-elgato-streamdeck library",
    "built on the shoulders of excellent open-source projects",
)


def _user_facing_copy() -> list[tuple[str, str]]:
    """(label, text) for every surface that talks to a user about the app."""
    out = []
    for tpl in sorted(TEMPLATES.rglob("*.html")):
        out.append((str(tpl.relative_to(ROOT)), tpl.read_text()))
    for extra in ("service/app/services/affiliate.py",
                  "cloud/app/routers/shares.py"):
        p = ROOT / extra
        out.append((extra, p.read_text()))
    for tpl in sorted((ROOT / "cloud/app/templates").rglob("*.html")):
        out.append((str(tpl.relative_to(ROOT)), tpl.read_text()))
    return out


def test_no_surface_calls_pantry_raider_open_source():
    offenders = []
    for label, text in _user_facing_copy():
        for line in text.splitlines():
            low = line.lower()
            if "open source" not in low and "open-source" not in low:
                continue
            if any(credit in line for credit in UPSTREAM_CREDIT_LINES):
                continue
            offenders.append(f"{label}: {line.strip()}")
    assert not offenders, (
        "PolyForm Noncommercial is not an open source license; say "
        "source-available, free for personal and non-commercial use:\n"
        + "\n".join(offenders))


def test_affiliate_disclosure_tracks_the_license():
    import sys
    sys.path.insert(0, str(ROOT / "service"))
    from app.services import affiliate

    assert "free for personal and non-commercial use" in affiliate.DISCLOSURE
    assert "open source" not in affiliate.DISCLOSURE


def test_apache_bundles_ship_their_license_text():
    for text_file, marker in ((VENDOR / "html5-qrcode.LICENSE", "html5-qrcode"),
                              (ESP_TOOLS / "LICENSE", "ESP Web Tools")):
        assert text_file.exists(), f"{text_file} is missing"
        body = text_file.read_text()
        assert body.startswith(marker)
        # The whole license, not a link to it.
        assert "Apache License" in body and "Version 2.0" in body
        assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in body
        assert "END OF TERMS AND CONDITIONS" in body


def test_vendored_bundles_carry_an_attribution_banner():
    head = (VENDOR / "html5-qrcode.min.js").read_text()[:600]
    assert "html5-qrcode" in head and "Apache-2.0" in head
    assert "html5-qrcode.LICENSE" in head

    esp = (ESP_TOOLS / "install-button.js").read_text()[:600]
    assert "esp-web-tools" in esp and "Apache-2.0" in esp
    # The NOTICE points at the sibling license text, not at an URL.
    notice = (ESP_TOOLS / "NOTICE").read_text()
    assert "LICENSE file in this directory" in notice
    assert "https://www.apache.org/licenses/LICENSE-2.0" not in notice


def test_notices_file_covers_every_bundled_component():
    assert NOTICES.exists(), "THIRD_PARTY_NOTICES.md is missing"
    body = NOTICES.read_text()
    for name in ("Bootstrap", "Bootstrap Icons", "Bootswatch", "html5-qrcode",
                 "ESP Web Tools", "LVGL", "ESPHome", "Open-Meteo",
                 "PolyForm Noncommercial"):
        assert name in body, f"THIRD_PARTY_NOTICES.md never mentions {name}"
    # Full texts, not just a table of names.
    assert "END OF TERMS AND CONDITIONS" in body           # Apache-2.0
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in body    # MIT
    assert "Redistribution and use in source and binary forms" in body  # BSD


def test_about_page_credits_the_scanner_we_actually_use():
    about = (TEMPLATES / "about.html").read_text()
    assert "html5-qrcode" in about
    # python-barcode stays, but as the optional label-printing extra it is.
    assert "python-barcode" in about
    assert "python-barcode / ZBar" not in about
    assert "ZBar" not in about
    assert "THIRD_PARTY_NOTICES.md" in about


def test_weather_page_credits_open_meteo():
    weather = (TEMPLATES / "weather.html").read_text()
    assert "Weather data by" in weather
    assert "open-meteo.com" in weather
    assert "CC BY 4.0" in weather
