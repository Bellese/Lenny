"""Validation endpoints — upload test bundles, run validation, view results."""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MAX_UPLOAD_SIZE
from app.db import get_session
from app.limiter import limiter
from app.models.validation import (
    BundleUpload,
    ExpectedResult,
    ValidationResult,
    ValidationRun,
    ValidationStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/validation", tags=["validation"])


def _sanitize_filename(name: str) -> str:
    name = os.path.basename(name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    if len(name) > 255:
        dot = name.rfind(".")
        if dot > 0:
            ext = name[dot:]
            stem = name[:dot][: 255 - len(ext)]
            name = stem + ext
        else:
            name = name[:255]
    if not name or name == ".":
        name = "upload.json"
    return name


class ValidationRunCreate(BaseModel):
    """Optional filter for starting a validation run."""

    measure_urls: Optional[list[str]] = None


UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "uploads")


@router.post("/upload-bundle")
@limiter.limit("10/minute")
async def upload_bundle(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upload a FHIR test bundle for validation.

    Triages resources asynchronously: measure definitions to engine,
    clinical data to CDR, expected results to MCT2 DB.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="File must have a filename")
    safe_name = _sanitize_filename(file.filename)
    if not safe_name.lower().endswith(".json"):
        raise HTTPException(status_code=400, detail="File must be a .json file")

    # Read and validate content — cap read to MAX_UPLOAD_SIZE+1 to avoid OOM on huge uploads
    content = await file.read(MAX_UPLOAD_SIZE + 1)
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 100MB size limit")

    try:
        bundle_json = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid JSON")

    if bundle_json.get("resourceType") != "Bundle":
        raise HTTPException(status_code=400, detail="JSON is not a FHIR Bundle (missing resourceType: Bundle)")

    # Save file to disk (off the event loop to avoid blocking on large uploads)
    await asyncio.to_thread(os.makedirs, UPLOAD_DIR, exist_ok=True)
    timestamp = int(time.time())
    # Composite on-disk name: "{10-digit ts}-{32-hex uuid}-{safe_name}"
    # Prefix overhead: 10 + 1 + 32 + 1 = 44 chars.  Keep total ≤ 255.
    _prefix_overhead = 44
    _max_name_len = 255 - _prefix_overhead  # 211
    if len(safe_name) > _max_name_len:
        dot = safe_name.rfind(".")
        if dot > 0:
            ext = safe_name[dot:]
            safe_name = safe_name[: _max_name_len - len(ext)] + ext
        else:
            safe_name = safe_name[:_max_name_len]
    safe_filename = f"{timestamp}-{uuid.uuid4().hex}-{safe_name}"
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    def _write_file() -> None:
        with open(file_path, "wb") as f:
            f.write(content)

    await asyncio.to_thread(_write_file)

    # Create queued upload record
    upload = BundleUpload(
        filename=safe_name,
        file_path=file_path,
        status=ValidationStatus.queued,
    )
    session.add(upload)
    await session.commit()
    await session.refresh(upload)

    return {
        "id": upload.id,
        "status": upload.status.value,
        "filename": upload.filename,
    }


@router.get("/uploads")
async def list_uploads(session: AsyncSession = Depends(get_session)) -> dict:
    """List all bundle uploads with status and counts."""
    result = await session.execute(select(BundleUpload).order_by(BundleUpload.created_at.desc()))
    uploads = result.scalars().all()
    return {
        "uploads": [
            {
                "id": u.id,
                "filename": u.filename,
                "status": u.status.value,
                "measures_loaded": u.measures_loaded,
                "patients_loaded": u.patients_loaded,
                "expected_results_loaded": u.expected_results_loaded,
                "error_message": u.error_message,
                "warning_message": u.warning_message,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "completed_at": u.completed_at.isoformat() if u.completed_at else None,
            }
            for u in uploads
        ]
    }


@router.get("/expected")
async def list_expected_results(session: AsyncSession = Depends(get_session)) -> dict:
    """List loaded expected results grouped by measure."""
    result = await session.execute(
        select(
            ExpectedResult.measure_url,
            func.count(ExpectedResult.id).label("patient_count"),
            func.min(ExpectedResult.period_start).label("period_start"),
            func.max(ExpectedResult.period_end).label("period_end"),
        ).group_by(ExpectedResult.measure_url)
    )
    measures = []
    for row in result.all():
        measures.append(
            {
                "measure_url": row.measure_url,
                "patient_count": row.patient_count,
                "period_start": row.period_start,
                "period_end": row.period_end,
            }
        )
    return {"measures": measures, "total_measures": len(measures)}


@router.post("/run")
async def start_validation_run(
    body: Optional[ValidationRunCreate] = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Start a new validation run.

    Optional body: {"measure_urls": ["..."]} to filter which measures to validate.
    """
    # Check that expected results exist
    query = select(func.count(ExpectedResult.id))
    if body and body.measure_urls:
        query = query.where(ExpectedResult.measure_url.in_(body.measure_urls))
    result = await session.execute(query)
    count = result.scalar()
    if not count:
        raise HTTPException(
            status_code=400,
            detail="No expected results loaded. Upload a test bundle first.",
        )

    run = ValidationRun(
        status=ValidationStatus.queued,
        measure_urls=body.measure_urls if body else None,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    return {
        "id": run.id,
        "status": run.status.value,
    }


@router.get("/runs")
async def list_validation_runs(session: AsyncSession = Depends(get_session)) -> dict:
    """List all validation runs with summary stats."""
    result = await session.execute(select(ValidationRun).order_by(ValidationRun.created_at.desc()))
    runs = result.scalars().all()
    return {
        "runs": [
            {
                "id": r.id,
                "status": r.status.value,
                "measure_urls": r.measure_urls,
                "measures_tested": r.measures_tested,
                "patients_tested": r.patients_tested,
                "patients_passed": r.patients_passed,
                "patients_failed": r.patients_failed,
                "error_message": r.error_message,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in runs
        ]
    }


@router.get("/runs/{run_id}")
async def get_validation_run(
    run_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get full validation run results grouped by measure."""
    run = await session.get(ValidationRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Validation run not found")

    # Get results sorted: failures first, then errors, then passes
    result = await session.execute(
        select(ValidationResult)
        .where(ValidationResult.validation_run_id == run_id)
        .order_by(
            # Explicit priority: failures first, then errors, then passes
            case(
                (ValidationResult.status == "fail", 0),
                (ValidationResult.status == "error", 1),
                else_=2,
            ),
            ValidationResult.measure_url.asc(),
        )
    )
    results = result.scalars().all()

    # Group by measure
    measures: dict[str, dict] = {}
    for vr in results:
        if vr.measure_url not in measures:
            measures[vr.measure_url] = {
                "measure_url": vr.measure_url,
                "patients": [],
                "passed": 0,
                "failed": 0,
                "errors": 0,
            }
        m = measures[vr.measure_url]
        m["patients"].append(
            {
                "patient_ref": vr.patient_ref,
                "patient_name": vr.patient_name,
                "expected_populations": vr.expected_populations,
                "actual_populations": vr.actual_populations,
                "status": vr.status,
                "error_message": vr.error_message,
                "mismatches": vr.mismatches,
            }
        )
        if vr.status == "pass":
            m["passed"] += 1
        elif vr.status == "fail":
            m["failed"] += 1
        else:
            m["errors"] += 1

    return {
        "id": run.id,
        "status": run.status.value,
        "measures_tested": run.measures_tested,
        "patients_tested": run.patients_tested,
        "patients_passed": run.patients_passed,
        "patients_failed": run.patients_failed,
        "error_message": run.error_message,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "measures": list(measures.values()),
    }
