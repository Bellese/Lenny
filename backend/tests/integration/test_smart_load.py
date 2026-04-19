"""Manifest-driven smart-load smoke test — end-to-end bundle loading pipeline.

Verifies that every entry in seed/connectathon-bundles/manifest.json:
  1. Has a corresponding bundle file on disk.
  2. Has a SHA256 that matches the file contents.
  3. Is loaded successfully by load_connectathon_bundles().
  4. After loading, each measure's canonical URL resolves on the measure engine.
  5. The DB contains the expected number of ExpectedResult rows per measure.
  6. The CDR CapabilityStatement references the QI-Core implementation guide.

All tests tagged @pytest.mark.integration require live HAPI FHIR + PostgreSQL
infrastructure (run via scripts/run-integration-tests.sh).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.validation import ExpectedResult
from app.services.bundle_loader import load_connectathon_bundles
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BUNDLE_DIR = _REPO_ROOT / "seed" / "connectathon-bundles"
_MANIFEST_PATH = _BUNDLE_DIR / "manifest.json"

# QI-Core IG canonical URL
_QICORE_IG_URL = "http://hl7.org/fhir/us/qicore/ImplementationGuide/hl7.fhir.us.qicore"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_manifest() -> dict:
    """Load and return the parsed manifest.json."""
    with open(_MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _sha256_file(path: pathlib.Path) -> str:
    """Return the lowercase hex SHA256 digest of a file."""
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _manifest_measures() -> list[dict]:
    """Return measures list from manifest, or empty list if manifest missing."""
    if not _MANIFEST_PATH.exists():
        return []
    return _load_manifest().get("measures", [])


# ---------------------------------------------------------------------------
# Static / offline tests — do not need live infrastructure
# ---------------------------------------------------------------------------


def test_manifest_exists():
    """manifest.json must exist at the expected path."""
    assert _MANIFEST_PATH.exists(), f"manifest.json not found at {_MANIFEST_PATH}"


def test_manifest_has_measures():
    """manifest.json must declare at least one measure entry."""
    manifest = _load_manifest()
    assert len(manifest.get("measures", [])) > 0, "manifest.json contains no measures"


@pytest.mark.parametrize(
    "entry",
    _manifest_measures(),
    ids=lambda e: e.get("id", "unknown"),
)
def test_bundle_file_exists(entry: dict):
    """Each manifest entry's bundle file must exist on disk."""
    bundle_path = _BUNDLE_DIR / entry["bundle_file"]
    assert bundle_path.exists(), (
        f"Bundle file missing for measure '{entry['id']}': {bundle_path}"
    )


@pytest.mark.parametrize(
    "entry",
    _manifest_measures(),
    ids=lambda e: e.get("id", "unknown"),
)
def test_bundle_sha256_matches(entry: dict):
    """Each bundle file's SHA256 must match the digest recorded in manifest.json."""
    bundle_path = _BUNDLE_DIR / entry["bundle_file"]
    if not bundle_path.exists():
        pytest.skip("Bundle file missing — covered by test_bundle_file_exists")
    actual = _sha256_file(bundle_path)
    expected = entry["sha256"]
    assert actual == expected, (
        f"SHA256 mismatch for '{entry['id']}' ({entry['bundle_file']}):\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}"
    )


# ---------------------------------------------------------------------------
# Session-scoped loader fixture — runs load_connectathon_bundles() once
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def loader_result(integration_session_factory):
    """Call load_connectathon_bundles() once per module and return the result dict.

    Patches settings so the loader talks to the test HAPI instances and uses
    the test PostgreSQL session factory (via the async_session context manager
    used inside bundle_loader).
    """
    with (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.bundle_loader.async_session", integration_session_factory),
    ):
        result = await load_connectathon_bundles(_BUNDLE_DIR)

    return result


@pytest_asyncio.fixture(scope="module")
async def expected_counts_by_measure(loader_result, integration_session_factory):
    """Capture ExpectedResult row counts per canonical URL immediately after loading.

    This fixture is module-scoped and runs right after loader_result — before any
    function-scoped _truncate_tables teardowns clear the expected_results table.
    Returns a dict mapping canonical_url → row count.
    """
    async with integration_session_factory() as session:
        result = await session.execute(
            select(ExpectedResult.measure_url, func.count().label("cnt"))
            .group_by(ExpectedResult.measure_url)
        )
        rows = result.all()
    return {row.measure_url: row.cnt for row in rows}


# ---------------------------------------------------------------------------
# Integration tests — require live infrastructure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_zero_failures(loader_result):
    """load_connectathon_bundles() must complete with failed == 0."""
    result = loader_result
    failed = result.get("failed", -1)
    details = result.get("details", [])
    failed_details = [d for d in details if d.get("status") == "failed"]
    assert failed == 0, (
        f"load_connectathon_bundles() reported {failed} failure(s):\n"
        + "\n".join(
            f"  {d['file']}: {d.get('error', '(no error message)')}"
            for d in failed_details
        )
    )


@pytest.mark.asyncio
async def test_loader_all_bundles_loaded(loader_result):
    """The loader must have attempted and loaded all bundles listed in the manifest."""
    manifest = _load_manifest()
    expected_files = {entry["bundle_file"] for entry in manifest["measures"]}

    result = loader_result
    details = result.get("details", [])
    loaded_files = {d["file"] for d in details if d.get("status") == "loaded"}

    assert loaded_files == expected_files, (
        f"Loaded bundle set does not match manifest.\n"
        f"  missing from loader: {expected_files - loaded_files}\n"
        f"  extra in loader:     {loaded_files - expected_files}"
    )


@pytest.mark.asyncio
async def test_loader_canonical_urls_on_measure_engine(loader_result):
    """After loading, every manifest canonical URL must resolve on the measure engine.

    Queries GET /Measure?url=<canonical_url>&_count=1 for each manifest entry.
    Failures are aggregated and reported together.
    """
    manifest = _load_manifest()
    missing: list[str] = []

    for entry in manifest["measures"]:
        canonical_url = entry["canonical_url"]
        search_url = f"{TEST_MEASURE_URL}/Measure?url={canonical_url}&_count=1"
        try:
            resp = httpx.get(search_url, timeout=30)
            resp.raise_for_status()
            entries = resp.json().get("entry", [])
            if not entries:
                missing.append(f"  {entry['id']}: {canonical_url!r} — not found on measure engine")
        except Exception as exc:
            missing.append(f"  {entry['id']}: {canonical_url!r} — request failed: {exc}")

    assert not missing, (
        f"{len(missing)} canonical URL(s) missing from measure engine after load:\n"
        + "\n".join(missing)
    )


@pytest.mark.asyncio
async def test_expected_results_counts(expected_counts_by_measure):
    """For each manifest entry with expected_test_cases > 0, the DB must have contained
    exactly that many ExpectedResult rows for the measure's canonical URL at load time.

    Uses expected_counts_by_measure (module-scoped) which captures row counts immediately
    after load_connectathon_bundles() completes — before function-scoped _truncate_tables
    teardowns clear the expected_results table between other tests.
    """
    manifest = _load_manifest()
    mismatches: list[str] = []

    for entry in manifest["measures"]:
        if entry["expected_test_cases"] == 0:
            continue  # definition-only bundle — skip

        canonical_url = entry["canonical_url"]
        actual_count = expected_counts_by_measure.get(canonical_url, 0)

        if actual_count != entry["expected_test_cases"]:
            mismatches.append(
                f"  {entry['id']} ({canonical_url!r}): "
                f"expected {entry['expected_test_cases']}, got {actual_count}"
            )

    assert not mismatches, (
        f"ExpectedResult count mismatch for {len(mismatches)} measure(s):\n"
        + "\n".join(mismatches)
    )


# ---------------------------------------------------------------------------
# QI-Core CapabilityStatement assertion
# ---------------------------------------------------------------------------


def test_cdr_capability_statement_references_qicore():
    """The CDR CapabilityStatement must reference the QI-Core implementation guide.

    Checks both the ``implementationGuide`` list in the CapabilityStatement root
    and profile URLs in rest.resource entries.
    """
    resp = httpx.get(f"{TEST_CDR_URL}/metadata", timeout=30)
    resp.raise_for_status()
    cs = resp.json()

    # Primary check: implementationGuide list at root level
    impl_guides = cs.get("implementationGuide", [])
    if any("qicore" in ig.lower() or "hl7.fhir.us.qicore" in ig for ig in impl_guides):
        return  # pass

    # Secondary check: profile URLs in rest resources
    for rest_entry in cs.get("rest", []):
        for resource in rest_entry.get("resource", []):
            for profile in resource.get("supportedProfile", []):
                if "qicore" in profile.lower():
                    return  # pass
            base_profile = resource.get("profile", "")
            if "qicore" in base_profile.lower():
                return  # pass

    pytest.fail(
        "CDR CapabilityStatement does not reference QI-Core.\n"
        f"  implementationGuide entries: {impl_guides}\n"
        "  Hint: run the QI-Core IG bootstrap or check HAPI FHIR configuration."
    )


def test_cdr_qicore_implementation_guide_resource():
    """Fallback: query the CDR for the QI-Core ImplementationGuide resource directly.

    This confirms the IG is loaded even if the CapabilityStatement metadata
    does not explicitly list it.
    """
    search_url = f"{TEST_CDR_URL}/ImplementationGuide?url={_QICORE_IG_URL}"
    resp = httpx.get(search_url, timeout=30)
    resp.raise_for_status()
    bundle = resp.json()

    total = bundle.get("total", 0)
    entries = bundle.get("entry", [])
    assert total > 0 or len(entries) > 0, (
        f"No QI-Core ImplementationGuide resource found on CDR.\n"
        f"  Searched: GET {search_url}\n"
        "  The QI-Core IG must be loaded into the CDR before running the connectathon."
    )
