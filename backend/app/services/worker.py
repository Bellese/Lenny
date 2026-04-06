"""Async background worker that polls PostgreSQL for queued jobs and validation tasks.

Runs as a background task within the FastAPI lifespan — no Celery needed.
Priority order: production Jobs first, then BundleUploads, then ValidationRuns.
"""

import asyncio
import logging

from sqlalchemy import select, update

from app.db import async_session
from app.models.job import Job, JobStatus
from app.models.validation import BundleUpload, ValidationRun, ValidationStatus
from app.services.orchestrator import run_job
from app.services.validation import process_bundle_upload, run_validation

logger = logging.getLogger(__name__)

# Global event used to signal the worker to stop
_shutdown_event = asyncio.Event()


async def worker_loop() -> None:
    """Poll for queued work and process sequentially.

    Uses SELECT ... FOR UPDATE SKIP LOCKED to safely pick up work.
    Priority: Jobs > BundleUploads > ValidationRuns.
    """
    logger.info("Worker loop started")

    while not _shutdown_event.is_set():
        found_work = False
        try:
            # --- Priority 1: Production Jobs ---
            job_id: int | None = None
            async with async_session() as session:
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
                found_work = True
                logger.info("Picked up job", extra={"job_id": job_id})
                try:
                    await run_job(job_id)
                except Exception:
                    logger.exception("Unhandled error in run_job", extra={"job_id": job_id})
                    async with async_session() as session:
                        job = await session.get(Job, job_id)
                        if job and job.status == JobStatus.running:
                            job.status = JobStatus.failed
                            job.error_message = "Unexpected worker error"
                            await session.commit()

            # --- Priority 2: Bundle Uploads ---
            if not found_work:
                upload_id: int | None = None
                async with async_session() as session:
                    result = await session.execute(
                        select(BundleUpload.id)
                        .where(BundleUpload.status == ValidationStatus.queued)
                        .order_by(BundleUpload.created_at.asc())
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                    row = result.scalar_one_or_none()
                    if row is not None:
                        upload_id = row
                        await session.execute(
                            update(BundleUpload)
                            .where(BundleUpload.id == upload_id)
                            .values(status=ValidationStatus.running)
                        )
                        await session.commit()

                if upload_id is not None:
                    found_work = True
                    logger.info("Picked up bundle upload", extra={"upload_id": upload_id})
                    try:
                        await process_bundle_upload(upload_id)
                    except Exception:
                        logger.exception(
                            "Unhandled error in process_bundle_upload",
                            extra={"upload_id": upload_id},
                        )
                        async with async_session() as session:
                            upload = await session.get(BundleUpload, upload_id)
                            if upload and upload.status == ValidationStatus.running:
                                upload.status = ValidationStatus.failed
                                upload.error_message = "Unexpected worker error"
                                await session.commit()

            # --- Priority 3: Validation Runs ---
            if not found_work:
                run_id: int | None = None
                async with async_session() as session:
                    result = await session.execute(
                        select(ValidationRun.id)
                        .where(ValidationRun.status == ValidationStatus.queued)
                        .order_by(ValidationRun.created_at.asc())
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                    row = result.scalar_one_or_none()
                    if row is not None:
                        run_id = row
                        await session.execute(
                            update(ValidationRun)
                            .where(ValidationRun.id == run_id)
                            .values(status=ValidationStatus.running)
                        )
                        await session.commit()

                if run_id is not None:
                    found_work = True
                    logger.info("Picked up validation run", extra={"run_id": run_id})
                    try:
                        await run_validation(run_id)
                    except Exception:
                        logger.exception(
                            "Unhandled error in run_validation",
                            extra={"run_id": run_id},
                        )
                        async with async_session() as session:
                            vrun = await session.get(ValidationRun, run_id)
                            if vrun and vrun.status == ValidationStatus.running:
                                vrun.status = ValidationStatus.failed
                                vrun.error_message = "Unexpected worker error"
                                await session.commit()

            # No work found — sleep before polling again
            if not found_work:
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
