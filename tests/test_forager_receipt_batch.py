"""A long receipt's shelf-life estimates on Forager.

Forager's AI proxy allows 30 requests a minute per install, and the estimates
used to be one enrich call per receipt line, so on a long receipt the lines
past about the 28th came back with no estimate. Forager now takes a batch of
lines in one request (the enrich_batch kind); the app splits a receipt into
batches Forager accepts and falls back to one call per line when Forager is a
release from before the kind (the two deploy separately). Forager is an
httpx.MockTransport here; nothing leaves the process.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import sys
import types
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

_SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(_SERVICE))

from app import dependencies  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.food import FoodItem, StorageType  # noqa: E402
from app.providers import cloud as cloud_mod  # noqa: E402
from app.providers.base import VisionProvider  # noqa: E402
from app.providers.cloud import CloudProvider  # noqa: E402
from app.routers import analyze  # noqa: E402
from app.services import usage  # noqa: E402

UNKNOWN_KIND = {"detail": "Unknown task kind: enrich_batch"}


def _form(request: httpx.Request) -> dict:
    """The kind and text of a text-only /v1/ai/analyze request."""
    fields = parse_qs(request.content.decode(), keep_blank_values=True)
    return {k: v[0] for k, v in fields.items()}


def _answer_every_line(lines, days=10):
    return json.dumps({"items": [
        {"index": line["index"], "storage_type": "refrigerated",
         "shelf_life_days": days + line["index"]} for line in lines]})


class _Forager:
    """Answers /v1/ai/analyze the way the proxy does, and keeps every request.

    knows_batch=False plays a release from before enrich_batch: it refuses
    the kind with its 400 before the rate limit, as every release has.
    rate_limit plays the per-install, per-minute window (one window for the
    whole test)."""

    def __init__(self, knows_batch=True, rate_limit=0, receipt_lines=0):
        self.knows_batch = knows_batch
        self.rate_limit = rate_limit
        self.receipt_lines = receipt_lines
        self.requests: list[dict] = []
        self.counted = 0
        self.fail_batch: set[int] = set()  # batch numbers (1-based) to fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        photo = re.search(rb'name="kind"\r\n\r\n([a-z_]+)\r\n', request.content)
        if photo:  # a multipart image task
            kind, text = photo.group(1).decode(), ""
        else:
            form = _form(request)
            kind, text = form["kind"], form.get("text", "")
        self.requests.append({"kind": kind, "text": text})
        if kind == "enrich_batch" and not self.knows_batch:
            return httpx.Response(400, json=UNKNOWN_KIND)
        if self.rate_limit and self.counted >= self.rate_limit:
            return httpx.Response(429, json={"detail": "Too many requests, slow down"})
        self.counted += 1
        if kind == "receipt":
            reply = json.dumps({"store": "Wegmans", "items": [
                {"name": f"Line {n}", "quantity": 1, "category": "Dairy",
                 "storage_type": "refrigerated"}
                for n in range(1, self.receipt_lines + 1)]})
        elif kind == "enrich":
            reply = json.dumps({"name": "x", "storage_type": "refrigerated",
                                "shelf_life_days": 9})
        else:
            batch_no = sum(1 for r in self.requests if r["kind"] == "enrich_batch")
            if batch_no in self.fail_batch:
                return httpx.Response(502, json={"detail": {"error": "upstream_error"}})
            reply = _answer_every_line(json.loads(text))
        return httpx.Response(200, json={"result": {"text": reply}, "tokens": 40})

    def of_kind(self, kind):
        return [r for r in self.requests if r["kind"] == kind]


def _provider(forager) -> CloudProvider:
    return CloudProvider("https://cloud.test", "prc_secret",
                         transport=httpx.MockTransport(forager))


def _infos(count):
    return [{"product_name": f"Item {n}", "brand": None, "category": "Dairy"}
            for n in range(1, count + 1)]


@pytest.fixture(autouse=True)
def _data_dir(monkeypatch, tmp_path):
    # The provider mirrors Forager's token counts into the local usage file.
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


# --- The provider ------------------------------------------------------------------

def test_a_receipt_goes_in_one_request():
    forager = _Forager()
    answers = asyncio.run(_provider(forager).estimate_shelf_lives(_infos(25)))
    assert [r["kind"] for r in forager.requests] == ["enrich_batch"]
    lines = json.loads(forager.requests[0]["text"])
    assert [line["index"] for line in lines] == list(range(1, 26))
    assert lines[0] == {"index": 1, "name": "Item 1", "category": "Dairy"}
    assert len(answers) == 25
    assert answers[0] == {"index": 1, "storage_type": "refrigerated", "shelf_life_days": 11}
    assert answers[24]["shelf_life_days"] == 35


def test_the_tokens_are_mirrored_into_the_local_history(_data_dir):
    asyncio.run(_provider(_Forager()).estimate_shelf_lives(_infos(3)))
    history = json.loads((_data_dir / "ai_usage.json").read_text())
    assert history["by_provider"]["cloud"] == 40


def test_a_hundred_lines_go_in_batches_forager_accepts():
    forager = _Forager()
    answers = asyncio.run(_provider(forager).estimate_shelf_lives(_infos(100)))
    batches = [json.loads(r["text"]) for r in forager.requests]
    assert [len(b) for b in batches] == [30, 30, 30, 10]
    assert all(len(r["text"]) <= cloud_mod._BATCH_MAX_CHARS for r in forager.requests)
    # Each request numbers its own lines from 1.
    assert all([line["index"] for line in b] == list(range(1, len(b) + 1)) for b in batches)
    assert len(answers) == 100 and all(a is not None for a in answers)
    # Line 31 is the first line of the second request.
    assert answers[30]["shelf_life_days"] == 11


def test_long_lines_split_on_the_text_limit_too():
    long = [{"product_name": "N" * 300, "brand": "B" * 300, "category": "Condiments"}
            for _ in range(60)]
    batches = cloud_mod.batch_requests(long)
    assert len(batches) > 2
    for batch in batches:
        text = cloud_mod._batch_text(batch)
        assert len(text) <= cloud_mod._BATCH_MAX_CHARS
        assert len(batch) <= cloud_mod.BATCH_MAX_ITEMS
    # Fields are cut to the receipt parser's own lengths.
    first = json.loads(cloud_mod._batch_text(batches[0]))[0]
    assert len(first["name"]) == 120 and len(first["brand"]) == 80
    assert sum(len(b) for b in batches) == 60


def test_answers_are_matched_by_their_index_only():
    reply = {"items": [
        {"index": 3, "shelf_life_days": 3},
        {"index": "1", "shelf_life_days": 1},      # a numeric string still counts
        {"index": 3, "shelf_life_days": 99},       # a repeat never overwrites
        {"index": 1.5, "shelf_life_days": 15},     # not a line number
        {"index": 0, "shelf_life_days": 0},        # out of range
        {"index": 6, "shelf_life_days": 6},
        {"index": True, "shelf_life_days": 1},     # true is not 1 here
        {"shelf_life_days": 4},                    # no index: never guessed
        "line five",
    ]}
    answers = cloud_mod.batch_answers(reply, 5)
    assert [a and a["shelf_life_days"] for a in answers] == [1, None, 3, None, None]


def test_a_bare_list_reply_is_read_too():
    answers = cloud_mod.batch_answers([{"index": 2, "storage_type": "frozen"}], 2)
    assert answers == [None, {"index": 2, "storage_type": "frozen"}]


@pytest.mark.parametrize("reply", [None, "text", {"items": "none"}, {}, 7])
def test_a_reply_with_no_list_gives_no_answers(reply):
    assert cloud_mod.batch_answers(reply, 3) == [None, None, None]


def test_odd_values_are_dropped_from_an_answer():
    # NaN and infinity cannot travel on as JSON (a satellite's estimates go
    # back over the relay), and nested values are nothing the app reads.
    text = ('{"items": [{"index": 1, "shelf_life_days": NaN, "storage_type": "frozen",'
            ' "extra": {"a": 1}, "more": [1], "flag": true, "big": 1e999}]}')
    p = CloudProvider("https://cloud.test", "prc_secret", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"result": {"text": text}, "tokens": 5})))
    answers = asyncio.run(p.estimate_shelf_lives(_infos(1)))
    assert answers == [{"index": 1, "storage_type": "frozen"}]
    json.dumps(answers, allow_nan=False)


def test_an_older_forager_means_one_call_per_line_and_is_remembered(monkeypatch):
    forager = _Forager(knows_batch=False)
    p = _provider(forager)
    clock = [1000.0]
    # Only the provider's own reference to the clock moves, never time itself.
    monkeypatch.setattr(cloud_mod, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    assert asyncio.run(p.estimate_shelf_lives(_infos(45))) is None
    assert len(forager.of_kind("enrich_batch")) == 2  # both batches asked at once
    # Scans in the next hour go straight to one call per line.
    clock[0] += 3500
    assert asyncio.run(p.estimate_shelf_lives(_infos(5))) is None
    assert len(forager.requests) == 2
    # After that it asks again, and an updated Forager is used.
    clock[0] += 200
    forager.knows_batch = True
    answers = asyncio.run(p.estimate_shelf_lives(_infos(5)))
    assert len(answers) == 5 and all(answers)


def test_a_failed_batch_costs_only_its_own_lines():
    forager = _Forager()
    forager.fail_batch = {2}
    p = _provider(forager)
    answers = asyncio.run(p.estimate_shelf_lives(_infos(60)))
    assert len(answers) == 60
    assert sum(a is not None for a in answers) == 30
    # The failure is not taken for a Forager without the kind.
    assert p._batch_retry_at == 0.0


@pytest.mark.parametrize("status, body", [
    (402, {"detail": {"error": "quota_exceeded", "used": 10, "quota": 10,
                      "month": "2026-10"}}),
    (429, {"detail": "Too many requests, slow down"}),
    (400, {"detail": "A batch request needs a JSON list of items."}),
    (413, {"detail": "That request carries too many items. The limit is 30 per request."}),
    (200, {"result": {"text": "I am not sure about these."}, "tokens": 3}),
])
def test_other_refusals_cost_the_estimates_not_the_scan(status, body):
    p = CloudProvider("https://cloud.test", "prc_secret", transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=body)))
    assert asyncio.run(p.estimate_shelf_lives(_infos(3))) == [None, None, None]
    # Only "unknown kind" switches to one call per line.
    assert p._batch_retry_at == 0.0


def test_an_unreachable_forager_costs_the_estimates_not_the_scan():
    def down(request):
        raise httpx.ConnectError("refused")

    p = CloudProvider("https://cloud.test", "prc_secret",
                      transport=httpx.MockTransport(down))
    assert asyncio.run(p.estimate_shelf_lives(_infos(2))) == [None, None]


def test_nothing_to_estimate_sends_nothing():
    forager = _Forager()
    assert asyncio.run(_provider(forager).estimate_shelf_lives([])) == []
    assert forager.requests == []


def test_the_other_kinds_keep_their_error_mapping():
    # Every Forager knows food, receipt and enrich; their errors read as before.
    p = CloudProvider("https://cloud.test", "prc_secret", transport=httpx.MockTransport(
        lambda request: httpx.Response(400, json={"detail": "Unknown task kind: enrich"})))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(p.enrich_product({"product_name": "x"}))
    assert exc.value.status_code == 502
    assert exc.value.detail == "Forager error: Unknown task kind: enrich"


# --- The receipt flow ----------------------------------------------------------------

class _BatchProvider(VisionProvider):
    """Has a batch path; counts both kinds of call."""

    def __init__(self, answers=None, error=None):
        self.batches: list[list[dict]] = []
        self.per_line = 0
        self.answers = answers
        self.error = error

    async def analyze_food(self, image_data, mime_type):
        raise NotImplementedError

    async def analyze_receipt(self, image_data, mime_type):
        raise NotImplementedError

    async def health_check(self):
        return True

    async def enrich_product(self, info):
        self.per_line += 1
        return {"storage_type": "dry", "shelf_life_days": 30}

    async def estimate_shelf_lives(self, items):
        self.batches.append(items)
        if self.error:
            raise self.error
        if self.answers is not None:
            return self.answers
        return [{"storage_type": "frozen", "shelf_life_days": 90} for _ in items]


@pytest.fixture
def budget_ok(monkeypatch):
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: False)


def _use(monkeypatch, provider):
    monkeypatch.setattr(dependencies, "get_enrich_provider", lambda: provider)
    return provider


def test_a_batch_provider_gets_the_lines_in_one_call(monkeypatch, budget_ok):
    provider = _use(monkeypatch, _BatchProvider())
    printed = date(2027, 3, 1)
    items = [FoodItem(name=f"Line {n}") for n in range(70)]
    items[1].best_by_date = printed
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.per_line == 0
    assert len(provider.batches) == 1
    sent = provider.batches[0]
    # The first 60 lines, less the one with a printed date.
    assert len(sent) == 59
    assert sent[0] == {"product_name": "Line 0", "brand": None, "category": "Other"}
    assert "Line 1" not in [s["product_name"] for s in sent]
    assert items[1].best_by_date == printed
    ninety = date.today() + timedelta(days=90)
    assert all(i.best_by_date == ninety for n, i in enumerate(items[:60]) if n != 1)
    assert all(i.storage_type == StorageType.frozen for n, i in enumerate(items[:60]) if n != 1)
    assert all(i.best_by_date is None for i in items[60:])


def test_no_batch_path_means_one_call_per_line(monkeypatch, budget_ok):
    provider = _use(monkeypatch, _BatchProvider())

    async def none(items):
        provider.batches.append(items)
        return None
    provider.estimate_shelf_lives = none
    items = [FoodItem(name=f"Line {n}") for n in range(5)]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert len(provider.batches) == 1
    assert provider.per_line == 5
    assert all(i.best_by_date == date.today() + timedelta(days=30) for i in items)


def test_a_provider_object_without_the_method_still_works(monkeypatch, budget_ok):
    class _Bare:
        calls = 0

        async def enrich_product(self, info):
            self.calls += 1
            return {"shelf_life_days": 4}

    provider = _use(monkeypatch, _Bare())
    items = [FoodItem(name="a"), FoodItem(name="b")]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.calls == 2
    assert all(i.best_by_date == date.today() + timedelta(days=4) for i in items)


def test_a_batch_failure_never_breaks_intake(monkeypatch, budget_ok):
    provider = _use(monkeypatch, _BatchProvider(error=RuntimeError("down")))
    items = [FoodItem(name=f"Line {n}") for n in range(3)]
    asyncio.run(analyze._estimate_shelf_lives(items))
    # Line by line would fail the same way, so it is not tried.
    assert provider.per_line == 0
    assert all(i.best_by_date is None for i in items)


def test_unusable_answers_keep_the_default(monkeypatch, budget_ok):
    _use(monkeypatch, _BatchProvider(answers=[
        None, {"shelf_life_days": math.inf}, {"shelf_life_days": "soon"},
        {"shelf_life_days": 6, "storage_type": "fridge"}]))
    items = [FoodItem(name=f"Line {n}") for n in range(4)]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert [i.best_by_date for i in items[:3]] == [None, None, None]
    assert items[3].best_by_date == date.today() + timedelta(days=6)
    assert items[3].storage_type == StorageType.refrigerated


def test_over_budget_makes_no_batch_call(monkeypatch):
    provider = _use(monkeypatch, _BatchProvider())
    monkeypatch.setattr(usage, "over_budget", lambda *a, **k: True)
    items = [FoodItem(name="Milk")]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.batches == [] and provider.per_line == 0
    assert items[0].best_by_date is None


def test_a_bandit_on_a_forager_server_batches_too(monkeypatch, budget_ok):
    """A Bandit's estimates run on its main server; with Forager there, the
    receipt reaches Forager as batches, not one relay and request per line.
    This process plays both the Bandit and the server."""
    from app.main import app
    from app.providers import fleet as fleet_mod
    from app.routers import fleet as fleet_router
    key = "fleet-test-key-fake"
    monkeypatch.setattr(settings, "deployment_mode", "server")
    monkeypatch.setattr(settings, "api_key", key)
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "remote_server_url", "http://testserver")
    monkeypatch.setattr(settings, "upstream_api_key", key)
    forager = _Forager(rate_limit=30)
    on_server = _provider(forager)
    monkeypatch.setattr(fleet_router, "_provider", lambda role: on_server)
    monkeypatch.setattr(fleet_mod, "_http", lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.168.1.20", 50000)),
        base_url="http://testserver"))
    _use(monkeypatch, fleet_mod.FleetProvider("enrich"))
    items = [FoodItem(name=f"Line {n}") for n in range(45)]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert [r["kind"] for r in forager.requests] == ["enrich_batch", "enrich_batch"]
    assert all(i.best_by_date is not None for i in items)


def test_a_receipt_of_printed_dates_asks_nothing(monkeypatch, budget_ok):
    provider = _use(monkeypatch, _BatchProvider())
    items = [FoodItem(name="Yogurt", best_by_date=date(2027, 1, 1))]
    asyncio.run(analyze._estimate_shelf_lives(items))
    assert provider.batches == [] and provider.per_line == 0


# --- End to end: a long receipt scanned on Forager -------------------------------------

@pytest.fixture
def forager_client(monkeypatch, budget_ok):
    cwd = os.getcwd()
    os.chdir(_SERVICE)
    monkeypatch.setattr(settings, "grocy_base_url", "http://grocy.test")
    monkeypatch.setattr(settings, "grocy_api_key", "k")
    monkeypatch.setattr(settings, "vision_provider", "cloud")
    monkeypatch.setattr(settings, "cloud_instance_token", "prc_secret")
    monkeypatch.setattr(settings, "llm_expiry_enabled", True)
    monkeypatch.setattr(settings, "auth_required", False, raising=False)
    monkeypatch.setattr(settings, "auth_password", "", raising=False)
    from app.main import app

    def scan(forager):
        provider = _provider(forager)
        app.dependency_overrides[dependencies.get_vision_provider] = lambda: provider
        monkeypatch.setattr(dependencies, "get_enrich_provider", lambda: provider)
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (200, 180, 160)).save(buf, format="PNG")
        return TestClient(app).post(
            "analyze/receipt", files={"file": ("r.png", buf.getvalue(), "image/png")})

    try:
        yield scan
    finally:
        app.dependency_overrides.clear()
        os.chdir(cwd)


def test_every_line_of_a_long_receipt_gets_an_estimate(forager_client):
    forager = _Forager(rate_limit=30, receipt_lines=60)
    r = forager_client(forager)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 60
    # One receipt request and two batches, well inside the limit.
    assert [x["kind"] for x in forager.requests] == ["receipt", "enrich_batch", "enrich_batch"]
    days = [(date.fromisoformat(i["best_by_date"]) - date.today()).days for i in items]
    assert days == [11 + (n % 30) for n in range(60)]


def test_an_older_forager_still_scans_line_by_line(forager_client):
    # The behavior before the batch kind, kept for a Forager that lacks it:
    # the scan works, and the estimates stop where the per-minute limit does.
    forager = _Forager(knows_batch=False, rate_limit=30, receipt_lines=60)
    r = forager_client(forager)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 60
    assert len(forager.of_kind("enrich_batch")) == 2
    assert len(forager.of_kind("enrich")) == 60
    estimated = [i for i in items if i["best_by_date"] == str(date.today() + timedelta(days=9))]
    assert len(estimated) == 29
