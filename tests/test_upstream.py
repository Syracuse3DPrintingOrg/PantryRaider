"""The satellite's upstream request helper (services/upstream.py)."""
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE))

from app.config import APP_VERSION, settings  # noqa: E402
from app.services import upstream  # noqa: E402


@pytest.fixture
def sat(monkeypatch):
    monkeypatch.setattr(settings, "deployment_mode", "pi_remote")
    monkeypatch.setattr(settings, "remote_server_url", "http://192.168.1.50:9284")
    monkeypatch.setattr(settings, "upstream_api_key", "")
    monkeypatch.setattr(settings, "upstream_credential_id", "")
    return monkeypatch


def test_caps_constants():
    assert upstream.RELAY_CAP == "relay1"
    assert upstream.DECK_PROXY_CAP == "deckproxy1"


def test_client_id():
    assert upstream.client_id() == "satellite/" + APP_VERSION


def test_headers_without_a_key(sat):
    assert upstream.headers() == {
        "X-PR-Client": "satellite/" + APP_VERSION,
        "X-PR-Caps": "relay1",
    }


def test_headers_with_a_key(sat):
    sat.setattr(settings, "upstream_api_key", "sat-key")
    assert upstream.headers() == {
        "X-PR-Client": "satellite/" + APP_VERSION,
        "X-PR-Caps": "relay1",
        "X-API-Key": "sat-key",
    }


def test_extra_caps_replace_the_default(sat):
    h = upstream.headers({"X-PR-Caps": "relay1,deckproxy1"})
    assert h["X-PR-Caps"] == "relay1,deckproxy1"
    assert [k for k in h if k.lower() == "x-pr-caps"] == ["X-PR-Caps"]
    # A differently spelled name still replaces it, never adds a second one.
    h = upstream.headers({"x-pr-caps": "relay1,deckproxy1"})
    assert [k for k in h if k.lower() == "x-pr-caps"] == ["x-pr-caps"]
    assert h["x-pr-caps"] == "relay1,deckproxy1"


def test_extra_content_type_appears_once(sat):
    sat.setattr(settings, "upstream_api_key", "sat-key")
    h = upstream.headers({"Content-Type": "application/json"})
    assert [k for k in h if k.lower() == "content-type"] == ["Content-Type"]
    assert h["Content-Type"] == "application/json"
    assert h["X-API-Key"] == "sat-key"


def test_extra_is_not_modified(sat):
    extra = {"Content-Type": "application/json"}
    upstream.headers(extra)
    assert extra == {"Content-Type": "application/json"}


def test_request_kwargs_shape(sat):
    sat.setattr(settings, "upstream_api_key", "sat-key")
    kw = upstream.request_kwargs()
    assert kw == {"headers": upstream.headers()}
    assert set(kw) == {"headers"}
    kw = upstream.request_kwargs({"Content-Type": "application/json"})
    assert set(kw) == {"headers"}
    assert kw["headers"]["Content-Type"] == "application/json"


def test_request_kwargs_on_the_wire(sat):
    # What the server receives when a call site splats request_kwargs() into
    # httpx: one value per header name, whatever the caller's spelling.
    import httpx
    sat.setattr(settings, "upstream_api_key", "sat-key")
    seen = []

    def handler(request):
        seen.append(list(request.headers.multi_items()))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.post("http://192.168.1.50:9284/api/x", content=b"{}",
                    **upstream.request_kwargs({"content-type": "application/json",
                                               "X-PR-Caps": "relay1,deckproxy1"}))
    items = seen[0]
    names = [k.lower() for k, _ in items]
    assert names.count("content-type") == 1
    assert names.count("x-pr-caps") == 1
    assert names.count("x-api-key") == 1
    got = {k.lower(): v for k, v in items}
    assert got["content-type"] == "application/json"
    assert got["x-pr-caps"] == "relay1,deckproxy1"
    assert got["x-pr-client"] == "satellite/" + APP_VERSION
    assert got["x-api-key"] == "sat-key"


def test_linked_follows_settings(sat):
    assert upstream.linked() is False
    assert upstream.linked() == settings.upstream_linked()
    sat.setattr(settings, "upstream_api_key", "sat-key")
    assert upstream.linked() is True
    sat.setattr(settings, "upstream_api_key", "")
    sat.setattr(settings, "upstream_credential_id", "cred-1")
    assert upstream.linked() is True
    assert upstream.linked() == settings.upstream_linked()
    # The key alone, without the server, is not a link.
    sat.setattr(settings, "remote_server_url", "")
    assert upstream.linked() is False


def test_signing_key(sat):
    assert upstream.signing_key() == ""
    sat.setattr(settings, "upstream_api_key", "sat-key")
    assert upstream.signing_key() == "sat-key"
