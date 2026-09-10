"""Measure readiness: can the active MCS actually evaluate this measure?

Two questions, in order:
  1. Does `$data-requirements` succeed? That is the compile check — it is what
     fails when a Library the CQL includes is absent.
  2. Is every ValueSet canonical the server named actually present?

Lenny parses no CQL. The server computes the dependency closure and returns it.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.measure_readiness import MeasureReadiness, ReadinessState
from app.services.fhir_errors import FhirOperationOutcome, hint_for_network_exception

logger = logging.getLogger(__name__)


# HAPI's CQL engine reports an unresolvable include as:
#   "Could not load source for library Status, version 1.15.000, namespace uri null."
# It names only the FIRST one it cannot load, so a parsed result is a starting
# point, never a complete inventory of what is missing.
_MISSING_LIBRARY_RE = re.compile(r"Could not load source for library ([\w.\-]+), version ([\w.\-]+)")


def extract_valueset_canonicals(library: dict) -> list[str]:
    """Collect every ValueSet canonical named by a `$data-requirements` response.

    Two locations carry them: `dataRequirement[].codeFilter[].valueSet` and
    `relatedArtifact[]` entries of type `depends-on`. Version suffixes are
    stripped at `|` so presence can be checked by URL. `relatedArtifact` mixes
    Library, ValueSet, and CodeSystem canonicals; only ValueSet-shaped entries
    are collected.
    """
    found: set[str] = set()

    for requirement in library.get("dataRequirement") or []:
        for code_filter in requirement.get("codeFilter") or []:
            canonical = code_filter.get("valueSet")
            if canonical:
                found.add(canonical.split("|")[0])

    for artifact in library.get("relatedArtifact") or []:
        if artifact.get("type") != "depends-on":
            continue
        resource = artifact.get("resource") or ""
        # A positive test, not an exclusion list. `relatedArtifact` mixes Library,
        # ValueSet and CodeSystem canonicals, and a blocklist of known code systems
        # fails open: anything not on the list is admitted as a ValueSet and then
        # searched for as one, which can never match. Only ValueSet-shaped
        # canonicals are collected.
        if "/ValueSet/" not in resource:
            continue
        found.add(resource.split("|")[0])

    return sorted(found)


def extract_missing_libraries(diagnostic: str | None) -> list[str]:
    """Pull library names out of the engine's compile diagnostic.

    Returns `[]` when nothing matches — an unrecognised message is not evidence
    of a missing library, and the raw text is preserved in the verdict's `error`
    either way.
    """
    if not diagnostic:
        return []
    return [f"{name} {version}" for name, version in _MISSING_LIBRARY_RE.findall(diagnostic)]


async def reclaim_stranded_checks(session: AsyncSession) -> int:
    """Convert rows left in `checking` by a crashed sweep into `unknown`.

    Called at startup. `asyncio.create_task` does not survive a restart, so
    without this a killed container leaves a spinner that never resolves.
    Returns the number of rows reclaimed.
    """
    result = await session.execute(
        sa_update(MeasureReadiness)
        .where(MeasureReadiness.state == ReadinessState.checking)
        .values(state=ReadinessState.unknown, error="Interrupted by backend restart")
    )
    await session.commit()
    return result.rowcount or 0


@dataclass
class ReadinessVerdict:
    """The outcome of one measure's check. Maps 1:1 onto a `MeasureReadiness` row."""

    state: ReadinessState
    missing_libraries: list[str] = field(default_factory=list)
    missing_valuesets: list[str] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0


def _error_diagnostic(outcome: FhirOperationOutcome | None) -> str | None:
    """The first error/fatal diagnostic, or None if the outcome is only advisory.

    Warning and information issues accompany successful responses; treating them
    as failures is the naive mistake #415 documented on the submit_data path.
    """
    if outcome is None:
        return None
    for issue in outcome.issues:
        if issue.severity in ("error", "fatal"):
            return issue.diagnostics or "Server reported an error with no diagnostic."
    return None


async def find_missing_valuesets(
    mcs_url: str,
    canonicals: list[str],
    *,
    auth_headers: dict[str, str],
    timeout: float,
    chunk_size: int = 10,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Return the subset of `canonicals` the MCS does not hold.

    Chunked because a measure's closure runs to two dozen URLs and a single
    comma-joined query would outgrow practical URL limits. Raises on transport
    failure; the caller maps that to `unknown` rather than `not_ready`.

    Version suffixes are stripped from `canonicals` before comparison, on the
    same `|` convention as the server-returned URLs — callers are not required
    to pre-strip versions themselves.
    """
    if not canonicals:
        return []

    normalised = [c.split("|")[0] for c in canonicals]

    present: set[str] = set()
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for start in range(0, len(normalised), chunk_size):
            chunk = normalised[start : start + chunk_size]
            resp = await client.get(
                f"{mcs_url}/ValueSet",
                params={"url": ",".join(chunk), "_elements": "url", "_count": str(len(chunk))},
                headers=auth_headers,
            )
            resp.raise_for_status()
            for entry in resp.json().get("entry") or []:
                url = (entry.get("resource") or {}).get("url")
                if url:
                    present.add(url.split("|")[0])

    return [c for c in normalised if c not in present]


async def check_measure_readiness(
    mcs_url: str,
    measure_id: str,
    *,
    auth_headers: dict[str, str],
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ReadinessVerdict:
    """Decide whether `measure_id` can be evaluated on the MCS at `mcs_url`.

    Read-only. Never raises: every failure mode becomes a verdict, because the
    caller's job is to write a row, not to propagate an exception.

    No period parameters are sent. Whether the Library graph resolves does not
    depend on a measurement period, and the DEQM path already calls the
    operation bare.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return round((time.monotonic() - started) * 1000)

    dr_url = f"{mcs_url}/Measure/{measure_id}/$data-requirements"
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            resp = await client.get(dr_url, headers=auth_headers)
    except Exception as exc:
        return ReadinessVerdict(
            state=ReadinessState.unknown, error=hint_for_network_exception(exc), duration_ms=elapsed()
        )

    # Authentication failures are OUR problem, not the measure's. Checked before
    # the general non-2xx branch, which would otherwise call them not_ready.
    if resp.status_code in (401, 403):
        auth_error = (
            f"HTTP {resp.status_code}: the measure server refused the request. Check this connection's credentials."
        )
        return ReadinessVerdict(state=ReadinessState.unknown, error=auth_error, duration_ms=elapsed())

    outcome = FhirOperationOutcome.from_response(resp)
    diagnostic = _error_diagnostic(outcome)

    if not resp.is_success:
        message = diagnostic or f"HTTP {resp.status_code} from $data-requirements."
        return ReadinessVerdict(
            state=ReadinessState.not_ready,
            missing_libraries=extract_missing_libraries(message),
            error=message,
            duration_ms=elapsed(),
        )

    # A 2xx can still carry an error OperationOutcome instead of the Library.
    if diagnostic:
        return ReadinessVerdict(
            state=ReadinessState.not_ready,
            missing_libraries=extract_missing_libraries(diagnostic),
            error=diagnostic,
            duration_ms=elapsed(),
        )

    try:
        library = resp.json()
    except Exception:
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error="$data-requirements returned a body that is not JSON.",
            duration_ms=elapsed(),
        )

    # Valid JSON but not a Library — e.g. `null`, a bare array, a string. We got
    # no meaningful answer about the measure, so this is `unknown`, not `not_ready`.
    if not isinstance(library, dict):
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error="$data-requirements returned a JSON body that is not a FHIR resource.",
            duration_ms=elapsed(),
        )

    canonicals = extract_valueset_canonicals(library)
    try:
        missing = await find_missing_valuesets(
            mcs_url, canonicals, auth_headers=auth_headers, timeout=timeout, transport=transport
        )
    except Exception as exc:
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error=f"Could not verify value sets: {hint_for_network_exception(exc)}",
            duration_ms=elapsed(),
        )

    if missing:
        noun = "value set" if len(missing) == 1 else "value sets"
        return ReadinessVerdict(
            state=ReadinessState.not_ready,
            missing_valuesets=missing,
            error=f"{len(missing)} {noun} referenced by this measure are not on this server.",
            duration_ms=elapsed(),
        )

    return ReadinessVerdict(state=ReadinessState.ready, duration_ms=elapsed())


def _session_factory():
    """Indirection so tests can substitute the session without patching `app.db`.

    The sweep runs detached from any request, so it cannot take a `Depends`
    session — it must open its own.
    """
    from app.db import async_session

    return async_session()


async def claim_unchecked(session: AsyncSession, mcs_id: int, measures: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Insert `checking` rows for measures with no verdict yet; return what was claimed.

    Writing the row synchronously, before the sweep starts, is what stops a page
    refresh during a 60s sweep from queueing a second one.
    """
    existing = set(
        (
            await session.execute(
                select(MeasureReadiness.measure_id, MeasureReadiness.measure_version).where(
                    MeasureReadiness.mcs_id == mcs_id
                )
            )
        ).all()
    )
    claimed = [(mid, ver) for mid, ver in measures if (mid, ver) not in existing]
    for measure_id, version in claimed:
        session.add(
            MeasureReadiness(
                mcs_id=mcs_id, measure_id=measure_id, measure_version=version, state=ReadinessState.checking
            )
        )
    if claimed:
        await session.commit()
    return claimed


async def mark_all_checking(session: AsyncSession, mcs_id: int, measures: list[tuple[str, str]]) -> None:
    """Force every listed measure into `checking`, inserting rows that are absent.

    Used by the manual re-check, where the point is to discard current verdicts.
    """
    await invalidate_mcs(session, mcs_id)
    for measure_id, version in measures:
        session.add(
            MeasureReadiness(
                mcs_id=mcs_id, measure_id=measure_id, measure_version=version, state=ReadinessState.checking
            )
        )
    await session.commit()


async def invalidate_mcs(session: AsyncSession, mcs_id: int) -> int:
    """Drop every cached verdict for one MCS. Returns the number removed.

    Deliberately whole-connection rather than per-measure: an uploaded bundle can
    carry a Library that several OTHER measures were missing, so invalidating
    only the uploaded measure would leave those stale and red.
    """
    result = await session.execute(sa_delete(MeasureReadiness).where(MeasureReadiness.mcs_id == mcs_id))
    await session.commit()
    return result.rowcount or 0


async def _store_verdict(
    session: AsyncSession, mcs_id: int, measure_id: str, version: str, verdict: ReadinessVerdict
) -> None:
    row = (
        await session.execute(
            select(MeasureReadiness).where(
                MeasureReadiness.mcs_id == mcs_id,
                MeasureReadiness.measure_id == measure_id,
                MeasureReadiness.measure_version == version,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = MeasureReadiness(mcs_id=mcs_id, measure_id=measure_id, measure_version=version)
        session.add(row)
    row.state = verdict.state
    row.missing_libraries = verdict.missing_libraries or None
    row.missing_valuesets = verdict.missing_valuesets or None
    row.error = verdict.error
    row.duration_ms = verdict.duration_ms
    row.checked_at = datetime.now(timezone.utc)
    await session.commit()


async def run_sweep(mcs_id: int, measures: list[tuple[str, str]]) -> None:
    """Check every listed measure against one MCS and store the verdicts.

    Safe as an `asyncio.create_task` target: opens its own session and never
    raises. Concurrency is capped because `$data-requirements` is expensive
    enough to have OOM-killed the engine once already
    (`fhir_client.py:371-375`).

    The whole sweep shares one `AsyncSession` (opened once, not per-measure), and
    an `AsyncSession` cannot run two operations concurrently on itself. The
    semaphore below bounds how many `$data-requirements` calls are in flight at
    once; a second, separate lock serialises the (fast) database write that
    follows each one so two verdicts never call `commit()` at the same time.
    """
    from app.dependencies import resolve_mcs_auth_headers
    from app.models.mcs_config import MCSConfig

    if not measures:
        return

    semaphore = asyncio.Semaphore(max(1, settings.READINESS_CONCURRENCY))
    write_lock = asyncio.Lock()

    async with _session_factory() as session:
        cfg = await session.get(MCSConfig, mcs_id)
        if cfg is None:
            logger.warning("Readiness sweep skipped: MCS %s no longer exists", mcs_id)
            return
        mcs_url = cfg.mcs_url
        try:
            auth_headers = await resolve_mcs_auth_headers(
                session, mcs_id=mcs_id, mcs_url=mcs_url, mcs_auth_type=cfg.auth_type.value, owner_label=f"MCS {mcs_id}"
            )
        except Exception as exc:
            logger.warning("Readiness sweep could not authenticate to MCS %s: %s", mcs_id, exc)
            auth_headers = {}

        async def one(measure_id: str, version: str) -> None:
            async with semaphore:
                try:
                    verdict = await check_measure_readiness(
                        mcs_url,
                        measure_id,
                        auth_headers=auth_headers,
                        timeout=float(settings.READINESS_TIMEOUT_SECONDS),
                    )
                except Exception as exc:
                    # A raising check must not leave the row spinning until restart.
                    logger.exception("Readiness check raised for %s", measure_id)
                    verdict = ReadinessVerdict(state=ReadinessState.unknown, error=f"Check failed: {exc}")
            async with write_lock:
                await _store_verdict(session, mcs_id, measure_id, version, verdict)

        await asyncio.gather(*(one(mid, ver) for mid, ver in measures))

    logger.info("Readiness sweep complete", extra={"mcs_id": mcs_id, "measures": len(measures)})
