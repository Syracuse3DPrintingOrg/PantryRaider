"""The satellite key and tunnel token must be redacted from shareable exports
(FoodAssistant-m4ir).

The support bundle's settings dump and the "redacted" backup download both blank
SECRET_SETTING_KEYS by name. upstream_api_key (a satellite's full-access key to
its server) and tunnel_token (the Forager/Pangolin credential) were missing, so
they leaked verbatim. These confirm they are now on the list and scrubbed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import SECRET_SETTING_KEYS  # noqa: E402
from app.services import support_bundle  # noqa: E402


def test_new_secrets_are_in_the_list():
    assert "upstream_api_key" in SECRET_SETTING_KEYS
    assert "tunnel_token" in SECRET_SETTING_KEYS


def test_support_bundle_dump_blanks_the_new_secrets():
    raw = json.dumps({
        "upstream_api_key": "SAT-FULL-ACCESS-KEY",
        "tunnel_token": "TUNNEL-CREDENTIAL",
        "grocy_base_url": "http://grocy:9383",  # not a secret: stays visible
    })
    dumped = support_bundle.redacted_settings_dump(raw)
    data = json.loads(dumped)
    assert data["upstream_api_key"] == "[redacted]"
    assert data["tunnel_token"] == "[redacted]"
    assert data["grocy_base_url"] == "http://grocy:9383"


def test_backup_redaction_blanks_the_new_secrets():
    from app.routers.admin import _redact_settings
    raw = json.dumps({
        "upstream_api_key": "SAT-FULL-ACCESS-KEY",
        "tunnel_token": "TUNNEL-CREDENTIAL",
    }).encode()
    out = json.loads(_redact_settings(raw))
    assert out["upstream_api_key"] == ""
    assert out["tunnel_token"] == ""


class _Obj:
    """A stand-in settings object for value scrubbing."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
    def __getattr__(self, name):
        return ""


def test_secret_values_now_include_the_new_secrets():
    obj = _Obj(upstream_api_key="SAT-KEY-XYZ", tunnel_token="TUN-123")
    values = support_bundle.secret_values(obj)
    assert "SAT-KEY-XYZ" in values
    assert "TUN-123" in values


# The spare AI keys (ai_extra_keys, a dict of lists per provider) and the extra
# satellite keys (extra_api_keys, a list) were turned into one string each, the
# text of the whole dict or list, which never appears in a log. So the keys
# inside were never scrubbed from the log download, the support bundle, or the
# redacted backup.

def test_spare_ai_keys_and_extra_satellite_keys_are_scrubbed_one_by_one():
    obj = _Obj(ai_extra_keys={"gemini": ["AIza-SPARE-ONE", "AIza-SPARE-TWO"],
                              "openai": ["sk-SPARE-THREE"]},
               extra_api_keys=["SAT-EXTRA-A", "SAT-EXTRA-B"])
    values = support_bundle.secret_values(obj)
    for key in ("AIza-SPARE-ONE", "AIza-SPARE-TWO", "sk-SPARE-THREE",
                "SAT-EXTRA-A", "SAT-EXTRA-B"):
        assert key in values


def test_parked_stack_snapshot_scrubs_only_its_secret_fields():
    obj = _Obj(hosted_config_snapshot={
        "gemini_api_key": "AIza-PARKED-KEY", "grocy_api_key": "GROCY-PARKED-KEY",
        "gemini_model": "gemini-2.5-flash", "grocy_base_url": "http://grocy:80",
        "llm_expiry_enabled": True})
    values = support_bundle.secret_values(obj)
    assert "AIza-PARKED-KEY" in values and "GROCY-PARKED-KEY" in values
    # Plain settings stay readable in the log.
    assert "gemini-2.5-flash" not in values
    assert "http://grocy:80" not in values
    assert "True" not in values


def test_support_bundle_log_hides_the_spare_keys(tmp_path):
    from app.services import diagnostics
    log = diagnostics.log_path(str(tmp_path))
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("POST https://example.test key=AIza-SPARE-ONE sat=SAT-EXTRA-A\n")
    obj = _Obj(data_dir=str(tmp_path),
               ai_extra_keys={"gemini": ["AIza-SPARE-ONE"]},
               extra_api_keys=["SAT-EXTRA-A"])
    files = support_bundle.build_bundle_files(obj)
    text = files["logs/foodassistant.log"]
    assert "AIza-SPARE-ONE" not in text and "SAT-EXTRA-A" not in text
    assert text.count("[redacted]") == 2


def test_admin_scrub_list_is_the_support_bundle_list(monkeypatch):
    from app.config import settings
    from app.routers import admin
    monkeypatch.setattr(settings, "ai_extra_keys", {"anthropic": ["sk-ant-SPARE-KEY"]})
    monkeypatch.setattr(settings, "extra_api_keys", ["SAT-EXTRA-KEY-9"])
    values = admin._secret_values()
    assert "sk-ant-SPARE-KEY" in values
    assert "SAT-EXTRA-KEY-9" in values


def test_log_download_hides_the_spare_keys(monkeypatch, tmp_path):
    import asyncio
    from app.config import settings
    from app.routers import admin
    from app.services import diagnostics
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "ai_extra_keys", {"gemini": ["AIza-SPARE-LOGGED"]})
    monkeypatch.setattr(settings, "extra_api_keys", ["SAT-EXTRA-LOGGED"])
    log = diagnostics.log_path(str(tmp_path))
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("calling with AIza-SPARE-LOGGED for SAT-EXTRA-LOGGED\n")

    async def body() -> bytes:
        resp = await admin.download_logs()
        return b"".join([chunk async for chunk in resp.body_iterator])

    text = asyncio.run(body()).decode()
    assert "AIza-SPARE-LOGGED" not in text and "SAT-EXTRA-LOGGED" not in text
    assert "[redacted]" in text
