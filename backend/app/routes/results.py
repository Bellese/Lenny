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

_POP_DESC_URL = "http://hl7.org/fhir/5.0/StructureDefinition/extension-MeasureReport.population.description"


def _extract_pop_info(
    measure_report: dict | None,
) -> tuple[dict[str, str] | None, list[str] | None]:
    """Extract population descriptions and defined codes from a MeasureReport.

    Returns (descriptions_dict, defined_codes_list).
    descriptions_dict maps FHIR population code → description string (only codes that
    have a description extension).
    defined_codes_list lists all population codes that appear in the MeasureReport,
    whether or not they have a description (used to hide irrelevant rows in the UI).
    Both values are None when the MeasureReport is absent.
    """
    if not measure_report:
        return None, None
    descriptions: dict[str, str] = {}
    defined: list[str] = []
    for group in measure_report.get("group", []):
        for pop in group.get("population", []):
            code: str | None = None
            for coding in pop.get("code", {}).get("coding", []):
                code = coding.get("code")
                break
            if not code:
                continue
            defined.append(code)
            for ext in pop.get("extension", []):
                if ext.get("url") == _POP_DESC_URL:
                    desc = (ext.get("valueString") or "").strip()
                    if desc:
                        descriptions[code] = desc
                    break
    return (descriptions or None), (defined or None)


@router.get("")
async def get_results(
    job_id: int = Query(..., description="Job ID to fetch results for"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Aggregate results for a job.

    Returns population sums across all MeasureResults and a performance rate.
    """
    result = await session.execute(select(MeasureResult).where(MeasureResult.job_id == job_id))
    results = result.scalars().all()

    if not results:
        return {
            "job_id": job_id,
            "total_patients": 0,
            "failed_patients": 0,
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
    failed_patients = 0
    population_descriptions: dict[str, str] | None = None
    defined_populations: list[str] | None = None

    for mr in results:
        populations = mr.populations or {}
        is_error = bool(populations.get("error"))
        is_partial = mr.error_phase == "gather_partial"
        if population_descriptions is None and mr.measure_report:
            population_descriptions, defined_populations = _extract_pop_info(mr.measure_report)
        if is_error:
            failed_patients += 1
        else:
            for key in pops:
                if populations.get(key):
                    pops[key] += 1
        patients_list.append(
            {
                "id": mr.id,
                "patient_id": mr.patient_id,
                "patient_name": mr.patient_name,
                "populations": populations,
                "status": "error" if is_error else "success",
                "error_message": populations.get("error_message") if is_error else None,
                "error_phase": mr.error_phase if (is_error or is_partial) else None,
                "error_details": mr.error_details if (is_error or is_partial) else None,
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
        "failed_patients": failed_patients,
        "populations": pops,
        "performance_rate": performance_rate,
        "population_descriptions": population_descriptions,
        "defined_populations": defined_populations,
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

    is_error = bool((mr.populations or {}).get("error"))
    is_partial = mr.error_phase == "gather_partial"
    return {
        "id": mr.id,
        "job_id": mr.job_id,
        "patient_id": mr.patient_id,
        "patient_name": mr.patient_name,
        "measure_report": mr.measure_report,
        "populations": mr.populations,
        "status": "error" if is_error else "success",
        "error_message": (mr.populations or {}).get("error_message"),
        "error_phase": mr.error_phase if (is_error or is_partial) else None,
        "error_details": mr.error_details if (is_error or is_partial) else None,
        "created_at": mr.created_at.isoformat() if mr.created_at else None,
    }


@router.get("/{result_id}/evaluated-resources")
async def get_evaluated_resources(
    result_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return evaluatedResource resources for a MeasureReport.

    New rows have a snapshot persisted at job time (see orchestrator.py) so this
    survives the next job's wipe_patient_data(). Legacy rows fall back to live
    resolution against the measure engine, which only works until the next job
    starts.
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
    evaluated_refs: list[str] = [
        ref_obj.get("reference") for ref_obj in measure_report.get("evaluatedResource", []) if ref_obj.get("reference")
    ]

    # New rows: snapshot was persisted at job time, survives subsequent wipes.
    if mr.evaluated_resources is not None:
        snapshot = mr.evaluated_resources
        return {
            "result_id": result_id,
            "patient_id": mr.patient_id,
            "total_references": len(evaluated_refs),
            "resolved": len(snapshot),
            "resources": snapshot,
            "errors": None,
            "source": "snapshot",
        }

    # Legacy rows (pre-snapshot feature): fall back to live resolution.
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
        "source": "live",
    }
