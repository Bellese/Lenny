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


async def resolve_job_mcs_auth_headers(session: AsyncSession, job_id: int) -> dict[str, str]:
    """Resolve a job's MCS credentials from the live config, using a caller's session.

    Extracted from `orchestrator._get_mcs_auth_headers` (issue #397) so a request
    handler can resolve a job's credentials on the request's own session instead of
    opening a second one. The orchestrator wrapper now delegates here, keeping ONE
    implementation of the URL-drift guard below — duplicating a check that decides
    whether to hand credentials to a host is exactly the kind of thing that rots
    apart.

    `Job.mcs_id` is ON DELETE SET NULL, so `mcs_id is None` means EITHER "this job
    never had an MCS config" OR "the config was deleted after creation". The
    snapshotted `mcs_auth_type` is what tells them apart: without that check, a job
    whose connection was deleted would silently run unauthenticated against the
    still-snapshotted `mcs_url`.
    """
    # Imported here rather than at module scope: app.models.job imports nothing from
    # this module today, but dependencies.py is imported by nearly every route, and
    # keeping its import surface narrow is what stops a future cycle.
    from app.models.job import Job
    from app.services.fhir_client import _build_auth_headers

    job = await session.get(Job, job_id)
    if job is None:
        return {}
    if job.mcs_id is None:
        if not job.mcs_auth_type or job.mcs_auth_type == "none":
            return {}
        raise RuntimeError(
            f"Job {job_id} has no mcs_id — MCS config was deleted after job creation. Cannot fetch auth credentials."
        )
    cfg = await session.get(MCSConfig, job.mcs_id)
    if cfg is None:
        # Defensive: unreachable under the ON DELETE SET NULL FK, but a database
        # without the constraint enforced would land here.
        raise RuntimeError(f"MCS config {job.mcs_id} referenced by job {job_id} no longer exists.")
    if job.mcs_url and cfg.mcs_url != job.mcs_url:
        # The URL comes from the job snapshot but credentials are read live, so a
        # config repointed at a different host after job creation would hand the new
        # host's token to the old one.
        raise RuntimeError(
            f"MCS config {job.mcs_id} now points at a different server than job {job_id} "
            "was created against. Refusing to send its credentials to the snapshotted URL."
        )
    return await _build_auth_headers(cfg.auth_type, cfg.auth_credentials)


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
    is_read_only: bool = False  # Shared across kinds (issue #396) — blocks writes
    # MCS-only (issue #392). True = a job full-wipes the target before running;
    # False = it wipes only the patients it is about to push. Always False for a
    # CDR context, which is never wiped as part of a job.
    wipe_before_job: bool = False
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
            # The built-in local measure engine is writable — uploading and
            # deleting measure bundles against it is the whole point.
            is_read_only=False,
            # ...and it is Lenny's own container, so the historical full wipe is
            # correct here (issue #392). This fallback fires on a stock install
            # before the seed has run; defaulting it to False would silently
            # change local behavior for exactly the users who never configured
            # anything.
            wipe_before_job=True,
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
        is_read_only=cfg.is_read_only,
        wipe_before_job=cfg.wipe_before_job,
        request_timeout_seconds=cfg.request_timeout_seconds,
        kind=ConnectionKind.mcs,
    )
