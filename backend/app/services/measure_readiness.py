"""Measure readiness: can the active MCS actually evaluate this measure?

Two questions, in order:
  1. Does `$data-requirements` succeed? That is the compile check — it is what
     fails when a Library the CQL includes is absent.
  2. Is every ValueSet canonical the server named actually present?

Lenny parses no CQL. The server computes the dependency closure and returns it.
"""

import logging
import re

from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.measure_readiness import MeasureReadiness, ReadinessState

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
        if not resource:
            continue
        # Exclude Library canonicals
        if resource.startswith("Library/") or "/Library/" in resource:
            continue
        # Exclude CodeSystem canonicals (explicit /CodeSystem/ path or known systems)
        if "/CodeSystem/" in resource or resource in (
            "http://loinc.org",
            "http://snomed.info/sct",
            "http://www.ama-assn.org/go/cpt",
        ):
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
