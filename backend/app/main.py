from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.routes import router as domain_router
from app.api.routes_ai import router as ai_router
from app.api.routes_jobs import router as jobs_router
from app.api.routes_workflow import router as workflow_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.startup_checks import validate_production_settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_production_settings(get_settings())
    yield


app = FastAPI(title="Appointment Lifecycle Automation", version="0.1.0", lifespan=lifespan)

# The chat UI (Phase 4) is same-origin -- mounted directly below, no CORS
# needed. The Phase 9 dashboard is a separate Next.js app on its own
# origin/port, the first cross-origin browser client this API has had.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().dashboard_origin],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(domain_router, prefix="/v1", tags=["domain"])
app.include_router(ai_router, prefix="/v1", tags=["ai"])
app.include_router(workflow_router, prefix="/v1", tags=["workflow"])
app.include_router(jobs_router, prefix="/v1", tags=["jobs"])

# The thin customer-facing chat channel (Phase 4). Static HTML/CSS/JS --
# no framework, no build step -- calling the same /v1/requests, /reply,
# and /confirm endpoints any other channel adapter would. Visit
# /chat/?business_id=<uuid>.
app.mount("/chat", StaticFiles(directory="static/chat", html=True), name="chat")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe that also proves the database connection is real,
    not just that the process is up."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}
