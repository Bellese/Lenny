"""Job orchestrator — the core $gather workflow.

Fetches patients from the CDR, pushes their data to the measure engine,
evaluates the measure, and stores results.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.db import async_session
from app.models.job import Batch, BatchStatus, Job, JobStatus, MeasureResult
from app.services.fhir_client import (
    BatchQueryStrategy,
    DataRequirementsStrategy,
    _build_auth_headers,
    evaluate_measure,
    get_group_members,
    push_resources,
    trigger_reindex_and_wait,
)
from app.services.measure_engine_reset import (
    load_measure_support_to_engine,
    reset_measure_engine,
)
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)


def _extract_populations(measure_report: dict[str, Any]) -> dict[str, bool]:
    """Parse a MeasureReport and return population boolean flags."""
    populations = {
        "initial_population": False,
        "denominator": False,
        "numerator": False,
        "denominator_exclusion": False,
        "numerator_exclusion": False,
    }
    code_map = {
        "initial-population": "initial_population",
        "denominator": "denominator",
        "numerator": "numerator",
        "denominator-exclusion": "denominator_exclusion",
        "numerator-exclusion": "numerator_exclusion",
    }
    for group in measure_report.get("group", []):
        for pop in group.get("population", []):
            code_coding = pop.get("code", {}).get("coding", [])
            for coding in code_coding:
                code = coding.get("code", "")
                if code in code_map:
                    count = pop.get("count", 0)
                    populations[code_map[code]] = count > 0
    return populations


def _extract_patient_name(patient_resource: dict[str, Any]) -> Optional[str]:
    """Extract a display name from a Patient FHIR resource."""
    for name_obj in patient_resource.get("name", []):
        parts = []
        given = name_obj.get("given", [])
        if given:
            parts.extend(given)
        family = name_obj.get("family")
        if family:
            parts.append(family)
        if parts:
            return " ".join(parts)
    return None


def _error_measure_report(patient_id: str, exc: Exception) -> dict[str, Any]:
    """Build a persisted per-patient error result for failed evaluations."""
    return {
        "resourceType": "OperationOutcome",
        "issue": [
            {
                "severity": "error",
                "code": "processing",
                "diagnostics": sanitize_error(exc),
            }
        ],
        "subject": {"reference": f"Patient/{patient_id}"},
    }


def _patient_data_strategy(measure_id: str):
    """Create the configured patient data acquisition strategy."""
    if settings.PATIENT_DATA_STRATEGY == "data_requirements":
        return DataRequirementsStrategy(measure_id)
    return BatchQueryStrategy()


async def _stop_or_delete_job(job_id: int) -> bool:
    """Return True when work should stop because the job was cancelled or deleted."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job:
            return True
        if job.delete_requested:
            await session.delete(job)
            await session.commit()
            return True
        return job.status == JobStatus.cancelled


async def run_job(job_id: int) -> None:
    """Execute the full $gather workflow for a job."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if not job:
            logger.error("Job not found", extra={"job_id": job_id})
            return
        if job.delete_requested:
            await session.delete(job)
            await session.commit()
            logger.info("Job deleted before start", extra={"job_id": job_id})
            return
        if job.status == JobStatus.cancelled:
            logger.info("Job already cancelled", extra={"job_id": job_id})
            return

        job.status = JobStatus.running
        await session.commit()

    try:
        # Step 1: Reset measure engine + reload only this measure's support resources.
        # Defeats cross-bundle terminology / CodeSystem-stub / library-cache contamination
        # by destroying and recreating the hapi-fhir-measure container.
        async with async_session() as session:
            job_for_measure = await session.get(Job, job_id)
            measure_id_for_reset = job_for_measure.measure_id if job_for_measure else None
        if not measure_id_for_reset:
            raise RuntimeError(f"Job {job_id} has no measure_id")

        logger.info(
            "Resetting measure engine for isolated evaluation",
            extra={"job_id": job_id, "measure_id": measure_id_for_reset},
        )
        reset_timings = await reset_measure_engine()
        if await _stop_or_delete_job(job_id):
            return

        load_timings = await load_measure_support_to_engine(measure_id_for_reset)
        if await _stop_or_delete_job(job_id):
            return

        # Step 2: Resolve CDR connection settings
        auth_headers = await _get_cdr_auth_headers(job_id)
        cdr_url = await _get_cdr_url(job_id)
        if await _stop_or_delete_job(job_id):
            return

        # Step 3: Fetch patients from CDR (optionally filtered by Group)
        async with async_session() as session:
            job_for_group = await session.get(Job, job_id)
            group_id = job_for_group.group_id if job_for_group else None

        if group_id:
            logger.info("Gathering patients from Group", extra={"job_id": job_id, "group_id": group_id})
            patients = await get_group_members(cdr_url, group_id, auth_headers)
        else:
            strategy = BatchQueryStrategy()
            logger.info("Gathering patients from CDR", extra={"job_id": job_id, "cdr_url": cdr_url})
            patients = await strategy.gather_patients(cdr_url, auth_headers)

        if not patients:
            if await _stop_or_delete_job(job_id):
                return
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if job:
                    job.status = JobStatus.complete
                    job.total_patients = 0
                    job.completed_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.info("No patients found, job complete", extra={"job_id": job_id})
            return

        # Step 4: Update total and create batches
        patient_map: dict[str, dict[str, Any]] = {p["id"]: p for p in patients}
        patient_ids = list(patient_map.keys())
        batch_size = settings.BATCH_SIZE

        if await _stop_or_delete_job(job_id):
            return
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if not job:
                return
            job.total_patients = len(patient_ids)
            batches_data: list[Batch] = []
            for i in range(0, len(patient_ids), batch_size):
                chunk = patient_ids[i : i + batch_size]
                batch = Batch(
                    job_id=job_id,
                    batch_number=len(batches_data) + 1,
                    patient_ids=chunk,
                    status=BatchStatus.pending,
                )
                session.add(batch)
                batches_data.append(batch)
            await session.commit()
            batch_ids = [b.id for b in batches_data]

        # Step 5: Process batches with concurrency control
        semaphore = asyncio.Semaphore(settings.MAX_WORKERS)

        async def process_batch(batch_id: int) -> None:
            async with semaphore:
                await _process_single_batch(
                    job_id=job_id,
                    batch_id=batch_id,
                    patient_map=patient_map,
                    cdr_url=cdr_url,
                    auth_headers=auth_headers,
                )

        # Check for cancellation before starting
        if await _stop_or_delete_job(job_id):
            return

        eval_start = time.monotonic()
        await asyncio.gather(*[process_batch(bid) for bid in batch_ids])
        eval_ms = (time.monotonic() - eval_start) * 1000.0
        logger.info(
            "Job per-stage timings",
            extra={
                "job_id": job_id,
                "measure_id": measure_id_for_reset,
                "reset_ms": round(reset_timings.total_ms, 1),
                "bundle_load_ms": round(load_timings.bundle_load_ms, 1),
                "reindex_ms": round(load_timings.reindex_ms, 1),
                "eval_ms": round(eval_ms, 1),
            },
        )

        # Step 6: Finalize job
        if await _stop_or_delete_job(job_id):
            return
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if not job:
                return
            if job.status == JobStatus.cancelled:
                return
            if job.total_patients and job.processed_patients == 0 and job.failed_patients > 0:
                job.status = JobStatus.failed
                job.error_message = f"All {job.failed_patients} patient evaluations failed"
            else:
                job.status = JobStatus.complete
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()

        logger.info("Job finalized", extra={"job_id": job_id})

    except Exception as exc:
        logger.exception("Job failed", extra={"job_id": job_id})
        if await _stop_or_delete_job(job_id):
            return
        async with async_session() as session:
            job = await session.get(Job, job_id)
            if job:
                job.status = JobStatus.failed
                job.error_message = sanitize_error(exc)[:2000]
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()


async def _get_cdr_auth_headers(job_id: int) -> dict[str, str]:
    """Resolve auth headers from the job's stamped CDR credentials.

    Reads cdr_auth_type / cdr_auth_credentials from the Job row (stamped at
    creation time) so the orchestrator is not affected by active CDR changes
    after the job was created.
    """
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job and job.cdr_auth_type:
            return await _build_auth_headers(job.cdr_auth_type, job.cdr_auth_credentials)
    return {}


async def _get_cdr_url(job_id: int) -> str:
    """Resolve the CDR URL for a job."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job:
            return job.cdr_url
    return settings.DEFAULT_CDR_URL


async def _process_single_batch(
    job_id: int,
    batch_id: int,
    patient_map: dict[str, dict[str, Any]],
    cdr_url: str,
    auth_headers: dict[str, str],
) -> None:
    """Process a single batch in two phases.

    Phase 1 — GATHER & PUSH: Fetch each patient's data from the CDR and push
    it to the measure engine.  After all patients are pushed, pause briefly so
    HAPI FHIR's asynchronous search indexes catch up.

    Phase 2 — EVALUATE: Call $evaluate-measure for each patient.  Because all
    patient data is already indexed, CQL evaluation sees the correct resources.
    """
    async with async_session() as session:
        batch = await session.get(Batch, batch_id)
        if not batch:
            return
        patient_ids: list[str] = batch.patient_ids  # type: ignore[assignment]
        batch.status = BatchStatus.running
        await session.commit()

    retry_count = 0

    while retry_count <= settings.MAX_RETRIES:
        try:
            processed = 0
            failed = 0
            if await _stop_or_delete_job(job_id):
                return

            # Read job params once
            async with async_session() as session:
                job = await session.get(Job, job_id)
                if not job:
                    return
                measure_id = job.measure_id
                period_start = job.period_start
                period_end = job.period_end

            strategy = _patient_data_strategy(measure_id)
            logger.info(
                "Using patient data strategy",
                extra={"strategy": settings.PATIENT_DATA_STRATEGY, "job_id": job_id, "batch_id": batch_id},
            )

            # ----------------------------------------------------------
            # Phase 1: Gather all patient data and push to measure engine
            # ----------------------------------------------------------
            for patient_id in patient_ids:
                if await _stop_or_delete_job(job_id):
                    return

                try:
                    resources = await strategy.gather_patient_data(cdr_url, patient_id, auth_headers)
                    if resources:
                        await push_resources(resources)
                    logger.info(
                        f"Pushed {len(resources)} resources for {patient_id[:8]}",
                        extra={"job_id": job_id, "patient_id": patient_id},
                    )
                except Exception as push_exc:
                    logger.warning(
                        "Failed to gather/push patient data",
                        extra={
                            "job_id": job_id,
                            "batch_id": batch_id,
                            "patient_id": patient_id,
                            "error": str(push_exc),
                        },
                    )

            # Wait for HAPI FHIR search indexes to catch up.
            # CQL evaluation relies on FHIR search internally; HAPI
            # indexes transactions asynchronously.
            if settings.HAPI_SYNC_AFTER_UPLOAD:
                logger.info(
                    "All patient data pushed — waiting for HAPI reindex",
                    extra={"job_id": job_id, "batch_id": batch_id},
                )
                try:
                    await asyncio.to_thread(trigger_reindex_and_wait, settings.MEASURE_ENGINE_URL)
                except Exception as exc:
                    logger.warning(
                        "HAPI reindex failed during job — falling back to sleep",
                        extra={"job_id": job_id, "batch_id": batch_id, "error": str(exc)},
                    )
                    await asyncio.sleep(settings.HAPI_INDEX_WAIT_SECONDS)
            else:
                logger.info(
                    "All patient data pushed — waiting for HAPI indexing",
                    extra={"job_id": job_id, "batch_id": batch_id},
                )
                await asyncio.sleep(settings.HAPI_INDEX_WAIT_SECONDS)
            if await _stop_or_delete_job(job_id):
                return

            # ----------------------------------------------------------
            # Phase 2: Evaluate each patient
            # ----------------------------------------------------------
            for patient_id in patient_ids:
                if await _stop_or_delete_job(job_id):
                    return

                try:
                    measure_report = await evaluate_measure(measure_id, patient_id, period_start, period_end)

                    populations = _extract_populations(measure_report)
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))

                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        result = MeasureResult(
                            job_id=job_id,
                            patient_id=patient_id,
                            patient_name=patient_name,
                            measure_report=measure_report,
                            populations=populations,
                        )
                        session.add(result)
                        await session.commit()

                    processed += 1

                except Exception as patient_exc:
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))
                    sanitized_error = sanitize_error(patient_exc)
                    error_report = _error_measure_report(patient_id, patient_exc)
                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        result = MeasureResult(
                            job_id=job_id,
                            patient_id=patient_id,
                            patient_name=patient_name,
                            measure_report=error_report,
                            populations={
                                "initial_population": False,
                                "denominator": False,
                                "numerator": False,
                                "denominator_exclusion": False,
                                "numerator_exclusion": False,
                                "error": True,
                                "error_message": sanitized_error,
                            },
                        )
                        session.add(result)
                        await session.commit()

                    logger.warning(
                        "Failed to evaluate patient",
                        extra={
                            "job_id": job_id,
                            "batch_id": batch_id,
                            "patient_id": patient_id,
                            "error": sanitized_error,
                        },
                    )
                    failed += 1

            # Update batch and job counters
            if await _stop_or_delete_job(job_id):
                return
            async with async_session() as session:
                batch = await session.get(Batch, batch_id)
                if batch:
                    batch.status = BatchStatus.complete
                    batch.completed_at = datetime.now(timezone.utc)
                    await session.commit()

                job = await session.get(Job, job_id)
                if job:
                    job.processed_patients = job.processed_patients + processed
                    job.failed_patients = job.failed_patients + failed
                    await session.commit()

            return  # Success — exit retry loop

        except Exception as batch_exc:
            retry_count += 1
            logger.warning(
                "Batch failed, retrying",
                extra={
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "retry": retry_count,
                    "error": str(batch_exc),
                },
            )
            if retry_count > settings.MAX_RETRIES:
                async with async_session() as session:
                    batch = await session.get(Batch, batch_id)
                    if batch:
                        batch.status = BatchStatus.failed
                        batch.retry_count = retry_count
                        batch.error_message = str(batch_exc)[:2000]
                        batch.completed_at = datetime.now(timezone.utc)
                        await session.commit()

                    job = await session.get(Job, job_id)
                    if job:
                        job.failed_patients = job.failed_patients + len(patient_ids)
                        await session.commit()
                return

            # Exponential backoff before retry
            await asyncio.sleep(2**retry_count)
