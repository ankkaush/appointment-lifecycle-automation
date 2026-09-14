from fastapi import FastAPI
from sqlalchemy import text

from app.api.routes import router as domain_router
from app.core.db import engine

app = FastAPI(title="Appointment Lifecycle Automation", version="0.1.0")
app.include_router(domain_router, prefix="/v1", tags=["domain"])


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe that also proves the database connection is real,
    not just that the process is up."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}
