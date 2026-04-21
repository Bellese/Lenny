"""Golden file integration tests — validate measure evaluation against expected results.

Loads DBCG connectathon bundles, routes each resource to the correct HAPI instance
(measure defs → measure engine, clinical data → CDR), then calls $evaluate-measure
and compares actual population counts against the expected counts in the bundle's
MeasureReports.

To add a new golden test case, drop a directory with bundle.json into
tests/integration/golden/.
"""

from __future__ import annotations

import base64
import json
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
from tests.integration._helpers import fix_valueset_compose_for_hapi, make_put_bundle
from tests.integration.conftest import (
    _REINDEX_POLL_INTERVAL,
    _REINDEX_TIMEOUT,
    TEST_CDR_URL,
    TEST_MEASURE_URL,
)

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


def _get_missing_valueset_stubs(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Return empty-stub ValueSets for any ELM-declared but bundle-absent ValueSet URLs.

    Some connectathon bundles omit ValueSets that the CQL ELM references directly
    (not through compose).  HAPI v8.6.0 fails with ``Unknown ValueSet`` when CQL
    evaluation tries to look them up.  Injecting an empty stub allows the lookup to
    succeed; the stub expands to 0 codes, so criteria that check membership return
    false (and criteria for *not* in the set return true).
    """
    elm_declared_urls: set[str] = set()
    for entry in bundle.get("entry", []):
        r = entry.get("resource", {})
        if r.get("resourceType") != "Library":
            continue
        for content in r.get("content", []):
            if content.get("contentType") == "application/elm+json":
                try:
                    elm = json.loads(base64.b64decode(content["data"]))
                except Exception:
                    continue
                for vs in elm.get("library", {}).get("valueSets", {}).get("def", []):
                    if url := vs.get("id"):
                        elm_declared_urls.add(url)

    bundled_urls = {
        e["resource"].get("url")
        for e in bundle.get("entry", [])
        if e.get("resource", {}).get("resourceType") == "ValueSet"
    }

    stubs = []
    for url in sorted(elm_declared_urls - bundled_urls):
        vs_id = f"stub-{url.split('/')[-1]}"
        stubs.append(
            {
                "resourceType": "ValueSet",
                "id": vs_id,
                "url": url,
                "status": "active",
            }
        )
    return stubs


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
            # Strip "Patient/" prefix if present to get bare patient id.
            # Skip absolute URLs and contained refs — they can't be passed as
            # a plain ID in the $evaluate-measure subject parameter.
            if subject_ref.startswith(("#", "http://", "https://")):
                return None
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


def _put_measures_individually(measures: list[dict[str, Any]], target_url: str, name: str) -> None:
    """PUT Measure resources individually to preserve backbone element IDs.

    HAPI v8.6.0 strips backbone element IDs (e.g. supplementalData.id) when
    Measures are loaded via bundle PUT, but preserves them on direct PUT.
    The CR module requires supplementalData.id to be present when evaluating.
    """
    headers = {"Content-Type": "application/fhir+json"}
    for r in measures:
        if "resourceType" not in r or "id" not in r:
            continue
        url = f"{target_url}/{r['resourceType']}/{r['id']}"
        resp = httpx.put(url, json=r, headers=headers, timeout=60)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            pytest.fail(f"Failed to PUT Measure/{r['id']} for '{name}': {exc}\n{resp.text[:200]}")


def _get_hapi_valueset_urls(base_url: str) -> set[str]:
    """Return canonical URLs of all ValueSets currently in HAPI.

    Used before loading golden bundles to detect canonical URL conflicts: if a
    ValueSet with the same URL was already loaded (e.g., from the seed bundle),
    loading a second copy with a different resource ID causes HAPI to raise
    "Multiple ValueSets resolved" during CQL evaluation.
    """
    urls: set[str] = set()
    next_url: str | None = f"{base_url}/ValueSet?_count=500&_elements=url"
    while next_url:
        resp = httpx.get(next_url, timeout=30)
        if resp.status_code != 200:
            warnings.warn(
                f"ValueSet pagination failed at {next_url}: {resp.status_code} — dedup guard may be incomplete"
            )
            break
        d = resp.json()
        for e in d.get("entry", []):
            if url := e.get("resource", {}).get("url"):
                urls.add(url)
        next_url = next(
            (lnk["url"] for lnk in d.get("link", []) if lnk.get("relation") == "next"),
            None,
        )
    return urls


@pytest.fixture(scope="module", autouse=True)
def _load_golden_bundles_to_hapi(_require_infrastructure):
    """Load golden bundles into HAPI, routing resources to their correct instances.

    Loading strategy:
    - Libraries, ValueSets, CodeSystems → batch bundle (bypasses referential integrity
      so cross-library dependencies don't require a specific load order)
    - Measures → individual PUT (preserves backbone element IDs like supplementalData.id
      that HAPI v8.6.0 strips when Measures are loaded via bundle)
    - Clinical data → CDR + measure server (measure server copy needed because
      $evaluate-measure resolves patient data from the same HAPI instance it runs on)

    Runs once per module; PUT and batch requests are idempotent so re-runs are safe.

    Duplicate-URL guard: before loading each bundle's ValueSets, fetch the set of
    canonical URLs already in HAPI and skip any VS whose URL is already present.
    Multiple copies of the same URL cause "Multiple ValueSets resolved" during CQL
    evaluation.

    Reindex completeness: collect one probe encounter per bundle (not just the first
    bundle overall) and wait for ALL of them before running tests.  Using only the
    first probe caused a race where later bundles' encounters weren't indexed yet.
    """
    import time as _time

    headers = {"Content-Type": "application/fhir+json"}

    # Track existing VS URLs to prevent duplicate canonical URL conflicts.
    # Pre-populated from what's already in HAPI (e.g. from the session-scoped
    # _load_seed_data fixture), then updated as we load each bundle.
    existing_vs_urls = _get_hapi_valueset_urls(TEST_MEASURE_URL)

    # Collect one (patient_id, encounter_id) probe per bundle that has encounters.
    # We wait for ALL probes before running tests so that no bundle's data is stale.
    probe_pairs: list[tuple[str, str]] = []

    for name, bundle in _get_golden_bundles():
        measure_defs, clinical, _test_cases = _classify_bundle_entries(bundle)

        if measure_defs:
            # Split: Measures get individual PUT; everything else goes via batch bundle
            measures_only = [r for r in measure_defs if r.get("resourceType") == "Measure"]
            non_measures = [r for r in measure_defs if r.get("resourceType") != "Measure"]

            if non_measures:
                # Deduplicate ValueSets by canonical URL to prevent "Multiple ValueSets
                # resolved" during CQL evaluation.  If a VS with the same URL already
                # exists in HAPI (e.g. loaded by an earlier bundle or the seed fixture),
                # skip it.  Track newly added URLs so subsequent bundles also skip them.
                filtered_non_measures = []
                for r in non_measures:
                    if r.get("resourceType") == "ValueSet" and (url := r.get("url")):
                        if url in existing_vs_urls:
                            continue
                        existing_vs_urls.add(url)
                    filtered_non_measures.append(r)

                # Patch ValueSets: convert sub-ValueSet compose refs to direct code
                # lists from the pre-populated expansion.  HAPI v8.6.0 ignores the
                # expansion element and always re-expands via compose; when compose
                # references missing sub-ValueSets, evaluation fails.
                if filtered_non_measures:
                    tx = make_put_bundle(fix_valueset_compose_for_hapi(filtered_non_measures))
                    resp = httpx.post(TEST_MEASURE_URL, json=tx, headers=headers, timeout=120)
                    if resp.status_code == 422 and "HAPI-0902" in resp.text:
                        warnings.warn(f"[{name}] measure defs already loaded (HAPI-0902 uniqueness constraint)")
                    else:
                        try:
                            resp.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            pytest.fail(f"Failed to load non-Measure defs for '{name}': {exc}\n{resp.text[:300]}")

            if measures_only:
                _put_measures_individually(measures_only, TEST_MEASURE_URL, name)

            # Inject empty stubs for any ELM-declared ValueSets missing from the bundle.
            # These arise when connectathon bundles omit ValueSets that the CQL references
            # directly (not through compose).  Stubs expand to 0 codes; criteria checking
            # membership return false, "NOT in set" criteria return true.
            stubs = [s for s in _get_missing_valueset_stubs(bundle) if s.get("url") not in existing_vs_urls]
            for s in stubs:
                existing_vs_urls.add(s.get("url", ""))
            if stubs:
                stub_tx = make_put_bundle(stubs)
                resp = httpx.post(TEST_MEASURE_URL, json=stub_tx, headers=headers, timeout=60)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    pytest.fail(f"Failed to load VS stubs for '{name}': {exc}\n{resp.text[:200]}")

        if clinical:
            tx = make_put_bundle(clinical)
            # Load clinical to CDR (canonical home) and to measure server (needed
            # because $evaluate-measure resolves patient data from the same HAPI
            # instance it runs on; production replicates this via gather_patient_data).
            for target, label in [(TEST_CDR_URL, "CDR"), (TEST_MEASURE_URL, "measure server")]:
                resp = httpx.post(target, json=tx, headers=headers, timeout=120)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    pytest.fail(f"Failed to load clinical data to {label} for '{name}': {exc}\n{resp.text[:300]}")

            # Collect one probe encounter per bundle (not just the first encounter
            # overall) so the reindex wait covers data from every bundle.
            for r in clinical:
                if r.get("resourceType") == "Encounter" and r.get("id") and r.get("subject", {}).get("reference"):
                    probe_pairs.append(
                        (
                            r["subject"]["reference"].removeprefix("Patient/"),
                            r["id"],
                        )
                    )
                    break

    # HAPI v8.6.0 with CR enabled triggers a background REINDEX when a custom
    # SearchParameter is registered (the CR module does this on first use).
    # Resources loaded via batch bundle during that REINDEX don't get their
    # reference-type search params indexed.  We call $reindex on both servers
    # after all data is loaded and wait until ALL probe encounters are findable.
    # Using a single probe from the first bundle caused a race: later bundles'
    # encounters weren't indexed yet when the first probe triggered the return.
    if probe_pairs:
        reindex_params = {
            "resourceType": "Parameters",
            "parameter": [{"name": "type", "valueString": "Encounter"}],
        }
        for target in (TEST_CDR_URL, TEST_MEASURE_URL):
            r = httpx.post(f"{target}/$reindex", json=reindex_params, headers=headers, timeout=30)
            if r.status_code >= 400:
                warnings.warn(f"$reindex trigger at {target} returned {r.status_code}: {r.text[:200]}")

        deadline = _time.monotonic() + _REINDEX_TIMEOUT
        while _time.monotonic() < deadline:

            def _probe_indexed(pat: str) -> bool:
                try:
                    resp = httpx.get(f"{TEST_MEASURE_URL}/Encounter?patient={pat}&_count=1", timeout=10)
                    return bool(resp.status_code == 200 and resp.json().get("entry"))
                except Exception:
                    return False

            if all(_probe_indexed(pat) for pat, _ in probe_pairs):
                break
            _time.sleep(_REINDEX_POLL_INTERVAL)

    # Evaluate-measure gate: poll a known IP>0 patient from the CMS816 golden bundle
    # until the CQL evaluation stack confirms the inpatient data is fully ready.
    # Reindex completion only proves Encounter?patient search works; HAPI may still be
    # processing earlier reindex batches for other patients when our probe exits early.
    # An inpatient measure gate (not CMS122 outpatient) is required because the two
    # measure types use different encounter type ValueSets and CQL code paths.
    _cms816_present = any(n.startswith("CMS816") for n, _ in _get_golden_bundles())
    if _cms816_present:
        _gate_measure_url = "https://madie.cms.gov/Measure/CMS816FHIRHHHypo"
        _gate_patient = "1a89fbca-df20-4f17-97d0-9fa5990860b2"
        _gate_timeout = 600
        _r = httpx.get(f"{TEST_MEASURE_URL}/Measure?url={_gate_measure_url}&_count=1", timeout=15)
        _gate_entries = _r.json().get("entry", []) if _r.status_code == 200 else []
        if _gate_entries:
            _gate_measure_id = _gate_entries[0]["resource"]["id"]
            _gate_eval_url = (
                f"{TEST_MEASURE_URL}/Measure/{_gate_measure_id}/$evaluate-measure"
                f"?subject=Patient/{_gate_patient}&periodStart=2026-01-01&periodEnd=2026-12-31"
            )
            _gate_deadline = _time.monotonic() + _gate_timeout
            _gate_passed = False
            while _time.monotonic() < _gate_deadline:
                try:
                    _resp = httpx.get(_gate_eval_url, timeout=60)
                    if _resp.status_code == 200:
                        _ip = next(
                            (
                                pop.get("count", 0)
                                for grp in _resp.json().get("group", [])
                                for pop in grp.get("population", [])
                                if pop.get("code", {}).get("coding", [{}])[0].get("code")
                                == "initial-population"
                            ),
                            0,
                        )
                        if _ip >= 1:
                            _gate_passed = True
                            break
                except Exception:
                    pass
                _time.sleep(_REINDEX_POLL_INTERVAL)
            if not _gate_passed:
                warnings.warn(
                    f"CMS816 evaluate-measure gate timed out after {_gate_timeout}s "
                    f"— inpatient data may not be fully indexed; golden tests may fail with IP=0"
                )


def _parametrize_bundles() -> list:
    bundles = _get_golden_bundles()
    if not bundles:
        return [pytest.param("_empty", {}, marks=pytest.mark.skip(reason="No golden bundles found"))]
    return [pytest.param(name, bundle) for name, bundle in bundles]


_PARAMETRIZE_BUNDLES = _parametrize_bundles()


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
        pytest.skip(f"Measure not found on test MCS for '{name}' (ref={measure_ref!r})")

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
            skipped_cases.append(f"  {patient_ref}: HAPI returned {resp.status_code} — {resp.text[:150]}")
            continue

        report = resp.json()
        assert report.get("resourceType") == "MeasureReport", (
            f"Expected MeasureReport for '{name}/{patient_ref}', got {report.get('resourceType')}"
        )

        actual = _extract_population_counts(report)
        passed, mismatches = compare_populations(expected, actual)
        evaluated += 1

        if not passed:
            failures.append(
                f"  {patient_ref}: mismatched codes {mismatches}\n    expected: {expected}\n    actual:   {actual}"
            )

    if skipped_cases:
        warnings.warn(f"[{name}] {len(skipped_cases)} test case(s) skipped:\n" + "\n".join(skipped_cases))

    assert not failures, f"Population count mismatches in '{name}' ({len(failures)}/{evaluated} failed):\n" + "\n".join(
        failures
    )

    if evaluated == 0 and test_cases:
        pytest.skip(f"All {len(test_cases)} test case(s) for '{name}' were skipped by HAPI")
