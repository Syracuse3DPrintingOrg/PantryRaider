"""IP-camera LAN discovery (FoodAssistant-d9rx). Network calls are injected so
the logic is exercised without touching the network."""
from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.services import camera_scan  # noqa: E402


class _Resp:
    def __init__(self, status_code=200, content=b"\xff\xd8\xff", ctype="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": ctype}


def test_looks_like_image_by_magic_and_ctype():
    assert camera_scan._looks_like_image(_Resp(content=b"\xff\xd8\xff", ctype="application/octet-stream"))
    assert camera_scan._looks_like_image(_Resp(content=b"junk", ctype="image/png"))
    assert not camera_scan._looks_like_image(_Resp(content=b"<html>", ctype="text/html"))


def test_probe_http_returns_first_working_path_and_auth(monkeypatch):
    good = "http://10.0.0.5/snap.jpg"

    def fetch(url):
        if url == good:
            return _Resp(200)
        return _Resp(404, content=b"", ctype="text/html")

    url, auth, brand, res = camera_scan._probe_http("10.0.0.5", 80, "http", 0.1, fetch=fetch)
    assert url == good and auth is False

    # All paths 401 -> no url, but auth flagged.
    url2, auth2, _b2, _r2 = camera_scan._probe_http(
        "10.0.0.9", 80, "http", 0.1, fetch=lambda u: _Resp(401, b"", "text/html"))
    assert url2 == "" and auth2 is True


def test_probe_http_reports_brand_for_a_known_path():
    good = "http://10.0.0.5/axis-cgi/jpg/image.cgi"
    def fetch(u):
        return _Resp(200) if u == good else _Resp(404, b"", "text/html")
    url, auth, brand, res = camera_scan._probe_http("10.0.0.5", 80, "http", 0.1, fetch=fetch)
    assert url == good and brand == "Axis"


def test_probe_with_auth_finds_snapshot_and_embeds_credentials(monkeypatch):
    monkeypatch.setattr(camera_scan, "_port_open", lambda ip, p, t: p == 80)
    def fetch(u):
        return _Resp(200) if u.endswith("/snapshot.jpg") else _Resp(401, b"", "text/html")
    out = camera_scan.probe_with_auth("10.0.0.5", "admin", "pw", fetch=fetch)
    assert out["ok"] is True
    assert out["snapshot_url"] == "http://admin:pw@10.0.0.5/snapshot.jpg"


def test_probe_with_auth_reports_failure(monkeypatch):
    monkeypatch.setattr(camera_scan, "_port_open", lambda ip, p, t: p == 80)
    out = camera_scan.probe_with_auth("10.0.0.5", "admin", "bad",
                                      fetch=lambda u: _Resp(401, b"", "text/html"))
    assert out["ok"] is False and out["error"]


def test_probe_camera_with_http_snapshot(monkeypatch):
    monkeypatch.setattr(camera_scan, "_port_open",
                        lambda ip, p, t: p == 80)  # only HTTP open

    def fetch(url):
        return _Resp(200) if url.endswith("/snapshot.jpg") else _Resp(404, b"", "text/html")

    cam = camera_scan.probe_camera("10.0.0.5", fetch=fetch)
    assert cam and cam["ip"] == "10.0.0.5"
    assert cam["snapshot_url"].endswith("/snapshot.jpg")
    assert cam["report"] is True and cam["kind"] == "snapshot"


def test_probe_camera_rtsp_only(monkeypatch):
    monkeypatch.setattr(camera_scan, "_port_open", lambda ip, p, t: p == 554)
    cam = camera_scan.probe_camera("10.0.0.6", fetch=lambda u: _Resp(404))
    assert cam and cam["rtsp"] is True and cam["snapshot_url"] == ""
    assert cam["report"] is True and cam["kind"] == "rtsp"


def test_probe_camera_auth_protected_is_reported(monkeypatch):
    # A password-protected snapshot (401) is still a camera worth listing.
    monkeypatch.setattr(camera_scan, "_port_open", lambda ip, p, t: p == 80)
    cam = camera_scan.probe_camera("10.0.0.8",
                                   fetch=lambda u: _Resp(401, b"", "text/html"))
    assert cam and cam["auth_required"] is True
    assert cam["report"] is True and cam["kind"] == "auth"


def test_probe_camera_plain_http_responds_but_not_reported(monkeypatch):
    # An HTTP host with no snapshot and no auth is a responder but not a camera,
    # so it counts toward "responded" but is filtered out of the camera list.
    monkeypatch.setattr(camera_scan, "_port_open", lambda ip, p, t: p == 80)
    cam = camera_scan.probe_camera("10.0.0.7",
                                   fetch=lambda u: _Resp(200, b"<html>", "text/html"))
    assert cam is not None and cam["report"] is False


def test_scan_returns_diagnostics(monkeypatch):
    # One real camera + one bare web host: cameras has 1, responded counts both.
    def fake_probe(ip, timeout, fetch=None):
        if ip.endswith(".5"):
            return {"ip": ip, "ports": [80], "snapshot_url": "http://x/s.jpg",
                    "rtsp": False, "auth_required": False, "report": True,
                    "kind": "snapshot", "name": ip}
        if ip.endswith(".6"):
            return {"ip": ip, "ports": [80], "snapshot_url": "", "rtsp": False,
                    "auth_required": False, "report": False, "kind": "open", "name": ip}
        return None
    monkeypatch.setattr(camera_scan, "probe_camera", fake_probe)
    out = camera_scan.scan_for_cameras("10.0.0.0/29")
    assert len(out["cameras"]) == 1
    assert out["responded"] == 2
    assert out["scanned"] >= 1


def test_scan_rejects_bad_cidr():
    out = camera_scan.scan_for_cameras("not-a-cidr")
    assert out.get("error")


def test_looks_dockerish():
    assert camera_scan.looks_dockerish("172.19.0.0/24") is True
    assert camera_scan.looks_dockerish("172.17.0.0/16") is True
    assert camera_scan.looks_dockerish("192.168.1.0/24") is False
    assert camera_scan.looks_dockerish("10.0.0.0/24") is False
    assert camera_scan.looks_dockerish("172.200.0.0/24") is False  # not the private range


def test_best_lan_cidr_prefers_real_lan_over_docker(monkeypatch):
    # With both a Docker 172.x and a real 192.168.x interface visible, pick the LAN.
    monkeypatch.setattr(camera_scan, "_candidate_ips",
                        lambda: {"172.19.0.5", "192.168.1.40"})
    assert camera_scan.best_lan_cidr() == "192.168.1.0/24"
    # 10.x is preferred over 172.x too.
    monkeypatch.setattr(camera_scan, "_candidate_ips", lambda: {"172.19.0.5", "10.1.2.3"})
    assert camera_scan.best_lan_cidr() == "10.1.2.0/24"
    # Only Docker visible (a bridge-networked container): we still return it, and
    # the endpoint flags it as dockerish so the UI tells the user to correct it.
    monkeypatch.setattr(camera_scan, "_candidate_ips", lambda: {"172.19.0.5"})
    assert camera_scan.best_lan_cidr() == "172.19.0.0/24"


def test_camera_scan_default_uses_grocy_url_lan(monkeypatch):
    """The camera scan default inherits the LAN from the Grocy/Mealie URL, the
    same as the device scan, so a containerized server does not default to its
    Docker subnet (Pantry Raider)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    from app.config import settings
    from app.services import lan_scan
    monkeypatch.setattr(settings, "grocy_base_url", "http://192.168.1.170:9383", raising=False)
    monkeypatch.setattr(settings, "mealie_base_url", "", raising=False)
    monkeypatch.setattr(settings, "grocy_public_url", "", raising=False)
    monkeypatch.setattr(settings, "mealie_public_url", "", raising=False)
    monkeypatch.setattr(settings, "lan_scan_cidr", "", raising=False)
    # Only a Docker candidate available -> falls through to the Grocy URL host.
    monkeypatch.setattr(lan_scan, "default_cidr", lambda: "172.19.0.0/24")
    assert lan_scan.resolve_lan_cidr("", candidates=["172.19.0.0/24"]) == "192.168.1.0/24"
