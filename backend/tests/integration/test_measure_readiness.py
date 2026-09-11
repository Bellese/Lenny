"""Measure readiness against a real HAPI measure server.

Everything else about this feature is tested against mocks. These two facts
cannot be: that HAPI treats a comma-separated `url` search as an OR match, and
that the seeded measures actually pass the check end to end.

Uses the `measure_url` fixture from `conftest.py` (`TEST_MEASURE_URL`,
``http://localhost:8181/fhir``) rather than an env var default. The dev stack
on this machine runs its measure engine on 8080 — hardcoding that port here
would silently test the wrong server. `measure_url` is what every other file
in this directory already uses to reach the integration harness's HAPI
instance, started via `USE_PREBAKED=1 ./scripts/run-integration-tests.sh`.
"""

import json
import pathlib

import httpx
import pytest

from app.models.measure_readiness import ReadinessState
from app.services.measure_readiness import check_measure_readiness, find_missing_valuesets

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Seeded-measure discovery (#434)
# ---------------------------------------------------------------------------
#
# The measure server in this integration suite is SHARED across files, not
# owned by this test. `test_fhir_operations.py::test_upload_and_list_measure`
# PUTs a stub Measure (id `test-integration-measure`, no CQL content) to
# exercise upload/list behavior, and that file sorts alphabetically before
# this one — so by the time this test runs, the stub is already sitting on
# the server. `check_measure_readiness` correctly reports it `not_ready`
# (HAPI throws a NullPointerException trying to evaluate a Measure with no
# real content): that is the feature working, not a bug.
#
# So "every Measure currently on the server" is the wrong universe for a test
# named `test_every_seeded_measure_is_ready` — it silently expands to include
# whatever fixtures earlier tests left behind. The right universe is derived
# directly from what the prebake's seeding step actually loads:
# seed/measure-bundle.json (the base seed) plus every measure listed in
# seed/connectathon-bundles/manifest.json (loaded by
# scripts/load_connectathon_bundles.py — see .github/workflows/bake-hapi-image.yml
# and scripts/run-integration-tests.sh, which hash these same files to key the
# prebaked image). Reading those two sources — an allowlist of known-seeded
# ids — rather than an id-prefix guess or a hardcoded exclusion of today's
# offending id, means: it tracks the real seed set as it changes, and it does
# NOT depend on this test's failure ever being about `test-integration-measure`
# specifically. If a *different* sibling test starts leaving a *different*
# stub Measure on the server, that id also won't be in this allowlist, and it
# will be excluded the same way — no per-fixture patching required.
#
# Do not widen this back to "every measure on the server". If you need to
# test a new sibling fixture's Measure, add it to seed data instead.

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MEASURE_BUNDLE_PATH = _REPO_ROOT / "seed" / "measure-bundle.json"
_CONNECTATHON_MANIFEST_PATH = _REPO_ROOT / "seed" / "connectathon-bundles" / "manifest.json"


def _seeded_measure_ids() -> set[str]:
    """Ids of measures the prebaked stack's seeding step actually loads.

    Derived from the on-disk seed sources (not an id-prefix heuristic) so this
    stays correct as the seed set changes.
    """
    ids: set[str] = set()

    if _MEASURE_BUNDLE_PATH.exists():
        with open(_MEASURE_BUNDLE_PATH, encoding="utf-8") as f:
            bundle = json.load(f)
        ids.update(
            entry["resource"]["id"]
            for entry in bundle.get("entry", [])
            if entry.get("resource", {}).get("resourceType") == "Measure"
        )

    if _CONNECTATHON_MANIFEST_PATH.exists():
        with open(_CONNECTATHON_MANIFEST_PATH, encoding="utf-8") as f:
            manifest = json.load(f)
        ids.update(entry["id"] for entry in manifest.get("measures", []))

    return ids


# HAPI's default (no `_sort`) ordering is deterministic and stable across
# repeated reads: identical back-to-back `ValueSet?_elements=url&_count=N`
# calls against the real prebaked server return the exact same sequence,
# every time, read-only. That determinism is what the OR-match test below
# leans on: the first `_DEFAULT_PAGE_SIZE` entries of that ordering are what
# *any* filter-dropping bug would fall back to returning, regardless of which
# canonicals were actually requested or how many chunks the request was split
# into (each chunk is an independent fresh query, not a paginated
# continuation, so a later chunk's dropped-filter fallback is the same head
# of the list, not a later page of it).
_DEFAULT_PAGE_SIZE = 20


async def test_comma_separated_valueset_url_search_is_an_or_match(measure_url):
    """The presence check batches canonicals into one `url=a,b,c` query.

    If HAPI ever treated that as a literal string match, every batched valueset
    would read as missing and every measure would go red — that failure mode
    is caught directly below (zero hits on a non-empty `url=` filter).

    A narrower failure mode needs a different fixture: an implementation that
    silently dropped the `url` filter entirely (e.g. a refactor that forgot to
    interpolate `chunk` into `params`) would, against a *static* server with a
    stable default sort, return the same default top-N ValueSets on every
    call — and if the requested canonicals happened to be drawn from that same
    top-N, every one of them would still show up as "present" by coincidence,
    for a reason that has nothing to do with the `url` filter working.

    So the canonicals used here are drawn from *past* `_DEFAULT_PAGE_SIZE` in
    the server's default ordering — deliberately outside what a dropped filter
    would fall back to returning. Confirmed against the real server (see
    task-9-report.md): with the filter genuinely dropped, every one of these
    deep-list canonicals comes back missing instead of present, because the
    fallback query returns the head of the list, not these entries. With the
    filter intact, all of them are found. That is the discriminator this test
    needs to be able to fail for the reason its name claims.

    Drawing more than one chunk's worth (`_count` here comfortably exceeds
    `chunk_size`) also exercises `find_missing_valuesets`'s multi-request
    chunking and merge, which a 2-3-canonical sample never touched.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        listing = await client.get(f"{measure_url}/ValueSet?_elements=url&_count=50")
        listing.raise_for_status()
        urls = [e["resource"]["url"] for e in listing.json().get("entry", []) if e["resource"].get("url")]

    # Need enough past the default-page boundary to draw a deep-list sample,
    # plus enough of a sample to force >1 chunk at chunk_size=10 below.
    deep = urls[_DEFAULT_PAGE_SIZE:]
    if len(deep) < 12:
        pytest.skip(
            f"Measure server has only {len(urls)} ValueSets ({len(deep)} past the "
            f"first {_DEFAULT_PAGE_SIZE}) — not enough to draw a deep-list OR-match sample from"
        )

    targets = deep[-15:] if len(deep) >= 15 else deep

    missing = await find_missing_valuesets(measure_url, targets, auth_headers={}, timeout=60.0, chunk_size=10)
    assert missing == [], f"Expected all {len(targets)} known valuesets to be found, missing: {missing}"


async def test_a_canonical_that_does_not_exist_is_reported_missing(measure_url):
    """The negative control. Without it, a check that always returns [] passes."""
    bogus = "http://example.invalid/ValueSet/definitely-not-here"
    missing = await find_missing_valuesets(measure_url, [bogus], auth_headers={}, timeout=60.0)
    assert missing == [bogus]


async def test_every_seeded_measure_is_ready(measure_url):
    """The local prebaked stack ships complete content; all of it must pass.

    Slow by design — $data-requirements measured at 6-11s per measure, and
    hapi-fhir-measure may run emulated on arm64 hosts, making it slower still.

    `_total=accurate` plus an assertion that the count matches what was
    fetched is what keeps this test honest if the seeded measure count ever
    grows past the page size requested here: silently iterating only the
    first page would let extra measures drop out of coverage with no signal
    that the check had become partial. This fails loudly instead.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        listing = await client.get(f"{measure_url}/Measure?_elements=id&_count=50&_total=accurate")
        listing.raise_for_status()
        body = listing.json()
        measure_ids = [e["resource"]["id"] for e in body.get("entry", [])]

    assert measure_ids, "No measures on the measure server — is the prebaked stack up?"
    assert body.get("total") == len(measure_ids), (
        f"Server reports {body.get('total')} total measures but only {len(measure_ids)} were "
        "fetched in one page — this test would silently stop covering 'every' measure. "
        "Raise _count (or add pagination) rather than letting the extras drop out of coverage."
    )

    # Narrow to seeded content only — see the module-level comment above. This
    # needs its own non-empty guard: a stack that silently stopped seeding
    # would otherwise leave `measures_to_check` empty and this test would pass
    # while checking nothing.
    seeded_ids = _seeded_measure_ids()
    assert seeded_ids, (
        f"No seeded measure ids found via {_MEASURE_BUNDLE_PATH} or {_CONNECTATHON_MANIFEST_PATH} "
        "— fix seeded-measure discovery before trusting this test."
    )

    measures_to_check = [measure_id for measure_id in measure_ids if measure_id in seeded_ids]
    assert measures_to_check, (
        f"None of the {len(measure_ids)} measures on the server ({measure_ids}) matched any of the "
        f"{len(seeded_ids)} known seeded ids ({sorted(seeded_ids)}) — is the prebaked stack seeded "
        "correctly, or did the seed id scheme change?"
    )

    failures = []
    for measure_id in measures_to_check:
        verdict = await check_measure_readiness(measure_url, measure_id, auth_headers={}, timeout=120.0)
        if verdict.state is not ReadinessState.ready:
            failures.append((measure_id, verdict.state.value, verdict.error))

    assert not failures, f"Measures not ready on a complete server: {failures}"
