"""Parametrized per-test-case connectathon evaluation tests.

One pytest test case per MADiE test-case MeasureReport found in the connectathon
bundles under seed/connectathon-bundles/.  Each test:

  1. Resolves the canonical measure URL to a HAPI resource ID on the test MCS.
  2. Calls ``$evaluate-measure`` for the specific patient/period.
  3. Compares actual population counts against expected using ``compare_populations``.
  4. Fails (or soft-skips when STRICT_STU6=0) on any mismatch.

STRICT_STU6 env var:
    "1" (default) — assert equality; hard-fail on mismatch or HTTP error.
    "0"           — warn and skip on mismatch; continue on HTTP error.

Definition-only bundles (expected_test_cases=0 in manifest) produce no parametrize
cases and are silently skipped.

Run with the real HAPI containers:
    docker compose -f docker-compose.test.yml up -d
    cd backend && python -m pytest tests/integration/test_connectathon_measures.py -v
"""

from __future__ import annotations

import json
import os
import pathlib
import warnings
from typing import Any

import httpx
import pytest

from app.services.validation import (
    _classify_bundle_entries,
    _extract_population_counts,
    compare_populations,
)
from tests.integration._helpers import fail_with_context, make_put_bundle
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Bundle discovery — load once at module import time
# ---------------------------------------------------------------------------

_BUNDLE_DIR = pathlib.Path(__file__).resolve().parents[3] / "seed" / "connectathon-bundles"
_MANIFEST_PATH = _BUNDLE_DIR / "manifest.json"


def _load_manifest() -> list[dict[str, Any]]:
    """Return the list of measure entries from manifest.json, or [] if missing."""
    if not _MANIFEST_PATH.exists():
        return []
    with open(_MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f).get("measures", [])


def _load_connectathon_test_cases() -> list[tuple]:
    """Scan all connectathon bundles and return one tuple per test-case MeasureReport.

    Each tuple: (measure_id, canonical_url, patient_ref, period_start, period_end,
                 expected_populations)

    Definition-only bundles (expected_test_cases=0 in manifest) produce no tuples.
    """
    cases: list[tuple] = []
    for entry in _load_manifest():
        measure_id: str = entry["id"]
        canonical_url: str = entry["canonical_url"]
        bundle_file: str = entry["bundle_file"]
        expected_count: int = entry.get("expected_test_cases", 0)

        if expected_count == 0:
            # Definition-only bundle — no test-case MeasureReports
            continue

        bundle_path = _BUNDLE_DIR / bundle_file
        if not bundle_path.exists():
            # Bundle file missing at module load time; a session-scope fixture will
            # catch this more gracefully; skip here to avoid import errors.
            continue

        with open(bundle_path, encoding="utf-8") as f:
            bundle = json.load(f)

        _measure_defs, _clinical, test_cases = _classify_bundle_entries(bundle)

        cases_for_this_bundle: list[tuple] = []
        for tc in test_cases:
            cases_for_this_bundle.append(
                (
                    measure_id,
                    canonical_url,
                    tc["patient_ref"],
                    tc["period_start"],
                    tc["period_end"],
                    tc["expected_populations"],
                )
            )

        if len(cases_for_this_bundle) != expected_count:
            warnings.warn(
                f"[{measure_id}] manifest declares {expected_count} test cases "
                f"but bundle parse yielded {len(cases_for_this_bundle)}"
            )

        cases.extend(cases_for_this_bundle)

    return cases


# Build the parametrize list once.  If no bundles found, produce a single
# skipped placeholder so pytest collects cleanly.
_ALL_TEST_CASES = _load_connectathon_test_cases()

if _ALL_TEST_CASES:
    _PARAMETRIZE_CASES = [
        pytest.param(
            measure_id,
            canonical_url,
            patient_ref,
            period_start,
            period_end,
            expected_populations,
            id=f"{measure_id}::{patient_ref}",
        )
        for measure_id, canonical_url, patient_ref, period_start, period_end, expected_populations in _ALL_TEST_CASES
    ]
else:
    _PARAMETRIZE_CASES = [
        pytest.param(
            "_none",
            "",
            "",
            "",
            "",
            {},
            id="_none::_none",
            marks=pytest.mark.skip(reason="No connectathon test-case bundles found"),
        )
    ]


# ---------------------------------------------------------------------------
# Module-scope fixture: load bundles into HAPI once per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _load_connectathon_bundles_to_hapi(_require_infrastructure):
    """Load all connectathon bundles into the test HAPI instances.

    Measure definitions → measure engine (TEST_MEASURE_URL).
    Clinical data      → CDR (TEST_CDR_URL).

    Skips gracefully if a bundle file is missing.  Re-runs are idempotent
    (PUT-based transactions).
    """
    headers = {"Content-Type": "application/fhir+json"}

    for entry in _load_manifest():
        bundle_file: str = entry["bundle_file"]
        measure_id: str = entry["id"]
        bundle_path = _BUNDLE_DIR / bundle_file

        if not bundle_path.exists():
            warnings.warn(f"[{measure_id}] bundle file not found: {bundle_path}")
            continue

        with open(bundle_path, encoding="utf-8") as f:
            bundle = json.load(f)

        measure_defs, clinical, _test_cases = _classify_bundle_entries(bundle)

        if measure_defs:
            tx = make_put_bundle(measure_defs)
            resp = httpx.post(TEST_MEASURE_URL, json=tx, headers=headers, timeout=120)
            if resp.status_code == 422 and "HAPI-0902" in resp.text:
                warnings.warn(f"[{measure_id}] measure defs already loaded (HAPI-0902)")
            else:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    pytest.fail(f"[{measure_id}] Failed to load measure defs: {exc}\n{resp.text[:300]}")

        if clinical:
            tx = make_put_bundle(clinical)
            resp = httpx.post(TEST_CDR_URL, json=tx, headers=headers, timeout=120)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pytest.fail(f"[{measure_id}] Failed to load clinical data: {exc}\n{resp.text[:300]}")


# ---------------------------------------------------------------------------
# Measure ID resolution (cached per canonical URL to avoid repeated HAPI calls)
# ---------------------------------------------------------------------------

_measure_id_cache: dict[str, str | None] = {}


def _resolve_measure_hapi_id(canonical_url: str) -> str | None:
    """Resolve a canonical measure URL to a HAPI FHIR resource ID.

    Result is cached so each canonical URL is queried at most once per run.
    """
    if canonical_url in _measure_id_cache:
        return _measure_id_cache[canonical_url]

    search_url = f"{TEST_MEASURE_URL}/Measure?url={canonical_url}&_count=1"
    try:
        resp = httpx.get(search_url, timeout=30)
        if resp.status_code != 200:
            _measure_id_cache[canonical_url] = None
            return None
        entries = resp.json().get("entry", [])
        hapi_id = entries[0].get("resource", {}).get("id") if entries else None
    except httpx.RequestError:
        hapi_id = None

    _measure_id_cache[canonical_url] = hapi_id
    return hapi_id


# ---------------------------------------------------------------------------
# Per-test-case evaluation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "measure_id,canonical_url,patient_ref,period_start,period_end,expected_populations",
    _PARAMETRIZE_CASES,
)
def test_connectathon_measure_per_patient(
    measure_id: str,
    canonical_url: str,
    patient_ref: str,
    period_start: str,
    period_end: str,
    expected_populations: dict[str, int],
) -> None:
    """Evaluate a single test-case MeasureReport and assert population counts match.

    Each parametrized invocation is one patient/measure combination from the
    MADiE connectathon bundles.
    """
    strict = os.environ.get("STRICT_STU6", "1") == "1"

    # --- Resolve measure to HAPI resource ID ---
    hapi_id = _resolve_measure_hapi_id(canonical_url)
    if not hapi_id:
        msg = f"[{measure_id}] Measure not found on test MCS (url={canonical_url!r})"
        if strict:
            fail_with_context(
                measure_id=measure_id,
                patient=patient_ref,
                phase="resolve",
                expected=canonical_url,
                actual=None,
                likely_source="mcs",
            )
        else:
            pytest.skip(msg)
        return

    # --- Call $evaluate-measure ---
    eval_url = (
        f"{TEST_MEASURE_URL}/Measure/{hapi_id}/$evaluate-measure"
        f"?subject=Patient/{patient_ref}"
        f"&periodStart={period_start}&periodEnd={period_end}"
    )

    try:
        resp = httpx.get(eval_url, timeout=60)
    except httpx.RequestError as exc:
        msg = f"[{measure_id}] HTTP request failed for patient {patient_ref!r}: {exc}"
        if strict:
            fail_with_context(
                measure_id=measure_id,
                patient=patient_ref,
                phase="evaluate",
                expected="HTTP 200",
                actual=f"RequestError: {exc}",
                likely_source="mcs",
            )
        else:
            warnings.warn(msg)
            pytest.skip(msg)
        return

    if resp.status_code != 200:
        msg = f"[{measure_id}] MCS returned {resp.status_code} for patient {patient_ref!r}: {resp.text[:200]}"
        if strict:
            fail_with_context(
                measure_id=measure_id,
                patient=patient_ref,
                phase="evaluate",
                expected="HTTP 200",
                actual=resp.status_code,
                likely_source="mcs",
            )
        else:
            warnings.warn(msg)
            pytest.skip(msg)
        return

    # --- Parse and compare populations ---
    report = resp.json()
    assert report.get("resourceType") == "MeasureReport", (
        f"[{measure_id}] Expected MeasureReport for patient {patient_ref!r}, "
        f"got resourceType={report.get('resourceType')!r}"
    )

    actual_populations = _extract_population_counts(report)
    passed, mismatches = compare_populations(expected_populations, actual_populations)

    if not passed:
        msg = f"[{measure_id}] Population mismatch for patient {patient_ref!r} — mismatched codes: {mismatches}"
        if strict:
            fail_with_context(
                measure_id=measure_id,
                patient=patient_ref,
                phase="compare",
                expected=expected_populations,
                actual=actual_populations,
                likely_source="mcs",
            )
        else:
            warnings.warn(msg)
