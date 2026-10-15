"""Connection tests for Grocy, Mealie, AI providers and recipe APIs, plus
zero-touch first-run provisioning."""

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...config import settings, device_hostname, model_retirement
from ...services import egress
from ...services import pairing as pairing_svc
from ._common import (
    router, TestGrocyPayload, TestMealiePayload, _refuse_probe_target, _safe_error,
)
from .page import TestProviderPayload, TestRecipesPayload, _LOCAL_GROCY_CANDIDATES
from .cloud import _cloud_client, _cloud_headers


def _is_local_grocy_host(url: str) -> bool:
    """True when url names this device (loopback, the Docker service, or the
    device's own <hostname>.local / LAN address). Such an address may be entered
    as a browser link the app container cannot itself reach, so the test is
    allowed to fall back to loopback candidates. A genuinely remote Grocy is
    tested only at the URL given, so we never mask its auth error with a
    different, co-hosted Grocy."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "grocy"}:
        return True
    own = (device_hostname() or "").lower()
    if own and host in (own, f"{own}.local"):
        return True
    return False


def _grocy_test_targets(entered: str) -> list[str]:
    """Server-side URLs to try for a Grocy connection test, best first.

    The user may enter a browser-facing address for a co-hosted Grocy (e.g.
    http://<host>.local:9383 or http://127.0.0.1:9383) that the app container
    cannot resolve or reach, even though Grocy is up. For such local addresses we
    try the entered URL first, then fall back to the addresses the app process
    can actually use (the Docker service name and loopback on the published
    port). A remote Grocy URL is tested as-is, with no fallback. Duplicates are
    dropped while preserving order.
    """
    entered = (entered or "").rstrip("/")
    candidates = [entered]
    if _is_local_grocy_host(entered):
        candidates += _LOCAL_GROCY_CANDIDATES
    targets: list[str] = []
    for u in candidates:
        u = (u or "").rstrip("/")
        if u and u not in targets:
            targets.append(u)
    return targets


@router.post("/test/grocy")
async def test_grocy(payload: TestGrocyPayload):
    entered = (payload.grocy_base_url or settings.grocy_base_url).rstrip("/")
    key = payload.grocy_api_key or settings.grocy_api_key
    if not entered or not key:
        return JSONResponse({"ok": False, "error": "URL and API key are both required."})

    # Grocy's API needs the GROCY-API-KEY header; a bare browser hit returns 401.
    # We always test the API endpoint with the key so "reachable" means "usable".
    last_unreachable = ""
    auth_failure = ""
    for url in _grocy_test_targets(entered):
        # Guard only the address the CALLER supplied. The rest of the list is
        # our own fixed fallback candidates (_LOCAL_GROCY_CANDIDATES), which are
        # not caller-controlled and legitimately include loopback: a co-hosted
        # Grocy on host networking really is at 127.0.0.1:9383. So entering a
        # browser-facing local address still finds the real backend, while an
        # address someone made up is never fetched.
        if url == entered:
            refusal = _refuse_probe_target(url)
            if refusal:
                last_unreachable = refusal
                continue
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{url}/api/system/info",
                                     headers={"GROCY-API-KEY": key})
        except Exception as e:
            last_unreachable = _safe_error(e, key)
            continue
        if r.status_code == 200:
            try:
                info = r.json()
            except ValueError:
                info = None
            if not isinstance(info, dict):
                # A 200 web page (a login portal, a proxy, Grocy's own PHP
                # error page) is not the API. The hint is our own copy for a
                # recognised Grocy problem, never the upstream body.
                from ...services.grocy import known_grocy_fix_hint
                error = "Grocy answered with a web page instead of data."
                hint = known_grocy_fix_hint(r.text)
                if hint:
                    error = f"{error} {hint}"
                return {"ok": False, "error": error}
            version = info.get("grocy_version", "?")
            if isinstance(version, dict):
                # Grocy 4 reports {"Version": "4.x.y", "ReleaseDate": ...}.
                version = version.get("Version") or "?"
            if url != entered:
                # Only a fallback answered. Inventory calls use the saved
                # address and nothing else, so reporting this as connected
                # would hide a dead inventory behind a green result.
                return {"ok": False, "fallback_url": url, "error": (
                    f"Grocy {version} is running, but Pantry Raider cannot "
                    f"reach it at {entered}. It answers at {url}.")}
            return {"ok": True, "message": f"Connected: Grocy {version}"}
        if r.status_code in (401, 403):
            # Reached Grocy, but it rejected the key. Distinct from unreachable:
            # the address is good, the API key is wrong or lacks permissions.
            # Keep the first reachable URL's report (the one the user entered).
            if not auth_failure:
                auth_failure = (f"Grocy is reachable at {url} but rejected the API "
                                f"key (HTTP {r.status_code}). Check the key under "
                                f"Grocy, Profile, Manage API keys.")
            continue
        if r.status_code >= 500:
            # Grocy 4.7 answers a broken config.php (a pre-4.7 AUTH_CLASS) with a
            # 500. The hint is our own copy for that known problem, so it can be
            # shown without echoing anything Grocy sent.
            from ...services.grocy import known_grocy_fix_hint
            hint = known_grocy_fix_hint(r.text[:20000])
            if hint:
                return {"ok": False, "error": (
                    f"Grocy is running at {url} but reported a problem with its "
                    f"own setup (HTTP {r.status_code}). {hint}")}
        # Status only, never the upstream body: this answer can go to an
        # unauthenticated caller during setup, and echoing a fetched body would
        # turn the connection test into a read primitive for anything on the
        # network that answers (FoodAssistant-3okp).
        last_unreachable = f"HTTP {r.status_code} from {url}"

    if auth_failure:
        return {"ok": False, "error": auth_failure}
    return {"ok": False,
            "error": last_unreachable or f"Could not reach Grocy at {entered}."}


_MEALIE_NOT_DATA_MSG = (
    "Mealie answered with a web page instead of data. Check that the Mealie "
    "address is the plain server address, with no /api or other path on the end.")


@router.post("/test/mealie")
async def test_mealie(payload: TestMealiePayload):
    url = (payload.mealie_base_url or settings.mealie_base_url).rstrip("/")
    key = payload.mealie_api_key or settings.mealie_api_key
    if not url or not key:
        return JSONResponse({"ok": False, "error": "URL and API token are both required."})
    refusal = _refuse_probe_target(url)
    if refusal:
        return {"ok": False, "error": refusal}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{url}/api/users/self",
                                 headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            body = None
            if "json" in r.headers.get("content-type", "").lower():
                try:
                    body = r.json()
                except ValueError:
                    body = None
            if not isinstance(body, dict):
                return {"ok": False, "error": _MEALIE_NOT_DATA_MSG}
            user = body.get("username") or body.get("email", "?")
            return {"ok": True, "message": f"Connected: authenticated as {user}"}
        # Status only, never the upstream body (see the Grocy test above).
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": _safe_error(e, key)}


class FirstRunPayload(BaseModel):
    base_url: str = ""


class FirstRunRevealPayload(BaseModel):
    service: str = ""


def first_run_reveal_mark(service: str, secret: str) -> str:
    """A short, one-way marker for one generated sign-in.

    Pure so the one-showing rule below is testable, and a fingerprint rather
    than the password itself so the record of what was already shown is not a
    second copy of the secret.
    """
    import hashlib
    return f"{service}:{hashlib.sha256(secret.encode()).hexdigest()[:16]}"


# Until setup is finished there is no login yet, so these two answer whoever
# asks. Both then insist on a browser that is really on the home network (no
# proxy or tunnel in between), and the sign-in is shown once per generated
# password, so an unattended new device does not hand its backend account to a
# passer-by on the network for as long as it sits on the wizard.
_FIRST_RUN_REMOTE_MSG = ("Open this from a browser on the same home network to "
                         "finish setting up this device.")
_FIRST_RUN_SHOWN_MSG = ("That sign-in was already shown once. Finish setup, then "
                        "you can see it again on the Settings page.")


@router.post("/first-run/reveal")
async def first_run_reveal(request: Request, payload: FirstRunRevealPayload):
    """Reveal the backend sign-in that first-run provisioning generated.

    Only answers for a password provisioning actually stored, and only on an
    explicit request from the settings pane; the secret is never rendered
    into the page itself. Registered before the /first-run/{service} route
    so "reveal" is never taken as a service name.
    """
    pre_setup = not settings.is_configured()
    if pre_setup and not pairing_svc.is_local_network_request(request):
        return JSONResponse({"ok": False, "error": _FIRST_RUN_REMOTE_MSG},
                            status_code=403)
    if payload.service == "grocy" and settings.grocy_admin_password:
        username, secret = "admin", settings.grocy_admin_password
    elif payload.service == "mealie" and settings.mealie_admin_password:
        from ...services.first_run import MEALIE_DEFAULT_EMAIL
        username, secret = MEALIE_DEFAULT_EMAIL, settings.mealie_admin_password
    else:
        return JSONResponse({"ok": False, "error": "No saved sign-in for that service."})
    if pre_setup:
        mark = first_run_reveal_mark(payload.service, secret)
        shown = [m for m in (settings.first_run_revealed or []) if isinstance(m, str)]
        if mark in shown:
            return JSONResponse({"ok": False, "error": _FIRST_RUN_SHOWN_MSG},
                                status_code=403)
        try:
            settings.save({"first_run_revealed": shown + [mark]})
        except OSError:
            pass  # unwritable data dir must not swallow the one showing
    return {"ok": True, "username": username, "password": secret}


@router.post("/first-run/{service}")
async def first_run_provision(request: Request, service: str,
                              payload: FirstRunPayload | None = None):
    """Set up Grocy or Mealie automatically ("Set up for me").

    Runs the first-run provisioning engine once against the given (or
    configured/suggested) address. Safe on any install: an already-connected
    backend, or one whose sign-in is no longer the factory default, is left
    untouched and reported as such. A satellite's backends live on its main
    server, so this is not offered there.
    """
    if not settings.is_configured() and not pairing_svc.is_local_network_request(request):
        return JSONResponse({"ok": False, "error": _FIRST_RUN_REMOTE_MSG},
                            status_code=403)
    if settings.is_satellite():
        return JSONResponse({"ok": False, "error": "Backends are managed on the main server."})
    from ...services import first_run
    if first_run.provisioning_in_progress():
        return {"ok": False, "configured": False, "retryable": True,
                "steps": [],
                "message": "Setting up is already running. Give it a moment, "
                           "then check again."}
    base = (payload.base_url if payload else "") or ""
    # Same unauthenticated-window guard as the connection tests: this also
    # fetches a caller-supplied address (FoodAssistant-3okp).
    refusal = _refuse_probe_target(base)
    if refusal:
        return JSONResponse({"ok": False, "error": refusal})
    if service == "grocy":
        report = await first_run.provision_grocy(base)
        # The wizard saves its address field on Finish, so tell it which
        # address the inventory is connected on.
        if isinstance(report, dict) and report.get("configured"):
            report = {**report, "base_url": settings.grocy_base_url}
        return report
    if service == "mealie":
        return await first_run.provision_mealie(base)
    return JSONResponse({"ok": False, "error": "Unknown service."}, status_code=404)


_OLLAMA_SERVICE_URL = "http://ollama:11434"


def _ollama_test_url(entered: str) -> str:
    """Where the Ollama connection test should look: the same place scans look
    (dependencies.ollama_base_url). A Pi Hosted appliance runs the app with
    host networking, so the compose service name does not resolve there and
    its Ollama answers on localhost; an empty or default address means
    localhost on that box and the compose service everywhere else. Anything
    else is used as entered."""
    url = (entered or "").strip().rstrip("/")
    if url in ("", _OLLAMA_SERVICE_URL):
        if settings.deployment_mode == "pi_hosted":
            return "http://localhost:11434"
        return _OLLAMA_SERVICE_URL
    return url


def _ollama_tag(name: str) -> str:
    """An Ollama model name with its tag spelled out: "llava" is "llava:latest"."""
    name = (name or "").strip()
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _model_retirement_note(model: str) -> tuple[str, bool]:
    """What to tell someone testing a model the provider shuts down.

    Returns (text, retired). An empty text means the model has no known
    shutdown date; retired is True once that date has passed.
    """
    import datetime as _dt
    rule = model_retirement(model)
    if not rule:
        return "", False
    repl, shutdown = rule
    day = _dt.date.fromisoformat(shutdown)
    when = f"{day:%B} {day.day}, {day.year}"
    if _dt.date.today() >= day:
        return (f"{model} was retired by its provider on {when}. "
                f"Choose {repl} or another current model."), True
    return (f" Its provider retires it on {when}; Pantry Raider moves to "
            f"{repl} that day unless you choose another model first."), False


@router.post("/test/provider")
async def test_provider(payload: TestProviderPayload):
    """Connection test for any LLM provider (Vision and Enrichment sections)."""
    note = ""
    if payload.provider in ("gemini", "openai", "anthropic") and payload.model:
        note, retired = _model_retirement_note(payload.model)
        if retired:
            return {"ok": False, "error": note}
    result = await _test_provider(payload)
    if note and result.get("ok"):
        result = {**result, "message": result.get("message", "") + note}
    return result


async def _test_provider(payload: TestProviderPayload):
    p = payload.provider
    saved_key = getattr(settings, f"{p}_api_key", "")
    key = payload.api_key or saved_key

    if p == "gemini":
        if not key:
            return {"ok": False, "error": "Gemini API key is required."}
        try:
            from google import genai
            model = payload.model or "gemini-3.8-flash"
            async with genai.Client(api_key=key).aio as client:
                await client.models.get(model=model)
            return {"ok": True, "message": f"Connected: model {model} available."}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if p == "ollama":
        entered = payload.base_url or settings.ollama_base_url
        url = _ollama_test_url(entered)
        model = (payload.model or settings.ollama_model or "").strip()
        # Our own default addresses (the compose service, or localhost on a
        # host-networked Pi) are fetched as they are. Any other address came
        # from the caller, so it gets the same guard as the other connection
        # tests: never this device itself or the cloud metadata address.
        own = (entered or "").strip().rstrip("/") in ("", _OLLAMA_SERVICE_URL)
        if not own:
            refusal = _refuse_probe_target(url)
            if refusal:
                return {"ok": False, "error": refusal}
        try:
            client_cm = (httpx.AsyncClient(timeout=6.0) if own
                         else egress.guarded_async_client(allow_private=True, timeout=6.0))
            async with client_cm as client:
                r = await client.get(f"{url}/api/tags")
            if r.status_code == 200:
                models = [m.get("name", "") for m in (r.json().get("models") or [])
                          if isinstance(m, dict)]
                if model and _ollama_tag(model) not in {_ollama_tag(n) for n in models}:
                    return {"ok": False, "error": (
                        f"Connected, but the model {model} is not installed. Pull "
                        f"it with: docker exec foodassistant-ollama ollama pull {model}")}
                model_list = ", ".join(models) if models else "none installed"
                return {"ok": True, "message": f"Connected: models: {model_list}"}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if p == "openai":
        if not key:
            return {"ok": False, "error": "OpenAI API key is required."}
        model = payload.model or "gpt-6-luna"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"https://api.openai.com/v1/models/{model}",
                                     headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                return {"ok": True, "message": f"Connected: model {model} available."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if p == "cloud":
        # Forager: "connected" means the stored instance token is
        # accepted by GET /v1/instance/me. No key or model to test.
        if not settings.cloud_linked():
            return {"ok": False, "error": ("Not connected to Forager yet. "
                                           "Sign in with your Forager "
                                           "account first, then test again.")}
        base = settings.cloud_base_url.rstrip("/")
        try:
            async with _cloud_client() as client:
                r = await client.get(f"{base}/v1/instance/me",
                                     headers=_cloud_headers())
            if r.status_code == 200:
                ent = (r.json() or {}).get("entitlement", {})
                # plan_label ("Complimentary", "Premium", "Trial until ...") is
                # the cloud's human answer; comped and trial accounts are active
                # without a Stripe subscription, so never say "subscription".
                label = (ent.get("plan_label") or "").strip()
                if ent.get("active"):
                    return {"ok": True, "message": (f"Connected: {label} plan active."
                                                    if label else
                                                    "Connected: plan active.")}
                return {"ok": True, "message": ("Connected, but the account has "
                                                "no active plan.")}
            if r.status_code == 401:
                return {"ok": False, "error": ("The cloud no longer accepts this "
                                               "install's link. Unlink and pair "
                                               "again with a new code.")}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, settings.cloud_instance_token)}

    if p == "anthropic":
        if not key:
            return {"ok": False, "error": "Anthropic API key is required."}
        model = payload.model or "claude-opus-4-8"
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=key)
            await client.models.retrieve(model)
            return {"ok": True, "message": f"Connected: model {model} available."}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    return {"ok": False, "error": "Unknown provider."}


@router.post("/test/recipes")
async def test_recipes(payload: TestRecipesPayload):
    """Connection test for the external recipe source."""
    if payload.source == "spoonacular":
        key = payload.api_key or settings.spoonacular_api_key
        if not key:
            return {"ok": False, "error": "Spoonacular API key is required."}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    "https://api.spoonacular.com/recipes/findByIngredients",
                    params={"ingredients": "apple", "number": 1, "apiKey": key})
            if r.status_code == 200:
                quota = r.headers.get("x-api-quota-left", "?")
                return {"ok": True, "message": f"Connected: quota left today: {quota} points."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if payload.source == "themealdb":
        key = payload.api_key or settings.themealdb_api_key or "1"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"https://www.themealdb.com/api/json/v1/{key}/filter.php",
                    params={"i": "chicken"})
            if r.status_code == 200 and (r.json() or {}).get("meals"):
                kind = "public key" if key == "1" else "premium key"
                return {"ok": True, "message": f"Connected: TheMealDB reachable ({kind})."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if payload.source == "off":
        return {"ok": True, "message": "External suggestions disabled."}
    return {"ok": False, "error": "Unknown source."}
