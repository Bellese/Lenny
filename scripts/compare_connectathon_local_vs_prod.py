#!/usr/bin/env python3
"""Compare connectathon validation results between local and production MCT2 APIs.

For each manifest measure, this script:
  1. Uploads the current bundle to both environments.
  2. Waits for bundle processing to finish.
  3. Starts a measure-filtered validation run in both environments.
  4. Polls each run to completion.
  5. Writes per-measure JSON + Markdown diff artifacts plus an overall summary.
"""

from __future__ import annotations

import argparse
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


def upload_bundle(api_base: str, bundle_path: Path) -> int:
    content = bundle_path.read_bytes()
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


def start_validation_run(api_base: str, measure_url: str) -> int:
    response = _http_json("POST", f"{api_base}/validation/run", json_body={"measure_urls": [measure_url]})
    return int(response["id"])


def wait_for_run(api_base: str, run_id: int, timeout_seconds: int, poll_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = _http_json("GET", f"{api_base}/validation/runs/{run_id}")
        if run.get("status") in {"complete", "failed"}:
            return run
        time.sleep(poll_seconds)
    raise TimeoutError(f"Validation run {run_id} did not finish within {timeout_seconds}s on {api_base}")


def normalize_run(run: dict[str, Any], measure_url: str) -> dict[str, Any]:
    measure_block = next((m for m in run.get("measures", []) if m.get("measure_url") == measure_url), None)
    patients = {}
    if measure_block:
        for patient in measure_block.get("patients", []):
            patient_ref = patient.get("patient_ref", "")
            patients[patient_ref] = {
                "patient_name": patient.get("patient_name"),
                "status": patient.get("status"),
                "expected_populations": patient.get("expected_populations"),
                "actual_populations": patient.get("actual_populations"),
                "mismatches": patient.get("mismatches") or [],
                "error_message": patient.get("error_message"),
            }

    return {
        "status": run.get("status"),
        "error_message": run.get("error_message"),
        "measures_tested": run.get("measures_tested"),
        "patients_tested": run.get("patients_tested"),
        "patients_passed": run.get("patients_passed"),
        "patients_failed": run.get("patients_failed"),
        "measure": {
            "measure_url": measure_url,
            "passed": measure_block.get("passed", 0) if measure_block else 0,
            "failed": measure_block.get("failed", 0) if measure_block else 0,
            "errors": measure_block.get("errors", 0) if measure_block else 0,
            "patients": patients,
        },
    }


def compare_normalized_runs(local_run: dict[str, Any], prod_run: dict[str, Any]) -> dict[str, Any]:
    local_patients = local_run["measure"]["patients"]
    prod_patients = prod_run["measure"]["patients"]
    patient_refs = sorted(set(local_patients) | set(prod_patients))

    patient_diffs = []
    for patient_ref in patient_refs:
        local_patient = local_patients.get(patient_ref)
        prod_patient = prod_patients.get(patient_ref)
        if local_patient == prod_patient:
            continue
        patient_diffs.append(
            {
                "patient_ref": patient_ref,
                "local": local_patient,
                "production": prod_patient,
            }
        )

    summary = {
        "local_status": local_run["status"],
        "production_status": prod_run["status"],
        "local_error_message": local_run["error_message"],
        "production_error_message": prod_run["error_message"],
        "local_counts": {
            "patients_tested": local_run["patients_tested"],
            "patients_passed": local_run["patients_passed"],
            "patients_failed": local_run["patients_failed"],
        },
        "production_counts": {
            "patients_tested": prod_run["patients_tested"],
            "patients_passed": prod_run["patients_passed"],
            "patients_failed": prod_run["patients_failed"],
        },
        "patient_diff_count": len(patient_diffs),
        "matches": len(patient_diffs) == 0 and local_run["status"] == prod_run["status"],
        "patient_diffs": patient_diffs,
    }
    return summary


def markdown_for_measure(measure_id: str, measure_url: str, comparison: dict[str, Any]) -> str:
    lines = [
        f"# {measure_id}",
        "",
        f"- Measure URL: `{measure_url}`",
        f"- Local run status: `{comparison['local_status']}`",
        f"- Production run status: `{comparison['production_status']}`",
        f"- Patient diffs: `{comparison['patient_diff_count']}`",
        "",
        "## Counts",
        "",
        "| Environment | Tested | Passed | Failed |",
        "|---|---:|---:|---:|",
        (
            f"| Local | {comparison['local_counts']['patients_tested']} | "
            f"{comparison['local_counts']['patients_passed']} | {comparison['local_counts']['patients_failed']} |"
        ),
        (
            f"| Production | {comparison['production_counts']['patients_tested']} | "
            f"{comparison['production_counts']['patients_passed']} | "
            f"{comparison['production_counts']['patients_failed']} |"
        ),
        "",
    ]

    if comparison["local_error_message"] or comparison["production_error_message"]:
        lines.extend(
            [
                "## Run Errors",
                "",
                f"- Local: {comparison['local_error_message'] or 'none'}",
                f"- Production: {comparison['production_error_message'] or 'none'}",
                "",
            ]
        )

    if comparison["patient_diffs"]:
        lines.extend(["## Patient Diffs", ""])
        for diff in comparison["patient_diffs"]:
            lines.append(f"### `{diff['patient_ref']}`")
            lines.append("")
            lines.append(f"- Local: `{json.dumps(diff['local'], sort_keys=True)}`")
            lines.append(f"- Production: `{json.dumps(diff['production'], sort_keys=True)}`")
            lines.append("")
    else:
        lines.extend(["## Patient Diffs", "", "No patient-level differences detected.", ""])

    return "\n".join(lines)


def summary_markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "# Connectathon Local vs Production Summary",
        "",
        "| Measure | Match | Local | Production | Patient diffs |",
        "|---|---|---|---|---:|",
    ]
    for result in results:
        comparison = result["comparison"]
        lines.append(
            f"| {result['measure_id']} | "
            f"{'yes' if comparison['matches'] else 'no'} | "
            f"{comparison['local_status']} | "
            f"{comparison['production_status']} | "
            f"{comparison['patient_diff_count']} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-api-base",
        default="http://localhost:8000",
        help="Local MCT2 API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--production-api-base",
        default="https://api.98-89-219-217.nip.io",
        help="Production MCT2 API base URL",
    )
    parser.add_argument(
        "--manifest",
        default=str(MANIFEST_PATH),
        help="Path to manifest.json",
    )
    parser.add_argument(
        "--measure-id",
        help="Optional single measure id to run",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "artifacts" / "connectathon-local-vs-prod"),
        help="Directory for JSON/Markdown output",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Per-upload and per-run timeout",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=5,
        help="Polling interval",
    )
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
    prod_api = _normalize_base_url(args.production_api_base)

    results = []
    for measure in measures:
        measure_id = measure["id"]
        measure_url = measure["canonical_url"]
        bundle_path = manifest_path.parent / measure["bundle_file"]

        print(f"[measure] {measure_id}", flush=True)

        local_upload_id = upload_bundle(local_api, bundle_path)
        prod_upload_id = upload_bundle(prod_api, bundle_path)

        local_upload = wait_for_upload(local_api, local_upload_id, args.timeout_seconds, args.poll_seconds)
        prod_upload = wait_for_upload(prod_api, prod_upload_id, args.timeout_seconds, args.poll_seconds)

        local_run_id = start_validation_run(local_api, measure_url)
        prod_run_id = start_validation_run(prod_api, measure_url)

        local_run = wait_for_run(local_api, local_run_id, args.timeout_seconds, args.poll_seconds)
        prod_run = wait_for_run(prod_api, prod_run_id, args.timeout_seconds, args.poll_seconds)

        normalized_local = normalize_run(local_run, measure_url)
        normalized_prod = normalize_run(prod_run, measure_url)
        comparison = compare_normalized_runs(normalized_local, normalized_prod)

        measure_payload = {
            "measure_id": measure_id,
            "measure_url": measure_url,
            "bundle_file": measure["bundle_file"],
            "local_upload": local_upload,
            "production_upload": prod_upload,
            "local_run": normalized_local,
            "production_run": normalized_prod,
            "comparison": comparison,
        }
        results.append(measure_payload)

        json_path = output_dir / f"{measure_id}.json"
        md_path = output_dir / f"{measure_id}.md"
        json_path.write_text(json.dumps(measure_payload, indent=2, sort_keys=True), encoding="utf-8")
        md_path.write_text(markdown_for_measure(measure_id, measure_url, comparison), encoding="utf-8")

    summary_payload = {"generated_at_epoch": int(time.time()), "results": results}
    (output_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2, sort_keys=True), encoding="utf-8")
    (output_dir / "summary.md").write_text(summary_markdown(results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
