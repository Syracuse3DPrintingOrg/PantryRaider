"""The admin panel: gate, totals, search, disable, comp, revoke, audit."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app

ADMIN = {"email": "dan@example.com", "password": "hunter2222",
         "confirm_password": "hunter2222"}
USER = {"email": "eve@example.com", "password": "eviltwin99",
        "confirm_password": "eviltwin99"}


@pytest.fixture
def admin_client(monkeypatch):
    """A browser signed in as the allowlisted admin."""
    monkeypatch.setattr(settings, "admin_emails", "dan@example.com")
    c = TestClient(app)
    assert c.post("/signup", data=ADMIN, follow_redirects=False).status_code == 303
    return c


@pytest.fixture
def user_setup(client):
    """A second, non-admin account with one paired kitchen. Returns
    (account_id, instance_token)."""
    assert client.post("/signup", data=USER,
                       follow_redirects=False).status_code == 303
    # The portal cookie does not authenticate /v1; sign in for a bearer.
    resp = client.post("/v1/accounts/login",
                       json={"email": USER["email"], "password": USER["password"]})
    token = resp.json()["session_token"]
    code = client.post("/v1/pairing/code",
                       headers={"Authorization": f"Bearer {token}"})
    redeemed = client.post("/v1/pairing/redeem",
                           json={"code": code.json()["code"], "name": "Eve Pi"})
    instance_token = redeemed.json()["instance_token"]
    from app.models import Account
    db = SessionLocal()
    account_id = db.query(Account).filter_by(email=USER["email"]).first().id
    db.close()
    return account_id, instance_token


def test_admin_is_404_for_anonymous_and_non_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", "someone-else@example.com")
    assert client.get("/admin").status_code == 404
    client.post("/signup", data=ADMIN)  # signed in, but not on the list
    assert client.get("/admin").status_code == 404
    assert client.get("/admin/accounts/1").status_code == 404
    assert client.post("/admin/accounts/1/disable").status_code == 404


def test_admin_empty_allowlist_means_nobody(client):
    assert settings.admin_emails == ""
    client.post("/signup", data=ADMIN)
    assert client.get("/admin").status_code == 404


def test_admin_allowlisted_email_gets_in(admin_client):
    page = admin_client.get("/admin")
    assert page.status_code == 200
    assert "Admin" in page.text
    assert "dan@example.com" in page.text


def test_disabled_admin_session_loses_access(admin_client, monkeypatch):
    """The session-level disabled check applies to admins too."""
    from app.models import Account
    db = SessionLocal()
    db.query(Account).filter_by(email="dan@example.com").update({"disabled": 1})
    db.commit()
    db.close()
    assert admin_client.get("/admin").status_code == 404


def test_totals_math(admin_client, user_setup, monkeypatch):
    account_id, _ = user_setup
    from tests.conftest import activate_entitlement
    activate_entitlement("eve@example.com")

    from app import usage
    from app.deps import utc_now_iso
    db = SessionLocal()
    usage.record(db, account_id, None, 1_500_000, "food",
                 usage.month_key(), utc_now_iso())
    usage.record(db, account_id, None, 500_000, "receipt",
                 "2020-01", utc_now_iso())  # another month, excluded
    db.close()

    monkeypatch.setattr(settings, "gemini_cost_per_million_tokens", 0.60)
    page = admin_client.get("/admin").text
    assert ">2<" in page  # accounts total (dan + eve)
    assert "1,500,000" in page  # month-to-date tokens
    assert "$0.90" in page  # 1.5M * $0.60/M
    assert "Eve Pi" not in page  # overview lists accounts, not kitchens
    # eve: 1 kitchen, premium plan, month tokens on her row
    assert "eve@example.com" in page
    assert "premium" in page


def test_comp_is_not_a_paid_sub_in_totals(admin_client, user_setup):
    account_id, _ = user_setup
    admin_client.post(f"/admin/accounts/{account_id}/comp",
                      data={"expires_on": "2099-01-01"})
    page = admin_client.get("/admin").text
    # The stat tile for active paid subs stays at 0.
    import re
    m = re.search(r'Active paid subs</div>\s*<div class="value">(\d+)</div>', page)
    assert m and m.group(1) == "0"


def test_search_filters_by_email_substring(admin_client, user_setup):
    page = admin_client.get("/admin", params={"q": "eve"}).text
    assert "eve@example.com" in page
    assert "dan@example.com" not in page.split("Accounts</h2>")[1]
    empty = admin_client.get("/admin", params={"q": "zebra"}).text
    assert "No accounts match" in empty


def test_disable_enforced_at_login_provision_and_proxy(admin_client, user_setup, client):
    account_id, instance_token = user_setup
    resp = admin_client.post(f"/admin/accounts/{account_id}/disable",
                             follow_redirects=False)
    assert resp.status_code == 303

    fresh = TestClient(app)
    # JSON login refuses with the clear message.
    login = fresh.post("/v1/accounts/login",
                       json={"email": USER["email"], "password": USER["password"]})
    assert login.status_code == 403
    assert "disabled" in login.json()["detail"].lower()
    # Portal login refuses too.
    portal = fresh.post("/login", data={"email": USER["email"],
                                        "password": USER["password"]})
    assert portal.status_code == 403
    assert "disabled" in portal.text.lower()
    # Provisioning refuses.
    prov = fresh.post("/v1/instances/provision",
                      json={"email": USER["email"], "password": USER["password"],
                            "device_name": "Sneaky Pi"})
    assert prov.status_code == 403
    # The AI proxy refuses the already-paired kitchen.
    proxy = fresh.post("/v1/ai/analyze",
                       data={"kind": "enrich", "text": "012345678905"},
                       headers={"Authorization": f"Bearer {instance_token}"})
    assert proxy.status_code == 403
    assert proxy.json()["detail"]["error"] == "account_disabled"
    # The pre-existing portal session is dead as well.
    assert client.get("/account", follow_redirects=False).status_code == 303

    # Enable restores everything.
    admin_client.post(f"/admin/accounts/{account_id}/enable")
    assert fresh.post("/v1/accounts/login",
                      json={"email": USER["email"],
                            "password": USER["password"]}).status_code == 200
    ok = fresh.post("/v1/ai/analyze",
                    data={"kind": "enrich", "text": "012345678905"},
                    headers={"Authorization": f"Bearer {instance_token}"})
    assert ok.status_code == 200


def test_comp_grant_and_expire(admin_client, user_setup):
    account_id, instance_token = user_setup
    resp = admin_client.post(f"/admin/accounts/{account_id}/comp",
                             data={"expires_on": "2099-12-31"},
                             follow_redirects=False)
    assert resp.status_code == 303

    me = admin_client.get("/v1/instance/me",
                          headers={"Authorization": f"Bearer {instance_token}"})
    ent = me.json()["entitlement"]
    assert ent["plan"] == "premium" and ent["entitled"] is True and ent["active"] is False
    assert ent["quota"] == 2_000_000

    detail = admin_client.get(f"/admin/accounts/{account_id}").text
    assert "comp" in detail
    assert "2099-12-31" in detail

    admin_client.post(f"/admin/accounts/{account_id}/comp/expire")
    me = admin_client.get("/v1/instance/me",
                          headers={"Authorization": f"Bearer {instance_token}"})
    ent = me.json()["entitlement"]
    assert ent["plan"] == "expired" and ent["active"] is False


def test_comp_past_its_expiry_falls_back_to_expired(admin_client, user_setup):
    account_id, instance_token = user_setup
    admin_client.post(f"/admin/accounts/{account_id}/comp",
                      data={"expires_on": "2020-01-01"})
    me = admin_client.get("/v1/instance/me",
                          headers={"Authorization": f"Bearer {instance_token}"})
    ent = me.json()["entitlement"]
    assert ent["plan"] == "expired" and ent["active"] is False


def test_comp_rejects_bad_dates_and_stripe_subscribers(admin_client, user_setup):
    account_id, _ = user_setup
    bad = admin_client.post(f"/admin/accounts/{account_id}/comp",
                            data={"expires_on": "soon"})
    assert bad.status_code == 400
    from tests.conftest import activate_entitlement
    activate_entitlement("eve@example.com")
    # Mark it as a Stripe entitlement, as the webhook would.
    from app.models import Entitlement
    db = SessionLocal()
    db.query(Entitlement).filter_by(account_id=account_id).update(
        {"source": "stripe"})
    db.commit()
    db.close()
    conflict = admin_client.post(f"/admin/accounts/{account_id}/comp",
                                 data={"expires_on": "2099-01-01"})
    assert conflict.status_code == 409


def test_admin_revoke_kitchen_kills_the_credential(admin_client, user_setup):
    account_id, instance_token = user_setup
    detail = admin_client.get(f"/admin/accounts/{account_id}").text
    assert "Eve Pi" in detail

    from app.models import Instance
    db = SessionLocal()
    kitchen_id = db.query(Instance).filter_by(account_id=account_id).first().id
    db.close()

    admin_client.post(f"/admin/accounts/{account_id}/kitchens/{kitchen_id}/revoke")
    denied = admin_client.get("/v1/instance/me",
                              headers={"Authorization": f"Bearer {instance_token}"})
    assert denied.status_code == 401
    # The kitchens card is empty now ("Eve Pi" still appears once, in the
    # audit trail's revoke entry, which is the point of the trail).
    after = admin_client.get(f"/admin/accounts/{account_id}").text
    assert "No kitchens linked" in after
    assert after.count("Eve Pi") == 1


def test_every_admin_mutation_writes_an_audit_row(admin_client, user_setup):
    account_id, _ = user_setup
    from app.models import Instance
    db = SessionLocal()
    kitchen_id = db.query(Instance).filter_by(account_id=account_id).first().id
    db.close()

    admin_client.post(f"/admin/accounts/{account_id}/disable")
    admin_client.post(f"/admin/accounts/{account_id}/enable")
    admin_client.post(f"/admin/accounts/{account_id}/comp",
                      data={"expires_on": "2099-01-01"})
    admin_client.post(f"/admin/accounts/{account_id}/comp/expire")
    admin_client.post(f"/admin/accounts/{account_id}/kitchens/{kitchen_id}/revoke")

    from app.models import AdminAction
    db = SessionLocal()
    rows = db.query(AdminAction).order_by(AdminAction.id).all()
    db.close()
    assert [r.action for r in rows] == [
        "disable", "enable", "comp", "expire-comp", "revoke-kitchen"]
    assert all(r.admin_email == "dan@example.com" for r in rows)
    assert all(r.account_id == account_id for r in rows)
    assert "premium until 2099-01-01" in rows[2].detail
    assert "Eve Pi" in rows[4].detail

    # The detail page shows the target's trail; the overview shows them all.
    detail = admin_client.get(f"/admin/accounts/{account_id}").text
    assert "revoke-kitchen" in detail and "expire-comp" in detail
    overview = admin_client.get("/admin").text
    assert "revoke-kitchen" in overview


def test_admin_detail_404_for_unknown_account(admin_client):
    assert admin_client.get("/admin/accounts/9999").status_code == 404


def test_account_page_still_speaks_plain_language(admin_client):
    """The subscriber-facing rule is untouched by the admin work."""
    page = admin_client.get("/account").text.lower()
    for word in ("token", "instance", "api"):
        assert word not in page
