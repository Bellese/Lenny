"""Job management endpoints."""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.config import settings
from app.db import get_session
from app.dependencies import CDRContext, get_active_cdr
from app.models.job import BatchStatus, Job, JobStatus, MeasureResult
from app.models.validation import ExpectedResult
from app.services.fhir_client import _build_auth_headers, _validate_ssrf_url, list_groups
from app.services.validation import _extract_population_counts, compare_populations

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,256}$")


class JobCreate(BaseModel):
    measure_id: str
    measure_name: Optional[str] = None
    period_start: str
    period_end: str
    cdr_url: Optional[str] = None  # if omitted, use active CDR config or default
    group_id: Optional[str] = None  # if set, only evaluate patients in this FHIR Group

    @field_validator("group_id")
    @classmethod
    def validate_group_id(cls, v: Optional[str]) -> Optional[str]:
        """Reject group_id values that could rewrite the CDR URL path."""
        if v is not None and not _GROUP_ID_RE.match(v):
            raise ValueError("group_id must be alphanumeric with hyphens, underscores, or dots only")
        return v


class JobResponse(BaseModel):
    id: int
    measure_id: str
    measure_name: Optional[str]
    period_start: str
    period_end: str
    cdr_url: str
    cdr_name: Optional[str] = None
    cdr_read_only: bool = False
    group_id: Optional[str]
    status: str
    total_patients: int
    processed_patients: int
    failed_patients: int
    total_batches: int = 0
    batches_completed: int = 0
    delete_requested: bool
    created_at: str
    completed_at: Optional[str]
    error_message: Optional[str]

    model_config = {"from_attributes": True}


class BatchResponse(BaseModel):
    id: int
    batch_number: int
    patient_ids: list[str]
    status: str
    retry_count: int
    error_message: Optional[str]
    created_at: str
    completed_at: Optional[str]

    model_config = {"from_attributes": True}


class JobDetailResponse(JobResponse):
    batches: list[BatchResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_to_response(job: Job) -> dict:
    batches = job.batches if job.batches is not None else []
    return {
        "id": job.id,
        "measure_id": job.measure_id,
        "measure_name": job.measure_name,
        "period_start": job.period_start,
        "period_end": job.period_end,
        "cdr_url": job.cdr_url,
        "cdr_name": job.cdr_name,
        "cdr_read_only": job.cdr_read_only,
        "group_id": job.group_id,
        "status": job.status.value if isinstance(job.status, JobStatus) else job.status,
        "total_patients": job.total_patients,
        "processed_patients": job.processed_patients,
        "failed_patients": job.failed_patients,
        "total_batches": len(batches),
        "batches_completed": sum(1 for b in batches if b.status == BatchStatus.complete),
        "delete_requested": job.delete_requested,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_message": job.error_message,
    }


def _batch_to_response(batch) -> dict:
    return {
        "id": batch.id,
        "batch_number": batch.batch_number,
        "patient_ids": batch.patient_ids,
        "status": batch.status.value if hasattr(batch.status, "value") else batch.status,
        "retry_count": batch.retry_count,
        "error_message": batch.error_message,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
    }


def _empty_comparison_response() -> dict:
    return {
        "has_expected": False,
        "matched": None,
        "total": None,
        "expected_total": 0,
        "actual_total": 0,
        "missing_results": 0,
        "unexpected_results": 0,
        "patients": [],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/groups")
async def get_groups(
    cdr: CDRContext = Depends(get_active_cdr),
) -> dict:
    """List FHIR Group resources from the CDR."""
    auth_headers = await _build_auth_headers(cdr.auth_type, cdr.auth_credentials)

    try:
        groups = await list_groups(cdr.cdr_url, auth_headers)
        return {"groups": groups}
    except Exception:
        logger.exception("Failed to fetch groups from CDR")
        raise HTTPException(
            status_code=502,
            detail="Cannot reach CDR to list groups. Check CDR connectivity in Settings.",
        )


@router.post("", response_model=JobResponse, status_code=201)
async def create_job(
    body: JobCreate,
    session: AsyncSession = Depends(get_session),
    cdr: CDRContext = Depends(get_active_cdr),
) -> dict:
    """Create a new measure calculation job."""
    if body.cdr_url:
        try:
            _validate_ssrf_url(body.cdr_url, label="cdr_url")
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": "error", "code": "security", "diagnostics": str(exc)}],
                },
            )
    cdr_url = body.cdr_url or cdr.cdr_url

    job = Job(
        measure_id=body.measure_id,
        measure_name=body.measure_name,
        period_start=body.period_start,
        period_end=body.period_end,
        cdr_url=cdr_url,
        group_id=body.group_id,
        status=JobStatus.queued,
        cdr_name=cdr.name,
        cdr_read_only=cdr.is_read_only,
        cdr_auth_type=cdr.auth_type.value if cdr.auth_type else None,
        cdr_id=cdr.id if cdr.id is not None else None,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    logger.info("Job created", extra={"job_id": job.id, "measure_id": job.measure_id})
    return _job_to_response(job)


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """List all jobs, most recent first."""
    result = await session.execute(select(Job).order_by(Job.created_at.desc()))
    jobs = result.scalars().all()
    return [_job_to_response(j) for j in jobs]


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get job details including batch breakdown."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"Job {job_id} not found",
                    }
                ],
            },
        )
    resp = _job_to_response(job)
    resp["batches"] = [_batch_to_response(b) for b in job.batches]
    return resp


@router.post("/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cancel a running or queued job."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"Job {job_id} not found",
                    }
                ],
            },
        )

    if job.status not in (JobStatus.queued, JobStatus.running):
        raise HTTPException(
            status_code=409,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "conflict",
                        "diagnostics": f"Job is already {job.status.value}, cannot cancel",
                    }
                ],
            },
        )

    job.status = JobStatus.cancelled
    job.completed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(job)

    logger.info("Job cancelled", extra={"job_id": job_id})
    return _job_to_response(job)


@router.delete("/{job_id}")
async def delete_job(
    job_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Delete a job and its dependent batches/results."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"Job {job_id} not found",
                    }
                ],
            },
        )

    if job.status == JobStatus.running:
        job.delete_requested = True
        await session.commit()
        logger.info("Job delete requested", extra={"job_id": job_id})
        return JSONResponse(
            status_code=202,
            content={"id": job_id, "status": "delete_requested", "delete_requested": True},
        )

    if job.status == JobStatus.queued:
        job.status = JobStatus.cancelled
        job.delete_requested = True
        job.completed_at = datetime.now(timezone.utc)
        await session.commit()
        logger.info("Queued job delete requested", extra={"job_id": job_id})
        return JSONResponse(
            status_code=202,
            content={"id": job_id, "status": "delete_requested", "delete_requested": True},
        )

    await session.delete(job)
    await session.commit()
    logger.info("Job deleted", extra={"job_id": job_id})
    return Response(status_code=204)


@router.get("/{job_id}/measure-report")
async def get_job_measure_report(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return a FHIR Bundle (collection) of individual MeasureReports for a job.

    Includes all patients whose populations["error"] is falsy — i.e. successful
    evaluations AND gather_partial patients (those have real MeasureReports from the
    engine; only their CDR push was partial). Excludes gather-failure and
    evaluate-failure patients whose measure_report is a synthetic OperationOutcome.

    Returns 404 if the job does not exist or has no results yet (consistent with
    the /results endpoint). Direct API calls on an in-progress job receive a
    partial bundle — intentional; no status gate is applied.

    Memory: up to ~500 patients x ~20 KB/report = ~10 MB per query (single load,
    no double-load due to noload() below). Revisit if cohort sizes grow to thousands.
    """
    job = await session.get(Job, job_id, options=[noload(Job.results), noload(Job.batches)])
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found", "diagnostics": f"Job {job_id} not found"}],
            },
        )

    result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
    results = result.scalars().all()

    if not results:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {"severity": "error", "code": "not-found", "diagnostics": f"No results found for job {job_id}"}
                ],
            },
        )

    # populations is non-nullable (models/job.py) — no `or {}` needed.
    # Filter by populations["error"] (not measure_report resourceType) so that
    # gather_partial patients with real engine-produced reports are included.
    entries = [
        {"resource": mr.measure_report} for mr in results if mr.measure_report and not mr.populations.get("error")
    ]

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "total": len(entries),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "entry": entries,
    }


@router.get("/{job_id}/comparison")
async def get_job_comparison(
    job_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Compare actual population counts against expected test case values."""
    job = await session.get(Job, job_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "not-found", "diagnostics": f"Job {job_id} not found"}],
            },
        )

    measure_url = ""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{settings.MEASURE_ENGINE_URL}/Measure/{job.measure_id}")
            if resp.status_code == 200:
                measure_url = resp.json().get("url", "")
    except Exception:
        logger.warning("Could not resolve measure URL for comparison", extra={"measure_id": job.measure_id})

    if not measure_url:
        return _empty_comparison_response()

    exp_result = await session.execute(
        select(ExpectedResult).where(
            ExpectedResult.measure_url == measure_url,
            ExpectedResult.period_start == job.period_start,
            ExpectedResult.period_end == job.period_end,
        )
    )
    expected_by_patient = {er.patient_ref: er for er in exp_result.scalars().all()}

    if not expected_by_patient:
        return _empty_comparison_response()

    result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
    actual_by_patient = {mr.patient_id: mr for mr in result.scalars().all()}

    patients_list = []
    matched_count = 0

    for patient_id, expected in sorted(expected_by_patient.items()):
        mr = actual_by_patient.get(patient_id)
        if not mr:
            patients_list.append(
                {
                    "subject_reference": f"Patient/{patient_id}",
                    "match": False,
                    "mismatches": ["missing-result"],
                    "expected": expected.expected_populations,
                    "actual": {},
                }
            )
            continue

        actual_counts = _extract_population_counts(mr.measure_report)
        passed, mismatches = compare_populations(expected.expected_populations, actual_counts)
        if passed:
            matched_count += 1

        patients_list.append(
            {
                "subject_reference": f"Patient/{mr.patient_id}",
                "match": passed,
                "mismatches": mismatches,
                "expected": expected.expected_populations,
                "actual": actual_counts,
            }
        )

    unexpected_result_count = len(set(actual_by_patient) - set(expected_by_patient))

    return {
        "has_expected": True,
        "matched": matched_count,
        "total": len(patients_list),
        "expected_total": len(expected_by_patient),
        "actual_total": len(actual_by_patient),
        "missing_results": len(expected_by_patient) - len(set(expected_by_patient) & set(actual_by_patient)),
        "unexpected_results": unexpected_result_count,
        "patients": patients_list,
    }
