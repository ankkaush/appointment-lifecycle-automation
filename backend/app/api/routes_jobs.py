"""Manual admin trigger for background jobs -- runs the same
run_worker_tick the worker process calls on a timer, on demand. Useful
for demoing/testing the full lifecycle without waiting for the poll
interval; not a replacement for app/worker.py's loop.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.workflow.jobs import run_worker_tick
from app.workflow.notification_dependency import get_notification_service
from app.workflow.notifications import NotificationService

router = APIRouter()


@router.post("/jobs/run-tick")
async def run_tick(
    db: AsyncSession = Depends(get_db),
    notification_service: NotificationService = Depends(get_notification_service),
) -> dict[str, int]:
    return await run_worker_tick(db, notification_service=notification_service)
