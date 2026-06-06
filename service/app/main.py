from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from .database import engine, get_db, Base
from .models import db_models  # noqa: F401 — registers models with Base
from .services.defaults import seed_defaults
from .routers import analyze, defaults, inventory, expiring


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        seed_defaults(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="FoodAssistant",
    description="Food spoilage tracker with LLM-powered photo import and Grocy integration",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(analyze.router)
app.include_router(defaults.router)
app.include_router(inventory.router)
app.include_router(expiring.router)


@app.get("/health")
async def health():
    from .dependencies import get_vision_provider
    from .services.grocy import GrocyClient
    provider = get_vision_provider()
    grocy = GrocyClient()
    return {
        "status": "ok",
        "vision_provider": "ok" if await provider.health_check() else "error",
        "grocy": "ok" if await grocy.health_check() else "error",
    }
