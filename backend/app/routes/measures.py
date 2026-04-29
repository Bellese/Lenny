"""Measure management endpoints — proxy to the measure engine."""

import json
import logging

import httpx
from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile

from app.config import MAX_UPLOAD_SIZE
from app.limiter import limiter
from app.services.fhir_client import delete_measure, list_measures, upload_measure_bundle
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/measures", tags=["measures"])


@router.get("")
async def get_measures() -> dict:
    """List all Measure resources from the measure engine."""
    try:
        bundle = await list_measures()
        # Simplify response for the frontend
        measures = []
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            if resource.get("resourceType") == "Measure":
                measures.append(
                    {
                        "id": resource.get("id"),
                        "name": resource.get("name"),
                        "title": resource.get("title"),
                        "version": resource.get("version"),
                        "status": resource.get("status"),
                        "url": resource.get("url"),
                        "description": resource.get("description"),
                    }
                )
        return {"measures": measures, "total": len(measures)}
    except Exception as exc:
        logger.exception("Failed to fetch measures from engine")
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": f"Cannot reach measure engine: {sanitize_error(exc)}",
                    }
                ],
            },
        )


@router.post("/upload")
@limiter.limit("10/minute")
async def upload_measure(request: Request, file: UploadFile = File(...)) -> dict:
    """Upload a FHIR Measure bundle (JSON) to the measure engine.

    Accepts a JSON file containing a FHIR Bundle with Measure and Library
    resources. POSTs it to the measure engine as a transaction Bundle.
    """
    if not file.filename or not file.filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": "File must be a .json FHIR Bundle",
                    }
                ],
            },
        )

    try:
        content = await file.read(MAX_UPLOAD_SIZE + 1)
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "too-long",
                            "diagnostics": "File exceeds 100MB size limit",
                        }
                    ],
                },
            )
        bundle_json = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": f"Invalid JSON: {sanitize_error(exc)}",
                    }
                ],
            },
        )

    if bundle_json.get("resourceType") != "Bundle":
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": "Root resource must be a FHIR Bundle",
                    }
                ],
            },
        )

    try:
        result = await upload_measure_bundle(bundle_json)
        logger.info("Measure bundle uploaded: %s", file.filename)
        return {
            "status": "success",
            "message": "Measure bundle uploaded successfully",
            "result": result,
        }
    except Exception as exc:
        logger.exception("Failed to upload measure bundle")
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": f"Measure engine rejected bundle: {sanitize_error(exc)}",
                    }
                ],
            },
        )


@router.delete("/{measure_id}", status_code=204)
async def delete_measure_route(measure_id: str) -> Response:
    """Delete a Measure resource from the measure engine."""
    try:
        await delete_measure(measure_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail={
                    "resourceType": "OperationOutcome",
                    "issue": [
                        {
                            "severity": "error",
                            "code": "not-found",
                            "diagnostics": f"Measure {measure_id} not found",
                        }
                    ],
                },
            ) from exc
        logger.exception("Measure engine rejected measure delete", extra={"measure_id": measure_id})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": f"Measure engine rejected delete: {sanitize_error(exc)}",
                    }
                ],
            },
        ) from exc
    except Exception as exc:
        logger.exception("Failed to delete measure", extra={"measure_id": measure_id})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": f"Cannot reach measure engine: {sanitize_error(exc)}",
                    }
                ],
            },
        ) from exc

    logger.info("Measure deleted", extra={"measure_id": measure_id})
    return Response(status_code=204)
