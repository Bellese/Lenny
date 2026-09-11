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
from urllib.parse import urljoin

import httpx
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.measure_readiness import MeasureReadiness, ReadinessState
from app.services.fhir_client import _same_origin
from app.services.fhir_errors import (
    FhirOperationOutcome,
    _sanitize_str,
    hint_for_network_exception,
    sanitize_url,
)
from app.services.validation import sanitize_error

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
    """DELETE rows left in `checking` by a crashed sweep. Returns how many.

    Called at startup. `asyncio.create_task` does not survive a restart, so
    without this a killed container leaves a spinner that never resolves.

    Deleting rather than marking them `unknown` is deliberate: `claim_unchecked`
    only claims measures with NO row, so an `unknown` row is terminal — the next
    page load would skip it and the measure would read "Not checked" forever,
    for a reason (a deploy-time restart) that has nothing to do with the
    measure. With the row gone, the next `GET /measures` re-claims it and the
    sweep runs again automatically. Nothing of value is lost: the row held only
    a `checking` placeholder, and the message the old code wrote was never
    rendered anywhere.
    """
    result = await session.execute(sa_delete(MeasureReadiness).where(MeasureReadiness.state == ReadinessState.checking))
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
    """The first error/fatal diagnostic, REDACTED, or None if only advisory.

    Warning and information issues accompany successful responses; treating them
    as failures is the naive mistake #415 documented on the submit_data path.

    Redacted here rather than at the display layer because this is the single
    point every caller reads the diagnostic through, and what the callers do
    with it is persist it: the text lands in `measure_readiness.error` and is
    rendered verbatim in the UI. The MCS is third-party by design (connectathon
    servers, BYO CDRs) and `redact_outcome` records why its text is not trusted
    — HAPI echoes failed request bodies into diagnostics, which can carry the
    Authorization header we just sent it. `_sanitize_str` is the same primitive
    `build_error_envelope` already applies to `issue[].diagnostics`.

    `extract_missing_libraries` is unaffected: it matches a library name and
    version, neither of which any redaction pattern touches.
    """
    if outcome is None:
        return None
    for issue in outcome.issues:
        if issue.severity in ("error", "fatal"):
            return (
                _sanitize_str(issue.diagnostics)
                if issue.diagnostics
                else "Server reported an error with no diagnostic."
            )
    return None


# `_count` is a PAGE SIZE, not a cap on how many resources an OR match may hit.
# FHIR stores each `(url, version)` pair as its own resource, so a server loaded
# from several MADiE bundles routinely holds one VSAC canonical at two or three
# versions: ten requested canonicals can legitimately match thirty resources.
# Sizing the page to `len(chunk)` truncated that and reported PRESENT value sets
# as missing — a false `not_ready`. The page is therefore sized generously AND
# `link[relation=next]` is followed, so the number is a round-trip optimisation
# rather than something correctness depends on.
_VALUESET_PAGE_FACTOR = 10
# A server that returns a `next` link pointing at itself would otherwise spin
# forever inside a check that is supposed to time out and return `unknown`.
_VALUESET_MAX_PAGES = 50


def _next_page_url(bundle: dict) -> str | None:
    """The `next` link of a searchset Bundle, or None when this is the last page."""
    for link in bundle.get("link") or []:
        if link.get("relation") == "next" and link.get("url"):
            return str(link["url"])
    return None


class PaginationBudgetExceededError(RuntimeError):
    """Raised when the page walk hits `_VALUESET_MAX_PAGES` with a `next` outstanding.

    Same reasoning as `UnsafePaginationLinkError`, from the other exit of the
    same loop: this function's result is a MISSING list, so stopping the walk
    early and returning what has been seen so far reports every canonical on
    the unread remainder as absent — a false `not_ready`, which tells an
    operator their working server is broken. The budget exists to stop a
    self-referential `next` link spinning forever, not to license a wrong
    answer; hitting it means we do not know, so the caller maps it to
    `unknown`.
    """


class UnsafePaginationLinkError(RuntimeError):
    """Raised when a ValueSet search's `next` link points off the MCS's origin.

    The MCS here is third-party by design (connectathon servers, BYO CDRs), so
    its response is not trusted input. `_same_origin` (fhir_client.py) is what
    blocks the SSRF: without it, a hostile or misconfigured server could point
    `next` at an internal host and this function would fetch it WITH
    `auth_headers` attached, handing over the MCS connection's credentials.

    Raised rather than silently stopping the page walk, unlike the other
    `_same_origin` call sites in `fhir_client.py`: those return best-effort
    partial data, but this function's result is a MISSING list. Silently
    truncating it would report every canonical on the unread remainder as
    absent — a false `not_ready`, which incorrectly tells an operator their
    working server is broken. `check_measure_readiness` already raises on
    transport failure and maps it to `unknown`; this reuses that path so an
    origin-mismatch produces the same honest "we don't know" instead of a
    wrong verdict in either direction.
    """


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
    failure, and on an off-origin `next` link (see `UnsafePaginationLinkError`);
    the caller maps both to `unknown` rather than `not_ready`.

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
            next_url: str | None = f"{mcs_url}/ValueSet"
            params: dict[str, str] | None = {
                "url": ",".join(chunk),
                "_elements": "url",
                "_count": str(len(chunk) * _VALUESET_PAGE_FACTOR),
            }
            for _ in range(_VALUESET_MAX_PAGES):
                resp = await client.get(next_url, params=params, headers=auth_headers)
                resp.raise_for_status()
                bundle = resp.json()
                for entry in bundle.get("entry") or []:
                    url = (entry.get("resource") or {}).get("url")
                    if url:
                        present.add(url.split("|")[0])
                # A `next` link already carries the whole query, filter included;
                # re-appending `params` would duplicate it.
                candidate = _next_page_url(bundle)
                if candidate is not None:
                    # Relative and protocol-relative links are resolved against
                    # `mcs_url` BEFORE the origin check, never after. A server is
                    # entitled to emit `next` as `/fhir?_getpages=...` (scheme
                    # `''`, hostname `None`), which the origin check would
                    # otherwise reject as a mismatch — a false `unknown` on a
                    # perfectly good server. Keeping the check on the far side of
                    # the join is what stops that leniency from opening a hole:
                    # `//evil.example/x` resolves to `https://evil.example/x`,
                    # which is exactly the off-origin link the guard must refuse.
                    try:
                        resolved: str | None = urljoin(mcs_url, candidate)
                    except ValueError:
                        resolved = None
                    if resolved is None or not _same_origin(mcs_url, resolved):
                        logger.warning(
                            "SSRF: readiness ValueSet pagination next link rejected (origin mismatch)",
                            extra={"mcs_url": sanitize_url(mcs_url), "next_url": sanitize_url(candidate)},
                        )
                        # Deliberately no URL in the text. This string is
                        # persisted to `measure_readiness.error`, returned by
                        # `GET /measures` and rendered in the UI, and
                        # `sanitize_url` only redacts DOTLESS hosts — so
                        # `http://10.0.2.15:8080/fhir` and
                        # `http://mcs.internal.corp/fhir` would pass through
                        # intact, undoing on the readiness path exactly what the
                        # measures route refuses to publish on its 200 path. The
                        # operator already knows which connection they are
                        # looking at.
                        raise UnsafePaginationLinkError(
                            "The measure server returned a ValueSet page link pointing to a different "
                            "origin than the configured measure server; refusing to follow it with "
                            "credentials attached."
                        )
                    candidate = resolved
                next_url = candidate
                params = None
                if next_url is None:
                    break
            else:
                # Budget exhausted with a `next` link still outstanding: see
                # `PaginationBudgetExceededError`. Falling out of the loop here
                # is what would produce the false `not_ready`.
                raise PaginationBudgetExceededError(
                    f"The measure server's ValueSet search did not finish within {_VALUESET_MAX_PAGES} "
                    "pages, so the results are incomplete."
                )

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

    # A redirect is not evidence about the measure. `follow_redirects` is off
    # (deliberately — an MCS response must not be able to steer a credentialed
    # request somewhere else), so a 3xx arrives here intact and would otherwise
    # fall into the non-2xx branch below and mark EVERY measure on the
    # connection `not_ready`. The commonest causes are entirely benign and have
    # nothing to do with CQL: an http→https upgrade or a trailing-slash
    # normalisation in a proxy in front of the MCS. We never saw an answer, so
    # the answer is `unknown`.
    if 300 <= resp.status_code < 400:
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error=(
                f"HTTP {resp.status_code}: the measure server redirected $data-requirements. "
                "Check this connection's URL."
            ),
            duration_ms=elapsed(),
        )

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

    # A dict is not enough. Anything dict-shaped with no `dataRequirement` and no
    # `relatedArtifact` yields zero canonicals, zero missing, and would fall
    # through to `ready` — a false READY, the one output this feature must never
    # produce. Reachable without any non-compliance: a `Parameters` wrapper
    # around the Library (a common server variation), an OperationOutcome
    # carrying only information/warning issues (`_error_diagnostic` returns None
    # for those, by design), or any 2xx body from a proxy that is not the
    # resource. We learned nothing about the measure, so this is `unknown`.
    if library.get("resourceType") != "Library":
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error="$data-requirements did not return a Library resource.",
            duration_ms=elapsed(),
        )

    # `resourceType == "Library"` is not enough either, and this is the same
    # false-READY hole one layer in. A Library carrying NEITHER
    # `dataRequirement` NOR `relatedArtifact` — absent, or present and empty —
    # yields zero canonicals, `find_missing_valuesets` short-circuits on the
    # empty list without a single network call, `missing` is falsy, and the
    # function returns `ready`: a green verdict produced without one byte of
    # evidence about the measure. Real producers are mundane, not hostile — a
    # Measure with no `library` element, a Library whose content attachment is
    # empty or went unparsed, a gateway serving a cached stub.
    #
    # A genuine measure's `$data-requirements` always declares SOMETHING: the
    # operation's whole purpose is to return the dependency closure the engine
    # computed. A response that declares no dependencies at all is therefore
    # telling us nothing about evaluability rather than telling us there is
    # nothing to check, and `unknown` is the only honest reading. The empty-array
    # case is covered on purpose: a `module-definition`-typed Library with
    # `dataRequirement: []` is shaped exactly like a valid answer and is the
    # variant a resourceType-and-profile check would wave through.
    if not library.get("dataRequirement") and not library.get("relatedArtifact"):
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error=(
                "$data-requirements returned a Library that declares no data requirements and no "
                "related artifacts, so nothing about this measure could be verified."
            ),
            duration_ms=elapsed(),
        )

    # A body that IS a Library but whose declared fields are the wrong shape
    # (`dataRequirement` a string, a `codeFilter` entry that is not an object)
    # makes `extract_valueset_canonicals` raise. Guarded here so this function
    # honours its own "never raises" contract standalone, rather than only
    # because `run_sweep` happens to wrap the call.
    #
    # Deliberately NOT fixed by teaching the extractor to skip malformed
    # entries: skipping yields zero canonicals, zero missing, and falls through
    # to `ready` — a false READY, the one output this feature must never
    # produce. A shape we cannot parse means we learned nothing, so: `unknown`.
    try:
        canonicals = extract_valueset_canonicals(library)
    except Exception as exc:
        logger.warning("Readiness: $data-requirements Library had an unparseable shape: %s", exc)
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error="$data-requirements returned a Library whose dependency fields are not the expected shape.",
            duration_ms=elapsed(),
        )

    try:
        missing = await find_missing_valuesets(
            mcs_url, canonicals, auth_headers=auth_headers, timeout=timeout, transport=transport
        )
    except (UnsafePaginationLinkError, PaginationBudgetExceededError) as exc:
        # A distinct branch so the operator sees the real reason (a rejected
        # off-origin next link, or a page walk that outran its budget) instead
        # of `hint_for_network_exception`'s generic transport-failure text,
        # which does not apply to either. Both are `unknown`, never
        # `not_ready`: in both cases part of the ValueSet search went unread,
        # so the missing list is incomplete by construction.
        return ReadinessVerdict(
            state=ReadinessState.unknown,
            error=f"Could not verify value sets: {exc}",
            duration_ms=elapsed(),
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

    The SELECT-then-INSERT above is not atomic: two concurrent callers (two
    `GET /measures` requests on separate sessions) can both read a measure as
    unclaimed and both attempt to insert it, and one loses to
    `uq_measure_readiness_key`. That must never surface as an error — it must
    never abort the whole batch (a page load with 9 uncontended measures and 1
    collision must still claim and sweep the other 8), and it must never look
    like an MCS connectivity failure to the route above.

    Each candidate is therefore inserted inside its own SAVEPOINT
    (`begin_nested`), flushed immediately so a unique-constraint violation
    surfaces right there rather than at the final `commit()` where every
    savepoint in the batch would already be tangled together. A failing
    savepoint rolls back to before its own INSERT and moves on to the next
    candidate; every other row's SAVEPOINT is independent and still commits.
    Losing the race is not a problem for the loser: whoever won already
    fired the sweep for that measure.
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
    candidates = [(mid, ver) for mid, ver in measures if (mid, ver) not in existing]
    claimed: list[tuple[str, str]] = []
    for measure_id, version in candidates:
        try:
            async with session.begin_nested():
                session.add(
                    MeasureReadiness(
                        mcs_id=mcs_id, measure_id=measure_id, measure_version=version, state=ReadinessState.checking
                    )
                )
                await session.flush()
        except IntegrityError:
            # Lost the race for this one row: another caller claimed
            # (mcs_id, measure_id, version) between our SELECT and this INSERT.
            # It already fired its own sweep, so there is nothing more to do.
            continue
        claimed.append((measure_id, version))
    if claimed:
        await session.commit()
    return claimed


async def mark_all_checking(session: AsyncSession, mcs_id: int, measures: list[tuple[str, str]]) -> None:
    """Force every listed measure into `checking`, inserting rows that are absent.

    Used by the manual re-check, where the point is to discard current verdicts.

    Each insert gets its own SAVEPOINT for the reason `claim_unchecked`
    documents at length: `uq_measure_readiness_key` is racy by construction and
    losing that race must never surface as an error. Here the window is between
    this call's `invalidate_mcs` DELETE and its INSERTs — a double-clicked
    Re-check, a second tab, or a retry runs both halves twice and the second
    flow's INSERT can land on a row the first already wrote. Unguarded, that
    `IntegrityError` escapes `refresh_readiness` (which does not catch it) as
    FastAPI's default 500, breaking the `OperationOutcome` shape every other
    path on that router returns. Losing the race costs nothing: the winner
    wrote the same `checking` placeholder and fired its own sweep.
    """
    await invalidate_mcs(session, mcs_id)
    for measure_id, version in measures:
        try:
            async with session.begin_nested():
                session.add(
                    MeasureReadiness(
                        mcs_id=mcs_id, measure_id=measure_id, measure_version=version, state=ReadinessState.checking
                    )
                )
                await session.flush()
        except IntegrityError:
            continue
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
    """Update the `checking` row this sweep is filling in. Never inserts.

    Update-only is load-bearing, not tidiness. Every sweep's rows are created
    first, synchronously, by `claim_unchecked` or `mark_all_checking`, so the
    only way the row can be gone by the time the verdict lands is that someone
    deliberately deleted it underneath us — which is exactly what
    `invalidate_mcs` does when the user uploads the missing Library, deletes a
    measure, or repoints the connection. Re-inserting there would resurrect the
    PRE-upload verdict, and since `claim_unchecked` only claims measures with no
    row and verdicts have no TTL, that stale red would be permanent. Dropping
    the write instead leaves no row, so the next page load re-claims and
    re-checks.

    It also closes the unique-violation window between `invalidate_mcs`'s commit
    and `mark_all_checking`'s inserts: a double-clicked Re-check can no longer
    have an in-flight sweep insert a row that the second re-check then collides
    with.
    """
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
        logger.info(
            "Readiness verdict dropped: the row was invalidated while the sweep ran",
            extra={"mcs_id": mcs_id, "measure_id": measure_id},
        )
        return
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
            # Do NOT degrade to an anonymous sweep. Every verdict downstream is
            # derived from whatever view of the server the headers buy, so
            # continuing with `{}` measures the ANONYMOUS view and then labels
            # the rows as if they described the user's connection. Against a
            # HAPI that permits unauthenticated reads — the common connectathon
            # setup — that produces perfectly plausible `ready` rows for a
            # dataset the user's credentials would never have been shown: the
            # false READY this feature must never emit, and the one no operator
            # can detect by looking at it. `resolve_mcs_auth_headers` raises
            # rather than degrading for exactly this reason; a sweep that
            # swallows the raise re-opens what it was protecting.
            #
            # The credential failure is ours, not the measure's, so it is
            # `unknown` for every measure in the sweep — written, not left
            # spinning, because `claim_unchecked` skips rows that already exist
            # and a `checking` row nothing ever fills in is a spinner until
            # restart. `sanitize_error`: the raw text of a SMART token-endpoint
            # or TLS failure carries the URL, credentials and internal hostnames
            # included, straight into a stored and rendered `error`.
            logger.warning("Readiness sweep could not authenticate to MCS %s: %s", mcs_id, exc)
            verdict = ReadinessVerdict(
                state=ReadinessState.unknown,
                error=(
                    "Could not authenticate to the measure server, so readiness could not be checked. "
                    f"Check this connection's credentials. ({sanitize_error(exc)})"
                ),
            )
            for measure_id, version in measures:
                try:
                    await _store_verdict(session, mcs_id, measure_id, version, verdict)
                except Exception:
                    logger.exception("Could not store readiness auth-failure verdict for %s", measure_id)
                    try:
                        await session.rollback()
                    except Exception:
                        logger.exception("Rollback after a failed readiness write also failed")
            return

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
                    # `sanitize_error`, not `{exc}`: this branch exists for the
                    # exceptions nothing else vetted, and the raw text of an
                    # httpx/SSL failure carries the MCS URL — credentials and
                    # internal hostnames included — straight into a stored,
                    # rendered `measure_readiness.error`.
                    logger.exception("Readiness check raised for %s", measure_id)
                    verdict = ReadinessVerdict(
                        state=ReadinessState.unknown, error=f"Check failed: {sanitize_error(exc)}"
                    )
            async with write_lock:
                # The write is guarded too. Unguarded, one DB fault (pool
                # exhaustion, a dropped connection) escaped `gather`, the
                # `async with _session_factory()` below closed the session out
                # from under every sibling task still awaiting it, and their
                # rows stayed `checking` forever — `claim_unchecked` skips rows
                # that exist, so no later page load rescues them.
                try:
                    await _store_verdict(session, mcs_id, measure_id, version, verdict)
                except Exception:
                    logger.exception("Could not store readiness verdict for %s", measure_id)
                    # A failed commit leaves the transaction in a state where
                    # every subsequent write on this session fails too, so the
                    # siblings queued behind this lock need it rolled back.
                    try:
                        await session.rollback()
                    except Exception:
                        logger.exception("Rollback after a failed readiness write also failed")

        # `return_exceptions=True` so a task that raises somewhere outside the
        # two guards above still cannot cancel its siblings mid-flight.
        results = await asyncio.gather(*(one(mid, ver) for mid, ver in measures), return_exceptions=True)
        for (measure_id, _version), result in zip(measures, results):
            if isinstance(result, BaseException):
                logger.error("Readiness task failed for %s: %s", measure_id, result)

    logger.info("Readiness sweep complete", extra={"mcs_id": mcs_id, "measures": len(measures)})
