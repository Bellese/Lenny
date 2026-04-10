"""CDR settings management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models.config import AuthType, CDRConfig
from app.services.fhir_client import verify_fhir_connection
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CDRSettingsResponse(BaseModel):
    id: int | None = None
    cdr_url: str
    auth_type: str
    is_active: bool

    model_config = {"from_attributes": True}


class CDRSettingsUpdate(BaseModel):
    cdr_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None


class TestConnectionRequest(BaseModel):
    cdr_url: str
    auth_type: str = "none"
    auth_credentials: dict | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=CDRSettingsResponse)
async def get_settings(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return the active CDR configuration."""
    result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
    config = result.scalar_one_or_none()

    if config:
        return {
            "id": config.id,
            "cdr_url": config.cdr_url,
            "auth_type": config.auth_type.value if isinstance(config.auth_type, AuthType) else config.auth_type,
            "is_active": config.is_active,
        }

    # No config yet — return defaults
    return {
        "id": None,
        "cdr_url": settings.DEFAULT_CDR_URL,
        "auth_type": "none",
        "is_active": True,
    }


@router.put("", response_model=CDRSettingsResponse)
async def update_settings(
    body: CDRSettingsUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Update the active CDR configuration.

    Deactivates any existing configs and creates/updates the active one.
    """
    # Validate auth_type
    try:
        auth_type_enum = AuthType(body.auth_type)
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
                        "diagnostics": f"Invalid auth_type: {body.auth_type}. Must be one of: {valid_types}",
                    }
                ],
            },
        )

    # Deactivate existing configs
    result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)))
    existing_configs = result.scalars().all()
    for cfg in existing_configs:
        cfg.is_active = False

    # Create new config
    new_config = CDRConfig(
        cdr_url=body.cdr_url,
        auth_type=auth_type_enum,
        auth_credentials=body.auth_credentials,
        is_active=True,
    )
    session.add(new_config)
    await session.commit()
    await session.refresh(new_config)

    logger.info("CDR settings updated", extra={"cdr_url": body.cdr_url})

    return {
        "id": new_config.id,
        "cdr_url": new_config.cdr_url,
        "auth_type": new_config.auth_type.value if isinstance(new_config.auth_type, AuthType) else new_config.auth_type,
        "is_active": new_config.is_active,
    }


@router.post("/test-connection")
async def test_cdr_connection(body: TestConnectionRequest) -> dict:
    """Test connectivity to a FHIR server."""
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
