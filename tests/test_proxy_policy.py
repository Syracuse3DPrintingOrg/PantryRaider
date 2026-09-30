"""What the satellite proxy passes along (services/proxy_policy.py).

The /api/proxy hop used to forward any Grocy or Mealie path with the server's
own admin credentials. These pin the path validation, the allow and deny
tables, the upstream re-encoding, and the batched recorder behind
GET /admin/proxy-paths.

The strongest check here is the vendored history: every Grocy and Mealie path
any release tag has called through the proxy (tests/data/
r1_fleet_proxy_client_paths.json, written by scripts/proxy-paths-history.sh)
must classify as allowed, so no fielded satellite loses a feature when the
policy is enforced.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import settings  # noqa: E402
from app.services import proxy_policy as pp  # noqa: E402

_HISTORY = Path(__file__).parent / "data" / "r1_fleet_proxy_client_paths.json"


# --- validate_path ---------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "", "api", "api/", "api//stock", "api/./stock", "api/../users", "api/stock/..",
    "api\\stock", "api/stock\x00", "api/sto\nck", "api/stock\x1f",
    "api/stock%2e%2e", "api/objects/products%2F1", "other/api/stock",
    "stock", "/api/stock",
])
def test_validate_path_refuses(path):
    with pytest.raises(pp.ProxyPathInvalid):
        pp.validate_path(path)


def test_validate_path_accepts_a_barcode_with_a_space():
    segs = pp.validate_path("api/stock/products/by-barcode/4006381 333931/consume")
    assert segs[4] == "4006381 333931"


# --- upstream_path ---------------------------------------------------------------

def test_upstream_path_reencodes_each_segment():
    segs = ["api", "stock", "products", "by-barcode", "40 06?#/x", "consume"]
    assert pp.upstream_path(segs) == \
        "api/stock/products/by-barcode/40%2006%3F%23%2Fx/consume"


def test_upstream_path_keeps_unreserved_and_subdelims():
    assert pp.upstream_path(["api", "recipes", "a-b_c.d~e!$&'()*+,;="]) == \
        "api/recipes/a-b_c.d~e!$&'()*+,;="


# --- classify --------------------------------------------------------------------

@pytest.mark.parametrize("backend,method,path,template", [
    ("grocy", "GET", "api/stock", "api/stock"),
    ("grocy", "GET", "api/system/info", "api/system/info"),
    ("grocy", "GET", "api/stock/products/12/entries", "api/stock/products/ID/entries"),
    ("grocy", "POST", "api/stock/products/by-barcode/0 1/consume",
     "api/stock/products/by-barcode/SEG/consume"),
    ("grocy", "PUT", "api/objects/stock/7", "api/objects/stock/ID"),
    ("grocy", "PUT", "api/stock/entry/7", "api/stock/entry/ID"),
    ("grocy", "DELETE", "api/objects/shopping_list/3", "api/objects/shopping_list/ID"),
    ("mealie", "GET", "api/users/self", "api/users/self"),
    ("mealie", "GET", "api/recipes/chili-con-carne", "api/recipes/SLUG"),
    ("mealie", "PATCH", "api/recipes/chili-con-carne", "api/recipes/SLUG"),
    ("mealie", "GET", "api/media/recipes/abc/images/original.webp",
     "api/media/recipes/SEG/images/FILE"),
    ("mealie", "GET", "api/groups/shopping/lists/abc", "api/groups/shopping/lists/SEG"),
    ("mealie", "DELETE", "api/households/mealplans/9", "api/households/mealplans/SEG"),
])
def test_allowed(backend, method, path, template):
    assert pp.classify(backend, method, path) == ("allow", template)


@pytest.mark.parametrize("backend,method,path", [
    ("grocy", "GET", "api/users"),
    ("grocy", "POST", "api/users"),
    ("grocy", "GET", "api/user/settings"),
    ("grocy", "GET", "api/system/db-changed-time"),
    ("grocy", "GET", "api/system/config"),
    ("grocy", "GET", "api/files/productpictures/x"),
    ("grocy", "GET", "api/objects/api_keys"),
    ("grocy", "DELETE", "api/objects/users/1"),
    ("grocy", "GET", "api/objects/permission_hierarchy"),
    ("grocy", "GET", "api/objects/sessions"),
    ("mealie", "GET", "api/admin/users"),
    ("mealie", "POST", "api/auth/token"),
    ("mealie", "GET", "api/users"),
    ("mealie", "GET", "api/users/1234/api-tokens"),
    ("mealie", "GET", "api/groups/members"),
    ("mealie", "GET", "api/households/invitations"),
    ("mealie", "DELETE", "api/organizers/tags/1/delete"),
    # Case and punctuation tricks on the same surfaces.
    ("grocy", "GET", "api/Users"),
    ("grocy", "GET", "api/USER/settings"),
    ("grocy", "GET", "api/users;x"),
    ("grocy", "GET", "api/objects/API_KEYS"),
    ("grocy", "GET", "api/objects/api_keys.json"),
    ("grocy", "GET", "api/objects/user_permissions"),
    ("grocy", "GET", "api/System/config"),
    ("grocy", "GET", "api/system/Info"),
    ("grocy", "GET", "api/calendar/ical/sharing-link"),
    ("mealie", "GET", "api/Admin/users"),
    ("mealie", "GET", "api/users/SELF"),
    ("mealie", "GET", "api/users/self;x"),
    ("mealie", "GET", "api/users/self/api-tokens"),
    ("mealie", "GET", "api/groups/shopping-x"),
    ("mealie", "POST", "api/households/Webhooks"),
    ("mealie", "GET", "api/households/Mealplans"),
])
def test_hard_denied(backend, method, path):
    assert pp.classify(backend, method, path)[0] == "deny"


@pytest.mark.parametrize("backend,method,path", [
    # Names that merely start like a denied one are not refused outright.
    ("grocy", "GET", "api/userfields/products/1"),
    ("grocy", "GET", "api/objects/userentities"),
    ("grocy", "GET", "api/calendar/ical"),
    ("mealie", "GET", "api/groups/mealplans/rules"),
    ("mealie", "GET", "api/households/shopping/lists/abc/items"),
])
def test_near_misses_are_not_denied(backend, method, path):
    assert pp.classify(backend, method, path)[0] != "deny"


def test_deny_wins_over_every_method():
    # Nothing on the allow list may open a denied surface, whatever the method.
    for backend in ("grocy", "mealie"):
        for method, tpl in pp.allow_templates(backend):
            path = tpl.replace("ID", "1").replace("SLUG", "s").replace(
                "SEG", "s").replace("FILE", "a.png")
            assert pp.classify(backend, method, path)[0] == "allow", (backend, method, tpl)


def test_offlist_and_generic_template_is_bounded():
    verdict, tpl = pp.classify("grocy", "GET", "api/chores/12/execute")
    assert verdict == "offlist"
    assert tpl == "api/chores/ID/execute"
    # A barcode or a long value never lands in the recorder as-is.
    _, tpl = pp.classify("grocy", "GET", "api/stock/barcodes/4006381 333931")
    assert tpl == "api/stock/barcodes/SEG"
    _, tpl = pp.classify("grocy", "GET", "api/" + "/".join(["x"] * 20))
    assert tpl.endswith("/...") and tpl.count("/") == 12


def test_method_matters():
    # DELETE on a product is on no list, even though PUT is.
    assert pp.classify("grocy", "DELETE", "api/objects/products/1")[0] == "offlist"
    assert pp.classify("grocy", "PUT", "api/objects/products/1")[0] == "allow"


def test_every_released_client_path_is_allowed():
    rows = json.loads(_HISTORY.read_text())
    assert len(rows) > 40
    # The scan reaches from the first satellite release to the current tree.
    assert "v0.6.0" in {r["first"] for r in rows}
    assert "HEAD" in {r["last"] for r in rows}
    for row in rows:
        segs = pp.validate_path(row["path"])
        verdict, _ = pp.classify(row["backend"], row["method"], "/".join(segs))
        assert verdict == "allow", row


def test_legacy_paths_stay_allowed():
    # Satellites up to v0.18.98 and v0.17.7 still in the field.
    assert pp.classify("grocy", "PUT", "api/objects/stock/5")[0] == "allow"
    assert pp.classify("grocy", "GET", "api/objects/shopping_list_items/5")[0] == "allow"
    assert pp.classify("grocy", "GET", "api/objects/shopping_list/5")[0] == "allow"


def test_enforcing_only_for_enforce():
    assert pp.enforcing("enforce") and pp.enforcing(" Enforce ")
    for v in ("report", "", None, "strict", 1):
        assert not pp.enforcing(v)


# --- refusal_detail --------------------------------------------------------------

def test_refusal_detail_reads_proxy_codes_only():
    body = json.dumps({"detail": "The main server does not pass this.", "code": "proxy_path_not_allowed"})
    assert pp.refusal_detail(403, body) == "The main server does not pass this."
    assert pp.refusal_detail(400, json.dumps({"detail": "x", "code": "proxy_path_invalid"})) == "x"
    assert pp.refusal_detail(403, json.dumps({"detail": "x"})) is None
    assert pp.refusal_detail(403, json.dumps({"detail": "x", "code": "other"})) is None
    assert pp.refusal_detail(403, "<html>forbidden</html>") is None
    assert pp.refusal_detail(500, body) is None
    assert pp.refusal_detail(403, json.dumps(["x"])) is None
    assert pp.refusal_detail(403, json.dumps({"code": "proxy_path_denied"}))


def test_user_copy_has_no_em_dash():
    for text in (pp.not_allowed_detail("grocy", "get", "api/x"),
                 pp.denied_detail("mealie", "GET", "api/admin"),
                 pp.invalid_detail("grocy")):
        assert "—" not in text
    assert pp.not_allowed_detail("grocy", "get", "api/x") == (
        "The main server does not pass this Grocy request along (GET /api/x). "
        "Update this device and the main server to the same version.")


# --- recorder --------------------------------------------------------------------

@pytest.fixture
def recorder(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    clock = {"t": 1_790_000_000.0}  # 2026-09
    monkeypatch.setattr(pp, "_now", lambda: clock["t"])
    pp.reset()
    yield tmp_path, clock
    pp.reset()


def _disk(tmp_path):
    return json.loads((tmp_path / "proxy_paths.json").read_text())["paths"]


def test_first_sighting_is_written_at_once(recorder):
    tmp_path, clock = recorder
    pp.record("grocy", "GET", "api/stock", "kitchen", "satellite/0.20.3", "allow")
    rows = _disk(tmp_path)
    row = rows["grocy GET api/stock allow"]
    assert row["count"] == 1
    assert row["credentials"] == {"kitchen": 1}
    assert row["clients"] == {"satellite/0.20.3": 1}
    mode = stat.S_IMODE(os.stat(tmp_path / "proxy_paths.json").st_mode)
    assert mode == 0o600


def test_repeat_traffic_is_batched(recorder, monkeypatch):
    tmp_path, clock = recorder
    pp.record("grocy", "GET", "api/stock", "k", "c", "allow")
    writes = []
    real = pp._flush_locked

    def counting(path):
        if pp._pending:
            writes.append(1)
        return real(path)
    monkeypatch.setattr(pp, "_flush_locked", counting)
    for _ in range(50):
        clock["t"] += 0.1
        pp.record("grocy", "GET", "api/stock", "k", "c", "allow")
    assert writes == []  # nothing new and under 30 s: no file write
    assert _disk(tmp_path)["grocy GET api/stock allow"]["count"] == 1
    clock["t"] += pp.FLUSH_INTERVAL
    pp.record("grocy", "GET", "api/stock", "k", "c", "allow")
    assert writes == [1]
    assert _disk(tmp_path)["grocy GET api/stock allow"]["count"] == 52


def test_new_template_flushes_immediately_and_snapshot_includes_pending(recorder):
    tmp_path, clock = recorder
    pp.record("grocy", "GET", "api/stock", "k", "c", "allow")
    clock["t"] += 1
    pp.record("grocy", "GET", "api/stock", "k", "c", "allow")   # pending only
    pp.record("mealie", "GET", "api/admin", "k", "c", "deny")   # new: flushes both
    rows = _disk(tmp_path)
    assert rows["mealie GET api/admin deny"]["count"] == 1
    assert rows["grocy GET api/stock allow"]["count"] == 2
    snap = pp.snapshot()
    assert snap[0]["verdict"] == "deny"  # refusals lead the review
    assert {r["template"] for r in snap} == {"api/stock", "api/admin"}


def test_recorder_merges_with_another_worker(recorder):
    tmp_path, clock = recorder
    (tmp_path / "proxy_paths.json").write_text(json.dumps({"paths": {
        "grocy GET api/stock allow": {
            "backend": "grocy", "method": "GET", "template": "api/stock",
            "verdict": "allow", "count": 10, "first_seen": "2026-01-01T00:00:00+00:00",
            "last_seen": "2026-01-02T00:00:00+00:00",
            "credentials": {"other": 10}, "clients": {"unlabelled": 10}}}}))
    pp.reset()
    clock["t"] += pp.FLUSH_INTERVAL
    pp.record("grocy", "GET", "api/stock", "k", "c", "allow")
    row = _disk(tmp_path)["grocy GET api/stock allow"]
    assert row["count"] == 11
    assert row["first_seen"] == "2026-01-01T00:00:00+00:00"
    assert row["credentials"] == {"other": 10, "k": 1}


def test_recorder_survives_unwritable_data_dir(monkeypatch, tmp_path):
    missing = tmp_path / "file-not-dir"
    missing.write_text("x")  # data_dir is a file: every write fails
    monkeypatch.setattr(settings, "data_dir", str(missing), raising=False)
    pp.reset()
    try:
        pp.record("grocy", "GET", "api/stock", "k", "c", "allow")
        pp.record("grocy", "GET", "api/stock", "k", "c", "allow")
        snap = pp.snapshot()
        assert snap[0]["count"] == 2  # kept in memory, still reviewable
    finally:
        pp.reset()


def test_invented_paths_cannot_force_a_write_per_request(recorder, monkeypatch):
    # A client sending a new made-up path each time gets the write-at-once
    # treatment only for the first MAX_KNOWN rows, then waits for the timer.
    monkeypatch.setattr(pp, "MAX_KNOWN", 5)
    writes = []
    real = pp._flush_locked

    def counting(path):
        if pp._pending:
            writes.append(1)
        return real(path)
    monkeypatch.setattr(pp, "_flush_locked", counting)
    for i in range(50):
        pp.record("grocy", "GET", f"api/made-up-{i}", "k", "c", "offlist")
    assert len(writes) == 5
    assert len(pp._known) == 5


def test_recorder_caps_distinct_rows(recorder, monkeypatch):
    monkeypatch.setattr(pp, "MAX_ENTRIES", 3)
    for i in range(10):
        pp.record("grocy", "GET", f"api/x{i}", "k", "c", "offlist")
    assert len(pp.snapshot()) == 3
