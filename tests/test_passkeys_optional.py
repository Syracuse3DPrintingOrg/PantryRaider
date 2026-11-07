"""The app starts without py_webauthn installed (FoodAssistant-kc2cy)."""
import subprocess
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
def test_app_starts_without_the_passkey_package():
    """An install whose code updated before its packages did still starts;
    passkeys are just not offered."""
    code = ("import sys; sys.modules['webauthn'] = None; sys.path.insert(0, '.');"
            "from app.services import passkeys as pk; assert pk.AVAILABLE is False;"
            "assert pk.for_site(type('S', (), {'local_passkeys': [{'id': 'a', 'public_key': 'b',"
            " 'rp_id': 'x'}]})(), 'x') == [];"
            "from app.main import app; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=SERVICE, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().endswith("ok")
