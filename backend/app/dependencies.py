"""FastAPI dependencies for active connection configurations.

`ConnectionContext` is the canonical dataclass returned by `get_active_<kind>`
dependencies. Future kinds (MCS, TS, MR, MRR) reuse this shape with the `kind`
field disambiguating which connection-management table the row came from.

`CDRContext` remains as a backwards-compatible alias so existing imports
(`from app.dependencies import CDRContext, get_active_cdr`) keep working
unchanged. The alias will be removed once all call sites migrate to
`ConnectionContext` (a follow-up PR).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models.config import AuthType, CDRConfig
from app.models.connection_base import ConnectionKind


@dataclass
class ConnectionContext:
    """Active-connection snapshot loaded by `get_active_<kind>` dependencies.

    `cdr_url` retains its kind-prefixed name for backwards compatibility with
    existing route handlers (`jobs.py`, `health.py`). When MCS lands the
    dataclass will gain a parallel `mcs_url` field. Generic-`url` normalization
    is deferred until kind #3 (Terminology Server) per the design doc.
    """

    id: int
    name: str
    cdr_url: str
    auth_type: str  # AuthType value
    auth_credentials: dict | None
    is_default: bool
    is_read_only: bool
    request_timeout_seconds: int = 30
    kind: ConnectionKind = ConnectionKind.cdr


# Backwards-compat alias. Removed once call sites migrate to ConnectionContext.
CDRContext = ConnectionContext


async def get_active_cdr(session: AsyncSession = Depends(get_session)) -> ConnectionContext:
    """FastAPI dependency: load the active CDR config from DB.

    Fallback to Local CDR defaults if no active row exists
    (defensive only — startup migration ensures a row).
    """
    result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        return ConnectionContext(
            id=0,
            name="Local CDR",
            cdr_url=settings.DEFAULT_CDR_URL,
            auth_type=AuthType.none,
            auth_credentials=None,
            is_default=True,
            is_read_only=False,
            request_timeout_seconds=30,
            kind=ConnectionKind.cdr,
        )
    return ConnectionContext(
        id=cfg.id,
        name=cfg.name or "Local CDR",
        cdr_url=cfg.cdr_url,
        auth_type=cfg.auth_type,
        auth_credentials=cfg.auth_credentials,
        is_default=cfg.is_default,
        is_read_only=cfg.is_read_only,
        request_timeout_seconds=cfg.request_timeout_seconds,
        kind=ConnectionKind.cdr,
    )
