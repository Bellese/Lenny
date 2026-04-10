"""Job management endpoints."""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import CDRContext, get_active_cdr
from app.models.job import Job, JobStatus
from app.services.fhir_client import _build_auth_headers, list_groups

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
        cdr_auth_type=str(cdr.auth_type) if cdr.auth_type else None,
        cdr_auth_credentials=cdr.auth_credentials,
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
