"""The Home Assistant part of the fleet proxy (/api/proxy/ha).

services/ha_proxy.py decides what the hop carries (three request shapes, only
for what the owner has set up, the key policy enforced exactly as the rest of
the app enforces it) and routers/proxy.py answers. Home Assistant itself is
never contacted: services/ha_client.py is stubbed, and so are the allowlist
lookups where a test is about the hop rather than the settings.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.routers import proxy  # noqa: E402
from app.services import access, ha_allowlist, ha_policy, ha_proxy  # noqa: E402

HA_TOKEN = "ha-owner-token-never-shown"
LOOPBACK = ("127.0.0.1", 50000)
NOT_SET_UP = {"detail": "That Home Assistant action is not set up here.",
              "code": "ha_not_allowed"}

SLOTS = [
    {"entity_id": "light.kitchen", "service": "light.toggle", "label": "Kitchen"},
    {"entity_id": "lock.front_door", "service": "lock.unlock", "label": "Door"},
    {"entity_id": "light.porch,light.hall", "service": "light.turn_on", "label": "Both"},
]


# --- parse -----------------------------------------------------------------------

def _svc_body(value):
    return json.dumps({"entity_id": value}).encode()


def test_parse_state_and_camera():
    call = ha_proxy.parse("GET", ["api", "states", "Light.Kitchen"])
    assert call.kind == "state" and call.entity_ids == ("light.kitchen",)
    assert call.path() == "api/states/light.kitchen"
    call = ha_proxy.parse("get", ["api", "camera_proxy", "camera.front"])
    assert call.kind == "camera" and call.entity_ids == ("camera.front",)
    for segs in (["api", "states", "light.a,light.b"], ["api", "states", "all"],
                 ["api", "states"], ["api", "camera_proxy", "light.kitchen"],
                 ["api", "states", "light.kitchen", "extra"], ["api", "config"],
                 ["api", "camera_proxy_stream", "camera.front"]):
        with pytest.raises(ha_proxy.HaNotAllowed):
            ha_proxy.parse("GET", segs)


def test_parse_service_shapes():
    call = ha_proxy.parse("POST", ["api", "services", "light", "turn_on"],
                          _svc_body(" Light.Kitchen , LIGHT.hall "))
    assert call.kind == "service" and call.service_id == "light.turn_on"
    assert call.entity_ids == ("light.kitchen", "light.hall")
    # The forwarded body carries the normalized comma string.
    assert json.loads(call.body_bytes()) == {"entity_id": "light.kitchen,light.hall"}
    call = ha_proxy.parse("POST", ["api", "services", "Light", "Toggle"],
                          _svc_body(["light.a", "LIGHT.B"]))
    assert call.service_id == "light.toggle" and call.entity_ids == ("light.a", "light.b")
    assert call.path() == "api/services/light/toggle"


def test_parse_entity_limit_is_the_policys():
    ids = [f"light.l{i}" for i in range(ha_policy.MAX_ENTITIES)]
    assert ha_policy.MAX_ENTITIES == 20
    call = ha_proxy.parse("POST", ["api", "services", "light", "turn_on"], _svc_body(ids))
    assert len(call.entity_ids) == 20
    ha_proxy.parse("POST", ["api", "services", "light", "turn_on"], _svc_body(",".join(ids)))
    for too_many in (ids + ["light.l20"], ",".join(ids + ["light.l20"])):
        with pytest.raises(ha_proxy.HaNotAllowed):
            ha_proxy.parse("POST", ["api", "services", "light", "turn_on"],
                           _svc_body(too_many))


@pytest.mark.parametrize("body", [
    _svc_body("all"), _svc_body("none"), _svc_body("ALL"), _svc_body(""),
    _svc_body([]), _svc_body(["light.a", ""]), _svc_body("light.a,,light.b"),
    _svc_body(["light.a", 5]), _svc_body(None), _svc_body({"x": 1}),
    json.dumps({"entity_id": "light.a", "brightness": 255}).encode(),
    json.dumps({"area_id": "kitchen"}).encode(), json.dumps({}).encode(),
    json.dumps(["light.a"]).encode(), b"", b"not json", b"\xff\xfe",
    _svc_body("light.a/../x"), _svc_body("light"), _svc_body("light.a b"),
    _svc_body("light.a" + "x" * 20000),
])
def test_parse_refuses_other_bodies(body):
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.parse("POST", ["api", "services", "light", "turn_on"], body)


@pytest.mark.parametrize("method,segs", [
    ("POST", ["api", "services", "light-x", "turn_on"]),
    ("POST", ["api", "services", "light", "turn on"]),
    ("POST", ["api", "services", "light", "turn_on", "x"]),
    ("POST", ["api", "services", "light"]),
    ("POST", ["api", "states", "light.kitchen"]),
    ("PUT", ["api", "services", "light", "turn_on"]),
    ("DELETE", ["api", "states", "light.kitchen"]),
    ("GET", ["api", "services", "light", "turn_on"]),
    ("POST", ["api", "events", "x"]),
    ("POST", ["api", "template", "x"]),
])
def test_parse_refuses_bad_domains_and_methods(method, segs):
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.parse(method, segs, _svc_body("light.kitchen"))


# --- authorize (pure) ----------------------------------------------------------

class _S:
    """Settings stand-in for authorize()."""

    def __init__(self, server_version=0, satellite=False, upstream_version=0):
        self.deployment_mode = "pi_remote" if satellite else "server"
        self.ha_key_policy_version = server_version
        self.upstream_ha_policy_version = upstream_version
        self.streamdeck_ha_slots = [dict(s) for s in SLOTS]
        self.streamdeck_key_overrides = []
        self.streamdeck_cameras = []
        self.gadget_ha_entities = []

    def is_satellite(self):
        return self.deployment_mode == "pi_remote"


SAT_KEY = access.Principal("key", "satellite", "c_0123456789abcdef", origin="lan_direct")
DECK = access.Principal("agent", "deck", "agent:streamdeck", origin="plain_loopback")


def _service(entity, service):
    domain, name = service.split(".")
    return ha_proxy.parse("POST", ["api", "services", domain, name], _svc_body(entity))


@pytest.mark.parametrize("principal", [
    SAT_KEY, DECK,
    access.Principal("key", "legacy", "lg_0123456789abcdef"),
    access.Principal("key", "admin", "c_0000000000000001"),
    access.Principal("legacy_loopback", "admin", None, origin="plain_loopback"),
])
def test_authorize_accepts_the_fleet_callers(principal):
    auth = ha_proxy.authorize(_service("light.kitchen", "light.toggle"), principal, _S(), [])
    assert auth.source == ("satellite" if principal.kind == "key" else "deck")
    assert auth.entries["light.kitchen"]["label"] == "Kitchen"


@pytest.mark.parametrize("principal", [
    access.Principal("key", "ha", "c_1"), access.Principal("key", "ha_rest", "c_2"),
    access.Principal("key", "display", "c_3"), access.Principal("key", "viewer", "c_4"),
    access.Principal("agent", "reader", "agent:gadgets", origin="plain_loopback"),
])
def test_authorize_refuses_narrower_keys(principal):
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.authorize(_service("light.kitchen", "light.toggle"), principal, _S(), [])


@pytest.mark.parametrize("principal", [
    access.ANONYMOUS("lan_direct"), access.ANONYMOUS("plain_loopback", bad_key=True),
    access.Principal("session", "admin", None, origin="lan_direct"),
    access.Principal("kiosk", "kiosk", None, origin="plain_loopback"),
    # Old trust counts only for programs on this device itself.
    access.Principal("legacy_loopback", "admin", None, origin="lan_direct"),
])
def test_authorize_wants_a_credential(principal):
    with pytest.raises(ha_proxy.HaUnauthorized) as info:
        ha_proxy.authorize(_service("light.kitchen", "light.toggle"), principal, _S(), [])
    assert info.value.status == 401 and info.value.body() == {"detail": "Unauthorized"}


def test_one_unconfigured_entity_refuses_the_whole_call():
    s = _S()
    ha_proxy.authorize(_service("light.porch,light.hall", "light.turn_on"), SAT_KEY, s, [])
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.authorize(_service("light.porch,light.garage", "light.turn_on"),
                           SAT_KEY, s, [])
    with pytest.raises(ha_proxy.HaNotAllowed):
        # A configured entity with a service no key makes.
        ha_proxy.authorize(_service("light.kitchen", "light.turn_off"), SAT_KEY, s, [])


def test_reads_need_a_configured_entity(monkeypatch):
    s = _S()
    ha_proxy.authorize(ha_proxy.parse("GET", ["api", "states", "light.hall"]), DECK, s, [])
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.authorize(ha_proxy.parse("GET", ["api", "states", "sensor.secret"]),
                           DECK, s, [])
    monkeypatch.setattr(ha_allowlist, "camera_entities", lambda settings: {"camera.front"})
    ha_proxy.authorize(ha_proxy.parse("GET", ["api", "camera_proxy", "camera.front"]),
                       DECK, s, [])
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.authorize(ha_proxy.parse("GET", ["api", "camera_proxy", "camera.back"]),
                           DECK, s, [])


def test_profile_keys_count_as_set_up():
    profiles = [{"name": "Night", "key_overrides": [
        {"id": "k1", "type": "ha_action", "entity_id": "fan.attic", "service": "fan.toggle"}]}]
    call = _service("fan.attic", "fan.toggle")
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.authorize(call, SAT_KEY, _S(), [])
    ha_proxy.authorize(call, SAT_KEY, _S(), profiles)


@pytest.mark.parametrize("settings_obj,enforced", [
    (_S(server_version=1), True),
    (_S(server_version=0), False),
    (_S(satellite=True, upstream_version=0, server_version=1), False),
    (_S(satellite=True, upstream_version=1), True),
])
def test_permitted_gets_the_install_enforcement(monkeypatch, settings_obj, enforced):
    calls = []

    def _recorder(entity_id, service, entry, source, **kw):
        calls.append({"args": (entity_id, service, source), "kw": kw})
        return True, ""
    monkeypatch.setattr(ha_policy, "permitted", _recorder)
    ha_proxy.authorize(_service("light.porch,light.hall", "light.turn_on"), SAT_KEY,
                       settings_obj, [])
    assert [c["args"] for c in calls] == [("light.porch", "light.turn_on", "satellite"),
                                          ("light.hall", "light.turn_on", "satellite")]
    assert all(c["kw"] == {"enforce": enforced} for c in calls)


def test_elevated_key_follows_enforcement():
    door = _service("lock.front_door", "lock.unlock")
    # Policy on (the server's key migration has run): refused until allowed.
    with pytest.raises(ha_proxy.HaNotAllowed):
        ha_proxy.authorize(door, DECK, _S(server_version=1), [])
    allowed = _S(server_version=1)
    allowed.streamdeck_ha_slots[1]["allow_elevated"] = True
    ha_proxy.authorize(door, DECK, allowed, [])
    # Policy not on yet: the grandfathered key keeps working, as in 0.20.
    ha_proxy.authorize(door, DECK, _S(server_version=0), [])
    ha_proxy.authorize(door, SAT_KEY, _S(satellite=True, upstream_version=0), [])


@pytest.mark.parametrize("version", [0, 1])
def test_invalid_calls_are_refused_in_every_state(version):
    s = _S(server_version=version)
    # Even a saved key with an invalid call never opens one: parse refuses
    # the shape, and configured_calls leaves invalid keys out.
    s.streamdeck_ha_slots.append({"entity_id": "all", "service": "light.turn_off"})
    for entity in ("all", "light.kitchen,all"):
        with pytest.raises(ha_proxy.HaNotAllowed):
            ha_proxy.authorize(_service(entity, "light.turn_off"), DECK, s, [])
    assert ("all", "light.turn_off") not in ha_allowlist.configured_calls(s, [])


# --- through the app ---------------------------------------------------------------

@pytest.fixture
def stubs(monkeypatch, tmp_path):
    """The app with a configured deck and Home Assistant stubbed out."""
    from app.services import ha_client
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(settings, "deployment_mode", "server", raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    monkeypatch.setattr(settings, "api_key", "legacy-key", raising=False)
    monkeypatch.setattr(settings, "extra_api_keys", [], raising=False)
    monkeypatch.setattr(settings, "extra_api_key_names", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_slots", [dict(s) for s in SLOTS],
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_key_overrides", [], raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_base_url", "http://ha.test:8123",
                        raising=False)
    monkeypatch.setattr(settings, "streamdeck_ha_token", HA_TOKEN, raising=False)
    monkeypatch.setattr(settings, "ha_key_policy_version", 0, raising=False)
    monkeypatch.setattr(settings, "upstream_ha_policy_version", 0, raising=False)
    monkeypatch.setattr(ha_allowlist, "camera_entities", lambda s: {"camera.front"})
    monkeypatch.setattr(proxy, "_ha_profiles", lambda: [])

    state = {"mode": "direct", "calls": [], "fail": None}

    async def _get_state(entity_id):
        if entity_id == "light.hall":
            return None
        return {"entity_id": entity_id, "state": "on"}

    async def _call_service(entity_id, service, *, source, entry=None):
        state["calls"].append((entity_id, service, source,
                               (entry or {}).get("label") if isinstance(entry, dict) else None))
        if state["fail"] == entity_id:
            return {"ok": False, "status": 500, "detail": "Home Assistant answered 500."}
        return {"ok": True, "status": 200, "detail": "Sent " + service}

    async def _camera(entity_id):
        return 200, b"\xff\xd8picture", "image/jpeg"

    monkeypatch.setattr(ha_client, "mode", lambda: state["mode"])
    monkeypatch.setattr(ha_client, "get_state", _get_state)
    monkeypatch.setattr(ha_client, "call_service", _call_service)
    monkeypatch.setattr(ha_client, "camera_snapshot", _camera)
    cwd = os.getcwd()
    os.chdir(SERVICE)
    try:
        yield state
    finally:
        os.chdir(cwd)


def _clients():
    from fastapi.testclient import TestClient
    from app.main import app
    return (TestClient(app),
            TestClient(app, client=LOOPBACK, base_url="http://localhost"))


def _mint(scope):
    from app.services import credentials
    return credentials.mint(scope, f"{scope} key", "manual")[1]


def _post(client, path, entity, **kw):
    return client.post("/api/proxy/ha/" + path, content=_svc_body(entity),
                       headers=dict(kw.get("headers") or {},
                                    **{"Content-Type": "application/json"}))


def _no_token(*responses):
    for r in responses:
        assert HA_TOKEN not in r.text


def _auth_headers(secret, bearer):
    return {"Authorization": f"Bearer {secret}"} if bearer else {"X-API-Key": secret}


@pytest.mark.parametrize("bearer", [False, True])
def test_deck_agent_reads_and_calls(stubs, bridge_token, bearer):
    from app.services import local_agents
    _lan, loop = _clients()
    deck = _auth_headers(local_agents.agent_key("streamdeck"), bearer)
    r = loop.get("/api/proxy/ha/api/states/light.kitchen", headers=deck)
    assert r.status_code == 200 and r.json() == {"entity_id": "light.kitchen", "state": "on"}
    r = _post(loop, "api/services/light/toggle", "light.kitchen", headers=deck)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert stubs["calls"] == [("light.kitchen", "light.toggle", "deck", "Kitchen")]
    r = loop.get("/api/proxy/ha/api/camera_proxy/camera.front", headers=deck)
    assert r.status_code == 200 and r.content == b"\xff\xd8picture"
    assert r.headers["content-type"] == "image/jpeg"
    _no_token(r)


@pytest.mark.parametrize("bearer", [False, True])
@pytest.mark.parametrize("which", ["satellite", "legacy", "admin"])
def test_fleet_keys_call_with_source_satellite(stubs, bearer, which):
    lan, _loop = _clients()
    secret = "legacy-key" if which == "legacy" else _mint(which)
    r = _post(lan, "api/services/light/turn_on", ["light.porch", "light.hall"],
              headers=_auth_headers(secret, bearer))
    assert r.status_code == 200, r.text
    assert stubs["calls"] == [("light.porch", "light.turn_on", "satellite", "Both"),
                              ("light.hall", "light.turn_on", "satellite", "Both")]


@pytest.mark.parametrize("scope", ["ha", "ha_rest", "display"])
def test_narrower_keys_are_refused_through_the_app(stubs, scope):
    lan, _loop = _clients()
    for bearer in (False, True):
        r = _post(lan, "api/services/light/toggle", "light.kitchen",
                  headers=_auth_headers(_mint(scope), bearer))
        assert r.status_code == 403
        _no_token(r)
    assert stubs["calls"] == []


def test_the_reader_agent_is_refused(stubs, bridge_token):
    from app.services import local_agents
    _lan, loop = _clients()
    r = _post(loop, "api/services/light/toggle", "light.kitchen",
              headers={"X-API-Key": local_agents.agent_key("gadgets")})
    assert r.status_code == 403
    assert stubs["calls"] == []


def test_narrower_keys_get_the_hop_refusal(stubs, monkeypatch):
    # Behind the app's own sign-in rules these never arrive; the hop still
    # answers them with its own refusal and notes it on the key.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.services import credentials
    secret = _mint("ha_rest")
    rec = credentials.lookup(secret)
    noted = []
    monkeypatch.setattr(credentials, "record_refusal", lambda cid, path: noted.append((cid, path)))
    app = FastAPI()

    @app.middleware("http")
    async def _principal(request, call_next):
        request.state.principal = access.Principal("key", "ha_rest", rec["id"])
        return await call_next(request)
    app.include_router(proxy.router)
    r = _post(TestClient(app), "api/services/light/toggle", "light.kitchen")
    assert r.status_code == 403 and r.json() == NOT_SET_UP
    assert noted and noted[0][0] == rec["id"]


def test_unknown_bearer_from_this_device_works_while_trust_lasts(stubs, own_screen):
    # A Stream Deck from before agent keys still sends the Home Assistant
    # token it was given; on this device that keeps working until the old
    # trust for local programs is retired.
    from app.services import auth_state
    lan, loop = _clients()
    old_deck = {"Authorization": f"Bearer {HA_TOKEN}"}
    r = _post(loop, "api/services/light/toggle", "light.kitchen", headers=old_deck)
    assert r.status_code == 200
    assert stubs["calls"] == [("light.kitchen", "light.toggle", "deck", "Kitchen")]
    # Not from another machine on the network.
    r = _post(lan, "api/services/light/toggle", "light.kitchen", headers=old_deck)
    assert r.status_code == 401 and r.json() == {"detail": "Unauthorized"}
    auth_state.retire("admin")
    r = _post(loop, "api/services/light/toggle", "light.kitchen", headers=old_deck)
    assert r.status_code == 401
    assert len(stubs["calls"]) == 1
    _no_token(r)


def test_nobody_gets_401(stubs):
    lan, _loop = _clients()
    for r in (lan.get("/api/proxy/ha/api/states/light.kitchen"),
              _post(lan, "api/services/light/toggle", "light.kitchen"),
              lan.get("/api/proxy/ha/api/states/light.kitchen",
                      headers={"X-API-Key": "made-up"})):
        assert r.status_code == 401 and r.json() == {"detail": "Unauthorized"}
    assert stubs["calls"] == []


def test_refusals_say_nothing_about_the_setup(stubs):
    lan, _loop = _clients()
    sat = {"X-API-Key": _mint("satellite")}
    answers = [
        lan.get("/api/proxy/ha/api/states/sensor.alarm_code", headers=sat),
        lan.get("/api/proxy/ha/api/camera_proxy/camera.back", headers=sat),
        _post(lan, "api/services/light/turn_on", "light.porch,light.garage", headers=sat),
        _post(lan, "api/services/homeassistant/restart", "light.kitchen", headers=sat),
        _post(lan, "api/services/light/toggle", "all", headers=sat),
        lan.get("/api/proxy/ha/api/config", headers=sat),
        lan.put("/api/proxy/ha/api/states/light.kitchen", headers=sat, json={"state": "off"}),
        lan.get("/api/proxy/ha/api/states/light.kitchen%25", headers=sat),
    ]
    for r in answers:
        assert r.status_code == 403 and r.json() == NOT_SET_UP, r.request.url
        _no_token(r)
    assert stubs["calls"] == []


def test_an_oversized_body_is_refused(stubs):
    lan, _loop = _clients()
    big = json.dumps({"entity_id": "light.kitchen", "pad": "x" * 40000}).encode()
    r = lan.post("/api/proxy/ha/api/services/light/toggle", content=big,
                 headers={"X-API-Key": _mint("satellite"),
                          "Content-Type": "application/json"})
    assert r.status_code == 403 and r.json() == NOT_SET_UP
    assert stubs["calls"] == []


def test_a_chunked_body_past_the_cap_is_refused(stubs):
    # No Content-Length: the hop stops reading once the body passes the cap.
    lan, _loop = _clients()
    pad = b"x" * (ha_proxy.MAX_BODY_BYTES + 10)

    def _chunks():
        yield b'{"entity_id": "light.kitchen", "pad": "'
        yield pad
        yield b'"}'
    r = lan.post("/api/proxy/ha/api/services/light/toggle", content=_chunks(),
                 headers={"X-API-Key": _mint("satellite"),
                          "Content-Type": "application/json"})
    assert r.status_code == 403 and r.json() == NOT_SET_UP
    assert stubs["calls"] == []

    def _small():
        yield b'{"entity_id": '
        yield b'"light.kitchen"}'
    r = lan.post("/api/proxy/ha/api/services/light/toggle", content=_small(),
                 headers={"X-API-Key": _mint("satellite"),
                          "Content-Type": "application/json"})
    assert r.status_code == 200
    assert stubs["calls"] == [("light.kitchen", "light.toggle", "satellite", "Kitchen")]


def test_state_reads_leave_out_home_assistant_tokens(stubs, monkeypatch):
    from app.services import ha_client
    cached = {"entity_id": "camera.front", "state": "idle", "attributes": {
        "access_token": "cam-access-token",
        "entity_picture": "/api/camera_proxy/camera.front?token=cam-access-token",
        "friendly_name": "Front"}}

    async def _get_state(entity_id):
        return cached
    monkeypatch.setattr(ha_client, "get_state", _get_state)
    lan, _loop = _clients()
    r = lan.get("/api/proxy/ha/api/states/camera.front",
                headers={"X-API-Key": _mint("satellite")})
    assert r.status_code == 200
    assert "cam-access-token" not in r.text
    assert r.json() == {"entity_id": "camera.front", "state": "idle",
                        "attributes": {"friendly_name": "Front"}}
    # The cached state itself is left as it was.
    assert cached["attributes"]["access_token"] == "cam-access-token"
    plain = {"state": "on", "attributes": {"entity_picture": "/local/lamp.png"}}
    assert ha_proxy.public_state(plain) == plain


def test_ha_calls_are_never_recorded(stubs):
    from app.services import proxy_policy
    proxy_policy.reset()
    lan, _loop = _clients()
    sat = {"X-API-Key": _mint("satellite")}
    lan.get("/api/proxy/ha/api/states/light.kitchen", headers=sat)
    lan.get("/api/proxy/ha/api/states/sensor.alarm_code", headers=sat)
    assert proxy_policy.snapshot() == []
    assert not os.path.exists(os.path.join(settings.data_dir, "proxy_paths.json"))


def test_elevated_key_on_a_server_with_the_policy_on(stubs, monkeypatch):
    lan, _loop = _clients()
    sat = {"X-API-Key": _mint("satellite")}
    monkeypatch.setattr(settings, "ha_key_policy_version", 1, raising=False)
    r = _post(lan, "api/services/lock/unlock", "lock.front_door", headers=sat)
    assert r.status_code == 403 and r.json() == NOT_SET_UP
    assert stubs["calls"] == []
    slots = [dict(s) for s in SLOTS]
    slots[1]["allow_elevated"] = True
    monkeypatch.setattr(settings, "streamdeck_ha_slots", slots, raising=False)
    r = _post(lan, "api/services/lock/unlock", "lock.front_door", headers=sat)
    assert r.status_code == 200
    assert stubs["calls"] == [("lock.front_door", "lock.unlock", "satellite", "Door")]


def test_elevated_key_on_a_server_before_its_migration(stubs):
    # ha_key_policy_version 0: the grandfathered lock key works as in 0.20.
    lan, _loop = _clients()
    r = _post(lan, "api/services/lock/unlock", "lock.front_door",
              headers={"X-API-Key": _mint("satellite")})
    assert r.status_code == 200
    assert stubs["calls"] == [("lock.front_door", "lock.unlock", "satellite", "Door")]


def test_elevated_key_on_a_satellite_whose_server_has_no_policy(stubs, bridge_token,
                                                                 monkeypatch):
    # A 0.21 Bandit in direct mode, its deck moved to the proxy form, under a
    # server that has not turned the policy on (upstream_ha_policy_version 0).
    from app.services import local_agents
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test:9284",
                        raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key", raising=False)
    monkeypatch.setattr(settings, "ha_key_policy_version", 1, raising=False)
    _lan, loop = _clients()
    deck = {"Authorization": "Bearer " + local_agents.agent_key("streamdeck")}
    r = _post(loop, "api/services/lock/unlock", "lock.front_door", headers=deck)
    assert r.status_code == 200, r.text
    assert stubs["calls"] == [("lock.front_door", "lock.unlock", "deck", "Door")]
    # Once the server's policy reaches the satellite, the same key needs the
    # owner's permission.
    monkeypatch.setattr(settings, "upstream_ha_policy_version", 1, raising=False)
    r = _post(loop, "api/services/lock/unlock", "lock.front_door", headers=deck)
    assert r.status_code == 403 and r.json() == NOT_SET_UP


@pytest.mark.parametrize("version", [0, 1])
def test_invalid_calls_are_refused_in_both_states(stubs, monkeypatch, version):
    lan, _loop = _clients()
    monkeypatch.setattr(settings, "ha_key_policy_version", version, raising=False)
    slots = [dict(s) for s in SLOTS] + [{"entity_id": "all", "service": "light.turn_off"}]
    monkeypatch.setattr(settings, "streamdeck_ha_slots", slots, raising=False)
    sat = {"X-API-Key": _mint("satellite")}
    for entity in ("all", "none", "light.kitchen,all"):
        r = _post(lan, "api/services/light/turn_off", entity, headers=sat)
        assert r.status_code == 403 and r.json() == NOT_SET_UP
    assert stubs["calls"] == []


def test_unknown_entity_is_404_and_service_failure_stops(stubs):
    lan, _loop = _clients()
    sat = {"X-API-Key": _mint("satellite")}
    r = lan.get("/api/proxy/ha/api/states/light.hall", headers=sat)
    assert r.status_code == 404
    assert r.json() == {"detail": "That Home Assistant entity was not found."}
    stubs["fail"] = "light.porch"
    r = _post(lan, "api/services/light/turn_on", "light.porch,light.hall", headers=sat)
    assert r.status_code == 500
    assert r.json() == {"ok": False, "detail": "Home Assistant answered 500."}
    # Stopped at the first failure.
    assert [c[0] for c in stubs["calls"]] == ["light.porch"]


def test_unreachable_home_assistant_is_502(stubs, monkeypatch):
    from app.services import ha_client

    async def _down(entity_id, service, *, source, entry=None):
        return {"ok": False, "status": 0, "detail": "Could not reach Home Assistant."}

    async def _no_picture(entity_id):
        return 0, b"", ""
    monkeypatch.setattr(ha_client, "call_service", _down)
    monkeypatch.setattr(ha_client, "camera_snapshot", _no_picture)
    lan, _loop = _clients()
    sat = {"X-API-Key": _mint("satellite")}
    r = _post(lan, "api/services/light/toggle", "light.kitchen", headers=sat)
    assert r.status_code == 502 and r.json()["ok"] is False
    r = lan.get("/api/proxy/ha/api/camera_proxy/camera.front", headers=sat)
    assert r.status_code == 502
    _no_token(r)


def test_camera_answer_must_be_a_picture(stubs, monkeypatch):
    from app.services import ha_client

    async def _page(entity_id):
        return 200, b"<html>" + HA_TOKEN.encode() + b"</html>", "text/html"
    monkeypatch.setattr(ha_client, "camera_snapshot", _page)
    lan, _loop = _clients()
    r = lan.get("/api/proxy/ha/api/camera_proxy/camera.front",
                headers={"X-API-Key": _mint("satellite")})
    assert r.status_code == 502
    _no_token(r)


def test_camera_svg_is_refused_and_pictures_are_not_sniffed(stubs, monkeypatch):
    from app.services import ha_client

    async def _svg(entity_id):
        return 200, b"<svg onload='alert(1)'/>", "image/svg+xml"
    monkeypatch.setattr(ha_client, "camera_snapshot", _svg)
    lan, _loop = _clients()
    sat = {"X-API-Key": _mint("satellite")}
    r = lan.get("/api/proxy/ha/api/camera_proxy/camera.front", headers=sat)
    assert r.status_code == 502
    assert r.json() == {"detail": ha_proxy.NO_PICTURE_MSG}

    async def _jpeg(entity_id):
        return 200, b"\xff\xd8picture", "image/jpeg"
    monkeypatch.setattr(ha_client, "camera_snapshot", _jpeg)
    r = lan.get("/api/proxy/ha/api/camera_proxy/camera.front", headers=sat)
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_not_connected_is_503(stubs):
    stubs["mode"] = "none"
    lan, _loop = _clients()
    r = _post(lan, "api/services/light/toggle", "light.kitchen",
              headers={"X-API-Key": _mint("satellite")})
    assert r.status_code == 503
    assert r.json() == {"detail": "Home Assistant is not connected on the main server."}
    assert stubs["calls"] == []


def test_relay_satellite_forwards_to_its_main_server(stubs, bridge_token, monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from app.config import APP_VERSION
    from app.services import local_agents
    stubs["mode"] = "upstream"
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test:9284/",
                        raising=False)
    monkeypatch.setattr(settings, "upstream_api_key", "shared-key", raising=False)
    upstream = AsyncMock(return_value=httpx.Response(
        200, json={"ok": True}, headers={"content-type": "application/json"}))
    monkeypatch.setattr(proxy._client, "request", upstream)
    _lan, loop = _clients()
    deck = {"X-API-Key": local_agents.agent_key("streamdeck")}
    r = _post(loop, "api/services/Light/Turn_On", " LIGHT.porch, light.hall", headers=deck)
    assert r.status_code == 200 and r.json() == {"ok": True}
    method, url = upstream.call_args.args
    kw = upstream.call_args.kwargs
    assert (method, url) == ("POST",
                             "http://server.test:9284/api/proxy/ha/api/services/light/turn_on")
    assert json.loads(kw["content"]) == {"entity_id": "light.porch,light.hall"}
    assert kw["headers"]["X-API-Key"] == "shared-key"
    assert kw["headers"]["X-PR-Client"] == f"satellite/{APP_VERSION}"
    assert kw["headers"]["X-PR-Caps"] == "relay1"
    assert kw["headers"]["Content-Type"] == "application/json"
    # The deck's own key never goes upstream, and Home Assistant is not
    # called from here.
    assert local_agents.agent_key("streamdeck") not in json.dumps(kw["headers"])
    assert stubs["calls"] == []

    upstream.return_value = httpx.Response(
        404, json={"detail": "That Home Assistant entity was not found."},
        headers={"content-type": "application/json"})
    r = loop.get("/api/proxy/ha/api/states/light.kitchen", headers=deck)
    assert r.status_code == 404
    method, url = upstream.call_args.args
    assert (method, url) == ("GET", "http://server.test:9284/api/proxy/ha/api/states/light.kitchen")
    assert upstream.call_args.kwargs["content"] is None

    upstream.side_effect = httpx.ConnectError("down")
    r = loop.get("/api/proxy/ha/api/states/light.kitchen", headers=deck)
    assert r.status_code == 502 and "server.test" not in r.text


def test_relay_satellite_still_checks_its_own_setup(stubs, bridge_token, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import local_agents
    stubs["mode"] = "upstream"
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote", raising=False)
    monkeypatch.setattr(settings, "remote_server_url", "http://server.test:9284",
                        raising=False)
    upstream = AsyncMock()
    monkeypatch.setattr(proxy._client, "request", upstream)
    _lan, loop = _clients()
    r = _post(loop, "api/services/light/turn_on", "light.garage",
              headers={"X-API-Key": local_agents.agent_key("streamdeck")})
    assert r.status_code == 403 and r.json() == NOT_SET_UP
    upstream.assert_not_called()


def test_copy_has_no_em_dash():
    for text in (ha_proxy.NOT_ALLOWED_MSG, ha_proxy.NOT_CONNECTED_MSG,
                 ha_proxy.NOT_FOUND_MSG, proxy._MAIN_SERVER_UNREACHABLE_MSG):
        assert "\u2014" not in text
