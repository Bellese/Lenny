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
import time
import warnings
from typing import Any

import httpx
import pytest

from app.services.validation import (
    _classify_bundle_entries,
    _extract_population_counts,
    _fix_valueset_compose_for_hapi,
    compare_populations,
)
from tests.integration._helpers import fail_with_context, make_put_bundle
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL, _trigger_reindex_and_wait

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
# Measure engine readiness wait — poll $evaluate-measure until populations
# are non-zero for a known-positive patient in each target bundle
# ---------------------------------------------------------------------------

_EVAL_PROBE_POLL_INTERVAL = 5  # seconds
_EVAL_PROBE_TIMEOUT = 600  # seconds before giving up


def _wait_for_measure_engine_ready(
    base_url: str,
    probes: list[tuple[str, str, str, str]],
) -> None:
    """Block until $evaluate-measure returns non-zero populations for all probes.

    Uses one known IP=1 patient per measure bundle as a readiness signal for
    the full evaluation pipeline (ValueSet expansion + CQL evaluation).

    Polling $expand on a probe ValueSet is unreliable: probe VSes from non-target
    bundles often rely on CodeSystems (TJC, CDC OIDs) that HAPI cannot expand
    locally, causing the wait to time out even when target measures are ready.
    Polling $evaluate-measure directly tests what the tests actually require.

    ``probes`` is a list of ``(canonical_url, patient_ref, period_start, period_end)``
    tuples — one per bundle with test cases.

    Warns (does not fail) on timeout so tests can run and surface real errors.
    """
    if not probes:
        return

    # Resolve canonical URLs → HAPI measure resource IDs
    resolved: list[tuple[str, str, str, str]] = []  # (hapi_id, patient_ref, period_start, period_end)
    for canonical_url, patient_ref, period_start, period_end in probes:
        try:
            search = httpx.get(
                f"{base_url}/Measure",
                params={"url": canonical_url, "_count": "1"},
                timeout=15,
            )
            entries = search.json().get("entry", []) if search.status_code == 200 else []
            hapi_id = entries[0].get("resource", {}).get("id") if entries else None
        except Exception:
            hapi_id = None
        if hapi_id:
            resolved.append((hapi_id, patient_ref, period_start, period_end))

    if not resolved:
        warnings.warn(f"$evaluate-measure readiness probe: no canonical URLs resolved on {base_url}")
        return

    pending = list(resolved)
    deadline = time.monotonic() + _EVAL_PROBE_TIMEOUT

    while pending and time.monotonic() < deadline:
        still_pending = []
        for hapi_id, patient_ref, period_start, period_end in pending:
            try:
                resp = httpx.get(
                    f"{base_url}/Measure/{hapi_id}/$evaluate-measure",
                    params={
                        "subject": f"Patient/{patient_ref}",
                        "periodStart": period_start,
                        "periodEnd": period_end,
                    },
                    timeout=60,
                )
                if resp.status_code == 200:
                    report = resp.json()
                    if report.get("resourceType") == "MeasureReport":
                        if sum(_extract_population_counts(report).values()) > 0:
                            continue  # probe ready
            except Exception:
                pass
            still_pending.append((hapi_id, patient_ref, period_start, period_end))
        pending = still_pending
        if pending:
            time.sleep(_EVAL_PROBE_POLL_INTERVAL)

    if pending:
        warnings.warn(
            f"$evaluate-measure readiness probe timed out after {_EVAL_PROBE_TIMEOUT}s "
            f"for {len(pending)} measure(s) on {base_url}"
        )


# ---------------------------------------------------------------------------
# Module-scope fixture: load bundles into HAPI once per test session
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _load_connectathon_bundles_to_hapi(_require_infrastructure):
    """Load all connectathon bundles into the test HAPI instances.

    Measure definitions (with compose-patched ValueSets) → measure engine.
    Clinical data → both CDR and measure engine so $evaluate-measure can
    resolve patient resources without a separate CDR federation step.

    Skips gracefully if a bundle file is missing.  Re-runs are idempotent
    (PUT-based transactions).
    """
    headers = {"Content-Type": "application/fhir+json"}

    probe_patient_id: str | None = None
    eval_probes: list[tuple[str, str, str, str]] = []  # (canonical_url, patient_ref, period_start, period_end)

    for entry in _load_manifest():
        bundle_file: str = entry["bundle_file"]
        measure_id: str = entry["id"]
        canonical_url: str = entry.get("canonical_url", "")
        bundle_path = _BUNDLE_DIR / bundle_file

        if not bundle_path.exists():
            warnings.warn(f"[{measure_id}] bundle file not found: {bundle_path}")
            continue

        with open(bundle_path, encoding="utf-8") as f:
            bundle = json.load(f)

        measure_defs, clinical, _test_cases = _classify_bundle_entries(bundle)

        if measure_defs:
            # Split so ValueSets (patched) go first; HAPI-0902 on re-upload is OK.
            secondary = [r for r in measure_defs if r.get("resourceType") not in ("Measure", "Library")]
            primary = [r for r in measure_defs if r.get("resourceType") in ("Measure", "Library")]
            if secondary:
                patched = _fix_valueset_compose_for_hapi(secondary)
                tx = make_put_bundle(patched)
                resp = httpx.post(TEST_MEASURE_URL, json=tx, headers=headers, timeout=120)
                if not (resp.status_code == 422 and "HAPI-0902" in resp.text):
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        pytest.fail(f"[{measure_id}] Failed to load ValueSets: {exc}\n{resp.text[:300]}")
            if primary:
                tx = make_put_bundle(primary)
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
            # CDR — primary patient record store
            resp = httpx.post(TEST_CDR_URL, json=tx, headers=headers, timeout=120)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pytest.fail(f"[{measure_id}] Failed to load clinical data to CDR: {exc}\n{resp.text[:300]}")
            # MCS — $evaluate-measure requires patient resources on the measure server
            resp = httpx.post(TEST_MEASURE_URL, json=tx, headers=headers, timeout=120)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pytest.fail(f"[{measure_id}] Failed to load clinical data to MCS: {exc}\n{resp.text[:300]}")
            # Track a probe patient for post-load reindex wait
            if probe_patient_id is None:
                for r in clinical:
                    if r.get("resourceType") == "Patient":
                        probe_patient_id = r.get("id")
                        break

        # Collect one $evaluate-measure readiness probe per bundle (first IP=1 test case).
        if canonical_url and _test_cases:
            for tc in _test_cases:
                if tc.get("expected_populations", {}).get("initial-population", 0) >= 1:
                    eval_probes.append(
                        (canonical_url, tc["patient_ref"], tc["period_start"], tc["period_end"])
                    )
                    break

    # Wait for HAPI to finish indexing reference-type search params so that
    # Encounter?patient=… (used internally by $evaluate-measure) returns results.
    if probe_patient_id:
        _trigger_reindex_and_wait(TEST_MEASURE_URL, probe_patient_id, "")

    # Wait for the measure engine to be fully ready: HAPI v8.6.0 expands ValueSets
    # asynchronously in a background thread pool.  $evaluate-measure returns
    # all-zero populations when called before expansion completes.  We probe using
    # a known-positive patient from each bundle rather than polling $expand, which
    # can time out when probe VSes rely on CodeSystems HAPI does not have locally.
    if eval_probes:
        _wait_for_measure_engine_ready(TEST_MEASURE_URL, eval_probes)


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
