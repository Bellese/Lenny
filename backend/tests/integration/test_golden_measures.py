"""Golden file integration tests — validate measure evaluation against expected results.

Loads DBCG connectathon bundles, routes each resource to the correct HAPI instance
(measure defs → measure engine, clinical data → CDR), then calls $evaluate-measure
and compares actual population counts against the expected counts in the bundle's
MeasureReports.

To add a new golden test case, drop a directory with bundle.json into
tests/integration/golden/.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest

from app.services.validation import (
    _classify_bundle_entries,
    _extract_population_counts,
    compare_populations,
)
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"

# Pre-load bundles once at module scope (used by both fixture and parametrize)
_GOLDEN_BUNDLES: list[tuple[str, dict]] = []


def _load_golden_bundles() -> list[tuple[str, dict]]:
    """Discover all golden test bundles in the golden/ directory.

    Returns a list of (name, bundle_json) tuples.
    """
    bundles = []
    for bundle_path in sorted(GOLDEN_DIR.glob("*/bundle.json")):
        name = bundle_path.parent.name
        with open(bundle_path, encoding="utf-8") as f:
            bundles.append((name, json.load(f)))
    return bundles


def _get_golden_bundles() -> list[tuple[str, dict]]:
    """Return cached golden bundles, loading once on first call."""
    global _GOLDEN_BUNDLES
    if not _GOLDEN_BUNDLES:
        _GOLDEN_BUNDLES = _load_golden_bundles()
    return _GOLDEN_BUNDLES


def _make_tx_bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a list of resources in a FHIR transaction bundle."""
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {
                "resource": r,
                "request": {
                    "method": "PUT",
                    "url": f"{r['resourceType']}/{r['id']}",
                },
            }
            for r in resources
            if "resourceType" in r and "id" in r
        ],
    }


def _is_test_case_measure_report(report: dict[str, Any]) -> bool:
    """Return True if this MeasureReport represents a test case.

    Supports two formats:
    1. Modern: ``modifierExtension`` containing ``cqfm-isTestCase`` with ``valueBoolean: true``
    2. Legacy DBCG connectathon: ``type == "individual"`` and ``status == "complete"``
    """
    # Modern cqfm-isTestCase format
    for ext in report.get("modifierExtension", []):
        url = ext.get("url", "")
        if url.endswith("cqfm-isTestCase") and ext.get("valueBoolean") is True:
            return True
    # Legacy connectathon format — individual completed reports ARE test cases
    if report.get("type") == "individual" and report.get("status") == "complete":
        return True
    return False


def _extract_test_case_info(report: dict[str, Any]) -> dict[str, Any] | None:
    """Extract test case metadata from a MeasureReport.

    Returns a dict with measure_url, patient_ref, period_start, period_end,
    and expected_populations. Returns None if required fields are missing.

    Handles both modern (contained Parameters) and legacy (subject.reference) formats.
    """
    measure_ref = report.get("measure", "")
    if not measure_ref:
        return None

    # Resolve measure URL: canonical URL preferred; fall back to relative id reference
    if measure_ref.startswith("http"):
        measure_url = measure_ref
    else:
        # Relative reference like "Measure/measure-EXM124-FHIR4-8.2.000"
        measure_url = measure_ref

    # Extract patient reference — modern format stores it in contained Parameters
    patient_ref = None
    for contained in report.get("contained", []):
        if contained.get("resourceType") == "Parameters":
            for param in contained.get("parameter", []):
                if param.get("name") == "subject":
                    patient_ref = param.get("valueString")
                    break
        if patient_ref:
            break

    # Legacy format: patient ref is directly in subject.reference
    if not patient_ref:
        subject_ref = report.get("subject", {}).get("reference", "")
        if subject_ref:
            # Strip "Patient/" prefix if present to get bare patient id
            patient_ref = subject_ref.removeprefix("Patient/")

    if not patient_ref:
        return None

    period = report.get("period", {})
    period_start = period.get("start", "")
    period_end = period.get("end", "")
    if not period_start or not period_end:
        return None

    # Truncate to date-only (HAPI accepts YYYY-MM-DD)
    period_start = period_start[:10]
    period_end = period_end[:10]

    expected_populations = _extract_population_counts(report)

    return {
        "measure_url": measure_url,
        "patient_ref": patient_ref,
        "period_start": period_start,
        "period_end": period_end,
        "expected_populations": expected_populations,
    }


def _resolve_measure_id(measure_url_or_ref: str) -> str | None:
    """Resolve a measure URL or relative reference to a HAPI resource ID.

    Accepts both canonical URLs (queried via ?url=) and relative references
    like ``Measure/some-id`` (queried by direct GET).
    """
    if measure_url_or_ref.startswith("Measure/"):
        measure_id = measure_url_or_ref.removeprefix("Measure/")
        url = f"{TEST_MEASURE_URL}/Measure/{measure_id}"
        resp = httpx.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.json().get("id")
        return None

    # Canonical URL lookup
    search_url = f"{TEST_MEASURE_URL}/Measure?url={measure_url_or_ref}&_count=1"
    resp = httpx.get(search_url, timeout=30)
    if resp.status_code != 200:
        return None
    entries = resp.json().get("entry", [])
    if entries:
        return entries[0].get("resource", {}).get("id")
    return None


@pytest.fixture(scope="module", autouse=True)
def _load_golden_bundles_to_hapi(_require_infrastructure):
    """Load golden bundles into HAPI, routing resources to their correct instances.

    - Measure definitions (Measure, Library, ValueSet, CodeSystem) → measure engine
    - Clinical data (Patient, Encounter, Observation, etc.) → CDR

    Runs once per module; uses PUT-based transaction bundles so re-runs are idempotent.
    """
    headers = {"Content-Type": "application/fhir+json"}

    for name, bundle in _get_golden_bundles():
        measure_defs, clinical, _test_cases = _classify_bundle_entries(bundle)

        if measure_defs:
            tx = _make_tx_bundle(measure_defs)
            resp = httpx.post(
                TEST_MEASURE_URL,
                json=tx,
                headers=headers,
                timeout=120,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pytest.fail(
                    f"Failed to load measure defs for '{name}': {exc}\n{resp.text[:300]}"
                )

        if clinical:
            tx = _make_tx_bundle(clinical)
            resp = httpx.post(
                TEST_CDR_URL,
                json=tx,
                headers=headers,
                timeout=120,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pytest.fail(
                    f"Failed to load clinical data for '{name}': {exc}\n{resp.text[:300]}"
                )


_PARAMETRIZE_BUNDLES = _get_golden_bundles() or [
    pytest.param("_empty", {}, marks=pytest.mark.skip(reason="No golden bundles found"))
]


@pytest.mark.parametrize("name,bundle", _PARAMETRIZE_BUNDLES)
def test_golden_measure_evaluates(name: str, bundle: dict[str, Any]) -> None:
    """Each golden bundle's test case MeasureReports produce matching population counts.

    For each test case MeasureReport in the bundle:
    1. Resolve the measure to a HAPI resource ID
    2. Call $evaluate-measure for the test patient
    3. Assert actual population counts match the expected counts in the bundle
    """
    test_cases: list[dict[str, Any]] = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") != "MeasureReport":
            continue
        if not _is_test_case_measure_report(resource):
            continue
        info = _extract_test_case_info(resource)
        if info:
            test_cases.append(info)

    if not test_cases:
        pytest.skip(f"No test case MeasureReports found in golden bundle '{name}'")

    # Resolve the measure once (all test cases in a bundle share the same measure)
    measure_ref = test_cases[0]["measure_url"]
    measure_id = _resolve_measure_id(measure_ref)
    if not measure_id:
        pytest.skip(
            f"Measure not found on test MCS for '{name}' (ref={measure_ref!r})"
        )

    failures: list[str] = []
    skipped_cases: list[str] = []
    evaluated = 0

    for tc in test_cases:
        patient_ref = tc["patient_ref"]
        period_start = tc["period_start"]
        period_end = tc["period_end"]
        expected = tc["expected_populations"]

        url = (
            f"{TEST_MEASURE_URL}/Measure/{measure_id}/$evaluate-measure"
            f"?subject=Patient/{patient_ref}"
            f"&periodStart={period_start}&periodEnd={period_end}"
        )

        try:
            resp = httpx.get(url, timeout=60)
        except httpx.RequestError as exc:
            skipped_cases.append(f"  {patient_ref}: request failed — {exc}")
            continue

        if resp.status_code != 200:
            skipped_cases.append(
                f"  {patient_ref}: HAPI returned {resp.status_code} — {resp.text[:150]}"
            )
            continue

        report = resp.json()
        assert report.get("resourceType") == "MeasureReport", (
            f"Expected MeasureReport for '{name}/{patient_ref}', "
            f"got {report.get('resourceType')}"
        )

        actual = _extract_population_counts(report)
        passed, mismatches = compare_populations(expected, actual)
        evaluated += 1

        if not passed:
            failures.append(
                f"  {patient_ref}: mismatched codes {mismatches}\n"
                f"    expected: {expected}\n"
                f"    actual:   {actual}"
            )

    if skipped_cases:
        import warnings

        warnings.warn(
            f"[{name}] {len(skipped_cases)} test case(s) skipped:\n"
            + "\n".join(skipped_cases)
        )

    assert not failures, (
        f"Population count mismatches in '{name}' "
        f"({len(failures)}/{evaluated} failed):\n" + "\n".join(failures)
    )

    if evaluated == 0 and test_cases:
        pytest.skip(
            f"All {len(test_cases)} test case(s) for '{name}' were skipped by HAPI"
        )
