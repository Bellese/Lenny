"""Result inspection endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.job import MeasureResult
from app.services.fhir_client import resolve_evaluated_resource
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/results", tags=["results"])


@router.get("")
async def get_results(
    job_id: int = Query(..., description="Job ID to fetch results for"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Aggregate results for a job.

    Returns population sums across all MeasureResults and a performance rate.
    """
    result = await session.execute(
        select(MeasureResult).where(MeasureResult.job_id == job_id)
    )
    results = result.scalars().all()

    if not results:
        return {
            "job_id": job_id,
            "total_patients": 0,
            "populations": {
                "initial_population": 0,
                "denominator": 0,
                "numerator": 0,
                "denominator_exclusion": 0,
                "numerator_exclusion": 0,
            },
            "performance_rate": None,
            "patients": [],
        }

    # Aggregate population counts
    pops = {
        "initial_population": 0,
        "denominator": 0,
        "numerator": 0,
        "denominator_exclusion": 0,
        "numerator_exclusion": 0,
    }
    patients_list = []

    for mr in results:
        populations = mr.populations or {}
        for key in pops:
            if populations.get(key):
                pops[key] += 1
        patients_list.append(
            {
                "id": mr.id,
                "patient_id": mr.patient_id,
                "patient_name": mr.patient_name,
                "populations": populations,
            }
        )

    # Performance rate = numerator / denominator
    # The denominator already excludes denominator-exclusion patients.
    performance_rate = None
    if pops["denominator"] > 0:
        performance_rate = round(pops["numerator"] / pops["denominator"] * 100, 1)

    return {
        "job_id": job_id,
        "total_patients": len(results),
        "populations": pops,
        "performance_rate": performance_rate,
        "patients": patients_list,
    }


@router.get("/{result_id}")
async def get_result(
    result_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Get an individual MeasureResult with full MeasureReport."""
    mr = await session.get(MeasureResult, result_id)
    if not mr:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"MeasureResult {result_id} not found",
                    }
                ],
            },
        )

    return {
        "id": mr.id,
        "job_id": mr.job_id,
        "patient_id": mr.patient_id,
        "patient_name": mr.patient_name,
        "measure_report": mr.measure_report,
        "populations": mr.populations,
        "created_at": mr.created_at.isoformat() if mr.created_at else None,
    }


@router.get("/{result_id}/evaluated-resources")
async def get_evaluated_resources(
    result_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Resolve evaluatedResource references from a MeasureReport.

    Proxies to the measure engine to fetch each referenced resource.
    The patient data is kept on the measure engine until the NEXT job
    starts, so these references remain resolvable.
    """
    mr = await session.get(MeasureResult, result_id)
    if not mr:
        raise HTTPException(
            status_code=404,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "not-found",
                        "diagnostics": f"MeasureResult {result_id} not found",
                    }
                ],
            },
        )

    measure_report = mr.measure_report or {}
    evaluated_refs: list[str] = []

    # Extract evaluatedResource references from the MeasureReport
    for ref_obj in measure_report.get("evaluatedResource", []):
        ref = ref_obj.get("reference")
        if ref:
            evaluated_refs.append(ref)

    # Resolve each reference from the measure engine
    resources: list[dict] = []
    errors: list[dict] = []

    for ref in evaluated_refs:
        try:
            resource = await resolve_evaluated_resource(ref)
            resources.append(resource)
        except Exception as exc:
            logger.warning(
                "Failed to resolve evaluated resource",
                extra={"reference": ref, "error": str(exc)},
            )
            errors.append({"reference": ref, "error": sanitize_error(exc)[:200]})

    return {
        "result_id": result_id,
        "patient_id": mr.patient_id,
        "total_references": len(evaluated_refs),
        "resolved": len(resources),
        "resources": resources,
        "errors": errors if errors else None,
    }
