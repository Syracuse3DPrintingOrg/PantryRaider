"""The Backups pane: the restore script and what the pane tells the user."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANES_JS = ROOT / "service" / "app" / "static" / "js" / "setup" / "panes.js"


def _function_source(text: str, name: str) -> str:
    start = text.index(f"async function {name}(")
    nxt = re.search(r"\n(async )?function ", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def test_restore_password_field_is_in_scope_for_finally():
    # It was declared inside the try, so the finally (which clears it and
    # resets the button) threw a ReferenceError on every restore.
    src = _function_source(PANES_JS.read_text(), "restoreBackup")
    decl = src.index("const pwField")
    assert decl < src.index("try {")
    assert "if (pwField) pwField.value = '';" in src[src.index("finally"):]


BACKUPS_PANE = ROOT / "service" / "app" / "templates" / "setup" / "_pane_backups.html"


def test_password_label_matches_the_download_gate():
    # Every download needs the password (the redacted one still carries the
    # whole database), so the label must not say only a secret-carrying one does.
    text = BACKUPS_PANE.read_text()
    assert "Needed to download a secret-carrying backup." not in text
    assert "Needed to download any backup." in text


def test_a_redacted_download_still_asks_for_the_password(monkeypatch, tmp_path):
    import asyncio
    import sys
    sys.path.insert(0, str(ROOT / "service"))
    import pytest
    from fastapi import HTTPException
    from app.config import settings
    from app.routers import admin
    from app.passwords import hash_secret
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "auth_password", hash_secret("pw"), raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.download_backup(backup_password="", include_secrets=False))
    assert exc.value.status_code == 403


def test_backup_copy_says_what_is_and_is_not_in_the_zip():
    text = BACKUPS_PANE.read_text()
    # The restore replaces native recipes, so it must not claim recipes are
    # untouched, and the pane must say the inventory is not in the zip.
    assert "Your inventory and recipe data are not touched." not in text
    assert "Recipes and the meal plan saved in Pantry Raider are replaced" in text
    assert "does not hold your inventory or shopping list" in text
    # A Compose user has no scripts/ folder: give the command itself.
    assert "scripts/backup.sh</code> on the host" not in text
    assert "sudo tar czf pantryraider-full-backup.tar.gz data grocy" in text
