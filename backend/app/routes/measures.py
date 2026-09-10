"""Measure management endpoints — proxy to the active MCS connection.

Every route here resolves the MCS from `get_active_mcs` rather than from
`settings.MEASURE_ENGINE_URL`. Reading the env var meant the measure list never
reflected the server the user had actually connected to (issue #396).
"""

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MAX_UPLOAD_SIZE
from app.db import get_session
from app.dependencies import ConnectionContext, get_active_mcs
from app.limiter import limiter
from app.models.measure_readiness import MeasureReadiness, ReadinessState
from app.services.fhir_client import _build_auth_headers, delete_measure, list_measures, upload_measure_bundle
from app.services.measure_readiness import claim_unchecked, invalidate_mcs, mark_all_checking, run_sweep
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/measures", tags=["measures"])


async def _resolve_auth(mcs: ConnectionContext) -> dict[str, str]:
    """Resolve MCS credentials, converting failures into a 502 OperationOutcome.

    SMART auth makes a token-endpoint round trip, so `_build_auth_headers` can
    fail exactly like any other upstream call (`ValueError` on malformed
    credentials, `HTTPStatusError` from the token endpoint). Left unguarded it
    escapes as a bare 500 with no OperationOutcome body — the one MCS failure
    mode on this surface that wouldn't name the server.

    Deliberately a wrapper rather than moving the call inside each handler's
    existing `try`: `delete_measure_route` catches `httpx.HTTPStatusError` and
    maps 404 to "measure not found", which would mis-report a 404 from a SMART
    token endpoint as a missing measure.
    """
    try:
        return await _build_auth_headers(mcs.auth_type, mcs.auth_credentials)
    except Exception as exc:
        logger.exception(
            "Failed to resolve MCS credentials",
            extra={"mcs_id": mcs.id, "mcs_name": mcs.name},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": (f"Cannot authenticate to measure engine '{mcs.name}': {sanitize_error(exc)}"),
                    }
                ],
            },
        ) from exc


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


def _readiness_payload(row: MeasureReadiness | None) -> dict:
    """Render one verdict for the API. A missing row is `unknown`, never absent.

    Always emitting the object keeps the frontend from having to distinguish
    "not checked" from "field not implemented".
    """
    if row is None:
        return {
            "state": ReadinessState.unknown.value,
            "checked_at": None,
            "missing_libraries": [],
            "missing_valuesets": [],
            "error": None,
        }
    return {
        "state": row.state.value,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "missing_libraries": row.missing_libraries or [],
        "missing_valuesets": row.missing_valuesets or [],
        "error": row.error,
    }


@router.get("")
async def get_measures(
    mcs: ConnectionContext = Depends(get_active_mcs),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List all Measure resources from the active MCS.

    No fallback to the local engine: if the connected MCS is unreachable the
    caller gets a 502 naming it, not a silently different measure list.
    """
    auth_headers = await _resolve_auth(mcs)
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

        # Readiness is a left join from the cache — never a blocking call. A
        # measure with no cached verdict is claimed as `checking` here,
        # synchronously, so a page refresh during a sweep cannot queue a second.
        #
        # `mcs.id == 0` is the defensive "no active MCSConfig row" fallback (see
        # `get_active_mcs`) — there is no real row for `measure_readiness.mcs_id`
        # to reference (the FK would reject the insert), so readiness is left
        # `unknown` rather than claimed. Real deployments never hit this path
        # once the startup seed has run.
        if mcs.id:
            keys = [(m["id"], m.get("version") or "") for m in measures if m.get("id")]
            rows = (
                (await session.execute(select(MeasureReadiness).where(MeasureReadiness.mcs_id == mcs.id)))
                .scalars()
                .all()
            )
            by_key = {(r.measure_id, r.measure_version): r for r in rows}

            claimed = await claim_unchecked(session, mcs.id, keys)
            if claimed:
                asyncio.create_task(run_sweep(mcs.id, claimed))
            claimed_set = set(claimed)

            for measure in measures:
                key = (measure.get("id"), measure.get("version") or "")
                row = by_key.get(key)
                if row is None and key in claimed_set:
                    measure["readiness"] = {
                        "state": ReadinessState.checking.value,
                        "checked_at": None,
                        "missing_libraries": [],
                        "missing_valuesets": [],
                        "error": None,
                    }
                else:
                    measure["readiness"] = _readiness_payload(row)
        else:
            for measure in measures:
                measure["readiness"] = _readiness_payload(None)

        # Identity only — deliberately no `url`. The 502 path below sanitizes
        # internal hostnames out of its diagnostics; publishing mcs_url on the
        # 200 path would undo that for no benefit. Consumers that need the URL
        # read it from GET /settings/mcs-connections.
        return {
            "measures": measures,
            "total": len(measures),
            "mcs": {"id": mcs.id, "name": mcs.name},
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
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upload a FHIR Measure bundle (JSON) to the active MCS.

    Accepts a JSON file containing a FHIR Bundle with Measure and Library
    resources. POSTs it to the active MCS as a transaction Bundle.
    """
    # Checked before `file.read()` and before any upstream call, so a read-only
    # MCS costs no memory and no network round trip.
    #
    # NOT a guard against transferring the bytes: Starlette has already parsed
    # the multipart body into `file` (spooling to disk past its threshold)
    # before this handler is entered. Rejecting the transfer itself would have
    # to happen at the ASGI/proxy layer, which this is not.
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

    auth_headers = await _resolve_auth(mcs)
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
        # An uploaded bundle can carry a Library that OTHER measures were missing,
        # so the whole connection's verdicts are stale, not just this measure's.
        await invalidate_mcs(session, mcs.id)
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
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a Measure resource from the active MCS."""
    if mcs.is_read_only:
        raise _read_only_outcome(mcs, "delete measures")

    auth_headers = await _resolve_auth(mcs)
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

    await invalidate_mcs(session, mcs.id)
    logger.info("Measure deleted", extra={"measure_id": measure_id, "mcs_id": mcs.id, "mcs_name": mcs.name})
    return Response(status_code=204)


@router.post("/readiness/refresh", status_code=202)
async def refresh_readiness(
    mcs: ConnectionContext = Depends(get_active_mcs),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Discard cached verdicts for the active MCS and re-check every measure.

    Returns 202 immediately — the sweep runs detached, and the caller learns the
    outcome by polling `GET /measures`. Read-only against the MCS, so it is
    allowed even when the connection is marked read-only.
    """
    auth_headers = await _resolve_auth(mcs)
    try:
        bundle = await list_measures(
            mcs.mcs_url,
            auth_headers=auth_headers,
            timeout=float(mcs.request_timeout_seconds),
        )
    except Exception as exc:
        logger.exception("Cannot list measures for readiness refresh", extra={"mcs_id": mcs.id})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": f"Cannot reach measure engine '{mcs.name}': {sanitize_error(exc)}",
                    }
                ],
            },
        ) from exc

    keys = [
        (r.get("id"), r.get("version") or "")
        for entry in bundle.get("entry", [])
        if (r := entry.get("resource", {})).get("resourceType") == "Measure" and r.get("id")
    ]
    # See the matching guard in `get_measures`: `mcs.id == 0` means there is no
    # real MCSConfig row to write `measure_readiness` rows against. Nothing is
    # queued in that case, so the response must say `skipped`, not `accepted`
    # — an identical 202 body for both would tell the caller "N measures
    # queued" when N is merely how many measures exist and nothing happened.
    # `GET /measures` renders `unknown` (not `checking`) in this same state;
    # the two endpoints must not disagree about what occurred.
    if not mcs.id:
        return {"status": "skipped", "measures": 0}

    await mark_all_checking(session, mcs.id, keys)
    asyncio.create_task(run_sweep(mcs.id, keys))
    return {"status": "accepted", "measures": len(keys)}
