"""Document print settings: pure mapping from the app's page size / color /
duplex settings to CUPS `lp -o` options (FoodAssistant-7xo5). No printer or
network touched."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import print_document as pd  # noqa: E402


def test_defaults_map_to_letter_size_omitted_color_and_sides():
    # "auto" page size deliberately omits the media option so the printer's
    # own default page size wins.
    opts = pd.document_print_options("auto", "color", "one-sided")
    assert opts == {"print-color-mode": "color", "sides": "one-sided"}


def test_known_page_sizes_map_to_cups_media_names():
    assert pd.document_print_options("letter", "color", "one-sided")["media"] == "Letter"
    assert pd.document_print_options("a4", "color", "one-sided")["media"] == "A4"
    assert pd.document_print_options("legal", "color", "one-sided")["media"] == "Legal"


def test_color_modes_map_correctly():
    assert pd.document_print_options("auto", "color", "one-sided")["print-color-mode"] == "color"
    assert pd.document_print_options("auto", "monochrome", "one-sided")["print-color-mode"] == "monochrome"


def test_duplex_modes_map_correctly():
    assert pd.document_print_options("auto", "color", "one-sided")["sides"] == "one-sided"
    assert pd.document_print_options("auto", "color", "two-sided")["sides"] == "two-sided-long-edge"


def test_unknown_values_are_omitted_not_raised():
    opts = pd.document_print_options("poster", "purple", "spiral")
    assert opts == {}


def test_blank_and_none_values_are_omitted():
    assert pd.document_print_options("", "", "") == {}
    assert pd.document_print_options(None, None, None) == {}


def test_case_and_whitespace_insensitive():
    opts = pd.document_print_options(" Letter ", "COLOR", " Two-Sided ")
    assert opts == {
        "media": "Letter",
        "print-color-mode": "color",
        "sides": "two-sided-long-edge",
    }


def test_returns_plain_dict_safe_for_print_bytes_options():
    opts = pd.document_print_options("letter", "monochrome", "two-sided")
    assert isinstance(opts, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in opts.items())
