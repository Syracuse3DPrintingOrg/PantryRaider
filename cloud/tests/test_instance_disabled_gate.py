"""Disabling an account must cut off its kitchen, not just its browser.

An instance token is a separate credential from a portal session. Disabling an
account kills its sessions and tears down its tunnels, but each instance route
used to have to remember the owner check for itself: the AI proxy and
provisioning did, cloud backup and the tunnel routes did not. So a disabled
account's kitchen could still list, download and upload its stored backups,
which is the most sensitive thing Forager holds.

The gate now lives in the ``current_instance`` dependency, so a new route
inherits it. The handful of routes that must still answer a disabled account
name ``instance_even_if_disabled`` instead, and this file pins that list so it
cannot quietly grow.
"""
import fastapi.routing
from fastapi import APIRouter, Depends, FastAPI

import app.deps as deps
from app.database import SessionLocal
from app.main import app
from app.models import Account
from tests.conftest import activate_entitlement
from tests.test_cloud_backup import _auth, _zip, backup_dir  # noqa: F401

# The only routes allowed to answer a disabled account: the status read, so
# the app can show why things stopped; self-unlink, so the owner can
# disconnect the device; and the Google unlock check, which answers a disabled
# account exactly like an unknown code.
EXEMPT = {
    ("GET", "/v1/instance/me"),
    ("DELETE", "/v1/instance"),
    ("POST", "/v1/instance/verify-unlock"),
}


def _reaches(dependant, target) -> bool:
    """Whether target appears anywhere in this dependency tree. The tree is
    not followed into current_instance: that is the gate, which checks the
    account and then uses the un-gated resolver itself."""
    for sub in dependant.dependencies:
        if sub.call is target:
            return True
        if sub.call is not deps.current_instance and _reaches(sub, target):
            return True
    return False


def _served_routes(routes) -> list:
    """Every route as the app serves it, with router prefixes and include-time
    dependencies applied. Newer FastAPI releases keep each included router
    wrapped rather than copying its routes into the app, and walk them with
    routing.iter_route_contexts (its OpenAPI builder does); older releases
    list the copied routes flat."""
    iter_contexts = getattr(fastapi.routing, "iter_route_contexts", None)
    return list(iter_contexts(routes)) if iter_contexts else list(routes)


def _routes_using(routes, target) -> set[tuple[str, str]]:
    """(method, path) for every route whose dependencies reach target."""
    found = set()
    for route in _served_routes(routes):
        dependant = getattr(route, "dependant", None)
        if dependant is None or not _reaches(dependant, target):
            continue
        for method in getattr(route, "methods", None) or {"WEBSOCKET"}:
            found.add((method, route.path))
    return found


def _disable(email="dan@example.com"):
    db = SessionLocal()
    a = db.query(Account).filter_by(email=email).first()
    assert a, "no account to disable"
    a.disabled = 1
    db.commit()
    db.close()


def test_backup_is_cut_off_when_the_account_is_disabled(client, instance_token):
    activate_entitlement(plan="premium")
    assert client.post("/v1/backup/upload", files=_zip(marker=b"before"),
                       headers=_auth(instance_token)).status_code == 200
    _disable()
    for path in ("/v1/backup/list", "/v1/backup/latest"):
        r = client.get(path, headers=_auth(instance_token))
        assert r.status_code == 403, f"{path} still answered a disabled account"
    assert client.post("/v1/backup/upload", files=_zip(marker=b"after"),
                       headers=_auth(instance_token)).status_code == 403


def test_the_refusal_keeps_the_shape_the_app_parses(client, instance_token):
    """The app reads detail["error"] to tell a disabled account apart from a
    plan or quota refusal, so the gate must not flatten it to a bare string."""
    _disable()
    r = client.post("/v1/ai/analyze", data={"kind": "vision", "text": "x"},
                    headers=_auth(instance_token))
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "account_disabled"


def test_status_and_unlink_still_answer_a_disabled_account(client, instance_token):
    """Two of the three deliberate exemptions: the app has to be able to show
    WHY it stopped working, and the owner has to be able to disconnect the
    device. (The third, the Google unlock check, answers a disabled account
    like an unknown code; test_google_unlock covers it.)"""
    _disable()
    assert client.get("/v1/instance/me", headers=_auth(instance_token)).status_code == 200
    assert client.delete("/v1/instance", headers=_auth(instance_token)).status_code == 200


def test_the_status_read_says_the_account_is_disabled(client, instance_token):
    me = client.get("/v1/instance/me", headers=_auth(instance_token))
    assert me.status_code == 200
    assert me.json()["account_disabled"] is False
    _disable()
    me = client.get("/v1/instance/me", headers=_auth(instance_token))
    assert me.status_code == 200
    assert me.json()["account_disabled"] is True


def test_the_exemption_list_is_exactly_these_three():
    """A new route anywhere in the app that reaches the un-gated dependency,
    directly or through a helper, should have to justify itself here rather
    than slipping in as a fourth quiet exception."""
    found = _routes_using(app.routes, deps.instance_even_if_disabled)
    assert found == EXEMPT, (
        "the un-gated instance dependency is used somewhere new: "
        + str(sorted(found - EXEMPT)) + "; or no longer used by: "
        + str(sorted(EXEMPT - found)))


def test_the_sweep_reads_the_whole_app():
    """The sweep above only means something if it sees the app's routes, so
    check that it does: every router's routes, and the gated routes too."""
    with_deps = [r for r in _served_routes(app.routes)
                 if getattr(r, "dependant", None)]
    assert len(with_deps) > 60, f"only {len(with_deps)} routes were read"
    gated = _routes_using(app.routes, deps.current_instance)
    paths = {path for _, path in gated}
    for path in ("/v1/ai/analyze", "/v1/backup/list", "/v1/backup/upload",
                 "/v1/instance/verify-login"):
        assert path in paths, f"{path} was not seen behind the gate"
    assert len(gated) >= 8, f"only {len(gated)} gated routes were seen"


def test_the_sweep_would_notice_a_new_exemption():
    """Break the rule on purpose in a throwaway app: a direct use, a use
    through a helper dependency, a route-level dependency and one added when
    the router is included are all caught, and a route behind the gate is
    not."""
    router = APIRouter(prefix="/v1/backup")

    def via_helper(inst=Depends(deps.instance_even_if_disabled)):
        return inst

    @router.get("/gated")
    def gated(inst=Depends(deps.current_instance)):
        return {}

    @router.get("/direct")
    def direct(inst=Depends(deps.instance_even_if_disabled)):
        return {}

    @router.get("/nested")
    def nested(inst=Depends(via_helper)):
        return {}

    @router.post("/route-level",
                 dependencies=[Depends(deps.instance_even_if_disabled)])
    def route_level():
        return {}

    tunnel = APIRouter(prefix="/v1/tunnel")

    @tunnel.get("/status")
    def status():
        return {}

    probe = FastAPI()
    probe.include_router(router)
    probe.include_router(
        tunnel, dependencies=[Depends(deps.instance_even_if_disabled)])
    assert _routes_using(probe.routes, deps.instance_even_if_disabled) == {
        ("GET", "/v1/backup/direct"),
        ("GET", "/v1/backup/nested"),
        ("POST", "/v1/backup/route-level"),
        ("GET", "/v1/tunnel/status"),
    }
