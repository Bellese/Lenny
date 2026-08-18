"""Measure management endpoints — proxy to the active MCS connection.

Every route here resolves the MCS from `get_active_mcs` rather than from
`settings.MEASURE_ENGINE_URL`. Reading the env var meant the measure list never
reflected the server the user had actually connected to (issue #396).
"""

import json
import logging

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile

from app.config import MAX_UPLOAD_SIZE
from app.dependencies import ConnectionContext, get_active_mcs
from app.limiter import limiter
from app.services.fhir_client import _build_auth_headers, delete_measure, list_measures, upload_measure_bundle
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/measures", tags=["measures"])


def _read_only_outcome(mcs: ConnectionContext, action: str) -> HTTPException:
    """403 OperationOutcome for a write attempt against a read-only MCS."""
    return HTTPException(
        status_code=403,
        detail={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "forbidden",
                    "diagnostics": (
                        f"The active measure calculation server '{mcs.name}' is marked read-only. "
                        f"Cannot {action}. Switch to a writable MCS connection in Settings."
                    ),
                }
            ],
        },
    )


@router.get("")
async def get_measures(mcs: ConnectionContext = Depends(get_active_mcs)) -> dict:
    """List all Measure resources from the active MCS.

    No fallback to the local engine: if the connected MCS is unreachable the
    caller gets a 502 naming it, not a silently different measure list.
    """
    auth_headers = await _build_auth_headers(mcs.auth_type, mcs.auth_credentials)
    try:
        bundle = await list_measures(
            mcs.mcs_url,
            auth_headers=auth_headers,
            timeout=float(mcs.request_timeout_seconds),
        )
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
        return {
            "measures": measures,
            "total": len(measures),
            "mcs": {"id": mcs.id, "name": mcs.name, "url": mcs.mcs_url},
        }
    except Exception as exc:
        logger.exception("Failed to fetch measures from engine", extra={"mcs_id": mcs.id, "mcs_name": mcs.name})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": (f"Cannot reach measure engine '{mcs.name}': {sanitize_error(exc)}"),
                    }
                ],
            },
        )


@router.post("/upload")
@limiter.limit("10/minute")
async def upload_measure(
    request: Request,
    file: UploadFile = File(...),
    mcs: ConnectionContext = Depends(get_active_mcs),
) -> dict:
    """Upload a FHIR Measure bundle (JSON) to the active MCS.

    Accepts a JSON file containing a FHIR Bundle with Measure and Library
    resources. POSTs it to the active MCS as a transaction Bundle.
    """
    # Checked before the body is read so a large upload to a read-only MCS is
    # rejected without transferring it.
    if mcs.is_read_only:
        raise _read_only_outcome(mcs, "upload measure bundles")

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

    auth_headers = await _build_auth_headers(mcs.auth_type, mcs.auth_credentials)
    try:
        result = await upload_measure_bundle(
            bundle_json,
            mcs.mcs_url,
            auth_headers=auth_headers,
            timeout=float(mcs.request_timeout_seconds),
        )
        logger.info(
            "Measure bundle uploaded: %s",
            file.filename,
            extra={"mcs_id": mcs.id, "mcs_name": mcs.name},
        )
        return {
            "status": "success",
            "message": "Measure bundle uploaded successfully",
            "result": result,
        }
    except Exception as exc:
        logger.exception("Failed to upload measure bundle", extra={"mcs_id": mcs.id, "mcs_name": mcs.name})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": (f"Measure engine '{mcs.name}' rejected bundle: {sanitize_error(exc)}"),
                    }
                ],
            },
        )


@router.delete("/{measure_id}", status_code=204)
async def delete_measure_route(
    measure_id: str,
    mcs: ConnectionContext = Depends(get_active_mcs),
) -> Response:
    """Delete a Measure resource from the active MCS."""
    if mcs.is_read_only:
        raise _read_only_outcome(mcs, "delete measures")

    auth_headers = await _build_auth_headers(mcs.auth_type, mcs.auth_credentials)
    try:
        await delete_measure(
            measure_id,
            mcs.mcs_url,
            auth_headers=auth_headers,
            timeout=float(mcs.request_timeout_seconds),
        )
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
        logger.exception(
            "Measure engine rejected measure delete",
            extra={"measure_id": measure_id, "mcs_id": mcs.id, "mcs_name": mcs.name},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": (f"Measure engine '{mcs.name}' rejected delete: {sanitize_error(exc)}"),
                    }
                ],
            },
        ) from exc
    except Exception as exc:
        logger.exception(
            "Failed to delete measure",
            extra={"measure_id": measure_id, "mcs_id": mcs.id, "mcs_name": mcs.name},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": (f"Cannot reach measure engine '{mcs.name}': {sanitize_error(exc)}"),
                    }
                ],
            },
        ) from exc

    logger.info("Measure deleted", extra={"measure_id": measure_id, "mcs_id": mcs.id, "mcs_name": mcs.name})
    return Response(status_code=204)
