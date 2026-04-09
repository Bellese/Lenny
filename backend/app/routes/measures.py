"""Measure management endpoints — proxy to the measure engine."""

import json
import logging

from fastapi import APIRouter, HTTPException, UploadFile, File

from app.services.fhir_client import list_measures, upload_measure_bundle
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
async def upload_measure(file: UploadFile = File(...)) -> dict:
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
        content = await file.read()
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
        logger.info(
            "Measure bundle uploaded",
            extra={"filename": file.filename},
        )
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
