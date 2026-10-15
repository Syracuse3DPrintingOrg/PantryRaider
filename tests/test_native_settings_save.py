"""Saving settings on a native install never loads the Mealie client.

/setup/save and /setup/maintenance/reload reset the Mealie caches, but only
when services.mealie is already loaded; a native install has nothing to reset
and must not import it. When Mealie is in use the reset still happens.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))


def test_native_settings_save_does_not_import_mealie(tmp_path):
    # A subprocess, so this suite's own earlier imports cannot mask a regression.
    code = """
import sys
from unittest.mock import patch
sys.path.insert(0, {service!r})
from app.config import settings
settings.data_dir = {data_dir!r}
settings.recipes_backend = ""
settings.shopping_backend = ""
settings.mealie_base_url = ""
settings.mealie_api_key = ""
settings.auth_required = False
from fastapi.testclient import TestClient
from app.main import app
from app.services import recipe_source
assert recipe_source.active_backend() == "native"
client = TestClient(app)
with patch.object(type(settings), "is_configured", lambda self: True):
    r = client.post("/setup/save", json={{"quiet_mode": True}})
    assert r.status_code == 200, r.text
    r = client.post("/setup/maintenance/reload")
    assert r.status_code == 200, r.text
assert "app.services.mealie" not in sys.modules, "Mealie was imported"
print("NATIVE_SAVE_OK")
""".format(service=str(SERVICE), data_dir=str(tmp_path))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(SERVICE),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "NATIVE_SAVE_OK" in proc.stdout


def test_reset_mealie_cache_clears_a_loaded_client(monkeypatch):
    from app.services import mealie, recipe_source
    monkeypatch.setattr(mealie, "_scope", "households")
    recipe_source.reset_mealie_cache()
    assert mealie._scope is None
