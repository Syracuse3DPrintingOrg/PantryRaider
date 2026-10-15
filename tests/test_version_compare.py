"""Shared version-comparison utility (FoodAssistant-ny8r)."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.version_compare import (  # noqa: E402
    normalize, is_version_tag, is_newer, compare_to, diff_level,
)


def test_diff_level_traffic_light():
    assert diff_level("", "0.6.5") == "unknown"
    assert diff_level("0.6.5", "0.6.5") == "same"          # green
    assert diff_level("0.6.4", "0.6.5") == "patch"         # yellow
    assert diff_level("0.6.7", "0.6.5") == "patch"         # patch ahead is still patch
    assert diff_level("0.7.0", "0.6.5") == "major_minor"   # red (minor differs)
    assert diff_level("1.0.0", "0.6.5") == "major_minor"   # red (major differs)


def test_normalize_strips_v_prefix():
    assert normalize("v1.2.3") == (1, 2, 3)
    assert normalize("1.2.3") == (1, 2, 3)
    # v and non-v forms compare equal (the project uses plain x.y.z versions).
    assert normalize("v0.6.10") == normalize("0.6.10")
    assert normalize("") == (0,)


def test_is_version_tag():
    assert is_version_tag("v1.0.0")
    assert is_version_tag("1.2")
    assert not is_version_tag("latest")
    assert not is_version_tag("")


def test_is_newer():
    assert is_newer("0.6.10", "0.6.9")
    assert is_newer("v1.0.0", "0.9.9")
    assert not is_newer("0.6.9", "0.6.9")
    assert not is_newer("0.6.8", "0.6.9")


def test_compare_to_classifies_update_status():
    assert compare_to("", "0.6.5") == "unknown"
    assert compare_to("0.6.4", "0.6.5") == "behind"
    assert compare_to("0.6.5", "0.6.5") == "current"
    assert compare_to("0.6.6", "0.6.5") == "ahead"


def test_admin_uses_shared_util():
    # The admin update-check imports the shared normalize, so the comparison rule
    # is defined in exactly one place.
    from app.routers import admin
    from app import version_compare
    assert admin._normalize is version_compare.normalize
    assert admin._is_version_tag is version_compare.is_version_tag


def test_untrusted_versions_never_raise():
    from app.version_compare import normalize, readable_version
    # Latin-1 superscripts pass str.isdigit() and make int() raise.
    assert normalize("1.\u00b2") == (1, 0)
    assert normalize("\u00b9.0") == (0, 0)
    assert not is_version_tag("\u00b9.0")
    # Past int()'s 4300 digit limit; too long to be a version at all.
    assert normalize("9" * 5000) == (0,)
    assert compare_to("9" * 5000, "0.20.0") == "behind"
    assert readable_version(" v0.20.3-rc.1 ") == "v0.20.3-rc.1"
    assert readable_version("0.19.0 beta") == ""
    assert readable_version("dev") == ""
    assert readable_version("1.\u00b2") == ""
    assert readable_version("1" * 65) == ""
    assert readable_version(None) == ""
