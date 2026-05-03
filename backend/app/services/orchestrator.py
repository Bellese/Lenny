"""Job orchestrator — the core $gather workflow.

Fetches patients from the CDR, pushes their data to the measure engine,
evaluates the measure, and stores results.
"""

import asyncio
import copy
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.config import settings
from app.db import async_session
from app.models.config import CDRConfig
from app.models.job import Batch, BatchStatus, Job, JobStatus, MeasureResult
from app.services.fhir_client import (
    BatchQueryStrategy,
    DataRequirementsStrategy,
    FhirOperationError,
    _build_auth_headers,
    evaluate_measure,
    get_group_members,
    push_resources,
    trigger_reindex_and_wait_for_patients,
    wipe_patient_data,
)
from app.services.fhir_errors import redact_outcome, sanitize_url
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)

LENNY_ERROR_EXT = "https://lenny.bellese.io/fhir/StructureDefinition/synthesized-error"


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


def _error_measure_report(
    patient_id: str,
    exc: Exception,
    upstream_outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a persisted per-patient error result for failed evaluations.

    When upstream_outcome is an OperationOutcome from the MCS, embed it directly
    (sanitized) with the synthesized error string attached as a FHIR Extension.
    Deep-copies to prevent cross-patient mutation when two patients share an OO.
    """
    if upstream_outcome and upstream_outcome.get("resourceType") == "OperationOutcome":
        oo = copy.deepcopy(redact_outcome(upstream_outcome))
        oo["subject"] = {"reference": f"Patient/{patient_id}"}
        oo.setdefault("extension", []).append({"url": LENNY_ERROR_EXT, "valueString": sanitize_error(exc)})
        return oo
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
        # Step 1: Wipe patient data from measure engine (cleanup from prior job)
        logger.info("Wiping prior patient data from measure engine", extra={"job_id": job_id})
        await wipe_patient_data(strict=False)
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
            logger.info("Gathering patients from CDR", extra={"job_id": job_id, "cdr_url": sanitize_url(cdr_url)})
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

        await asyncio.gather(*[process_batch(bid) for bid in batch_ids])

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
                job.error_message = str(exc)[:2000]
                job.completed_at = datetime.now(timezone.utc)
                await session.commit()


async def _get_cdr_auth_headers(job_id: int) -> dict[str, str]:
    """Resolve auth headers by reading live credentials from the referenced CDR config."""
    async with async_session() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return {}
        if job.cdr_id is None:
            # No CDR config linked — either job was created without one (unauthenticated
            # direct URL) or the config was deleted after creation.  If auth type is
            # "none"/unset no credentials are needed; for auth-bearing types the
            # credentials are unrecoverable.
            if not job.cdr_auth_type or job.cdr_auth_type == "none":
                return {}
            raise RuntimeError(
                f"Job {job_id} has no cdr_id — CDR config was deleted after job creation. "
                "Cannot fetch auth credentials."
            )
        cfg = await session.get(CDRConfig, job.cdr_id)
        if cfg is None:
            raise RuntimeError(f"CDR config {job.cdr_id} referenced by job {job_id} no longer exists.")
        return await _build_auth_headers(cfg.auth_type, cfg.auth_credentials)


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
            # Track patients that FULLY failed gather so they are skipped in evaluate.
            # Partial-gather patients proceed to evaluate with available data (AT-2).
            # Per-patient exceptions MUST stay swallowed here — letting them escape
            # would trigger the outer batch-retry handler and re-push healthy patients.
            gather_failed_patients: set[str] = set()
            # Partial-gather: some resource types failed but data was pushed.
            # Mapped to error_details dict for annotation after evaluate succeeds.
            partial_gather_patients: dict[str, dict] = {}
            # Track patients with Encounters so the reindex probe uses them
            # (not a phantom pre-baked patient from the measure engine image).
            patients_with_encounters: list[str] = []

            for patient_id in patient_ids:
                if await _stop_or_delete_job(job_id):
                    return

                try:
                    gather_result = await strategy.gather_patient_data(cdr_url, patient_id, auth_headers)
                    if gather_result.resources:
                        await push_resources(gather_result.resources)
                        if any(r.get("resourceType") == "Encounter" for r in gather_result.resources):
                            patients_with_encounters.append(patient_id)
                    logger.info(
                        f"Pushed {len(gather_result.resources)} resources for {patient_id[:8]}",
                        extra={"job_id": job_id, "patient_id": patient_id},
                    )

                    if gather_result.has_partial_failure:
                        # Partial gather — continue to evaluate with available data (AT-2).
                        # Record which types failed so we can annotate the result after evaluate.
                        failed_type_names = [f.resource_type for f in gather_result.failed_types]
                        succeeded_type_names = sorted(
                            {r.get("resourceType") for r in gather_result.resources if r.get("resourceType")}
                        )
                        partial_gather_patients[patient_id] = {
                            "operation": "gather",
                            "failed_types": failed_type_names,
                            "succeeded_types": succeeded_type_names,
                        }
                        logger.warning(
                            "Partial CDR gather — continuing evaluation with available data",
                            extra={
                                "job_id": job_id,
                                "patient_id": patient_id,
                                "failed_types": failed_type_names,
                            },
                        )

                except Exception as push_exc:
                    gather_failed_patients.add(patient_id)
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))
                    sanitized_msg = sanitize_error(push_exc)
                    error_details: dict[str, Any] = {"operation": "gather", "error": sanitized_msg}
                    if isinstance(push_exc, FhirOperationError):
                        error_details["url"] = push_exc.url
                        error_details["status_code"] = push_exc.status_code
                        error_details["latency_ms"] = push_exc.latency_ms
                        if push_exc.outcome:
                            error_details["raw_outcome"] = redact_outcome(push_exc.outcome.raw)
                    error_report = _error_measure_report(
                        patient_id,
                        push_exc,
                        push_exc.outcome.raw if isinstance(push_exc, FhirOperationError) and push_exc.outcome else None,
                    )
                    logger.warning(
                        "Failed to gather/push patient data",
                        extra={
                            "job_id": job_id,
                            "batch_id": batch_id,
                            "patient_id": patient_id,
                            "error": sanitized_msg,
                        },
                    )
                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        existing_row = (
                            await session.execute(
                                select(MeasureResult).where(
                                    MeasureResult.job_id == job_id,
                                    MeasureResult.patient_id == patient_id,
                                )
                            )
                        ).scalar_one_or_none()
                        if existing_row:
                            existing_row.measure_report = error_report
                            existing_row.populations = {
                                "initial_population": False,
                                "denominator": False,
                                "numerator": False,
                                "denominator_exclusion": False,
                                "numerator_exclusion": False,
                                "error": True,
                                "error_message": sanitized_msg,
                                "error_phase": "gather",
                            }
                            existing_row.error_details = error_details
                            existing_row.error_phase = "gather"
                        else:
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
                                    "error_message": sanitized_msg,
                                    "error_phase": "gather",
                                },
                                error_details=error_details,
                                error_phase="gather",
                            )
                            session.add(result)
                        await session.commit()
                    failed += 1

            # Wait for HAPI FHIR search indexes to catch up.
            # CQL evaluation relies on FHIR search internally; HAPI
            # indexes transactions asynchronously.
            if settings.HAPI_SYNC_AFTER_UPLOAD:
                logger.info(
                    "All patient data pushed — waiting for HAPI reindex",
                    extra={"job_id": job_id, "batch_id": batch_id},
                )
                if patients_with_encounters:
                    try:
                        await asyncio.to_thread(
                            trigger_reindex_and_wait_for_patients,
                            settings.MEASURE_ENGINE_URL,
                            patients_with_encounters,
                        )
                    except Exception as exc:
                        logger.warning(
                            "HAPI reindex failed during job — falling back to sleep",
                            extra={"job_id": job_id, "batch_id": batch_id, "error": str(exc)},
                        )
                        await asyncio.sleep(settings.HAPI_INDEX_WAIT_SECONDS)
                else:
                    # No Encounter-bearing patients in this batch; can't use the
                    # Encounter-probe strategy, so fall back to a timed sleep.
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
                if patient_id in gather_failed_patients:
                    continue  # Already persisted error row in Phase 1

                if await _stop_or_delete_job(job_id):
                    return

                try:
                    measure_report = await evaluate_measure(measure_id, patient_id, period_start, period_end)

                    populations = _extract_populations(measure_report)
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))

                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        existing_row = (
                            await session.execute(
                                select(MeasureResult).where(
                                    MeasureResult.job_id == job_id,
                                    MeasureResult.patient_id == patient_id,
                                )
                            )
                        ).scalar_one_or_none()
                        gather_partial_details = partial_gather_patients.get(patient_id)
                        if existing_row:
                            existing_row.measure_report = measure_report
                            existing_row.populations = populations
                            existing_row.patient_name = patient_name
                            existing_row.error_details = gather_partial_details
                            existing_row.error_phase = "gather_partial" if gather_partial_details else None
                        else:
                            result = MeasureResult(
                                job_id=job_id,
                                patient_id=patient_id,
                                patient_name=patient_name,
                                measure_report=measure_report,
                                populations=populations,
                                error_details=gather_partial_details,
                                error_phase="gather_partial" if gather_partial_details else None,
                            )
                            session.add(result)
                        await session.commit()

                    processed += 1

                except Exception as patient_exc:
                    patient_name = _extract_patient_name(patient_map.get(patient_id, {}))
                    sanitized_error = sanitize_error(patient_exc)

                    upstream_outcome_raw: dict[str, Any] | None = None
                    eval_error_details: dict[str, Any] = {
                        "operation": "evaluate-measure",
                        "error": sanitized_error,
                        "error_phase": "evaluate",
                    }
                    if isinstance(patient_exc, FhirOperationError):
                        eval_error_details["url"] = patient_exc.url
                        eval_error_details["status_code"] = patient_exc.status_code
                        eval_error_details["latency_ms"] = patient_exc.latency_ms
                        if patient_exc.outcome:
                            upstream_outcome_raw = patient_exc.outcome.raw
                            eval_error_details["raw_outcome"] = redact_outcome(patient_exc.outcome.raw)

                    error_report = _error_measure_report(patient_id, patient_exc, upstream_outcome_raw)
                    if await _stop_or_delete_job(job_id):
                        return
                    async with async_session() as session:
                        existing_row = (
                            await session.execute(
                                select(MeasureResult).where(
                                    MeasureResult.job_id == job_id,
                                    MeasureResult.patient_id == patient_id,
                                )
                            )
                        ).scalar_one_or_none()
                        if existing_row:
                            existing_row.measure_report = error_report
                            existing_row.populations = {
                                "initial_population": False,
                                "denominator": False,
                                "numerator": False,
                                "denominator_exclusion": False,
                                "numerator_exclusion": False,
                                "error": True,
                                "error_message": sanitized_error,
                                "error_phase": "evaluate",
                            }
                            existing_row.error_details = eval_error_details
                            existing_row.error_phase = "evaluate"
                        else:
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
                                    "error_phase": "evaluate",
                                },
                                error_details=eval_error_details,
                                error_phase="evaluate",
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
