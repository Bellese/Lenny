"""FastAPI dependency for active CDR configuration."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models.config import AuthType, CDRConfig


@dataclass
class CDRContext:
    id: int
    name: str
    cdr_url: str
    auth_type: str  # AuthType value
    auth_credentials: dict | None
    is_default: bool
    is_read_only: bool


async def get_active_cdr(session: AsyncSession = Depends(get_session)) -> CDRContext:
    """FastAPI dependency: load the active CDR config from DB.

    Fallback to Local CDR defaults if no active row exists
    (defensive only — startup migration ensures a row).
    """
    result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        return CDRContext(
            id=0,
            name="Local CDR",
            cdr_url=settings.DEFAULT_CDR_URL,
            auth_type=AuthType.none,
            auth_credentials=None,
            is_default=True,
            is_read_only=False,
        )
    return CDRContext(
        id=cfg.id,
        name=cfg.name or "Local CDR",
        cdr_url=cfg.cdr_url,
        auth_type=cfg.auth_type,
        auth_credentials=cfg.auth_credentials,
        is_default=cfg.is_default,
        is_read_only=cfg.is_read_only,
    )
