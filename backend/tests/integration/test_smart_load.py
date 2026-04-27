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

CI subset (bundle-loader-test job)
-----------------------------------
Structural tests (file presence, SHA256) run against all 12 bundles — they are
fast and guard against checked-in corruption.

Loader-replay tests (test_loader_*) exercise load_connectathon_bundles() against
a 2-bundle subset (_CI_SUBSET) to keep vanilla-HAPI CI runtime under 60 min.
The subset was chosen to cover two distinct clinical domains that share VSAC
ValueSets (race/ethnicity/payer demographics), exercising the shared-VS PUT path
without loading all 12 bundles.  All 12 bundles are still loaded nightly by the
bake job (scripts/load_connectathon_bundles.py) which is the authoritative
source-of-truth coverage.

See: https://github.com/Bellese/mct2/issues/202
"""

from __future__ import annotations

import hashlib
import json
import os
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

# Loader-replay tests run against this 2-bundle subset to keep CI runtime manageable.
# Structural tests (file presence, SHA256) still cover all 12 bundles.
_CI_SUBSET = frozenset(
    {
        "CMS122FHIRDiabetesAssessGreaterThan9Percent-bundle.json",
        "CMS124FHIRCervicalCancerScreening-bundle.json",
    }
)


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


def _subset_manifest_entries() -> list[dict]:
    """Return manifest entries restricted to _CI_SUBSET (preserves manifest order)."""
    if not _MANIFEST_PATH.exists():
        return []
    return [m for m in _load_manifest().get("measures", []) if m["bundle_file"] in _CI_SUBSET]


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
    assert bundle_path.exists(), f"Bundle file missing for measure '{entry['id']}': {bundle_path}"


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
        f"SHA256 mismatch for '{entry['id']}' ({entry['bundle_file']}):\n  expected: {expected}\n  actual:   {actual}"
    )


# ---------------------------------------------------------------------------
# Session-scoped loader fixture — runs load_connectathon_bundles() once
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def loader_result(integration_session_factory, tmp_path_factory):
    """Call load_connectathon_bundles() once per module and return a result dict.

    Patches settings so the loader talks to the test HAPI instances and uses
    the test PostgreSQL session factory (via the async_session context manager
    used inside bundle_loader).

    Runs against _CI_SUBSET (2 bundles) rather than the full 12 to keep
    vanilla-HAPI CI runtime under 60 min.  All 12 bundles are covered nightly
    by the bake job (scripts/load_connectathon_bundles.py).

    Returns a dict with keys:
      - ``failed``         — number of failed bundle loads
      - ``loaded``         — number of successfully loaded bundles
      - ``details``        — per-bundle detail list from load_connectathon_bundles()
      - ``counts_by_measure`` — dict mapping canonical_url → ExpectedResult row count,
                                captured atomically right after loading (before any
                                function-scoped _truncate_tables teardown can clear rows)

    When HAPI_PREBAKED=1 the bundles are already loaded in the pre-baked image.
    Re-uploading them here would add 10-15 minutes to every PR gate run.  Skip
    these tests instead; they run nightly via the connectathon-measures workflow.
    """
    if os.environ.get("HAPI_PREBAKED") == "1":
        pytest.skip("Bundle-loader tests skipped in pre-baked mode — run nightly")

    # Build a temp directory with only the CI-subset bundles (symlinked from the
    # real bundle dir) so the production loader signature is unchanged.
    subset_dir = tmp_path_factory.mktemp("connectathon-subset")
    for name in _CI_SUBSET:
        (subset_dir / name).symlink_to(_BUNDLE_DIR / name)
    # Symlink the manifest so the loader's manifest.json skip is exercised.
    (subset_dir / "manifest.json").symlink_to(_MANIFEST_PATH)

    with (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.bundle_loader.async_session", integration_session_factory),
    ):
        result = await load_connectathon_bundles(subset_dir)

    # Capture ExpectedResult counts immediately — before any function-scoped
    # _truncate_tables teardown can wipe the expected_results table.
    async with integration_session_factory() as session:
        count_result = await session.execute(
            select(ExpectedResult.measure_url, func.count().label("cnt")).group_by(ExpectedResult.measure_url)
        )
        rows = count_result.all()

    counts_by_measure = {row.measure_url: row.cnt for row in rows}

    return {
        "failed": result.get("failed", -1),
        "loaded": result.get("loaded", 0),
        "details": result.get("details", []),
        "counts_by_measure": counts_by_measure,
    }


# ---------------------------------------------------------------------------
# Integration tests — require live infrastructure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loader_zero_failures(loader_result):
    """load_connectathon_bundles() must complete with failed == 0."""
    failed = loader_result["failed"]
    details = loader_result["details"]
    failed_details = [d for d in details if d.get("status") == "failed"]
    assert failed == 0, f"load_connectathon_bundles() reported {failed} failure(s):\n" + "\n".join(
        f"  {d['file']}: {d.get('error', '(no error message)')}" for d in failed_details
    )


@pytest.mark.asyncio
async def test_loader_all_bundles_loaded(loader_result):
    """The loader must have loaded all bundles in the CI subset."""
    expected_files = set(_CI_SUBSET)
    loaded_files = {d["file"] for d in loader_result["details"] if d.get("status") == "loaded"}

    assert expected_files.issubset(loaded_files), (
        f"CI-subset bundles not fully loaded.\n  missing from loader: {expected_files - loaded_files}"
    )


@pytest.mark.asyncio
async def test_loader_canonical_urls_on_measure_engine(loader_result):
    """After loading, every CI-subset canonical URL must resolve on the measure engine.

    Queries GET /Measure?url=<canonical_url>&_count=1 for each subset entry.
    Failures are aggregated and reported together.
    """
    missing: list[str] = []

    for entry in _subset_manifest_entries():
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

    assert not missing, f"{len(missing)} canonical URL(s) missing from measure engine after load:\n" + "\n".join(
        missing
    )


@pytest.mark.asyncio
async def test_expected_results_counts(loader_result):
    """For each manifest entry with expected_test_cases > 0, the DB must have contained
    exactly that many ExpectedResult rows for the measure's canonical URL at load time.

    Uses loader_result["counts_by_measure"] which is captured atomically inside the
    module-scoped loader_result fixture, immediately after load_connectathon_bundles()
    completes — before any function-scoped _truncate_tables teardown can clear rows.
    """
    counts_by_measure = loader_result["counts_by_measure"]
    mismatches: list[str] = []

    for entry in _subset_manifest_entries():
        if entry["expected_test_cases"] == 0:
            continue  # definition-only bundle — skip

        canonical_url = entry["canonical_url"]
        actual_count = counts_by_measure.get(canonical_url, 0)

        if actual_count != entry["expected_test_cases"]:
            mismatches.append(
                f"  {entry['id']} ({canonical_url!r}): expected {entry['expected_test_cases']}, got {actual_count}"
            )

    assert not mismatches, f"ExpectedResult count mismatch for {len(mismatches)} measure(s):\n" + "\n".join(mismatches)


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


@pytest.mark.skip(
    reason=(
        "HAPI loads QI-Core IG packages for profile validation but does not persist the "
        "ImplementationGuide resource in the FHIR store. "
        "test_cdr_capability_statement_references_qicore covers the same intent."
    )
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
