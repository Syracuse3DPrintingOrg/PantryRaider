"""Camera logins never reach a shareable file (security review, Oct 2026).

The file log recorded every outbound request's full URL (a camera snapshot URL
can carry ?user=..&password=..), and the support bundle and the redacted
backup only blanked the named secret settings, so a Reolink password or a
login inside a camera URL survived both. These pin the log scrub, the 0600 log
file, the bundle and backup redaction, and the restore that puts this device's
own camera logins back.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import cameras, diagnostics, support_bundle  # noqa: E402

PW = "CAMPASS-MARK-9"
REO = "REOLINK-PASS-MARK"
_CAMS = [
    {"name": "Porch", "source": "manual",
     "snapshot_url": f"http://127.0.0.1:1/cgi-bin/api.cgi?cmd=Snap&user=admin&password={PW}",
     "stream_url": f"rtsp://admin:{PW}@192.168.1.31:554/h264"},
    {"name": "Door", "source": "reolink", "host": "192.168.1.30",
     "username": "admin", "password": REO},
    {"name": "Plain", "source": "manual", "snapshot_url": "http://cam.local/snap.jpg"},
]


def test_scrub_urls_drops_login_and_query():
    line = (f'HTTP Request: GET http://cam/api.cgi?user=admin&password={PW} '
            f'"HTTP/1.1 200 OK" and rtsp://admin:{PW}@cam:554/h264')
    out = diagnostics.scrub_urls(line)
    assert PW not in out
    assert "http://cam/api.cgi?[redacted]" in out
    assert "rtsp://[redacted]@cam:554/h264" in out
    assert diagnostics.scrub_urls("nothing here") == "nothing here"


def test_file_log_never_holds_a_url_login(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        for h in before:
            if getattr(h, diagnostics._HANDLER_TAG, False):
                root.removeHandler(h)
        path = diagnostics.configure_file_logging(str(tmp_path), debug=False)
        logging.getLogger("httpx").info(
            'HTTP Request: GET %s "HTTP/1.1 200 OK"', _CAMS[0]["snapshot_url"])
        for h in root.handlers:
            h.flush()
        text = path.read_text()
        assert "HTTP Request" in text and PW not in text
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        for h in list(root.handlers):
            if getattr(h, diagnostics._HANDLER_TAG, False) and h not in before:
                root.removeHandler(h)
                h.close()
        for h in before:
            if h not in root.handlers:
                root.addHandler(h)


def test_redact_camera_entry_blanks_logins_and_url_credentials():
    out = cameras.redact_camera_list(_CAMS)
    blob = json.dumps(out)
    assert PW not in blob and REO not in blob
    assert out[1]["username"] == "[redacted]" and out[1]["host"] == "192.168.1.30"
    assert out[0]["snapshot_url"].startswith("http://127.0.0.1:1/cgi-bin/api.cgi?cmd=Snap")
    assert out[2] == _CAMS[2]


def test_bundle_settings_dump_scrubs_cameras():
    raw = json.dumps({"grocy_api_key": "g", "streamdeck_cameras": _CAMS})
    out = support_bundle.redacted_settings_dump(raw)
    assert PW not in out and REO not in out


def test_secret_values_include_camera_passwords():
    s = SimpleNamespace(streamdeck_cameras=_CAMS)
    vals = support_bundle.secret_values(s)
    assert PW in vals and REO in vals
    assert "admin" not in vals  # a username is too common to scrub from text


def test_redacted_backup_and_restore_keep_camera_logins():
    from app.routers.admin import _redact_settings
    blob = _redact_settings(json.dumps({"streamdeck_cameras": _CAMS}).encode())
    assert PW.encode() not in blob and REO.encode() not in blob
    restored = json.loads(blob)["streamdeck_cameras"]
    merged = cameras.restore_camera_credentials(restored, _CAMS)
    assert merged == _CAMS
    # A camera this device does not have stays as the archive left it.
    assert cameras.restore_camera_credentials(restored, []) == restored
