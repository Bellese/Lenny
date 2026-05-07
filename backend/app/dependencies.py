"""FastAPI dependencies for active connection configurations.

`ConnectionContext` is the canonical dataclass returned by `get_active_<kind>`
dependencies. Each kind populates its own URL field (`cdr_url`, `mcs_url`)
and leaves the others empty; `kind` disambiguates which is meaningful.

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
from app.models.mcs_config import MCSConfig


@dataclass
class ConnectionContext:
    """Active-connection snapshot loaded by `get_active_<kind>` dependencies.

    Per the doc-locked decision (eng review 1.5): each kind keeps its own
    kind-prefixed URL field (`cdr_url`, `mcs_url`) instead of a generic `url`.
    For a CDR context, `cdr_url` is set and `mcs_url` is empty; for an MCS
    context, the reverse. Read `kind` to know which one to use, or use the
    `url` property for kind-agnostic access.

    Generic-`url` normalization is deferred until kind #3 (Terminology Server).
    """

    id: int
    name: str
    auth_type: str  # AuthType value
    auth_credentials: dict | None
    is_default: bool
    cdr_url: str = ""
    mcs_url: str = ""
    is_read_only: bool = False  # CDR-specific; default for other kinds
    request_timeout_seconds: int = 30
    kind: ConnectionKind = ConnectionKind.cdr

    @property
    def url(self) -> str:
        """Kind-agnostic URL accessor — returns the populated kind-prefixed URL."""
        if self.kind == ConnectionKind.cdr:
            return self.cdr_url
        if self.kind == ConnectionKind.mcs:
            return self.mcs_url
        raise ValueError(f"Unknown ConnectionKind: {self.kind}")


# Backwards-compat alias. Removed once call sites migrate to ConnectionContext.
CDRContext = ConnectionContext


async def get_active_cdr(session: AsyncSession = Depends(get_session)) -> ConnectionContext:
    """FastAPI dependency: load the active CDR config from DB.

    Fallback to Local CDR defaults if no active row exists
    (defensive only — startup seed ensures a row).
    """
    result = await session.execute(select(CDRConfig).where(CDRConfig.is_active.is_(True)).limit(1))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        return ConnectionContext(
            id=0,
            name="Local CDR",
            auth_type=AuthType.none,
            auth_credentials=None,
            is_default=True,
            cdr_url=settings.DEFAULT_CDR_URL,
            is_read_only=False,
            request_timeout_seconds=30,
            kind=ConnectionKind.cdr,
        )
    return ConnectionContext(
        id=cfg.id,
        name=cfg.name or "Local CDR",
        auth_type=cfg.auth_type,
        auth_credentials=cfg.auth_credentials,
        is_default=cfg.is_default,
        cdr_url=cfg.cdr_url,
        is_read_only=cfg.is_read_only,
        request_timeout_seconds=cfg.request_timeout_seconds,
        kind=ConnectionKind.cdr,
    )


async def get_active_mcs(session: AsyncSession = Depends(get_session)) -> ConnectionContext:
    """FastAPI dependency: load the active MCS config from DB.

    Fallback to Local Measure Engine defaults if no active row exists
    (defensive only — startup seed ensures a row).
    """
    result = await session.execute(select(MCSConfig).where(MCSConfig.is_active.is_(True)).limit(1))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        return ConnectionContext(
            id=0,
            name="Local Measure Engine",
            auth_type=AuthType.none,
            auth_credentials=None,
            is_default=True,
            mcs_url=settings.MEASURE_ENGINE_URL,
            request_timeout_seconds=30,
            kind=ConnectionKind.mcs,
        )
    return ConnectionContext(
        id=cfg.id,
        name=cfg.name or "Local Measure Engine",
        auth_type=cfg.auth_type,
        auth_credentials=cfg.auth_credentials,
        is_default=cfg.is_default,
        mcs_url=cfg.mcs_url,
        request_timeout_seconds=cfg.request_timeout_seconds,
        kind=ConnectionKind.mcs,
    )
