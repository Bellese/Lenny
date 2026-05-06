#!/usr/bin/env python3
"""Validate all Lenny connectathon measures against known-good expected populations.

Runs each measure through Lenny's Jobs pipeline and compares per-patient
population outputs against ground-truth expected populations embedded in the
connectathon bundle test-case MeasureReports.

No HAPI calls are made — only local bundle JSON reads + Lenny REST API calls.
The prebaked HAPI images must be running (all patients + Groups + measure
definitions pre-loaded).

Exit codes:
  0  All strict measures pass (excluding known xfails)
  1  One or more strict-measure population mismatches or Lenny errors
  2  Configuration/infrastructure error (API unreachable, bundle dir missing,
     prebaked image stale)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE_DIR = REPO_ROOT / "seed" / "connectathon-bundles"

# Maps FHIR hyphenated population codes to Lenny's DB underscore keys.
_FHIR_TO_DB_KEY: dict[str, str] = {
    "initial-population": "initial_population",
    "denominator": "denominator",
    "denominator-exclusion": "denominator_exclusion",
    "numerator": "numerator",
    "numerator-exclusion": "numerator_exclusion",
}

# Known HAPI CQL divergences — mirrors test_connectathon_measures.py:_HAPI_DE_XFAIL.
# Root cause: HAPI v8.8.0 evaluates AIFrailLTCF exclusion criteria differently
# from the MADiE CQL reference engine (frailty/dementia/mastectomy timing).
_HAPI_DE_XFAIL: frozenset[tuple[str, str]] = frozenset(
    {
        # CMS122 — AIFrailLTCF frailty criteria divergence (×6)
        (
            "CMS122FHIRDiabetesAssessGreaterThan9Percent",
            "9cba6cfa-9671-4850-803d-e286c7d59ee7",
        ),
        (
            "CMS122FHIRDiabetesAssessGreaterThan9Percent",
            "ede0ee7a-18ab-4ba7-934c-23618f1270ea",
        ),
        (
            "CMS122FHIRDiabetesAssessGreaterThan9Percent",
            "3b62b0a8-44f2-4365-bcb9-7cadef5bab2e",
        ),
        (
            "CMS122FHIRDiabetesAssessGreaterThan9Percent",
            "e61be907-af68-493f-a6bc-3d93ef8b6c6e",
        ),
        (
            "CMS122FHIRDiabetesAssessGreaterThan9Percent",
            "cade5021-b1bf-43e9-a0a4-659c05b386d0",
        ),
        (
            "CMS122FHIRDiabetesAssessGreaterThan9Percent",
            "f5771b74-a7de-439a-a51f-49a3863e086b",
        ),
        # CMS125 — AIFrailLTCF + mastectomy period-end boundary (×10)
        ("CMS125FHIRBreastCancerScreening", "4cf81a94-81fb-4be2-b075-7d8f9ff02a6e"),
        ("CMS125FHIRBreastCancerScreening", "d4540640-2561-4ebd-b7c6-15878a4dc582"),
        ("CMS125FHIRBreastCancerScreening", "857fec09-9c8c-4e4b-a123-85f473b8fc2a"),
        ("CMS125FHIRBreastCancerScreening", "14b87edd-7f1e-4f6a-9910-f905966ec904"),
        ("CMS125FHIRBreastCancerScreening", "5e3f01ad-1eda-4cb7-8d37-1146beae59e9"),
        ("CMS125FHIRBreastCancerScreening", "8278ae07-69ec-469c-ae01-e933d051f764"),
        ("CMS125FHIRBreastCancerScreening", "f38ce16a-658f-4aa0-b4a6-fac61d2e58a8"),
        ("CMS125FHIRBreastCancerScreening", "da85601e-ce6f-4351-b639-1e58c725bf2f"),
        ("CMS125FHIRBreastCancerScreening", "0ced1e0c-9c92-4582-a4b1-e44f130e436f"),
        ("CMS125FHIRBreastCancerScreening", "24557438-17c9-405c-88dc-0c0bfda17d27"),
        # CMS130 — dementia condition divergence (×1)
        ("CMS130FHIRColorectalCancerScreening", "f9ef1fd1-cced-47ad-a47b-d9c20254511c"),
    }
)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_json(
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    headers = {"Accept": "application/json"}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} → HTTP {exc.code}: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} → {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Bundle parsing
# ---------------------------------------------------------------------------


def _is_test_case(resource: dict[str, Any]) -> bool:
    for ext in resource.get("modifierExtension", []):
        if (
            ext.get("url")
            == "http://hl7.org/fhir/us/cqfmeasures/StructureDefinition/cqfm-isTestCase"
            and ext.get("valueBoolean") is True
        ):
            return True
    return resource.get("type") == "individual" and resource.get("status") == "complete"


def _parse_bundle(
    bundle_path: Path,
) -> tuple[str | None, str | None, dict[str, dict[str, bool]]]:
    """Parse a connectathon bundle file.

    Returns (period_start, period_end, {patient_id: {pop_key: bool}}).
    patient_id is the bare UUID (no "Patient/" prefix).
    Population keys use Lenny's DB underscore format (e.g. "initial_population").
    """
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    period_start: str | None = None
    period_end: str | None = None
    patients: dict[str, dict[str, bool]] = {}

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") != "MeasureReport":
            continue
        if not _is_test_case(resource):
            continue

        # Extract patient ref from contained Parameters
        patient_ref: str | None = None
        for contained in resource.get("contained", []):
            if contained.get("resourceType") == "Parameters":
                for param in contained.get("parameter", []):
                    if param.get("name") == "subject":
                        patient_ref = param.get("valueString", "")
                        break
                if patient_ref:
                    break
        if not patient_ref:
            continue
        patient_id = patient_ref.removeprefix("Patient/")

        # Extract period (use first found)
        if period_start is None:
            period = resource.get("period", {})
            period_start = period.get("start") or None
            period_end = period.get("end") or None

        # Extract and normalize populations
        raw_pops: dict[str, int] = {}
        for group in resource.get("group", []):
            for pop in group.get("population", []):
                for coding in pop.get("code", {}).get("coding", []):
                    code = coding.get("code", "")
                    if code in _FHIR_TO_DB_KEY:
                        raw_pops[code] = raw_pops.get(code, 0) + pop.get("count", 0)

        patients[patient_id] = {
            _FHIR_TO_DB_KEY[k]: bool(v)
            for k, v in raw_pops.items()
            if k in _FHIR_TO_DB_KEY
        }

    return period_start, period_end, patients


# ---------------------------------------------------------------------------
# Lenny API helpers
# ---------------------------------------------------------------------------


def _check_api_reachable(base_url: str) -> None:
    try:
        _http_json("GET", f"{base_url}/health", timeout=10)
    except RuntimeError as exc:
        print(f"ERROR: Lenny API unreachable at {base_url}: {exc}", file=sys.stderr)
        sys.exit(2)


def _check_measures_present(base_url: str, expected_canonical_urls: list[str]) -> None:
    """Probe GET /measures and assert all expected canonical URLs are present."""
    try:
        result = _http_json("GET", f"{base_url}/measures", timeout=30)
    except RuntimeError as exc:
        print(f"ERROR: GET /measures failed: {exc}", file=sys.stderr)
        sys.exit(2)

    present_urls = {m.get("url") for m in result.get("measures", [])}
    missing = [url for url in expected_canonical_urls if url not in present_urls]
    if missing:
        print(
            "ERROR: Prebaked image appears stale or not loaded. "
            f"Missing {len(missing)} measure(s) from measure engine:",
            file=sys.stderr,
        )
        for url in missing[:5]:
            print(f"  {url}", file=sys.stderr)
        if len(missing) > 5:
            print(f"  ... and {len(missing) - 5} more", file=sys.stderr)
        sys.exit(2)


def _create_job(
    base_url: str, measure_id: str, period_start: str, period_end: str
) -> int:
    resp = _http_json(
        "POST",
        f"{base_url}/jobs",
        json_body={
            "measure_id": measure_id,
            "group_id": measure_id,
            "period_start": period_start,
            "period_end": period_end,
        },
        timeout=30,
    )
    return int(resp["id"])


def _poll_job(
    base_url: str, job_id: int, timeout_seconds: int, poll_seconds: int
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = _http_json("GET", f"{base_url}/jobs/{job_id}", timeout=15)
        if job.get("status") in {"complete", "failed"}:
            return job
        time.sleep(poll_seconds)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout_seconds}s")


def _get_results(base_url: str, job_id: int) -> list[dict[str, Any]]:
    resp = _http_json("GET", f"{base_url}/results?job_id={job_id}", timeout=30)
    return resp.get("patients", [])


# ---------------------------------------------------------------------------
# Per-measure validation
# ---------------------------------------------------------------------------


def _validate_measure(
    base_url: str,
    measure_id: str,
    strict: bool,
    bundle_path: Path,
    timeout_seconds: int,
    poll_seconds: int,
    failures_only: bool,
) -> dict[str, Any]:
    """Run one measure through Lenny Jobs and validate results.

    Returns a summary dict with keys: pass_count, fail_count, xfail_count,
    error_count, failures (list of dicts), job_status, job_id.
    """
    period_start, period_end, expected_patients = _parse_bundle(bundle_path)
    if not period_start or not period_end:
        return {
            "measure_id": measure_id,
            "job_id": None,
            "job_status": "skipped",
            "pass_count": 0,
            "fail_count": 0,
            "xfail_count": 0,
            "error_count": 0,
            "failures": [
                {"reason": "No measurement period found in bundle test cases"}
            ],
        }

    job_id = _create_job(base_url, measure_id, period_start, period_end)

    try:
        job = _poll_job(base_url, job_id, timeout_seconds, poll_seconds)
    except TimeoutError as exc:
        return {
            "measure_id": measure_id,
            "job_id": job_id,
            "job_status": "timeout",
            "pass_count": 0,
            "fail_count": 0,
            "xfail_count": 0,
            "error_count": 1,
            "failures": [{"reason": str(exc)}],
        }

    if job.get("status") == "failed":
        return {
            "measure_id": measure_id,
            "job_id": job_id,
            "job_status": "failed",
            "pass_count": 0,
            "fail_count": 0,
            "xfail_count": 0,
            "error_count": 1,
            "failures": [
                {"reason": f"Job failed: {job.get('error_message', 'unknown')}"}
            ],
        }

    patients = _get_results(base_url, job_id)

    pass_count = 0
    fail_count = 0
    xfail_count = 0
    error_count = 0
    failures: list[dict[str, Any]] = []

    for r in patients:
        pid = r["patient_id"]
        is_xfail = (measure_id, pid) in _HAPI_DE_XFAIL

        if r.get("status") == "error":
            if is_xfail:
                xfail_count += 1
            else:
                error_count += 1
                failures.append(
                    {
                        "patient_id": pid,
                        "type": "lenny_error",
                        "error_message": r.get("error_message"),
                        "error_phase": r.get("error_phase"),
                    }
                )
            continue

        exp = expected_patients.get(pid)
        if exp is None:
            continue  # no ground truth for this patient

        actual = r.get("populations", {})
        mismatches = [k for k, v in exp.items() if actual.get(k) != v]

        if not mismatches:
            if is_xfail:
                # Expected failure but actually passed — note it but don't fail
                pass_count += 1
            else:
                pass_count += 1
        else:
            if is_xfail:
                xfail_count += 1
            elif strict:
                fail_count += 1
                failures.append(
                    {
                        "patient_id": pid,
                        "type": "population_mismatch",
                        "expected": exp,
                        "actual": {k: actual.get(k) for k in exp},
                        "mismatches": mismatches,
                    }
                )
            else:
                # Non-strict measure: count as pass, not failure.
                # Non-strict measures have known MADiE export issues — mismatches
                # do not indicate a Lenny bug and must not trigger exit 1.
                pass_count += 1

    return {
        "measure_id": measure_id,
        "job_id": job_id,
        "job_status": job.get("status"),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "xfail_count": xfail_count,
        "error_count": error_count,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _print_summary(results: list[dict[str, Any]]) -> None:
    print()
    print(
        f"{'Measure':<50} {'Cases':>6} {'Pass':>5} {'Fail':>5} {'XFail':>6} {'Err':>5} {'Status'}"
    )
    print("-" * 95)
    for r in results:
        total = r["pass_count"] + r["fail_count"] + r["xfail_count"] + r["error_count"]
        status = "✓" if r["fail_count"] == 0 and r["error_count"] == 0 else "✗"
        print(
            f"{r['measure_id']:<50} {total:>6} {r['pass_count']:>5} {r['fail_count']:>5} "
            f"{r['xfail_count']:>6} {r['error_count']:>5} {status}"
        )
    print()


def _print_failures(results: list[dict[str, Any]]) -> None:
    for r in results:
        if not r["failures"]:
            continue
        print(f"\n{'=' * 60}")
        print(f"FAILURES: {r['measure_id']} (job {r['job_id']})")
        print(f"{'=' * 60}")
        for f in r["failures"]:
            if f.get("type") == "lenny_error":
                print(
                    f"  [{f['patient_id']}] Lenny error in {f.get('error_phase', '?')}: {f.get('error_message', '?')}"
                )
            elif f.get("type") == "population_mismatch":
                print(f"  [{f['patient_id']}] Population mismatch")
                for k in f.get("mismatches", []):
                    exp_val = f["expected"].get(k)
                    act_val = f["actual"].get(k)
                    print(f"    {k}: expected={exp_val} actual={act_val}")
            else:
                print(f"  {f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Lenny API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--bundle-dir",
        default=str(DEFAULT_BUNDLE_DIR),
        help="Path to connectathon bundles directory",
    )
    parser.add_argument(
        "--measures",
        help="Comma-separated measure IDs to run (default: all)",
    )
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="Only print failing patients (suppress passing rows)",
    )
    parser.add_argument(
        "--json-out",
        help="Write structured JSON failure report to this path",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="Per-job timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=5,
        help="Job status polling interval in seconds (default: 5)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    bundle_dir = Path(args.bundle_dir)

    # Startup checks
    if not bundle_dir.exists():
        print(f"ERROR: Bundle directory not found: {bundle_dir}", file=sys.stderr)
        sys.exit(2)

    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest.json not found in {bundle_dir}", file=sys.stderr)
        sys.exit(2)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    measures = manifest.get("measures", [])

    # Filter to requested measures
    if args.measures:
        requested = {m.strip() for m in args.measures.split(",")}
        measures = [m for m in measures if m["id"] in requested]
        if not measures:
            print(
                f"ERROR: None of the requested measures found in manifest: {args.measures}",
                file=sys.stderr,
            )
            sys.exit(2)

    _check_api_reachable(base_url)

    expected_urls = [m["canonical_url"] for m in measures if "canonical_url" in m]
    _check_measures_present(base_url, expected_urls)

    results = []
    for measure in measures:
        measure_id = measure["id"]
        bundle_file = measure.get("bundle_file")
        strict = measure.get("strict", False)
        if not bundle_file:
            print(f"[{measure_id}] SKIP — no bundle_file in manifest")
            continue

        bundle_path = bundle_dir / bundle_file
        if not bundle_path.exists():
            print(f"[{measure_id}] SKIP — bundle file not found: {bundle_path}")
            continue

        print(f"[{measure_id}] Running...", flush=True)
        result = _validate_measure(
            base_url,
            measure_id,
            strict,
            bundle_path,
            args.timeout_seconds,
            args.poll_seconds,
            args.failures_only,
        )
        results.append(result)

        status_line = (
            f"  pass={result['pass_count']} fail={result['fail_count']} "
            f"xfail={result['xfail_count']} error={result['error_count']} "
            f"job_status={result['job_status']}"
        )
        print(status_line, flush=True)

    _print_summary(results)
    _print_failures(results)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at_epoch": int(time.time()),
            "base_url": base_url,
            "results": results,
        }
        out_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(f"JSON report written to: {args.json_out}")

    _infra_statuses = {"failed", "timeout"}
    strict_failures = sum(
        r["fail_count"] + r["error_count"]
        for r in results
        if r.get("job_status") not in _infra_statuses
    )
    infra_failures = sum(
        1 for r in results if r.get("job_status") in {"failed", "timeout"}
    )

    if infra_failures > 0:
        return 1
    if strict_failures > 0:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        raise SystemExit(130)
