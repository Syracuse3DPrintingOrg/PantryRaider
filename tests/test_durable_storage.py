"""Settings, keys and the access record survive a power cut (FoodAssistant-nw31c).

A file switched off mid-save comes back empty or full of NUL bytes. Each store
must put back its last good copy, keep the damaged file for a person, and say
so in the inbox; an install with a password must never come up open.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import durable
from app.config import SETTINGS_LOCKED_PASSWORD, SettingsLockedError, settings
from app.passwords import hash_secret, verify_secret

SERVICE = Path(__file__).resolve().parents[1] / "service"


@pytest.fixture(autouse=True)
def _clear_notices():
    durable.take_notices()
    yield
    durable.take_notices()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    return tmp_path


# --- The helper --------------------------------------------------------------


def test_write_keeps_a_last_good_copy_that_matches(tmp_path):
    p = tmp_path / "store.json"
    durable.write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    assert json.loads(durable.last_good_path(p).read_text()) == {"a": 1}
    assert (p.stat().st_mode & 0o777) == 0o600
    # No temp file is left behind.
    assert sorted(x.name for x in tmp_path.iterdir()) == ["store.json", "store.json.last-good"]


def test_short_lived_state_skips_the_copy(tmp_path):
    p = tmp_path / "pairing.json"
    durable.write_json(p, {"requests": {}}, keep_last_good=False)
    assert not durable.last_good_path(p).exists()


@pytest.mark.parametrize("damage", [b"", b"\x00" * 64, b'{"a": 1, "b"', b"[1, 2]"])
def test_damaged_file_comes_back_from_its_last_good_copy(tmp_path, damage):
    p = tmp_path / "store.json"
    durable.write_json(p, {"a": 1})
    p.write_bytes(damage)
    loaded = durable.read_json(p, what="The store")
    assert loaded.state == durable.RECOVERED
    assert loaded.doc == {"a": 1}
    assert json.loads(p.read_text()) == {"a": 1}
    aside = list(tmp_path.glob("store.json.corrupt-*"))
    assert len(aside) == 1 and aside[0].read_bytes() == damage
    notes = durable.pending_notices()
    assert len(notes) == 1 and "last good copy was put back" in notes[0]["body"]
    assert aside[0].name in notes[0]["body"]


def test_missing_is_not_damage(tmp_path):
    loaded = durable.read_json(tmp_path / "nothing.json")
    assert loaded.state == durable.MISSING and loaded.doc is None
    assert not durable.pending_notices()


def test_no_good_copy_sets_aside_or_keeps_in_place(tmp_path):
    p = tmp_path / "store.json"
    p.write_text("{broken")
    loaded = durable.read_json(p)
    assert loaded.state == durable.DAMAGED
    assert not p.exists() and loaded.set_aside is not None
    assert loaded.set_aside.read_text() == "{broken"

    q = tmp_path / "kept.json"
    q.write_text("{broken")
    loaded = durable.read_json(q, keep_damaged_in_place=True)
    assert loaded.state == durable.DAMAGED
    assert q.read_text() == "{broken"


def test_a_damaged_last_good_copy_is_never_used(tmp_path):
    p = tmp_path / "store.json"
    p.write_text("{broken")
    durable.last_good_path(p).write_text("{also broken")
    assert durable.read_json(p).state == durable.DAMAGED


def test_side_copies_are_recognised():
    assert durable.is_side_copy("settings.json.last-good")
    assert durable.is_side_copy("credentials.json.corrupt-20261015T101010")
    assert not durable.is_side_copy("settings.json")


# --- settings.json -----------------------------------------------------------


def test_save_recovers_damaged_settings_and_keeps_every_key(data_dir):
    sf = data_dir / "settings.json"
    settings.save({"grocy_api_key": "grocy-key", "mealie_base_url": "http://m:9285"})
    sf.write_bytes(b"\x00" * 200)   # the power went off
    settings.save({"ui_theme": "forest"})
    on_disk = json.loads(sf.read_text())
    assert on_disk["grocy_api_key"] == "grocy-key"
    assert on_disk["mealie_base_url"] == "http://m:9285"
    assert on_disk["ui_theme"] == "forest"
    assert list(data_dir.glob("settings.json.corrupt-*"))
    assert any("settings file" in n["body"] for n in durable.pending_notices())


def test_save_refuses_to_write_over_settings_with_no_good_copy(data_dir):
    sf = data_dir / "settings.json"
    sf.write_text('{"auth_password": "scrypt$..." , "grocy_api_ke')
    with pytest.raises(SettingsLockedError):
        settings.save({"secret_key": "abc"})
    # The damaged file is still there, untouched, for a person to look at.
    assert sf.read_text().startswith('{"auth_password"')


def test_old_settings_bak_is_a_fallback(data_dir):
    sf = data_dir / "settings.json"
    (data_dir / "settings.json.bak").write_text(json.dumps({"grocy_api_key": "from-bak"}))
    sf.write_text("")
    settings.save({"ui_theme": "forest"})
    assert json.loads(sf.read_text())["grocy_api_key"] == "from-bak"


def _start(data_dir: Path, code: str, env_extra: dict | None = None) -> dict:
    """Import the app's config in a fresh process, the way a restart does."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("AUTH_PASSWORD", "PYTEST_XDIST_WORKER")}
    env["DATA_DIR"] = str(data_dir)
    env.update(env_extra or {})
    script = ("import json, sys; sys.path.insert(0, %r)\n" % str(SERVICE)) + code
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                         text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


_REPORT = (
    "from app import config\n"
    "from app.passwords import verify_secret\n"
    "print(json.dumps({'damaged': config.SETTINGS_DAMAGED,\n"
    "  'password': config.settings.auth_password,\n"
    "  'grocy': config.settings.grocy_api_key,\n"
    "  'opens': verify_secret('hunter2', config.settings.auth_password)}))\n")


def test_restart_after_damage_keeps_the_password_from_the_last_good_copy(tmp_path):
    good = {"auth_password": hash_secret("hunter2"), "grocy_api_key": "grocy-key",
            "secret_key": "s" * 64, "device_id": "abcd1234"}
    sf = tmp_path / "settings.json"
    durable.write_json(sf, good, indent=2)
    sf.write_bytes(b"")
    report = _start(tmp_path, _REPORT)
    assert report["damaged"] is False
    assert report["password"] == good["auth_password"] and report["opens"] is True
    assert report["grocy"] == "grocy-key"
    assert json.loads(sf.read_text())["auth_password"] == good["auth_password"]
    assert list(tmp_path.glob("settings.json.corrupt-*"))


def test_restart_with_no_good_copy_stays_locked(tmp_path):
    sf = tmp_path / "settings.json"
    sf.write_bytes(b"\x00" * 300)
    report = _start(tmp_path, _REPORT)
    assert report["damaged"] is True
    # An install that may have had a password never comes up open.
    assert report["password"] == SETTINGS_LOCKED_PASSWORD
    assert report["opens"] is False
    assert not verify_secret("", SETTINGS_LOCKED_PASSWORD)
    # The start-up auto-saves (secret key, device id) did not write over it.
    assert sf.read_bytes() == b"\x00" * 300


def test_locked_install_can_be_opened_with_auth_password(tmp_path):
    (tmp_path / "settings.json").write_text("{nope")
    report = _start(tmp_path, _REPORT, {"AUTH_PASSWORD": "hunter2"})
    assert report["damaged"] is True and report["opens"] is True


def test_locked_login_page_says_why(monkeypatch):
    from app import config
    from app.routers import ui
    monkeypatch.setattr(config, "SETTINGS_DAMAGED", True)
    assert "could not be read" in ui._settings_locked_note()
    monkeypatch.setattr(config, "SETTINGS_DAMAGED", False)
    assert ui._settings_locked_note() == ""


# --- Inbox -------------------------------------------------------------------


def test_queued_notices_reach_the_inbox(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.services import action_items

    engine = create_engine(f"sqlite:///{tmp_path}/inbox.db")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        p = tmp_path / "store.json"
        durable.write_json(p, {"a": 1})
        p.write_text("")
        durable.read_json(p, what="The store")
        items = action_items.list_active(db)
        assert [i["kind"] for i in items] == [action_items.KIND_STORAGE_REPAIRED]
        assert items[0]["title"] == "The store was repaired"
        assert not durable.pending_notices()
        # A second read does not raise it again.
        assert len(action_items.list_active(db)) == 1
    finally:
        db.close()
        engine.dispose()


# --- Keys and the access record ----------------------------------------------


def test_credentials_come_back_from_the_last_good_copy(data_dir):
    from app.services import credentials

    path = data_dir / credentials.FILENAME
    doc = {"v": 1, "records": {"c_0123456789abcdef": {"id": "c_0123456789abcdef"}}}
    assert credentials._write_disk(doc)
    assert durable.last_good_path(path).exists()
    path.write_bytes(b"\x00" * 50)
    data, mtime = credentials._read_disk()
    assert data == doc and mtime is not None
    assert list(data_dir.glob("credentials.json.corrupt-*"))


def test_access_record_comes_back_from_the_last_good_copy(data_dir):
    from app.services import auth_state

    path = data_dir / auth_state.FILENAME
    doc = {"v": 1, "origin": "fresh"}
    assert auth_state._write_disk(doc)
    path.write_text("")
    data, _mt = auth_state._read_disk()
    assert data == doc
    # With no good copy it is left in place and reads as unreadable.
    path.write_text("{x")
    durable.last_good_path(path).unlink()
    data, mt = auth_state._read_disk()
    assert data is None and mt is not None and path.read_text() == "{x"


def test_side_copies_never_travel_in_a_backup(data_dir):
    import io
    import zipfile

    from app.routers import admin

    durable.write_json(data_dir / "credentials.json", {"v": 1, "records": {}})
    (data_dir / "settings.json").write_text("{}")
    (data_dir / "staples.txt").write_text("salt\n")
    (data_dir / "foo.json.corrupt-20261015T000000").write_text("x")
    (data_dir / "db-snapshots").mkdir()
    (data_dir / "db-snapshots" / "foodassistant-20261015T000000.db").write_bytes(b"x")
    for secrets in (False, True):
        with zipfile.ZipFile(io.BytesIO(admin._build_zip(include_secrets=secrets)[0])) as zf:
            names = {n.split("/", 1)[1] for n in zf.namelist()}
        assert "staples.txt" in names
        assert not any(durable.is_side_copy(n) or n.startswith("db-snapshots") for n in names), names


def test_no_store_writes_json_the_old_way():
    """The stores this change covers write through durable.py, never a bare
    write_text(json.dumps(...)) or an unflushed temp file."""
    root = SERVICE / "app"
    for rel in ("config.py", "services/credentials.py", "services/auth_state.py",
                "services/pairing.py", "services/setup_code.py", "services/local_agents.py"):
        text = (root / rel).read_text()
        assert "write_text(json.dumps" not in text, rel
        assert "settings.json.tmp" not in text, rel
        assert "durable" in text, rel
