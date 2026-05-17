"""Experimental Groups endpoints (issue #322).

Lists CQL-evaluatable Groups on the active CDR and invokes
`Group/<id>/$evaluate`. Architecturally independent from the Measure pipeline:
this module imports nothing from `app.orchestrator`, `app.routes.jobs`,
`app.routes.measures`, `app.routes.results`, or measure/job state modules.
"""

from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.dependencies import ConnectionContext, get_active_cdr
from app.models.app_setting import AppSetting
from app.services.fhir_client import (
    GroupEvaluateError,
    _build_auth_headers,
    evaluate_group_and_resolve_members,
    list_groups_with_expression,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/groups", tags=["groups"])

_GROUP_ID_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9_\-\.]{1,256}$")


async def _require_feature_enabled(session: AsyncSession) -> None:
    """Return 404 unless the `groups_enabled` admin setting is true."""
    row = await session.get(AppSetting, "groups_enabled")
    enabled = (row.value if row is not None else "false") == "true"
    if not enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("")
async def list_groups_endpoint(
    session: AsyncSession = Depends(get_session),
    cdr: ConnectionContext = Depends(get_active_cdr),
) -> dict:
    await _require_feature_enabled(session)
    auth_headers = await _build_auth_headers(cdr.auth_type, cdr.auth_credentials)
    try:
        groups = await list_groups_with_expression(cdr.cdr_url, auth_headers)
    except Exception:
        logger.exception("Failed to list groups for $evaluate page")
        raise HTTPException(
            status_code=502,
            detail="Cannot reach CDR to list groups. Check CDR connectivity in Settings.",
        )
    return {"groups": groups}


@router.post("/{group_id}/evaluate")
async def evaluate_group_endpoint(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    cdr: ConnectionContext = Depends(get_active_cdr),
) -> dict:
    await _require_feature_enabled(session)

    if not _GROUP_ID_RE.match(group_id):
        raise HTTPException(
            status_code=400,
            detail="group_id must be alphanumeric with hyphens, underscores, or dots only",
        )

    auth_headers = await _build_auth_headers(cdr.auth_type, cdr.auth_credentials)
    try:
        result = await evaluate_group_and_resolve_members(cdr.cdr_url, group_id, auth_headers)
    except GroupEvaluateError as exc:
        detail = {"error": str(exc), "status": exc.status_code}
        if exc.operation_outcome is not None:
            detail["operation_outcome"] = exc.operation_outcome
        raise HTTPException(status_code=502, detail=detail)
    except httpx.TimeoutException as exc:
        logger.warning("Group $evaluate timed out", extra={"group_id": group_id})
        raise HTTPException(status_code=504, detail={"error": str(exc)})
    except Exception:
        logger.exception("Group $evaluate unexpected failure", extra={"group_id": group_id})
        raise HTTPException(status_code=502, detail={"error": "Group $evaluate failed"})

    return result
