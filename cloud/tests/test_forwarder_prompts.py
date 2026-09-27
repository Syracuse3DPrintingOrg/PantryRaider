"""The forwarder asks Gemini for exactly what the app reads back.

Forager's food, receipt and enrich prompts are copies of the app's own, Gemini
is asked for JSON, and each reply comes back both as text (which the app
parses) and as parsed fields (which app releases from before that change read
directly, and without which every Forager scan came back as "Unknown").
Gemini is an httpx.MockTransport here; nothing leaves the process.
"""
import ast
import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest

from app import forwarder
from app.forwarder import GeminiForwarder
from tests.conftest import activate_entitlement

# The app's own provider, read as source (never imported: the cloud is a
# separate app, and that module needs the Google SDK).
APP_GEMINI = (Path(__file__).resolve().parents[2]
              / "service" / "app" / "providers" / "gemini.py")

FOOD_FIELDS = ("name", "quantity", "unit", "best_by_date", "storage_type",
               "category", "confidence")


def _gemini_reply(text):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 300,
                              "candidatesTokenCount": 40}}


def _run(kind, reply, text=""):
    """Forward one task to a mocked Gemini that answers with `reply`.
    Returns (result payload, the JSON body Gemini was sent)."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_gemini_reply(reply))

    fwd = GeminiForwarder(api_key="test-key", timeout=5.0,
                          transport=httpx.MockTransport(handler))
    image = b"fake-jpeg" if kind in ("food", "receipt") else None
    out = asyncio.run(fwd.forward(kind, image, "image/jpeg" if image else "",
                                  text))
    return out.result, seen["body"]


def _prompt(body):
    return body["contents"][0]["parts"][0]["text"]


# --- The request --------------------------------------------------------------

def test_food_prompt_asks_for_the_fields_the_app_reads():
    _, body = _run("food", "{}")
    prompt = _prompt(body)
    for field in FOOD_FIELDS:
        assert f'"{field}"' in prompt, f"the food prompt never asks for {field}"


def test_receipt_prompt_asks_for_store_date_and_items():
    _, body = _run("receipt", "{}")
    prompt = _prompt(body)
    for field in ("store", "purchase_date", "items", "storage_type",
                  "category"):
        assert f'"{field}"' in prompt


def test_enrich_prompt_carries_the_product_data_inside_it():
    info = json.dumps({"product_name": "kewpie mayo", "brands": "Kewpie"})
    _, body = _run("enrich", "{}", text=info)
    prompt = _prompt(body)
    # In the prompt's own slot, braces and all, with the instructions after
    # it, the way the app's Gemini provider builds it; not tacked on the end.
    assert info in prompt
    assert prompt.index(info) < prompt.index(
        "Return a JSON object with these exact fields")
    assert prompt == forwarder._ENRICH_PROMPT.format(info=info)
    for field in ("name", "category", "storage_type", "shelf_life_days",
                  "brand"):
        assert f'"{field}"' in prompt
    assert "{{" not in prompt and "{info}" not in prompt


@pytest.mark.parametrize("kind", ["food", "receipt", "enrich"])
def test_json_kinds_ask_gemini_for_json(kind):
    _, body = _run(kind, "{}")
    assert body["generationConfig"]["responseMimeType"] == "application/json"


@pytest.mark.parametrize("kind,cap", [("food", 2048), ("enrich", 1024),
                                      ("receipt", 8192), ("recipe", 8192)])
def test_every_kind_caps_its_output_and_leaves_room_for_the_answer(kind, cap):
    _, body = _run(kind, "{}")
    config = body["generationConfig"]
    assert config["maxOutputTokens"] == cap
    # Gemini 2.5 counts thinking toward maxOutputTokens, so an unbounded
    # think could use the whole cap and cut the answer off. The explicit
    # budget keeps at least half the cap for the answer itself.
    budget = config["thinkingConfig"]["thinkingBudget"]
    assert 0 <= budget <= cap // 2


def test_recipe_prompt_and_config_are_unchanged_apart_from_the_cap():
    _, body = _run("recipe", "{}", text="my chili recipe")
    assert _prompt(body).startswith("A home cook is sharing a recipe.")
    assert _prompt(body).endswith("\n\nmy chili recipe")
    assert "responseMimeType" not in body["generationConfig"]


# --- The result ---------------------------------------------------------------

def test_food_reply_comes_back_parsed_and_as_text():
    reply = '{"name": "Roma tomatoes", "quantity": 1}'
    result, _ = _run("food", reply)
    assert result["name"] == "Roma tomatoes"
    assert result["quantity"] == 1
    assert result["text"] == reply


def test_recipe_reply_is_still_exactly_the_text():
    # recipe_format.parse_recipe_draft reads this text; it must stay as is.
    reply = json.dumps({"title": "Chili", "ingredients": ["1 lb beans"],
                        "steps": ["Simmer."]})
    result, _ = _run("recipe", reply)
    assert result == {"text": reply}


def test_fenced_reply_is_read_the_way_the_app_reads_it():
    reply = ('```json\n{"name": "Kewpie Mayonnaise", '
             '"storage_type": "refrigerated"}\n```')
    result, _ = _run("enrich", reply, text="{}")
    assert result["name"] == "Kewpie Mayonnaise"
    assert result["storage_type"] == "refrigerated"
    assert result["text"] == reply


def test_receipt_object_keeps_store_date_and_items():
    reply = json.dumps({"store": "Wegmans", "purchase_date": "2026-09-20",
                        "items": [{"name": "Bananas", "quantity": 6}]})
    result, _ = _run("receipt", reply)
    assert result["store"] == "Wegmans"
    assert result["purchase_date"] == "2026-09-20"
    assert result["items"] == [{"name": "Bananas", "quantity": 6}]
    assert result["text"] == reply


def test_receipt_list_is_wrapped_as_items():
    reply = '[{"name": "Whole milk", "quantity": 1}]'
    result, _ = _run("receipt", reply)
    assert result == {"items": [{"name": "Whole milk", "quantity": 1}],
                      "text": reply}


@pytest.mark.parametrize("reply", [
    "not json at all",
    '[{"name": "Roma tomatoes"}]',   # a list is only unwrapped for receipts
    '"just a string"',
    "",
])
def test_a_reply_without_an_object_comes_back_as_text_only(reply):
    result, _ = _run("food", reply)
    assert result == {"text": reply}


@pytest.mark.parametrize("reply", [
    '{"name": "x", "quantity": 1e999}',
    '{"name": "x", "quantity": NaN}',
    '{"name": "x", "confidence": -Infinity}',
])
def test_non_finite_numbers_stay_in_the_text(reply):
    # The proxy answers in strict JSON, which has no infinity or NaN.
    result, _ = _run("food", reply)
    assert result == {"text": reply}


# --- End to end through the proxy -------------------------------------------

def _proxy_with_gemini_saying(monkeypatch, reply):
    from app.routers import ai as ai_router

    def handler(request):
        return httpx.Response(200, json=_gemini_reply(reply))

    monkeypatch.setattr(ai_router, "get_forwarder", lambda: GeminiForwarder(
        api_key="test-key", timeout=5.0,
        transport=httpx.MockTransport(handler)))


def test_proxy_hands_the_parsed_fields_to_the_app(client, instance_token,
                                                  monkeypatch):
    activate_entitlement()
    reply = json.dumps({"name": "Roma tomatoes", "quantity": 4,
                        "unit": "pieces", "storage_type": "room_temp",
                        "category": "Produce", "confidence": 0.93})
    _proxy_with_gemini_saying(monkeypatch, reply)
    resp = client.post(
        "/v1/ai/analyze", data={"kind": "food"},
        files={"image": ("p.jpg", io.BytesIO(b"fake"), "image/jpeg")},
        headers={"Authorization": f"Bearer {instance_token}"})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["name"] == "Roma tomatoes"
    assert result["storage_type"] == "room_temp"
    assert result["category"] == "Produce"
    assert json.loads(result["text"]) == json.loads(reply)


def test_proxy_still_answers_when_the_reply_holds_nan(client, instance_token,
                                                     monkeypatch):
    activate_entitlement()
    reply = '{"name": "x", "quantity": NaN}'
    _proxy_with_gemini_saying(monkeypatch, reply)
    resp = client.post(
        "/v1/ai/analyze", data={"kind": "enrich", "text": "{}"},
        headers={"Authorization": f"Bearer {instance_token}"})
    assert resp.status_code == 200
    assert resp.json()["result"] == {"text": reply}


def test_proxy_still_answers_when_the_reply_holds_half_a_surrogate_pair(
        client, instance_token, monkeypatch):
    # "\ud800" on its own decodes to half of a surrogate pair, which UTF-8
    # cannot encode: parsed into the result, it would fail the proxy's answer
    # after the tokens were charged. The text carries it as written.
    activate_entitlement()
    reply = '{"name": "Tomato \\ud800", "quantity": 1}'
    _proxy_with_gemini_saying(monkeypatch, reply)
    resp = client.post(
        "/v1/ai/analyze", data={"kind": "enrich", "text": "{}"},
        headers={"Authorization": f"Bearer {instance_token}"})
    assert resp.status_code == 200
    assert resp.json()["result"] == {"text": reply}


def test_a_whole_surrogate_pair_is_parsed():
    result, _ = _run("food", '{"name": "Tomato \\ud83c\\udf45", "quantity": 1}')
    assert result["name"] == "Tomato \U0001f345"


# --- The copies stay copies ---------------------------------------------------

def _app_prompts() -> dict:
    """The app's module-level prompt strings (each a triple-quoted string with
    .strip() applied), read from its source with ast."""
    if not APP_GEMINI.exists():
        pytest.skip("the app's source is not beside this checkout")
    found = {}
    for node in ast.parse(APP_GEMINI.read_text(encoding="utf-8")).body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        value = node.value
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr == "strip"
                and isinstance(value.func.value, ast.Constant)
                and isinstance(value.func.value.value, str)):
            found[node.targets[0].id] = value.func.value.value.strip()
    return found


@pytest.mark.parametrize("name", ["_FOOD_PROMPT", "_RECEIPT_PROMPT",
                                  "_ENRICH_PROMPT"])
def test_prompts_are_the_apps_own(name):
    app_prompts = _app_prompts()
    assert name in app_prompts, (
        f"{name} is no longer a plain string in service/app/providers/"
        "gemini.py; update this reader and the copy in cloud/app/forwarder.py")
    assert getattr(forwarder, name) == app_prompts[name], (
        f"{name} in cloud/app/forwarder.py no longer matches the app's; "
        "copy the app's prompt across so both ask for the same JSON")
