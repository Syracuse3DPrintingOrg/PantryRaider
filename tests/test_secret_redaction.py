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
