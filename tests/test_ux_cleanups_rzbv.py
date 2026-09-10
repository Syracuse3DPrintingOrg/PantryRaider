"""UX audit cleanups + restore password re-entry (FoodAssistant-rzbv).

Covers four things the audit batch touched:

* the app login and Forager sign-in inputs now carry real <label>s (audit 5.1),
* the recipe preview / card images carry alt text (audit 5.2),
* the Settings side menu label and its Overview card label agree for the three
  panes whose labels had drifted (audit 3.1), and
* POST /admin/restore now re-confirms the current app password the same way the
  backup download does (eg1j LOW-4), refusing a wrong or missing password and
  changing nothing.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.passwords import hash_secret  # noqa: E402
import app.routers.admin as admin  # noqa: E402

TEMPLATES = SERVICE / "app" / "templates"


# --- 5.1 login labels -------------------------------------------------------

def test_login_inputs_have_associated_labels():
    """Every credential input on the login screen has a <label for=...> that
    points at its id, so a screen reader announces the field (not just its
    placeholder)."""
    html = (TEMPLATES / "login.html").read_text()
    for input_id in ("login-password", "login-totp-code",
                     "forager-email", "forager-password", "forager-code"):
        assert f'id="{input_id}"' in html, f"missing input id {input_id}"
        assert f'for="{input_id}"' in html, f"no <label for> tied to {input_id}"


# --- 5.2 image alt text -----------------------------------------------------

def test_recipe_preview_and_card_images_carry_alt():
    recipes = (TEMPLATES / "recipes.html").read_text()
    cook = (TEMPLATES / "cook.html").read_text()
    # The preview modal image (static element, alt also set in JS).
    m = re.search(r'<img[^>]*id="pv-image"[^>]*>', recipes)
    assert m and "alt=" in m.group(0), "pv-image has no alt"
    # The JS-built recipe row image and the cook suggestion card image.
    assert '<img src="${r.image}" alt=' in recipes, "recipe card image has no alt"
    assert '<img src="${s.image}" alt=' in cook, "cook card image has no alt"
    # The add-item photo preview.
    add = (TEMPLATES / "add.html").read_text()
    m = re.search(r'<img[^>]*id="preview-img"[^>]*>', add)
    assert m and "alt=" in m.group(0), "preview-img has no alt"


# --- 3.1 Settings label consistency -----------------------------------------

def _norm(s: str) -> str:
    return s.replace("&amp;", "&")


def test_sidebar_and_overview_labels_match_for_reconciled_panes():
    """The panes whose side-menu and Overview-card labels had drifted now
    read the same in both places. The Stream Deck is its own entry under
    Devices and the This Device entry is plain Start Page (FoodAssistant-gn6g),
    in the menu and on the Overview cards alike. Bandit Remotes and
    Thermometers & Sensors are the 2026-07-15 reorg names (label-only renames
    of the old Fleet & Remote Access and Thermometers panes)."""
    sidebar = _norm((TEMPLATES / "setup.html").read_text())
    overview = _norm((TEMPLATES / "setup" / "_pane_overview.html").read_text())
    for label in ("Start Page", "Stream Deck", "Bandit Remotes",
                  "Thermometers & Sensors", "Backups & Updates"):
        assert label in sidebar, f"side menu missing reconciled label: {label}"
        assert label in overview, f"overview card missing reconciled label: {label}"
    # The combined label is retired everywhere: one entry per surface.
    assert "Start Page & Stream Deck" not in sidebar, "combined menu label still present"
    assert "Start Page & Stream Deck" not in overview, "combined overview label still present"
    # And the old drifted "Deck" short form is gone from the Overview cards.
    assert "Start Page & Deck" not in overview, "stale overview label still present"


# --- eg1j LOW-4 restore password re-entry -----------------------------------

_ZIP_MAGIC = b"PK\x03\x04"
PASSWORD = "hunter2-long-enough"


def _restore_zip_bytes(grocy_url: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("foodassistant-data/settings.json",
                    json.dumps({"grocy_base_url": grocy_url}))
    return buf.getvalue()


@pytest.fixture
def client(monkeypatch, tmp_path):
    cwd = os.getcwd()
    os.chdir(SERVICE)
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text('{"grocy_base_url": "http://original"}')
    monkeypatch.setattr(settings, "data_dir", str(data), raising=False)
    from fastapi.testclient import TestClient
    from app.main import app
    try:
        yield TestClient(app)
    finally:
        os.chdir(cwd)


def _configured(monkeypatch, password=PASSWORD):
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "grocy_base_url", "http://original", raising=False)
    monkeypatch.setattr(settings, "grocy_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "auth_required", True, raising=False)
    monkeypatch.setattr(settings, "totp_secret", "", raising=False)
    monkeypatch.setattr(settings, "auth_password",
                        hash_secret(password) if password else "", raising=False)


def _login(client, password):
    return client.post("/ui/login", data={"password": password},
                       follow_redirects=False)


def _post_restore(client, zip_bytes, password):
    return client.post(
        "/admin/restore",
        files={"file": ("backup.zip", zip_bytes, "application/zip")},
        data={"restore_password": password},
    )


def test_restore_without_password_refused_and_changes_nothing(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, PASSWORD)
    r = _post_restore(client, _restore_zip_bytes("http://restored"), "")
    assert r.status_code == 403
    assert settings.grocy_base_url == "http://original"  # nothing applied
    # No pre-restore snapshot was taken, so _restore_zip never ran.
    data_dir = Path(settings.data_dir)
    assert not list(data_dir.parent.glob(f"{data_dir.name}.pre-restore-*"))


def test_restore_wrong_password_refused_and_changes_nothing(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, PASSWORD)
    r = _post_restore(client, _restore_zip_bytes("http://restored"), "wrong")
    assert r.status_code == 403
    assert settings.grocy_base_url == "http://original"


def test_restore_correct_password_proceeds(client, monkeypatch):
    _configured(monkeypatch)
    _login(client, PASSWORD)
    r = _post_restore(client, _restore_zip_bytes("http://restored"), PASSWORD)
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert settings.grocy_base_url == "http://restored"


def test_restore_open_install_proceeds_without_password(client, monkeypatch):
    _configured(monkeypatch, password="")  # no auth_password: open install
    with patch.object(type(settings), "is_configured", lambda self: True):
        r = _post_restore(client, _restore_zip_bytes("http://restored"), "")
    assert r.status_code == 200
    assert settings.grocy_base_url == "http://restored"


def test_restore_gate_shares_backup_helper(monkeypatch):
    """The restore gate uses the same current-password check as the backup
    download, so the two cannot drift apart."""
    monkeypatch.setattr(settings, "auth_password", hash_secret(PASSWORD), raising=False)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        admin._require_current_password("nope", "msg")
    assert ei.value.status_code == 403
    admin._require_current_password(PASSWORD, "msg")  # does not raise
