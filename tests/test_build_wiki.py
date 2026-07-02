"""Wiki builder link rewriting (FoodAssistant-y1jn).

The wiki is a generated mirror of the docs (scripts/build-wiki.py). These guard
the link rewriting: mirrored pages become wiki page links, images become raw
URLs, other repo files become blob URLs, and external/app links are untouched.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_wiki", _ROOT / "scripts" / "build-wiki.py")
build_wiki = importlib.util.module_from_spec(_spec)
sys.modules["build_wiki"] = build_wiki
_spec.loader.exec_module(build_wiki)

REPO = "Syracuse3DPrintingOrg/PantryRaider"
PAGE_MAP = build_wiki._src_to_page()


def _rw(text: str, src: str) -> str:
    return build_wiki._rewrite_links(text, src, PAGE_MAP, REPO, "main")


def test_mirrored_doc_link_becomes_wiki_page():
    # From the README, a link to docs/platforms.md points at the Platforms page.
    out = _rw("see [platforms](docs/platforms.md)", "README.md")
    assert "(Platforms)" in out


def test_relative_link_resolves_from_source_dir():
    # From docs/hardware/sd-image.md, a sibling link resolves to the right page.
    out = _rw("[hw](supported-hardware.md)", "docs/hardware/sd-image.md")
    assert "(Supported-Hardware)" in out
    # And ../README.md from a top-level docs page resolves to Home.
    assert "(Home)" in _rw("[home](../README.md)", "docs/platforms.md")


def test_anchor_is_preserved_on_wiki_link():
    out = _rw("[s](docs/settings-matrix.md#cameras)", "README.md")
    assert "(Settings-Reference#cameras)" in out


def test_image_becomes_raw_url():
    out = _rw("![x](docs/screenshots/inventory.png)", "README.md")
    assert "raw.githubusercontent.com/" + REPO + "/main/docs/screenshots/inventory.png" in out


def test_unmirrored_repo_file_becomes_blob_url():
    out = _rw("[license](LICENSE)", "README.md")
    assert f"github.com/{REPO}/blob/main/LICENSE" in out


def test_external_and_app_links_untouched():
    assert "https://grocy.info/" in _rw("[g](https://grocy.info/)", "README.md")
    assert "(/ui/about)" in _rw("[a](/ui/about)", "README.md")
    assert "(#section)" in _rw("[s](#section)", "README.md")


def test_build_writes_pages_and_nav(tmp_path):
    count = build_wiki.build(_ROOT, tmp_path, REPO, "main")
    assert count >= 10
    assert (tmp_path / "Home.md").exists()
    assert (tmp_path / "_Sidebar.md").exists()
    assert (tmp_path / "_Footer.md").exists()
    sidebar = (tmp_path / "_Sidebar.md").read_text()
    assert "[Settings reference](Settings-Reference)" in sidebar
