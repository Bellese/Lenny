"""CDR settings management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.config import AuthType, CDRConfig
from app.services.fhir_client import verify_fhir_connection
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Schemas
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
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_auth_type(auth_type: str) -> AuthType:
    try:
        return AuthType(auth_type)
    except ValueError:
        valid_types = ", ".join(t.value for t in AuthType)
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": f"Invalid auth_type: {auth_type}. Must be one of: {valid_types}",
                    }
                ],
            },
        )


def _validate_smart_credentials(auth_type: str, auth_credentials: dict | None) -> None:
    if auth_type != "smart":
        return
    required = {"client_id", "client_secret", "token_endpoint"}
    creds = auth_credentials or {}
    if not required.issubset(creds.keys()):
        raise HTTPException(
            status_code=400,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "invalid",
                        "diagnostics": (
                            "SMART on FHIR requires client_id, client_secret, and token_endpoint in auth_credentials."
                        ),
                    }
                ],
            },
        )


def _cfg_to_response(cfg: CDRConfig) -> dict:
    return {
        "id": cfg.id,
        "name": cfg.name,
        "cdr_url": cfg.cdr_url,
        "auth_type": cfg.auth_type.value if isinstance(cfg.auth_type, AuthType) else cfg.auth_type,
        "is_active": cfg.is_active,
        "is_default": cfg.is_default,
        "is_read_only": cfg.is_read_only,
    }


# ---------------------------------------------------------------------------
# Connections endpoints
# ---------------------------------------------------------------------------


@router.get("/connections", response_model=list[CDRConnectionResponse])
async def list_connections(session: AsyncSession = Depends(get_session)) -> list:
    result = await session.execute(select(CDRConfig).order_by(CDRConfig.id))
    configs = result.scalars().all()
    return [_cfg_to_response(c) for c in configs]


@router.post("/connections", response_model=CDRConnectionResponse, status_code=201)
async def create_connection(
    body: CDRConnectionCreate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    _validate_auth_type(body.auth_type)
    _validate_smart_credentials(body.auth_type, body.auth_credentials)

    existing = await session.execute(select(CDRConfig).where(CDRConfig.name == body.name))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "conflict",
                        "diagnostics": f"A connection named '{body.name}' already exists.",
                    }
                ],
            },
        )

    cfg = CDRConfig(
        name=body.name,
        cdr_url=body.cdr_url,
        auth_type=AuthType(body.auth_type),
        auth_credentials=body.auth_credentials,
        is_read_only=body.is_read_only,
        is_active=False,
        is_default=False,
    )
    session.add(cfg)
    await session.commit()
    await session.refresh(cfg)
    return _cfg_to_response(cfg)


@router.get("/connections/{connection_id}", response_model=CDRConnectionResponse)
async def get_connection(
    connection_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    cfg = await session.get(CDRConfig, connection_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return _cfg_to_response(cfg)


@router.put("/connections/{connection_id}", response_model=CDRConnectionResponse)
async def update_connection(
    connection_id: int,
    body: CDRConnectionCreate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    cfg = await session.get(CDRConfig, connection_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    _validate_auth_type(body.auth_type)
    _validate_smart_credentials(body.auth_type, body.auth_credentials)

    duplicate = await session.execute(
        select(CDRConfig).where(CDRConfig.name == body.name, CDRConfig.id != connection_id)
    )
    if duplicate.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "conflict",
                        "diagnostics": f"A connection named '{body.name}' already exists.",
                    }
                ],
            },
        )

    cfg.name = body.name
    cfg.cdr_url = body.cdr_url
    cfg.auth_type = AuthType(body.auth_type)
    cfg.auth_credentials = body.auth_credentials
    cfg.is_read_only = body.is_read_only
    await session.commit()
    await session.refresh(cfg)
    return _cfg_to_response(cfg)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: int,
    session: AsyncSession = Depends(get_session),
) -> None:
    cfg = await session.get(CDRConfig, connection_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    if cfg.is_default:
        raise HTTPException(
            status_code=409,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "conflict",
                        "diagnostics": "Cannot delete the built-in Local CDR connection.",
                    }
                ],
            },
        )

    if cfg.is_active:
        raise HTTPException(
            status_code=409,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "conflict",
                        "diagnostics": "Cannot delete the active connection. Activate a different connection first.",
                    }
                ],
            },
        )

    await session.delete(cfg)
    await session.commit()


@router.post("/connections/{connection_id}/activate", response_model=CDRConnectionResponse)
async def activate_connection(
    connection_id: int,
    session: AsyncSession = Depends(get_session),
) -> dict:
    cfg = await session.get(CDRConfig, connection_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Connection not found")

    active_result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)))
    for active_cfg in active_result.scalars().all():
        active_cfg.is_active = False

    cfg.is_active = True
    await session.commit()
    await session.refresh(cfg)
    return _cfg_to_response(cfg)


# ---------------------------------------------------------------------------
# Test-connection endpoint
# ---------------------------------------------------------------------------


@router.post("/test-connection")
async def test_cdr_connection(body: TestConnectionRequest) -> dict:
    """Test connectivity to a FHIR server."""
    _validate_auth_type(body.auth_type)
    _validate_smart_credentials(body.auth_type, body.auth_credentials)
    try:
        result = await verify_fhir_connection(
            fhir_url=body.cdr_url,
            auth_type=body.auth_type,
            auth_credentials=body.auth_credentials,
        )
        return result
    except Exception as exc:
        logger.warning(
            "CDR connection test failed",
            extra={"cdr_url": body.cdr_url, "error": str(exc)},
        )
        raise HTTPException(
            status_code=502,
            detail={
                "resourceType": "OperationOutcome",
                "issue": [
                    {
                        "severity": "error",
                        "code": "exception",
                        "diagnostics": f"Connection failed: {sanitize_error(exc)}",
                    }
                ],
            },
        )
