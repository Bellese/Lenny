"""Settings management endpoints — CDR connection routes via the connection
factory, plus admin and measure-engine routes that don't fit the connection
shape.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.app_setting import AppSetting
from app.models.config import CDRConfig
from app.models.connection_base import ConnectionKind
from app.models.job import Job
from app.models.mcs_config import MCSConfig
from app.routes.connection_factory import make_connection_router
from app.services.fhir_client import probe_mcs_data_requirements, wipe_measure_definitions
from app.services.fhir_errors import (
    HINT_BY_STATUS,
    FhirOperationError,
    build_error_envelope,
    hint_for_network_exception,
    sanitize_url,
)
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# CDR Schemas
# ---------------------------------------------------------------------------


class CDRConnectionResponse(BaseModel):
    id: int
    name: str
    cdr_url: str
    auth_type: str
    is_active: bool
    is_default: bool
    is_read_only: bool

    model_config = {"from_attributes": True}


class CDRConnectionCreate(BaseModel):
    name: str
    cdr_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None
    is_read_only: bool = False


class TestConnectionRequest(BaseModel):
    cdr_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None


# ---------------------------------------------------------------------------
# MCS Schemas
# ---------------------------------------------------------------------------


class MCSConnectionResponse(BaseModel):
    id: int
    name: str
    mcs_url: str
    auth_type: str
    is_active: bool
    is_default: bool

    model_config = {"from_attributes": True}


class MCSConnectionCreate(BaseModel):
    name: str
    mcs_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None


class MCSTestConnectionRequest(BaseModel):
    mcs_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None


# ---------------------------------------------------------------------------
# CDR connection routes — delegated to the factory
# ---------------------------------------------------------------------------

router.include_router(
    make_connection_router(
        model=CDRConfig,
        response_schema=CDRConnectionResponse,
        create_schema=CDRConnectionCreate,
        test_request_schema=TestConnectionRequest,
        prefix="/connections",
        kind=ConnectionKind.cdr,
        url_field="cdr_url",
        default_name="Local CDR",
        job_fk_column=Job.cdr_id,
        audit_logger=logger,
    )
)


# ---------------------------------------------------------------------------
# MCS connection routes — same factory, different model+schemas
# ---------------------------------------------------------------------------

router.include_router(
    make_connection_router(
        model=MCSConfig,
        response_schema=MCSConnectionResponse,
        create_schema=MCSConnectionCreate,
        test_request_schema=MCSTestConnectionRequest,
        prefix="/mcs-connections",
        kind=ConnectionKind.mcs,
        url_field="mcs_url",
        default_name="Local Measure Engine",
        # Job FK to MCS lands in PR #4 alongside the fhir_client wiring.
        job_fk_column=None,
        audit_logger=logger,
    )
)


# ---------------------------------------------------------------------------
# MCS deep-probe — Verify with sample evaluate
# ---------------------------------------------------------------------------


@router.post("/mcs-connections/{connection_id}/probe")
async def probe_mcs_connection(
    connection_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Deep-probe an MCS connection by exercising `$data-requirements`.

    Stricter than test-connection (which only fetches /metadata): this picks
    a measure on the MCS and confirms the engine can resolve its Library +
    ValueSets. Returns success summary on green; raises HTTPException with
    the standard FHIR error envelope on any failure.
    """
    cfg = await session.get(MCSConfig, connection_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="MCS connection not found")

    try:
        return await probe_mcs_data_requirements(
            mcs_url=cfg.mcs_url,
            auth_type=cfg.auth_type,
            auth_credentials=cfg.auth_credentials,
        )
    except FhirOperationError as exc:
        logger.warning(
            "mcs probe failed",
            extra={
                "mcs_url": sanitize_url(cfg.mcs_url),
                "status_code": exc.status_code,
                "error": sanitize_error(exc),
            },
        )
        if exc.status_code is not None:
            hint = HINT_BY_STATUS.get(exc.status_code)
        else:
            hint = hint_for_network_exception(exc.__cause__) if exc.__cause__ else None
        # Empty-MCS path: status 200 with a synthesized OperationOutcome.
        # Surface as 200 + envelope so the frontend can render the warning
        # without treating it as a hard failure.
        if exc.status_code == 200 and exc.outcome is not None:
            return {
                "status": "warning",
                "outcome": exc.outcome.raw,
                "url": sanitize_url(exc.url),
            }
        http_status = exc.status_code if exc.status_code and exc.status_code >= 400 else 502
        raise HTTPException(
            status_code=http_status,
            detail=build_error_envelope(
                operation="probe-data-requirements",
                url=exc.url,
                status_code=exc.status_code,
                outcome=exc.outcome,
                latency_ms=exc.latency_ms,
                hint=hint,
            ),
        )
    except ValueError as exc:
        # SSRF rejection from _validate_ssrf_url
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "security", "diagnostics": sanitize_error(exc)}],
            },
        )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

_ADMIN_DEFAULTS: dict[str, str] = {
    "validation_enabled": "true",
}


async def _get_setting(session: AsyncSession, key: str) -> str:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else _ADMIN_DEFAULTS[key]


@router.get("/admin")
async def get_admin_settings(session: AsyncSession = Depends(get_session)) -> dict:
    """Return current admin settings."""
    return {
        "validation_enabled": (await _get_setting(session, "validation_enabled")) == "true",
    }


class AdminSettingsUpdate(BaseModel):
    validation_enabled: bool | None = None


@router.put("/admin")
async def update_admin_settings(
    body: AdminSettingsUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Persist admin settings."""
    updates: dict[str, str] = {}
    if body.validation_enabled is not None:
        updates["validation_enabled"] = "true" if body.validation_enabled else "false"

    for key, value in updates.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    await session.commit()

    return {
        "validation_enabled": (await _get_setting(session, "validation_enabled")) == "true",
    }


@router.post("/admin/wipe-measure-engine", status_code=200)
async def wipe_measure_engine() -> dict:
    """Delete all measure-definition resources (Library, Measure, ValueSet, CodeSystem, ConceptMap)
    from the HAPI measure engine.

    Recovers from JVM/H2 state corruption that causes CQL compilation failures (issue #238).
    The engine is automatically re-seeded on the next job run via bundle_loader.
    """
    try:
        await wipe_measure_definitions()
    except Exception as exc:
        logger.error("Measure engine wipe failed", extra={"error": sanitize_error(exc)})
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "exception", "diagnostics": sanitize_error(exc)}],
            },
        )
    logger.info("Measure engine definitions wiped via admin endpoint")
    return {"status": "ok", "message": "Measure engine definitions wiped. Engine will re-seed on next job run."}
