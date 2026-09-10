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
import inspect

from app.database import SessionLocal
from app.models import Account
from tests.conftest import activate_entitlement
from tests.test_cloud_backup import _auth, _zip, backup_dir  # noqa: F401


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
    """Two deliberate exemptions: the app has to be able to show WHY it stopped
    working, and the owner has to be able to disconnect the device."""
    _disable()
    assert client.get("/v1/instance/me", headers=_auth(instance_token)).status_code == 200
    assert client.delete("/v1/instance", headers=_auth(instance_token)).status_code == 200


def test_the_exemption_list_is_exactly_these_three():
    """A new route that names the un-gated dependency should have to justify
    itself here, rather than slipping in as a fourth quiet exception."""
    from app.routers import instances
    import app.deps as deps

    exempt = set()
    for name, fn in vars(instances).items():
        if not callable(fn) or not hasattr(fn, "__code__"):
            continue
        # current_instance is imported here and itself depends on the un-gated
        # resolver; it is the gate, not a route exempt from it.
        if getattr(fn, "__module__", "") != instances.__name__:
            continue
        try:
            sig = inspect.signature(fn)
        except (ValueError, TypeError):
            continue
        for param in sig.parameters.values():
            dep = getattr(param.default, "dependency", None)
            if dep is deps.instance_even_if_disabled:
                exempt.add(name)

    assert exempt == {"instance_me", "revoke_instance", "verify_unlock"}, (
        "the un-gated instance dependency is used somewhere new: " + str(exempt))
