"""Validate Lenny's Jobs pipeline against ground-truth connectathon expected populations.

Runs all 11 connectathon measures (CMS1017 skipped — HTTP 400 from HAPI) through
Lenny's orchestration pipeline and asserts per-patient population outputs match the
expected populations embedded in the connectathon bundle test-case MeasureReports.

Requires prebaked HAPI images (HAPI_PREBAKED=1):
  - CDR has all 568 test patients + FHIR Groups synthesized per measure_id
  - Measure engine has all 12 measure definitions + Libraries + ValueSets

This test closes the gap between:
  - test_connectathon_measures.py (calls HAPI directly, validates correct counts)
  - test_full_workflow.py (uses Lenny Jobs API, only checks structure not correctness)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from tests.integration.conftest import TEST_CDR_URL, TEST_MEASURE_URL
from tests.integration.test_connectathon_measures import _HAPI_DE_XFAIL

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUNDLE_DIR = _REPO_ROOT / "seed" / "connectathon-bundles"
_MANIFEST_PATH = _BUNDLE_DIR / "manifest.json"

# CMS1017 triggers HTTP 400 from HAPI on $evaluate-measure — skip entirely.
_SKIP_MEASURES = {"CMS1017FHIRHHFI"}

# Maps FHIR hyphenated population codes to Lenny's DB underscore keys.
_FHIR_TO_DB_KEY: dict[str, str] = {
    "initial-population": "initial_population",
    "denominator": "denominator",
    "denominator-exclusion": "denominator_exclusion",
    "numerator": "numerator",
    "numerator-exclusion": "numerator_exclusion",
}

# ---------------------------------------------------------------------------
# Module-level setup — skip entire module if prebaked images not in use
# ---------------------------------------------------------------------------

_MANIFEST: list[dict[str, Any]] = []
if _MANIFEST_PATH.exists():
    _MANIFEST = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8")).get("measures", [])

MEASURES = [
    (m["id"], m.get("expected_test_cases", 0), m.get("strict", False))
    for m in _MANIFEST
    if m["id"] not in _SKIP_MEASURES
]


@pytest.fixture(scope="module", autouse=True)
def _require_prebaked_stack():
    """Skip this entire module unless the prebaked HAPI images are in use.

    Without prebaked images the CDR has no FHIR Groups, so every job produces
    0 patients.
    """
    if os.environ.get("HAPI_PREBAKED") != "1":
        pytest.skip(
            "test_full_jobs_pipeline requires HAPI_PREBAKED=1 (prebaked images with Groups). "
            "Run via: USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_full_jobs_pipeline.py"
        )


# ---------------------------------------------------------------------------
# Population normalization helper
# ---------------------------------------------------------------------------


def _normalize_expected(fhir_pops: dict[str, int]) -> dict[str, bool]:
    """Convert FHIR hyphenated int populations to Lenny's DB underscore bool format."""
    return {_FHIR_TO_DB_KEY[k]: bool(v) for k, v in fhir_pops.items() if k in _FHIR_TO_DB_KEY}


# ---------------------------------------------------------------------------
# Module-scoped fixtures: parse all bundle files once
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundle_data_by_measure() -> dict[str, dict[str, Any]]:
    """Parse all connectathon bundles once per module.

    Returns {measure_id: {"period": (start, end), "patients": {pid: normalized_pops}}}.
    """
    from app.services.validation import _extract_test_case_info

    data: dict[str, dict[str, Any]] = {}
    for measure in _MANIFEST:
        measure_id = measure["id"]
        bundle_file = measure.get("bundle_file")
        if not bundle_file:
            continue
        bundle_path = _BUNDLE_DIR / bundle_file
        if not bundle_path.exists():
            continue

        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        patients: dict[str, dict[str, bool]] = {}
        period: tuple[str, str] | None = None

        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            if resource.get("resourceType") != "MeasureReport":
                continue
            info = _extract_test_case_info(resource)
            if not info:
                continue
            pid = info["patient_ref"].removeprefix("Patient/")
            patients[pid] = _normalize_expected(info["expected_populations"])
            if period is None and info.get("period_start"):
                period = (info["period_start"], info["period_end"])

        if period:
            data[measure_id] = {"period": period, "patients": patients}

    return data


@pytest.fixture(scope="module")
def expected_by_measure(bundle_data_by_measure) -> dict[str, dict[str, dict[str, bool]]]:
    return {mid: d["patients"] for mid, d in bundle_data_by_measure.items()}


@pytest.fixture(scope="module")
def period_by_measure(bundle_data_by_measure) -> dict[str, tuple[str, str]]:
    return {mid: d["period"] for mid, d in bundle_data_by_measure.items()}


# ---------------------------------------------------------------------------
# Parametrized test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("measure_id,expected_test_cases,strict", MEASURES)
async def test_lenny_jobs_produce_correct_results(
    measure_id: str,
    expected_test_cases: int,
    strict: bool,
    integration_client,
    integration_session_factory,
    expected_by_measure: dict[str, dict[str, dict[str, bool]]],
    period_by_measure: dict[str, tuple[str, str]],
) -> None:
    """Run a measure through Lenny's Jobs pipeline and assert correct per-patient populations."""
    from app.services.orchestrator import run_job

    if measure_id not in period_by_measure:
        pytest.skip(f"No measurement period found in bundle for {measure_id}")

    period_start, period_end = period_by_measure[measure_id]
    expected = expected_by_measure.get(measure_id, {})

    # 1. Create job
    resp = await integration_client.post(
        "/jobs",
        json={
            "measure_id": measure_id,
            "group_id": measure_id,
            "period_start": period_start,
            "period_end": period_end,
            "cdr_url": TEST_CDR_URL,
        },
    )
    assert resp.status_code == 201, f"Job creation failed for {measure_id}: {resp.text}"
    job_id = resp.json()["id"]

    # 2. Run the orchestrator directly (normal execution path for integration tests)
    with (
        patch("app.config.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.config.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.orchestrator.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.fhir_client.settings.MEASURE_ENGINE_URL", TEST_MEASURE_URL),
        patch("app.services.fhir_client.settings.DEFAULT_CDR_URL", TEST_CDR_URL),
        patch("app.services.orchestrator.settings.MAX_RETRIES", 1),
        patch("app.services.orchestrator.settings.BATCH_SIZE", 100),
        patch("app.services.orchestrator.async_session", integration_session_factory),
    ):
        await run_job(job_id)

    # 3. Assert job completed
    detail_resp = await integration_client.get(f"/jobs/{job_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "complete", (
        f"{measure_id} job did not complete: status={detail['status']}, error={detail.get('error_message')}"
    )

    # 4. Assert patient count matches FHIR Group membership in CDR
    group_resp = httpx.get(f"{TEST_CDR_URL}/Group/{measure_id}", timeout=15)
    assert group_resp.status_code == 200, (
        f"CDR Group/{measure_id} not found — prebaked image may be missing Groups (HTTP {group_resp.status_code})"
    )
    group_member_count = len(group_resp.json().get("member", []))
    assert detail["total_patients"] == group_member_count, (
        f"{measure_id}: job processed {detail['total_patients']} patients but "
        f"Group/{measure_id} has {group_member_count} members"
    )

    # 5. Fetch per-patient results
    results_resp = await integration_client.get(f"/results?job_id={job_id}")
    assert results_resp.status_code == 200
    patients = results_resp.json()["patients"]

    # 6. Validate per-patient populations
    failures: list[str] = []
    for r in patients:
        pid = r["patient_id"]
        is_xfail = (measure_id, pid) in _HAPI_DE_XFAIL

        if r.get("status") == "error":
            if is_xfail:
                continue  # known xfail — HAPI CQL divergence causes error
            failures.append(f"{pid}: Lenny error in {r.get('error_phase', '?')}: {r.get('error_message', '?')}")
            continue

        exp = expected.get(pid)
        if exp is None:
            continue  # patient in Group but not in bundle test cases — no ground truth

        actual = r.get("populations", {})
        mismatches = [k for k, v in exp.items() if actual.get(k) != v]

        if not mismatches:
            continue

        if is_xfail:
            pytest.xfail(f"Known HAPI CQL divergence for {measure_id}/{pid}: mismatches={mismatches}")

        if strict:
            exp_str = ", ".join(f"{k}={v}" for k, v in sorted(exp.items()))
            act_str = ", ".join(f"{k}={actual.get(k)}" for k in sorted(exp))
            failures.append(f"{pid}: expected [{exp_str}] got [{act_str}]")

    assert not failures, f"Population mismatches in {measure_id} ({len(failures)} patient(s)):\n" + "\n".join(failures)
