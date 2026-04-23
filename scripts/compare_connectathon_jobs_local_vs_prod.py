#!/usr/bin/env python3
"""Compare real Connectathon Jobs results between local and production MCT2 APIs.

For each manifest measure, this script:
  1. Uploads the measure bundle to both environments.
  2. Adds/replaces a FHIR Group containing that bundle's Patient resources.
  3. Starts a real /jobs calculation using the measure id and that Group.
  4. Polls /jobs/{id} to a terminal state.
  5. Fetches /results?job_id={id} and writes patient-level diff artifacts.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "seed" / "connectathon-bundles" / "manifest.json"
TERMINAL_JOB_STATUSES = {"complete", "completed", "failed", "cancelled", "canceled"}


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def _http_json(
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> Any:
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code} {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc


def _multipart_body(field_name: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"codex-{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode("utf-8"),
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), boundary


def _bundle_resources(bundle: dict[str, Any], resource_type: str) -> list[dict[str, Any]]:
    return [
        entry.get("resource", {})
        for entry in bundle.get("entry", [])
        if entry.get("resource", {}).get("resourceType") == resource_type
    ]


def _measure_id_from_bundle(bundle: dict[str, Any]) -> str:
    measures = _bundle_resources(bundle, "Measure")
    if not measures or not measures[0].get("id"):
        raise ValueError("Bundle does not contain a Measure resource with an id")
    return str(measures[0]["id"])


def _bundle_with_patient_group(bundle_path: Path, group_id: str) -> bytes:
    """Return bundle JSON bytes with a Group/{group_id} for all bundle patients."""
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle = copy.deepcopy(bundle)
    patients = [p for p in _bundle_resources(bundle, "Patient") if p.get("id")]
    group = {
        "resourceType": "Group",
        "id": group_id,
        "type": "person",
        "actual": True,
        "name": group_id,
        "member": [{"entity": {"reference": f"Patient/{patient['id']}"}} for patient in patients],
    }

    replaced = False
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        if resource.get("resourceType") == "Group" and resource.get("id") == group_id:
            entry["resource"] = group
            replaced = True
            break

    if not replaced:
        bundle.setdefault("entry", []).append(
            {
                "resource": group,
                "request": {
                    "method": "PUT",
                    "url": f"Group/{group_id}",
                },
            }
        )

    return json.dumps(bundle, separators=(",", ":")).encode("utf-8")


def upload_bundle(api_base: str, bundle_path: Path, group_id: str) -> int:
    content = _bundle_with_patient_group(bundle_path, group_id)
    body, boundary = _multipart_body("file", bundle_path.name, content)
    response = _http_json(
        "POST",
        f"{api_base}/validation/upload-bundle",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return int(response["id"])


def wait_for_upload(api_base: str, upload_id: int, timeout_seconds: int, poll_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = _http_json("GET", f"{api_base}/validation/uploads")
        for upload in response.get("uploads", []):
            if upload.get("id") != upload_id:
                continue
            status = upload.get("status")
            if status in {"complete", "failed"}:
                return upload
            break
        time.sleep(poll_seconds)
    raise TimeoutError(f"Upload {upload_id} did not finish within {timeout_seconds}s on {api_base}")


def create_job(api_base: str, measure_id: str, group_id: str, period: dict[str, str]) -> int:
    response = _http_json(
        "POST",
        f"{api_base}/jobs",
        json_body={
            "measure_id": measure_id,
            "group_id": group_id,
            "period_start": period["start"],
            "period_end": period["end"],
        },
    )
    return int(response["id"])


def wait_for_job(api_base: str, job_id: int, timeout_seconds: int, poll_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = _http_json("GET", f"{api_base}/jobs/{job_id}")
        if str(job.get("status", "")).lower() in TERMINAL_JOB_STATUSES:
            return job
        time.sleep(poll_seconds)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout_seconds}s on {api_base}")


def get_results(api_base: str, job_id: int) -> dict[str, Any]:
    return _http_json("GET", f"{api_base}/results?job_id={job_id}")


def normalize_job_result(job: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    patients = {}
    for patient in results.get("patients", []):
        patient_id = patient.get("patient_id") or patient.get("id")
        if not patient_id:
            continue
        populations = patient.get("populations") or {}
        patients[patient_id] = {
            "patient_name": patient.get("patient_name"),
            "status": patient.get("status") or ("error" if populations.get("error") else "success"),
            "populations": {
                "initial_population": bool(populations.get("initial_population")),
                "denominator": bool(populations.get("denominator")),
                "numerator": bool(populations.get("numerator")),
                "denominator_exclusion": bool(populations.get("denominator_exclusion")),
                "numerator_exclusion": bool(populations.get("numerator_exclusion")),
            },
            "error": bool(populations.get("error")),
            "error_message": patient.get("error_message") or populations.get("error_message"),
        }

    return {
        "job": {
            "id": job.get("id"),
            "status": job.get("status"),
            "error_message": job.get("error_message"),
            "total_patients": job.get("total_patients"),
            "processed_patients": job.get("processed_patients"),
            "failed_patients": job.get("failed_patients"),
        },
        "results": {
            "total_patients": results.get("total_patients"),
            "failed_patients": results.get("failed_patients"),
            "populations": results.get("populations") or {},
            "performance_rate": results.get("performance_rate"),
            "patients": patients,
        },
    }


def compare_normalized(local: dict[str, Any], production: dict[str, Any]) -> dict[str, Any]:
    local_patients = local["results"]["patients"]
    production_patients = production["results"]["patients"]
    patient_ids = sorted(set(local_patients) | set(production_patients))

    patient_diffs = []
    for patient_id in patient_ids:
        local_patient = local_patients.get(patient_id)
        production_patient = production_patients.get(patient_id)
        if local_patient == production_patient:
            continue
        patient_diffs.append(
            {
                "patient_id": patient_id,
                "local": local_patient,
                "production": production_patient,
            }
        )

    local_job = local["job"]
    production_job = production["job"]
    job_counts_match = {
        key: local_job.get(key) == production_job.get(key)
        for key in ("status", "total_patients", "processed_patients", "failed_patients")
    }
    aggregate_match = local["results"] == production["results"] or (
        local["results"].get("total_patients") == production["results"].get("total_patients")
        and local["results"].get("failed_patients") == production["results"].get("failed_patients")
        and local["results"].get("populations") == production["results"].get("populations")
        and local["results"].get("performance_rate") == production["results"].get("performance_rate")
        and len(patient_diffs) == 0
    )

    return {
        "matches": all(job_counts_match.values()) and aggregate_match,
        "job_counts_match": job_counts_match,
        "patient_diff_count": len(patient_diffs),
        "patient_diffs": patient_diffs,
        "local_job": local_job,
        "production_job": production_job,
        "local_results_summary": {
            key: local["results"].get(key)
            for key in ("total_patients", "failed_patients", "populations", "performance_rate")
        },
        "production_results_summary": {
            key: production["results"].get(key)
            for key in ("total_patients", "failed_patients", "populations", "performance_rate")
        },
    }


def markdown_for_measure(measure_id: str, measure_fhir_id: str, group_id: str, comparison: dict[str, Any]) -> str:
    lines = [
        f"# {measure_id}",
        "",
        f"- Measure FHIR id: `{measure_fhir_id}`",
        f"- Group id: `{group_id}`",
        f"- Match: `{'yes' if comparison['matches'] else 'no'}`",
        f"- Patient diffs: `{comparison['patient_diff_count']}`",
        "",
        "## Job Counts",
        "",
        "| Environment | Job ID | Status | Total | Processed | Failed |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for label, key in (("Local", "local_job"), ("Production", "production_job")):
        job = comparison[key]
        lines.append(
            f"| {label} | {job.get('id')} | {job.get('status')} | {job.get('total_patients')} | "
            f"{job.get('processed_patients')} | {job.get('failed_patients')} |"
        )

    lines.extend(
        [
            "",
            "## Result Aggregates",
            "",
            "| Environment | Result Rows | Failed Rows | Initial Pop | Denominator | Numerator | Denom Excl | Perf Rate |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for label, key in (("Local", "local_results_summary"), ("Production", "production_results_summary")):
        result = comparison[key]
        pops = result.get("populations") or {}
        lines.append(
            f"| {label} | {result.get('total_patients')} | {result.get('failed_patients')} | "
            f"{pops.get('initial_population')} | {pops.get('denominator')} | {pops.get('numerator')} | "
            f"{pops.get('denominator_exclusion')} | {result.get('performance_rate')} |"
        )

    if comparison["patient_diffs"]:
        lines.extend(["", "## Patient Diffs", ""])
        for diff in comparison["patient_diffs"][:50]:
            lines.append(f"### `{diff['patient_id']}`")
            lines.append("")
            lines.append(f"- Local: `{json.dumps(diff['local'], sort_keys=True)}`")
            lines.append(f"- Production: `{json.dumps(diff['production'], sort_keys=True)}`")
            lines.append("")
        if len(comparison["patient_diffs"]) > 50:
            lines.append(f"Only first 50 of {len(comparison['patient_diffs'])} diffs shown.")
            lines.append("")
    else:
        lines.extend(["", "## Patient Diffs", "", "No patient-level differences detected.", ""])

    return "\n".join(lines)


def summary_markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Connectathon Jobs Local vs Production Summary",
        "",
        "| Measure | Match | Local status | Production status | Local rows | Production rows | Patient diffs |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for result in results:
        comparison = result["comparison"]
        lines.append(
            f"| {result['measure_id']} | {'yes' if comparison['matches'] else 'no'} | "
            f"{comparison['local_job'].get('status')} | {comparison['production_job'].get('status')} | "
            f"{comparison['local_results_summary'].get('total_patients')} | "
            f"{comparison['production_results_summary'].get('total_patients')} | "
            f"{comparison['patient_diff_count']} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-api-base", default="http://localhost:8000")
    parser.add_argument("--production-api-base", default="https://api.98-89-219-217.nip.io")
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--measure-id", help="Optional manifest measure id to run")
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "artifacts" / "connectathon-jobs-local-vs-prod"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--poll-seconds", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    measures = manifest.get("measures", [])
    if args.measure_id:
        measures = [m for m in measures if m.get("id") == args.measure_id]
        if not measures:
            raise SystemExit(f"Measure id not found in manifest: {args.measure_id}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    local_api = _normalize_base_url(args.local_api_base)
    production_api = _normalize_base_url(args.production_api_base)

    all_results = []
    for measure in measures:
        measure_id = measure["id"]
        bundle_path = manifest_path.parent / measure["bundle_file"]
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        measure_fhir_id = _measure_id_from_bundle(bundle)
        group_id = measure_fhir_id

        print(f"[measure] {measure_id} measure={measure_fhir_id} group={group_id}", flush=True)

        local_upload_id = upload_bundle(local_api, bundle_path, group_id)
        production_upload_id = upload_bundle(production_api, bundle_path, group_id)
        local_upload = wait_for_upload(local_api, local_upload_id, args.timeout_seconds, args.poll_seconds)
        production_upload = wait_for_upload(production_api, production_upload_id, args.timeout_seconds, args.poll_seconds)

        local_job_id = create_job(local_api, measure_fhir_id, group_id, measure["period"])
        production_job_id = create_job(production_api, measure_fhir_id, group_id, measure["period"])

        local_job = wait_for_job(local_api, local_job_id, args.timeout_seconds, args.poll_seconds)
        production_job = wait_for_job(production_api, production_job_id, args.timeout_seconds, args.poll_seconds)

        local_results = get_results(local_api, local_job_id)
        production_results = get_results(production_api, production_job_id)

        normalized_local = normalize_job_result(local_job, local_results)
        normalized_production = normalize_job_result(production_job, production_results)
        comparison = compare_normalized(normalized_local, normalized_production)

        payload = {
            "measure_id": measure_id,
            "measure_fhir_id": measure_fhir_id,
            "group_id": group_id,
            "bundle_file": measure["bundle_file"],
            "local_upload": local_upload,
            "production_upload": production_upload,
            "local": normalized_local,
            "production": normalized_production,
            "comparison": comparison,
        }
        all_results.append(payload)

        (output_dir / f"{measure_id}.json").write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        (output_dir / f"{measure_id}.md").write_text(
            markdown_for_measure(measure_id, measure_fhir_id, group_id, comparison),
            encoding="utf-8",
        )

    summary_payload = {"generated_at_epoch": int(time.time()), "results": all_results}
    (output_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "summary.md").write_text(summary_markdown(all_results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
