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
from tests.integration._helpers import (
    fail_with_context,
    fix_duplicate_claim_ids,
    fix_library_deps_for_hapi,
    fix_valueset_compose_for_hapi,
    make_put_bundle,
)
from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL, _trigger_reindex_and_wait, _wait_for_valueset_expansion

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
                 expected_populations, strict)

    Definition-only bundles (expected_test_cases=0 in manifest) produce no tuples.
    """
    cases: list[tuple] = []
    for entry in _load_manifest():
        measure_id: str = entry["id"]
        canonical_url: str = entry["canonical_url"]
        bundle_file: str = entry["bundle_file"]
        expected_count: int = entry.get("expected_test_cases", 0)
        measure_strict: bool = entry.get("strict", True)

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
                    measure_strict,
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
            measure_strict,
            id=f"{measure_id}::{patient_ref}",
        )
        for measure_id, canonical_url, patient_ref, period_start, period_end, expected_populations, measure_strict in _ALL_TEST_CASES
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
            True,
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
    Clinical data      → CDR (TEST_CDR_URL) AND measure server (TEST_MEASURE_URL).

    Clinical data must go to the measure server because $evaluate-measure resolves
    patient data from the same HAPI instance it runs on.

    Skips gracefully if a bundle file is missing.  Re-runs are idempotent
    (PUT-based transactions).
    """
    headers = {"Content-Type": "application/fhir+json"}

    probe_patient_id: str | None = None
    probe_encounter_id: str | None = None

    # Pass 1: collect and deduplicate measure definitions across all bundles.
    # Deduplication is critical: the same large ValueSet (e.g. 1797-code VSAC OID)
    # often appears in multiple bundles.  Without deduplication each subsequent PUT
    # resets HAPI's background pre-expansion clock, so by the end of loading the
    # clock has been reset ~3× and the 600s wait never catches up.
    # With deduplication every resource is PUT exactly once; HAPI queues it for
    # pre-expansion once and the wait reliably succeeds.
    all_measure_defs: dict[str, dict] = {}  # "ResourceType/id" → resource (last bundle wins)
    clinical_per_bundle: list[tuple[str, list[dict]]] = []  # (measure_id, clinical_resources)

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
        measure_defs = fix_valueset_compose_for_hapi(measure_defs)
        measure_defs = fix_library_deps_for_hapi(measure_defs)

        for r in measure_defs:
            key = f"{r.get('resourceType')}/{r.get('id')}"
            all_measure_defs[key] = r

        if clinical:
            clinical_per_bundle.append((measure_id, clinical))

    # Pass 2: load deduplicated measure defs in a single batch.
    deduped = list(all_measure_defs.values())

    # Resolve ValueSet ID conflicts before batch load.
    # Some ValueSets in connectathon bundles use bare OID IDs (e.g. "…1082") while
    # the seed fixture (conftest._load_seed_data) may have already loaded the same
    # ValueSet under a versioned ID (e.g. "…1082-20190315").  HAPI enforces unique
    # url+version, so a batch PUT with a different ID silently fails with HAPI-0902.
    # Fix: query HAPI by URL and rewrite our resource ID in-place so the PUT becomes
    # an update of the existing resource (replacing any truncated seed compose with
    # the full connectathon compose).
    for r in deduped:
        if r.get("resourceType") != "ValueSet" or not r.get("url"):
            continue
        try:
            resp = httpx.get(
                f"{TEST_MEASURE_URL}/ValueSet?url={r['url']}&_count=1",
                timeout=10,
            )
            if resp.status_code == 200:
                entries = resp.json().get("entry", [])
                if entries:
                    hapi_id = entries[0]["resource"]["id"]
                    if hapi_id != r["id"]:
                        warnings.warn(
                            f"[VS conflict remap] {r['id']} → {hapi_id} "
                            f"(url={r['url'][-50:]})"
                        )
                        r["id"] = hapi_id
        except httpx.RequestError:
            pass

    if deduped:
        tx = make_put_bundle(deduped)
        resp = httpx.post(TEST_MEASURE_URL, json=tx, headers=headers, timeout=300)
        if resp.status_code == 422 and "HAPI-0902" in resp.text:
            warnings.warn("measure defs already loaded (HAPI-0902)")
        else:
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pytest.fail(f"Failed to load measure defs: {exc}\n{resp.text[:300]}")

    # Pass 3: wait for HAPI to pre-expand large ValueSets before loading clinical data.
    # HAPI's in-memory expansion cap is 1000; ValueSets with >1000 codes produce
    # HAPI-0831 during CQL retrieves, silently returning empty results (IP=0).
    # Background pre-expansion stores codes in HAPI's DB with no size limit.
    large_valueset_ids = [
        r["id"]
        for r in deduped
        if r.get("resourceType") == "ValueSet"
        and r.get("id")
        and sum(len(inc.get("concept", [])) for inc in r.get("compose", {}).get("include", [])) > 900
    ]
    if large_valueset_ids:
        warnings.warn(
            f"Waiting for HAPI to pre-expand {len(large_valueset_ids)} large ValueSet(s)..."
        )
        _wait_for_valueset_expansion(TEST_MEASURE_URL, large_valueset_ids)

    # Pass 4: load clinical data per bundle.
    for measure_id, clinical in clinical_per_bundle:
        # Fix duplicate Claim IDs (MADiE v0.3.x CMS71 exports all Claims with the same ID).
        clinical = fix_duplicate_claim_ids(clinical)
        tx = make_put_bundle(clinical)
        for target, label in [(TEST_CDR_URL, "CDR"), (TEST_MEASURE_URL, "measure server")]:
            resp = httpx.post(target, json=tx, headers=headers, timeout=120)
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pytest.fail(f"[{measure_id}] Failed to load clinical data to {label}: {exc}\n{resp.text[:300]}")

        # Capture first patient+encounter pair to use as reindex probe
        if not probe_patient_id:
            enc_resources = [r for r in clinical if r.get("resourceType") == "Encounter"]
            if enc_resources:
                first_enc = enc_resources[0]
                probe_encounter_id = first_enc.get("id")
                probe_patient_id = first_enc.get("subject", {}).get("reference", "").removeprefix("Patient/")

    # HAPI v8.6.0+ with CR enabled triggers an async REINDEX ~40s after startup.
    # Resources written during that window don't get reference-type search params
    # indexed, so Encounter?patient=X returns 0 and $evaluate-measure yields IP=0.
    # Trigger a fresh $reindex and wait for reference indexes to settle.
    if probe_patient_id and probe_encounter_id:
        for target in (TEST_CDR_URL, TEST_MEASURE_URL):
            _trigger_reindex_and_wait(target, probe_patient_id, probe_encounter_id)


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
    "measure_id,canonical_url,patient_ref,period_start,period_end,expected_populations,measure_strict",
    _PARAMETRIZE_CASES,
)
def test_connectathon_measure_per_patient(
    measure_id: str,
    canonical_url: str,
    patient_ref: str,
    period_start: str,
    period_end: str,
    expected_populations: dict[str, int],
    measure_strict: bool,
) -> None:
    """Evaluate a single test-case MeasureReport and assert population counts match.

    Each parametrized invocation is one patient/measure combination from the
    MADiE connectathon bundles.
    """
    strict = os.environ.get("STRICT_STU6", "1") == "1" and measure_strict

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
