"""Health check endpoint."""

import logging

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.dependencies import CDRContext, get_active_cdr
from app.services.validation import sanitize_error

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(
    session: AsyncSession = Depends(get_session),
    cdr: CDRContext = Depends(get_active_cdr),
) -> dict:
    """Check connectivity to database, measure engine, and CDR."""
    status: dict = {
        "status": "healthy",
        "database": {"status": "unknown"},
        "measure_engine": {"status": "unknown"},
        "cdr": {"status": "unknown"},
    }

    # Database check
    try:
        await session.execute(text("SELECT 1"))
        status["database"] = {"status": "connected"}
    except Exception as exc:
        status["database"] = {"status": "disconnected", "error": sanitize_error(exc)[:200]}
        status["status"] = "degraded"

    # Measure engine check
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.MEASURE_ENGINE_URL}/metadata")
            if resp.status_code == 200:
                status["measure_engine"] = {"status": "connected"}
            else:
                status["measure_engine"] = {
                    "status": "disconnected",
                    "error": f"HTTP {resp.status_code}",
                }
                status["status"] = "degraded"
    except Exception as exc:
        status["measure_engine"] = {"status": "disconnected", "error": sanitize_error(exc)[:200]}
        status["status"] = "degraded"

    # CDR check
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{cdr.cdr_url}/metadata")
            if resp.status_code == 200:
                status["cdr"] = {"status": "connected", "name": cdr.name}
            else:
                status["cdr"] = {
                    "status": "disconnected",
                    "name": cdr.name,
                    "error": f"HTTP {resp.status_code}",
                }
                status["status"] = "degraded"
    except Exception as exc:
        status["cdr"] = {"status": "disconnected", "name": cdr.name, "error": sanitize_error(exc)[:200]}
        status["status"] = "degraded"

    return status
