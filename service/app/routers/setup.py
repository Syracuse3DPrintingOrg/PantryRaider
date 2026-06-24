import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from ..config import (
    settings, APP_VERSION, THEMES, _DEFAULT_THEME,
    UI_SCALES, _DEFAULT_UI_SCALE,
    DISPLAY_ROTATIONS, _DEFAULT_DISPLAY_ROTATION,
    DEPLOYMENT_MODES, _DEFAULT_DEPLOYMENT_MODE,
)
from ..dependencies import reset_providers
from ..hardware import is_raspberry_pi, board_model
from ..navigation import all_tabs
from ..storage_categories import custom_categories, _normalize_custom, storable
from ..templating import templates

router = APIRouter(prefix="/setup", tags=["setup"])

# Saved values for these are never rendered back into the page. The form
# sends "" to keep the stored value and "__CLEAR__" to erase it.
_SECRET_FIELDS = [
    "gemini_api_key", "openai_api_key", "anthropic_api_key",
    "grocy_api_key", "mealie_api_key",
    "themealdb_api_key", "spoonacular_api_key",
    "auth_password", "api_key", "upstream_api_key", "kiosk_pin",
]
_CLEAR = "__CLEAR__"


# Extra-key rows that were left untouched in the UI come back as this sentinel
# instead of the real (masked) value, so saved keys are never echoed to the
# browser. "__KEEP__:2" means "keep the stored extra key at index 2".
_KEEP_PREFIX = "__KEEP__:"


def _merge_extra_keys(submitted) -> dict | None:
    """Resolve the submitted extra-key map against what is already stored.

    Returns a clean {provider: [keys]} dict, or None when nothing was sent
    (so the caller leaves the stored extras untouched). Placeholders of the
    form "__KEEP__:<index>" are replaced with the matching stored key; blanks
    and duplicates are dropped.
    """
    if not isinstance(submitted, dict):
        return None
    stored = settings.ai_extra_keys if isinstance(settings.ai_extra_keys, dict) else {}
    result: dict = {}
    for provider, rows in submitted.items():
        if not isinstance(rows, list):
            continue
        prev = [k for k in stored.get(provider, []) if isinstance(k, str)]
        clean: list[str] = []
        for row in rows:
            if not isinstance(row, str):
                continue
            val = row.strip()
            if val.startswith(_KEEP_PREFIX):
                try:
                    val = prev[int(val[len(_KEEP_PREFIX):])]
                except (ValueError, IndexError):
                    continue
            if val and val not in clean:
                clean.append(val)
        if clean:
            result[provider] = clean
    return result


def _safe_error(e: Exception | str, *secrets: str) -> str:
    """Error text with any known secrets blanked (URLs can embed API keys)."""
    msg = str(e)
    for s in secrets:
        if s:
            msg = msg.replace(s, "•••")
    return msg


class SetupPayload(BaseModel):
    vision_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    ollama_base_url: str = ""
    ollama_model: str = "llava:7b"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    # Extra API keys per provider, e.g. {"gemini": ["key2", "key3"]}. When
    # absent the stored extras are left untouched (see save handler).
    ai_extra_keys: dict[str, list[str]] | None = None
    barcode_enrichment: str = "llm"
    enrich_provider: str = ""
    enrich_model: str = ""
    grocy_base_url: str = ""
    grocy_api_key: str = ""
    grocy_public_url: str = ""
    mealie_base_url: str = ""
    mealie_api_key: str = ""
    mealie_public_url: str = ""
    recipe_source: str = "themealdb"
    themealdb_api_key: str = ""
    spoonacular_api_key: str = ""
    staple_items: str = ""
    perishable_days: int = 14
    expiring_soon_days: int = 5
    suggest_per_tier: int = 8
    nav_order: str = ""
    nav_hidden: str = ""
    ui_theme: str = _DEFAULT_THEME
    ui_scale: str = _DEFAULT_UI_SCALE
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION
    deployment_mode: str = ""
    remote_server_url: str = ""
    upstream_api_key: str = ""
    kiosk_pin: str = ""
    barcode_llm_fallback: bool = False
    barcode_autocheck_shopping: bool = False
    cook_ai_context: str = ""
    has_streamdeck: bool = False
    streamdeck_key_count: int = 0
    display_touch: bool = False
    auth_required: bool = True
    auth_password: str = ""
    api_key: str = ""
    rclone_remote: str = ""
    rclone_schedule_hours: int = 0


class TestGrocyPayload(BaseModel):
    grocy_base_url: str = ""
    grocy_api_key: str = ""


class TestMealiePayload(BaseModel):
    mealie_base_url: str = ""
    mealie_api_key: str = ""


class TestRemotePayload(BaseModel):
    remote_server_url: str = ""


@router.post("/test/remote")
async def test_remote(payload: TestRemotePayload):
    """Check that a Pi Remote can reach the FoodAssistant server it controls."""
    url = (payload.remote_server_url or settings.remote_server_url).rstrip("/")
    if not url:
        return {"ok": False, "error": "Server URL is required."}
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            r = await client.get(f"{url}/health")
        if r.status_code == 200:
            return {"ok": True, "message": f"Connected: FoodAssistant reachable at {url}"}
        return {"ok": False, "error": f"HTTP {r.status_code} from {url}/health"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class SatelliteSyncPayload(BaseModel):
    remote_server_url: str = ""
    upstream_api_key: str = ""


@router.post("/satellite/sync")
async def satellite_sync(payload: SatelliteSyncPayload):
    """Save the upstream link, then pull backend config + defaults from it.

    Used by the satellite setup flow: enter the main server URL + API key, then
    sync. On success the satellite has Grocy/Mealie/AI config and is usable.
    """
    data = {"deployment_mode": "pi_remote"}
    if payload.remote_server_url:
        data["remote_server_url"] = payload.remote_server_url.rstrip("/")
    if payload.upstream_api_key and payload.upstream_api_key != _CLEAR:
        data["upstream_api_key"] = payload.upstream_api_key
    settings.save(data)

    from ..services.satellite import sync_from_upstream
    result = await run_in_threadpool(sync_from_upstream)
    if result.get("ok"):
        return {
            "ok": True,
            "message": f"Synced {len(result['applied'])} settings and "
                       f"{result['defaults']} expiry defaults from the server.",
        }
    return {"ok": False, "error": result.get("error", "Sync failed.")}


class TestProviderPayload(BaseModel):
    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""   # ollama only


class TestRecipesPayload(BaseModel):
    source: str = "themealdb"
    api_key: str = ""


_LOCAL_GROCY_CANDIDATES = [
    "http://localhost:9383",
    "http://127.0.0.1:9383",
    "http://grocy:80",
]


async def _detect_local_grocy() -> str:
    """Return the first candidate Grocy URL that responds with a 200/401, or ''."""
    async with httpx.AsyncClient(timeout=1.5) as client:
        for url in _LOCAL_GROCY_CANDIDATES:
            try:
                r = await client.get(f"{url}/api/system/info")
                if r.status_code in (200, 401):
                    return url
            except Exception:
                pass
    return ""


def _grocy_url_for_browser(request: Request, detected: str) -> str:
    """Adjust a locally-detected Grocy URL for the browser making the request.

    When Grocy is detected at localhost:9383 but the setup page is opened from
    another machine, localhost resolves to the user's own computer, not the Pi.
    In that case, substitute the hostname the browser used to reach us (same IP,
    port 9383) so the pre-filled URL is immediately usable.
    """
    if not detected:
        return detected
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1", "localhost", ""):
        return detected
    server_host = request.url.hostname or client_host
    return f"http://{server_host}:9383"


def _suggest_mealie_url(request: Request) -> str:
    """Return a suggested Mealie URL for the browser, or '' if not applicable.

    On a Pi appliance Mealie runs (or will run) on the same host at port 9285.
    Returns '' when Mealie is already configured or we are not on a Pi.
    """
    if not is_raspberry_pi():
        return ""
    if settings.mealie_base_url:
        return ""
    server_host = request.url.hostname or ""
    if not server_host or server_host in ("127.0.0.1", "::1", "localhost"):
        return "http://localhost:9285"
    return f"http://{server_host}:9285"


def available_modes() -> dict:
    """Deployment modes offered on this host.

    On a Raspberry Pi we offer the two Pi modes and hide "Server hosted"
    (which targets a general server). Elsewhere only "Server hosted" applies.
    """
    pi = is_raspberry_pi()
    return {k: v for k, v in DEPLOYMENT_MODES.items() if v["pi"] == pi}


@router.get("", response_class=HTMLResponse)
async def setup_page(request: Request):
    suggested_grocy_url = ""
    if not settings.grocy_base_url or settings.grocy_base_url == "http://grocy:80":
        raw = await _detect_local_grocy()
        suggested_grocy_url = _grocy_url_for_browser(request, raw)
    modes = available_modes()
    # Default the picker: keep the saved choice if still valid, else the only
    # mode that fits this host (or the generic default).
    current_mode = settings.deployment_mode
    if current_mode not in modes:
        current_mode = next(iter(modes), _DEFAULT_DEPLOYMENT_MODE)
    return templates.TemplateResponse(request, "setup.html", {
        "request": request,
        "s": settings,
        "configured": settings.is_configured(),
        # booleans only: never the stored secrets themselves
        "has": {f: bool(getattr(settings, f, "")) for f in _SECRET_FIELDS},
        # count of stored extra keys per provider (values never sent to the page)
        "extra_key_counts": {
            p: len([k for k in (settings.ai_extra_keys.get(p, []) if isinstance(settings.ai_extra_keys, dict) else []) if k])
            for p in ("gemini", "openai", "anthropic")
        },
        "tabs": all_tabs(),
        "version": APP_VERSION,
        "custom_categories": custom_categories(),
        "themes": THEMES,
        "ui_scales": UI_SCALES,
        "display_rotations": DISPLAY_ROTATIONS,
        "suggested_grocy_url": suggested_grocy_url,
        "suggested_mealie_url": _suggest_mealie_url(request),
        "deployment_modes": modes,
        "current_mode": current_mode,
        "is_pi": is_raspberry_pi(),
        "board_model": board_model(),
    })


class ModePayload(BaseModel):
    deployment_mode: str = _DEFAULT_DEPLOYMENT_MODE
    remote_server_url: str = ""


@router.post("/mode")
async def save_mode(payload: ModePayload):
    """Persist the deployment mode chosen on wizard step 1.

    Saved on its own (before the rest of setup) so the wizard can branch and,
    on a Pi, the provisioner can read the choice to decide what to install.
    """
    mode = payload.deployment_mode
    if mode not in DEPLOYMENT_MODES:
        return JSONResponse({"ok": False, "error": "Unknown deployment mode."})
    data = {"deployment_mode": mode}
    if mode == "pi_remote":
        data["remote_server_url"] = payload.remote_server_url.rstrip("/")
    settings.save(data)
    return {"ok": True, "mode": mode}


class ThemePayload(BaseModel):
    ui_theme: str = _DEFAULT_THEME


@router.post("/theme")
async def save_theme(payload: ThemePayload):
    settings.save({"ui_theme": payload.ui_theme})
    return {"ok": True}


class ScalePayload(BaseModel):
    ui_scale: str = _DEFAULT_UI_SCALE
    display_rotation: int = _DEFAULT_DISPLAY_ROTATION


@router.post("/scale")
async def save_scale(payload: ScalePayload):
    scale = payload.ui_scale if payload.ui_scale in UI_SCALES else _DEFAULT_UI_SCALE
    rot = payload.display_rotation if payload.display_rotation in DISPLAY_ROTATIONS else _DEFAULT_DISPLAY_ROTATION
    settings.save({"ui_scale": scale, "display_rotation": rot})
    return {"ok": True}


@router.post("/save")
async def save_setup(payload: SetupPayload):
    data = payload.model_dump(exclude_unset=True)
    for f in _SECRET_FIELDS:
        if data.get(f) == "":
            data.pop(f, None)        # blank = keep existing value
        elif data.get(f) == _CLEAR:
            data[f] = ""             # explicit clear
    data["ai_extra_keys"] = _merge_extra_keys(data.get("ai_extra_keys"))
    if data["ai_extra_keys"] is None:
        data.pop("ai_extra_keys", None)   # absent = keep stored extras
    if data.get("display_rotation") not in DISPLAY_ROTATIONS:
        data["display_rotation"] = _DEFAULT_DISPLAY_ROTATION
    # Drop an unknown deployment mode rather than persisting a broken value;
    # an empty/absent mode leaves the existing choice untouched.
    if data.get("deployment_mode") and data["deployment_mode"] not in DEPLOYMENT_MODES:
        data.pop("deployment_mode", None)
    if data.get("remote_server_url"):
        data["remote_server_url"] = data["remote_server_url"].rstrip("/")
    settings.save(data)
    reset_providers()   # apply new provider/model/key without a restart
    from ..services.mealie import reset_cache as reset_mealie_cache, reset_staple_cache
    reset_mealie_cache()
    reset_staple_cache()
    return {"ok": True}


class StorageCategoriesPayload(BaseModel):
    categories: list[dict] = []


@router.post("/storage-categories")
async def save_storage_categories(payload: StorageCategoriesPayload):
    """Replace the set of user-defined storage categories.

    Entries are normalized/validated (blank, keyless, or built-in-colliding
    rows are dropped) before saving, so the inventory dashboard never sees a
    malformed category.
    """
    clean = [storable(c) for c in _normalize_custom(payload.categories)]
    settings.save({"custom_storage_categories": clean})
    return {"ok": True, "categories": clean}


@router.post("/test/grocy")
async def test_grocy(payload: TestGrocyPayload):
    url = (payload.grocy_base_url or settings.grocy_base_url).rstrip("/")
    key = payload.grocy_api_key or settings.grocy_api_key
    if not url or not key:
        return JSONResponse({"ok": False, "error": "URL and API key are both required."})
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{url}/api/system/info",
                                 headers={"GROCY-API-KEY": key})
        if r.status_code == 200:
            version = r.json().get("grocy_version", "?")
            return {"ok": True, "message": f"Connected: Grocy {version}"}
        return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
    except Exception as e:
        return {"ok": False, "error": _safe_error(e, key)}


@router.post("/test/mealie")
async def test_mealie(payload: TestMealiePayload):
    url = (payload.mealie_base_url or settings.mealie_base_url).rstrip("/")
    key = payload.mealie_api_key or settings.mealie_api_key
    if not url or not key:
        return JSONResponse({"ok": False, "error": "URL and API token are both required."})
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(f"{url}/api/users/self",
                                 headers={"Authorization": f"Bearer {key}"})
        if r.status_code == 200:
            user = r.json().get("username") or r.json().get("email", "?")
            return {"ok": True, "message": f"Connected: authenticated as {user}"}
        return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
    except Exception as e:
        return {"ok": False, "error": _safe_error(e, key)}


@router.post("/test/provider")
async def test_provider(payload: TestProviderPayload):
    """Connection test for any LLM provider (Vision and Enrichment sections)."""
    p = payload.provider
    saved_key = getattr(settings, f"{p}_api_key", "")
    key = payload.api_key or saved_key

    if p == "gemini":
        if not key:
            return {"ok": False, "error": "Gemini API key is required."}
        try:
            import google.generativeai as genai
            genai.configure(api_key=key)
            model = payload.model or "gemini-2.5-flash"
            genai.get_model(f"models/{model}")
            return {"ok": True, "message": f"Connected: model {model} available."}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

    if p == "ollama":
        url = (payload.base_url or settings.ollama_base_url or "http://localhost:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{url}/api/tags")
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                model_list = ", ".join(models) if models else "none installed"
                return {"ok": True, "message": f"Connected: models: {model_list}"}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if p == "openai":
        if not key:
            return {"ok": False, "error": "OpenAI API key is required."}
        model = payload.model or "gpt-4o-mini"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(f"https://api.openai.com/v1/models/{model}",
                                     headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                return {"ok": True, "message": f"Connected: model {model} available."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {_safe_error(r.text[:200], key)}"}
        except Exception as e:
            return {"ok": False, "error": _safe_error(e, key)}

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


# TOTP 2FA setup endpoints

class TOTPVerifyPayload(BaseModel):
    secret: str
    code: str


@router.post("/totp/generate")
async def totp_generate():
    """Generate a new TOTP secret and return the provisioning URI for QR display."""
    import pyotp, qrcode, base64, io
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name="FoodAssistant", issuer_name="FoodAssistant")
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"secret": secret, "qr": f"data:image/png;base64,{b64}", "uri": uri}


@router.post("/totp/verify")
async def totp_verify(payload: TOTPVerifyPayload):
    """Confirm the user's authenticator app is in sync before enabling TOTP."""
    import pyotp
    try:
        totp = pyotp.TOTP(payload.secret)
        ok = totp.verify(payload.code.strip(), valid_window=1)
    except Exception:
        ok = False
    if ok:
        settings.save({"totp_secret": payload.secret})
        return {"ok": True, "message": "Two-factor authentication enabled."}
    return {"ok": False, "error": "Code did not match. Check your authenticator app clock."}


@router.post("/totp/disable")
async def totp_disable():
    """Remove the stored TOTP secret, disabling 2FA."""
    settings.save({"totp_secret": ""})
    return {"ok": True, "message": "Two-factor authentication disabled."}


# Pi host bridge endpoints
# -------------------------
# These call a small helper service running on 127.0.0.1:9299 on the Pi host.
# Because docker-compose.appliance.yml uses network_mode: host, localhost in the
# container is the same as localhost on the host, so no special networking is needed.
# On non-Pi or non-appliance installs the endpoints return a clear error.

_HOST_BRIDGE = "http://127.0.0.1:9299"


@router.get("/network/status")
async def network_status():
    """Current Wi-Fi SSID and hostname, via the Pi host bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            wifi = (await c.get(f"{_HOST_BRIDGE}/wifi/status")).json()
            hn = (await c.get(f"{_HOST_BRIDGE}/hostname")).json()
        return {
            "ok": True,
            "ssid": wifi.get("ssid", ""),
            "wifi_state": wifi.get("state", ""),
            "wifi_detail": wifi.get("detail", ""),
            "hostname": hn.get("hostname", ""),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/network/scan")
async def network_scan():
    """List visible Wi-Fi networks, via the Pi host bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/wifi/scan")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


class WifiPayload(BaseModel):
    ssid: str = ""
    password: str = ""


@router.post("/network/wifi")
async def network_wifi(payload: WifiPayload):
    """Connect to a Wi-Fi network (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if not payload.ssid.strip():
        return JSONResponse({"ok": False, "error": "SSID is required."})
    try:
        async with httpx.AsyncClient(timeout=35.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/wifi/connect",
                             json={"ssid": payload.ssid.strip(), "password": payload.password})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


class HostnamePayload(BaseModel):
    hostname: str = ""


@router.post("/network/hostname")
async def network_hostname(payload: HostnamePayload):
    """Change the device hostname (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    name = payload.hostname.strip().lower()
    if not name:
        return JSONResponse({"ok": False, "error": "Hostname is required."})
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/hostname", json={"hostname": name})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/display/rotation")
async def display_rotation_status():
    """Current KMS framebuffer rotation (Pi appliance only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=4.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/display/rotation")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


class KmsRotationPayload(BaseModel):
    degrees: int = 0
    reboot: bool = False


@router.post("/display/rotation")
async def set_display_rotation(payload: KmsRotationPayload):
    """Set the KMS framebuffer rotation (Pi appliance only). Takes effect after reboot."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    if payload.degrees not in (0, 90, 180, 270):
        return JSONResponse({"ok": False, "error": "degrees must be 0, 90, 180, or 270."})
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/display/rotation",
                             json={"degrees": payload.degrees, "reboot": payload.reboot})
        settings.save({"display_rotation": payload.degrees})
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/streamdeck/restart")
async def streamdeck_restart():
    """Restart the Stream Deck systemd service (Pi appliance only)."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/streamdeck/restart")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/kiosk/restart")
async def kiosk_restart():
    """Restart the kiosk browser so display scale/rotation changes apply."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=35.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/kiosk/restart")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/mealie/start")
async def mealie_start():
    """Kick off the Mealie container start on a Pi appliance via the host bridge.

    The bridge runs the image pull/up in the background and returns at once, so
    a short timeout is enough; the web UI polls /mealie/status for progress.
    """
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/mealie/start")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/mealie/status")
async def mealie_status():
    """Mealie start progress (not-installed / starting / running), via the bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/mealie/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


_LOG_NAMES = {"mealie", "kiosk", "streamdeck"}


@router.get("/logs/{name}")
async def install_logs(name: str):
    """Tail of an install/start log (mealie / kiosk / streamdeck), via the bridge.

    Mirrors the /mealie/status proxy: the setup UI polls this while a start or
    install is in flight to show live output. Returns {ok, name, running,
    lines}. Unknown names and bridge errors are reported, never raised.
    """
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    if name not in _LOG_NAMES:
        return {"ok": False, "error": "unknown log name"}
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/logs/{name}")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/hardware/status")
async def hardware_status():
    """Display / Stream Deck presence and service state, via the Pi host bridge."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/hardware/status")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/kiosk/install")
async def kiosk_install():
    """Provision the kiosk service for a display attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/kiosk/install")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/streamdeck/config")
async def streamdeck_config_get():
    """Proxy GET config from host bridge."""
    if _HOST_BRIDGE:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.get(f"{_HOST_BRIDGE}/streamdeck/config")
                return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return JSONResponse(status_code=500, content={"ok": False, "error": "bridge unavailable"})


@router.post("/streamdeck/config")
async def streamdeck_config_set(request: Request):
    """Proxy POST config to host bridge."""
    if _HOST_BRIDGE:
        try:
            payload = await request.json()
            async with httpx.AsyncClient(timeout=3.0) as c:
                r = await c.post(f"{_HOST_BRIDGE}/streamdeck/config", json=payload)
                return JSONResponse(status_code=r.status_code, content=r.json())
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
    return JSONResponse(status_code=500, content={"ok": False, "error": "bridge unavailable"})


@router.get("/streamdeck/actions")
async def streamdeck_actions():
    """List assignable Stream Deck actions for the grid editor (Pi only)."""
    if not is_raspberry_pi():
        return {"ok": False, "error": "Not available on this platform."}
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = (await c.get(f"{_HOST_BRIDGE}/streamdeck/actions")).json()
        return r
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/streamdeck/install")
async def streamdeck_install():
    """Provision the Stream Deck service for a deck attached after first install."""
    if not is_raspberry_pi():
        return JSONResponse({"ok": False, "error": "Not available on this platform."})
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{_HOST_BRIDGE}/streamdeck/install")
        return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# Backwards-compatible alias for the old endpoint name
@router.post("/test/vision")
async def test_vision_legacy(payload: dict):
    provider = payload.get("vision_provider") or payload.get("provider", "")
    key_field = f"{provider}_api_key"
    return await test_provider(TestProviderPayload(
        provider=provider,
        api_key=payload.get(key_field, payload.get("api_key", "")),
        model=payload.get(f"{provider}_model", payload.get("model", "")),
        base_url=payload.get("ollama_base_url", payload.get("base_url", "")),
    ))
