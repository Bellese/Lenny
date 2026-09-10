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

import httpx
import pytest

from app.models.measure_readiness import ReadinessState
from app.services.measure_readiness import check_measure_readiness, find_missing_valuesets

pytestmark = pytest.mark.integration


async def test_comma_separated_valueset_url_search_is_an_or_match(measure_url):
    """The presence check batches canonicals into one `url=a,b,c` query.

    If HAPI ever treated that as a literal string match, every batched valueset
    would read as missing and every measure would go red. Assert the behaviour
    rather than trusting it.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        listing = await client.get(f"{measure_url}/ValueSet?_elements=url&_count=3")
        listing.raise_for_status()
        urls = [e["resource"]["url"] for e in listing.json().get("entry", []) if e["resource"].get("url")]

    if len(urls) < 2:
        pytest.skip("Measure server has fewer than 2 ValueSets to test an OR match with")

    missing = await find_missing_valuesets(measure_url, urls, auth_headers={}, timeout=60.0, chunk_size=10)
    assert missing == [], f"Expected all {len(urls)} known valuesets to be found, missing: {missing}"


async def test_a_canonical_that_does_not_exist_is_reported_missing(measure_url):
    """The negative control. Without it, a check that always returns [] passes."""
    bogus = "http://example.invalid/ValueSet/definitely-not-here"
    missing = await find_missing_valuesets(measure_url, [bogus], auth_headers={}, timeout=60.0)
    assert missing == [bogus]


async def test_every_seeded_measure_is_ready(measure_url):
    """The local prebaked stack ships complete content; all of it must pass.

    Slow by design — $data-requirements measured at 6-11s per measure, and
    hapi-fhir-measure may run emulated on arm64 hosts, making it slower still.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        listing = await client.get(f"{measure_url}/Measure?_elements=id&_count=50")
        listing.raise_for_status()
        measure_ids = [e["resource"]["id"] for e in listing.json().get("entry", [])]

    assert measure_ids, "No measures on the measure server — is the prebaked stack up?"

    failures = []
    for measure_id in measure_ids:
        verdict = await check_measure_readiness(measure_url, measure_id, auth_headers={}, timeout=120.0)
        if verdict.state is not ReadinessState.ready:
            failures.append((measure_id, verdict.state.value, verdict.error))

    assert not failures, f"Measures not ready on a complete server: {failures}"
