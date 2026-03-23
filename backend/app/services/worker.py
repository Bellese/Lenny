"""Async background worker that polls PostgreSQL for queued jobs.

Runs as a background task within the FastAPI lifespan — no Celery needed.
"""

import asyncio
import logging

from sqlalchemy import select, update

from app.db import async_session
from app.models.job import Job, JobStatus
from app.services.orchestrator import run_job

logger = logging.getLogger(__name__)

# Global event used to signal the worker to stop
_shutdown_event = asyncio.Event()


async def worker_loop() -> None:
    """Poll for queued jobs and process them sequentially.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to safely pick up jobs
    even if multiple workers were running (future-proof).
    """
    logger.info("Worker loop started")

    while not _shutdown_event.is_set():
        job_id: int | None = None
        try:
            async with async_session() as session:
                # Atomically claim the oldest queued job
                result = await session.execute(
                    select(Job.id)
                    .where(Job.status == JobStatus.queued)
                    .order_by(Job.created_at.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    job_id = row
                    await session.execute(
                        update(Job)
                        .where(Job.id == job_id)
                        .values(status=JobStatus.running)
                    )
                    await session.commit()

            if job_id is not None:
                logger.info("Picked up job", extra={"job_id": job_id})
                try:
                    await run_job(job_id)
                except Exception:
                    logger.exception("Unhandled error in run_job", extra={"job_id": job_id})
                    # Mark failed if not already
                    async with async_session() as session:
                        job = await session.get(Job, job_id)
                        if job and job.status == JobStatus.running:
                            job.status = JobStatus.failed
                            job.error_message = "Unexpected worker error"
                            await session.commit()
            else:
                # No work available — sleep before polling again
                try:
                    await asyncio.wait_for(_shutdown_event.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        except Exception:
            logger.exception("Worker loop error — will retry in 5s")
            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    logger.info("Worker loop stopped")


def request_shutdown() -> None:
    """Signal the worker loop to stop gracefully."""
    _shutdown_event.set()
