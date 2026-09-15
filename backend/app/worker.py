"""The background-job worker process. A second process running the same
image on a timer, coordinating through Postgres (SKIP LOCKED) -- no
Redis, no broker, no separate infrastructure component, per the standing
instruction to avoid unnecessary infrastructure. Run via:

    python -m app.worker

(the docker-compose `worker` service does exactly this).
"""

from __future__ import annotations

import asyncio
import logging

from app.core.db import SessionLocal
from app.workflow.jobs import run_worker_tick
from app.workflow.notifications import MockNotificationProvider

POLL_INTERVAL_SECONDS = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(message)s")
logger = logging.getLogger("worker")


async def tick() -> None:
    async with SessionLocal() as db:
        summary = await run_worker_tick(db, notification_service=MockNotificationProvider())
    if any(summary.values()):
        logger.info("tick: %s", summary)


async def main() -> None:
    logger.info("starting, polling every %ss", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001 -- one bad tick must never kill the loop
            logger.exception("tick failed, will retry next interval")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
