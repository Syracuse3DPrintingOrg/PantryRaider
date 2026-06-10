import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..config import settings
from ..dependencies import reset_providers

router = APIRouter(prefix="/setup", tags=["setup"])
templates = Jinja2Templates(directory="app/templates")


class SetupPayload(BaseModel):
    vision_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-flash"
    ollama_base_url: str = ""
    ollama_model: str = "llava:7b"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    barcode_enrichment: str = "llm"
    enrich_provider: str = ""
    enrich_model: str = ""
    grocy_base_url: str = ""
    grocy_api_key: str = ""
    mealie_base_url: str = ""
    mealie_api_key: str = ""
    auth_password: str = ""
    api_key: str = ""


class TestGrocyPayload(BaseModel):
    grocy_base_url: str
    grocy_api_key: str


class TestMealiePayload(BaseModel):
    mealie_base_url: str
    mealie_api_key: str


class TestProviderPayload(BaseModel):
    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""   # ollama only


@router.get("", response_class=HTMLResponse)
async def setup_page(request: Request):
    return templates.TemplateResponse("setup.html", {
        "request": request,
        "s": settings,
        "configured": settings.is_configured(),
    })


@router.post("/save")
async def save_setup(payload: SetupPayload):
    settings.save(payload.model_dump())
    reset_providers()   # apply new provider/model/key without a restart
    from ..services.mealie import reset_cache as reset_mealie_cache
    reset_mealie_cache()
    return {"ok": True}


@router.post("/test/grocy")
async def test_grocy(payload: TestGrocyPayload):
    url = payload.grocy_base_url.rstrip("/")
    if not url or not payload.grocy_api_key:
        return JSONResponse({"ok": False, "error": "URL and API key are both required."})
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(
                f"{url}/api/system/info",
                headers={"GROCY-API-KEY": payload.grocy_api_key},
            )
        if r.status_code == 200:
            version = r.json().get("grocy_version", "?")
            return {"ok": True, "message": f"Connected — Grocy {version}"}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/test/mealie")
async def test_mealie(payload: TestMealiePayload):
    url = payload.mealie_base_url.rstrip("/")
    if not url or not payload.mealie_api_key:
        return JSONResponse({"ok": False, "error": "URL and API token are both required."})
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(
                f"{url}/api/users/self",
                headers={"Authorization": f"Bearer {payload.mealie_api_key}"},
            )
        if r.status_code == 200:
            user = r.json().get("username") or r.json().get("email", "?")
            return {"ok": True, "message": f"Connected — authenticated as {user}"}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/test/provider")
async def test_provider(payload: TestProviderPayload):
    """Connection test for any LLM provider (used by both Vision and Enrichment sections)."""
    p = payload.provider

    if p == "gemini":
        if not payload.api_key:
            return {"ok": False, "error": "Gemini API key is required."}
        try:
            import google.generativeai as genai
            genai.configure(api_key=payload.api_key)
            model = payload.model or "gemini-1.5-flash"
            genai.get_model(f"models/{model}")
            return {"ok": True, "message": f"Connected — model {model} available."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if p == "ollama":
        url = (payload.base_url or "http://localhost:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(f"{url}/api/tags")
            if r.status_code == 200:
                models = [m["name"] for m in r.json().get("models", [])]
                model_list = ", ".join(models) if models else "none installed"
                return {"ok": True, "message": f"Connected — models: {model_list}"}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if p == "openai":
        if not payload.api_key:
            return {"ok": False, "error": "OpenAI API key is required."}
        model = payload.model or "gpt-4o-mini"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.get(
                    f"https://api.openai.com/v1/models/{model}",
                    headers={"Authorization": f"Bearer {payload.api_key}"},
                )
            if r.status_code == 200:
                return {"ok": True, "message": f"Connected — model {model} available."}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if p == "anthropic":
        if not payload.api_key:
            return {"ok": False, "error": "Anthropic API key is required."}
        model = payload.model or "claude-opus-4-8"
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=payload.api_key)
            await client.models.retrieve(model)
            return {"ok": True, "message": f"Connected — model {model} available."}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {"ok": False, "error": "Unknown provider."}


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
