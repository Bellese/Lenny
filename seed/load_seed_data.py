#!/usr/bin/env python3
"""Lenny seed data loader.

Loads all seed data into HAPI FHIR in the correct order:
  1. patient-bundle.json  → CDR + measure engine (demo patients for CMS122 1.0.000)
  2. measure-bundle.json  → measure engine (CMS122 1.0.000 Measure + Libraries + ValueSets)
  3. connectathon-bundles → measure engine (measure defs) + CDR + measure engine (clinical)

Idempotent: uses PUT-based batch bundles — safe to re-run.

Applies the same patches as the integration test suite so HAPI can evaluate correctly:
  - ValueSet compose synthesised from expansion codes (HAPI re-expands via compose)
  - Library dependency URLs rewritten ecqi.healthit.gov → madie.cms.gov
  - ValueSet ID conflicts remapped to existing HAPI resources (prevents HAPI-0902)

Environment variables (for the Docker ENTRYPOINT / docker compose seed service):
  CDR_URL     (default http://hapi-fhir-cdr:8080/fhir)
  MEASURE_URL (default http://hapi-fhir-measure:8080/fhir)
  SEED_DIR    (default /seed)
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import time
from typing import Any

import httpx

CDR_URL = os.environ.get("CDR_URL", "http://hapi-fhir-cdr:8080/fhir")
MEASURE_URL = os.environ.get("MEASURE_URL", "http://hapi-fhir-measure:8080/fhir")
SEED_DIR = pathlib.Path(os.environ.get("SEED_DIR", "/seed"))
BUNDLE_DIR = SEED_DIR / "connectathon-bundles"
MANIFEST_PATH = BUNDLE_DIR / "manifest.json"

HEADERS = {"Content-Type": "application/fhir+json"}
_MEASURE_DEF_TYPES = {"Measure", "Library", "ValueSet", "CodeSystem"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[seed] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# HAPI readiness
# ---------------------------------------------------------------------------

def wait_for_server(url: str, name: str, retries: int = 60, interval: int = 5) -> None:
    log(f"Waiting for {name} at {url}/metadata ...")
    for attempt in range(1, retries + 1):
        try:
            r = httpx.get(f"{url}/metadata", timeout=10)
            if r.status_code < 300:
                log(f"{name} is ready.")
                return
        except httpx.RequestError:
            pass
        log(f"{name} not ready (attempt {attempt}/{retries}). Retrying in {interval}s...")
        time.sleep(interval)
    log(f"ERROR: {name} did not become ready after {retries * interval}s.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------

def classify_bundle(bundle: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Split into (measure_def_resources, clinical_resources). Drops MeasureReports."""
    measure_defs: list[dict] = []
    clinical: list[dict] = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource")
        if not resource or "resourceType" not in resource:
            continue
        rt = resource["resourceType"]
        if rt in _MEASURE_DEF_TYPES:
            measure_defs.append(resource)
        elif rt != "MeasureReport":
            clinical.append(resource)
    return measure_defs, clinical


def fix_valueset_compose(resources: list[dict]) -> list[dict]:
    """Synthesise compose from expansion codes for ValueSets HAPI can't re-expand."""
    result = []
    for r in resources:
        if r.get("resourceType") != "ValueSet" or "expansion" not in r:
            result.append(r)
            continue
        include = r.get("compose", {}).get("include", [])
        needs_fix = (
            "compose" not in r
            or not include
            or any(inc.get("valueSet") for inc in include)
            or (
                sum(len(inc.get("concept", [])) for inc in include) == 0
                and not any(inc.get("filter") for inc in include)
            )
        )
        if needs_fix:
            r = copy.deepcopy(r)
            codes_by_system: dict[str, list[dict]] = {}

            def _flatten(nodes: list[dict]) -> None:
                for ce in nodes:
                    sys_ = ce.get("system", "")
                    code = ce.get("code", "")
                    disp = ce.get("display", "")
                    if sys_ and code:
                        entry: dict = {"code": code}
                        if disp:
                            entry["display"] = disp
                        codes_by_system.setdefault(sys_, []).append(entry)
                    if ce.get("contains"):
                        _flatten(ce["contains"])

            _flatten(r["expansion"].get("contains", []))
            r["compose"] = {
                "include": [{"system": s, "concept": c} for s, c in codes_by_system.items()]
            }
        result.append(r)
    return result


def fix_library_deps(resources: list[dict]) -> list[dict]:
    """Rewrite ecqi.healthit.gov Library dep URLs to madie.cms.gov."""
    _ECQI = "http://ecqi.healthit.gov/ecqms/Library/"
    _MADIE = "https://madie.cms.gov/Library/"
    result = []
    for r in resources:
        if r.get("resourceType") != "Library":
            result.append(r)
            continue
        needs_fix = any(
            ra.get("type") == "depends-on" and ra.get("resource", "").startswith(_ECQI)
            for ra in r.get("relatedArtifact", [])
        )
        if needs_fix:
            r = copy.deepcopy(r)
            for ra in r.get("relatedArtifact", []):
                dep = ra.get("resource", "")
                if ra.get("type") == "depends-on" and dep.startswith(_ECQI):
                    ra["resource"] = _MADIE + dep[len(_ECQI):]
        result.append(r)
    return result


def make_put_bundle(resources: list[dict]) -> dict:
    """Wrap resources in a FHIR batch bundle using PUT (idempotent)."""
    return {
        "resourceType": "Bundle",
        "type": "batch",
        "entry": [
            {
                "resource": r,
                "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"},
            }
            for r in resources
            if "resourceType" in r and "id" in r
        ],
    }


def resolve_valueset_id_conflicts(resources: list[dict], measure_url: str) -> list[dict]:
    """Remap ValueSet IDs to match existing HAPI resources (prevents HAPI-0902)."""
    for r in resources:
        if r.get("resourceType") != "ValueSet" or not r.get("url"):
            continue
        try:
            resp = httpx.get(
                f"{measure_url}/ValueSet",
                params={"url": r["url"], "_count": "1"},
                timeout=10,
            )
            if resp.status_code == 200:
                entries = resp.json().get("entry", [])
                if entries:
                    hapi_id = entries[0]["resource"]["id"]
                    if hapi_id != r["id"]:
                        log(f"  [VS remap] {r['id']} → {hapi_id}")
                        r["id"] = hapi_id
        except httpx.RequestError:
            pass
    return resources


def post_bundle(url: str, bundle: dict, label: str, timeout: int = 300) -> bool:
    resp = httpx.post(url, json=bundle, headers=HEADERS, timeout=timeout)
    if resp.status_code == 422 and "HAPI-0902" in resp.text:
        log(f"  {label}: already loaded (HAPI-0902 — idempotent, skipping)")
    elif resp.status_code >= 400:
        log(f"  ERROR {label}: HTTP {resp.status_code}: {resp.text[:400]}")
        return False
    else:
        # FHIR batch returns 200 even when individual entries fail — check each entry.
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/") else {}
        entry_errors = [
            e.get("response", {})
            for e in body.get("entry", [])
            if int(e.get("response", {}).get("status", "200").split(" ")[0]) >= 400
        ]
        if entry_errors:
            log(f"  WARN {label}: HTTP 200 but {len(entry_errors)} entry error(s): "
                f"{[e.get('status') for e in entry_errors[:5]]}")
        else:
            log(f"  {label}: HTTP {resp.status_code} OK")
    return True


def trigger_reindex(base_url: str, patient_id: str, encounter_id: str, timeout: int = 180) -> None:
    params = {
        "resourceType": "Parameters",
        "parameter": [{"name": "type", "valueString": "Encounter"}],
    }
    try:
        httpx.post(f"{base_url}/$reindex", json=params, headers=HEADERS, timeout=30)
        log(f"  $reindex triggered on {base_url}")
    except Exception as exc:
        log(f"  WARNING: $reindex request failed: {exc}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"{base_url}/Encounter",
                params={"patient": patient_id, "_count": "1"},
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("entry"):
                log(f"  $reindex complete on {base_url}")
                return
        except Exception:
            pass
        time.sleep(5)
    log(f"  WARNING: $reindex did not complete within {timeout}s on {base_url}")


# ---------------------------------------------------------------------------
# Group synthesis
# ---------------------------------------------------------------------------

def synthesize_group_from_patients(bundle: dict, measure_id: str) -> dict | None:
    """Return a FHIR Group for the bundle's patients, or None if one already exists.

    Skips synthesis when:
    - The bundle already contains a Group resource (preserves curated data, e.g.
      CMS1017's artifact-testArtifact extension).
    - The bundle has no Patient resources (empty Group would be meaningless).
    """
    entries = bundle.get("entry", [])
    for entry in entries:
        if entry.get("resource", {}).get("resourceType") == "Group":
            return None

    patient_ids = [
        entry["resource"]["id"]
        for entry in entries
        if entry.get("resource", {}).get("resourceType") == "Patient"
        and entry.get("resource", {}).get("id")
    ]
    if not patient_ids:
        return None

    return {
        "resourceType": "Group",
        "id": measure_id,
        "name": measure_id,
        "type": "person",
        "actual": True,
        "member": [{"entity": {"reference": f"Patient/{pid}"}} for pid in patient_ids],
    }


# ---------------------------------------------------------------------------
# Connectathon bundle loader (callable by shim and by main)
# ---------------------------------------------------------------------------

def load_connectathon_bundles(
    cdr_url: str,
    measure_url: str,
    bundle_dir: pathlib.Path,
) -> tuple[str | None, str | None]:
    """Load all connectathon bundles to the measure engine and CDR.

    Returns (probe_patient_id, probe_encounter_id) — the first Encounter-bearing
    patient found, suitable for a subsequent $reindex probe.

    Synthesizes a Group resource for each bundle that does not already include one
    and PUTs it to the CDR only (Groups are not needed for measure evaluation on the
    measure engine).
    """
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        log(f"No manifest found at {manifest_path} — skipping connectathon bundles")
        return None, None

    with open(manifest_path) as f:
        manifest = json.load(f)

    measures = manifest["measures"]
    log(f"Found {len(measures)} connectathon bundles to load")

    # Pass 1: collect + deduplicate measure defs; store raw bundles for Group synthesis
    all_measure_defs: dict[str, dict] = {}
    clinical_per_bundle: list[tuple[str, list[dict], dict]] = []  # (id, clinical, raw_bundle)

    for entry in measures:
        measure_id = entry["id"]
        bundle_path = bundle_dir / entry["bundle_file"]
        if not bundle_path.exists():
            log(f"SKIP: {measure_id} bundle not found")
            continue
        log(f"Parsing {measure_id}...")
        with open(bundle_path) as f:
            bundle = json.load(f)
        measure_defs, clinical = classify_bundle(bundle)
        measure_defs = fix_valueset_compose(measure_defs)
        measure_defs = fix_library_deps(measure_defs)
        for r in measure_defs:
            key = f"{r.get('resourceType')}/{r.get('id')}"
            all_measure_defs[key] = r
        if clinical:
            clinical_per_bundle.append((measure_id, clinical, bundle))

    deduped = list(all_measure_defs.values())
    log(f"Total unique measure def resources: {len(deduped)}")

    # Resolve ValueSet ID conflicts before loading
    log("Resolving ValueSet ID conflicts with existing HAPI resources...")
    deduped = resolve_valueset_id_conflicts(deduped, measure_url)

    # Pass 2: load measure defs to measure engine
    log("Loading measure definitions to measure engine...")
    tx = make_put_bundle(deduped)
    post_bundle(measure_url, tx, "connectathon measure defs → measure engine", timeout=300)

    # Pass 3: load clinical data + synthesized Groups
    probe_patient_id: str | None = None
    probe_encounter_id: str | None = None

    for measure_id, clinical, raw_bundle in clinical_per_bundle:
        log(f"Loading clinical data: {measure_id} ({len(clinical)} resources)...")
        tx = make_put_bundle(clinical)
        post_bundle(cdr_url, tx, f"{measure_id} clinical → CDR")
        post_bundle(measure_url, tx, f"{measure_id} clinical → measure engine")

        # Synthesize a Group for this bundle if it doesn't already include one,
        # and PUT it to the CDR only (not needed on the measure engine)
        group = synthesize_group_from_patients(raw_bundle, measure_id)
        if group:
            log(f"  Synthesizing Group/{group['id']} ({len(group['member'])} members) → CDR")
            group_tx = make_put_bundle([group])
            post_bundle(cdr_url, group_tx, f"{measure_id} Group → CDR")

        if not probe_patient_id:
            enc_resources = [r for r in clinical if r.get("resourceType") == "Encounter"]
            if enc_resources:
                probe_encounter_id = enc_resources[0].get("id")
                probe_patient_id = (
                    enc_resources[0].get("subject", {}).get("reference", "").removeprefix("Patient/")
                )

    return probe_patient_id, probe_encounter_id


# ---------------------------------------------------------------------------
# Main (Docker ENTRYPOINT / docker compose seed service)
# ---------------------------------------------------------------------------

def main() -> None:
    log("Starting Lenny seed data loader...")
    log(f"CDR:            {CDR_URL}")
    log(f"Measure engine: {MEASURE_URL}")
    log(f"Seed dir:       {SEED_DIR}")

    wait_for_server(CDR_URL, "HAPI FHIR CDR")
    wait_for_server(MEASURE_URL, "HAPI FHIR Measure Engine")

    # -----------------------------------------------------------------------
    # Step 1: Load patient-bundle.json → CDR + measure engine
    # -----------------------------------------------------------------------
    patient_bundle_path = SEED_DIR / "patient-bundle.json"
    probe_patient_id: str | None = None
    probe_encounter_id: str | None = None

    if patient_bundle_path.exists():
        log("Loading patient-bundle.json (demo patients)...")
        with open(patient_bundle_path) as f:
            patient_bundle = json.load(f)
        post_bundle(CDR_URL, patient_bundle, "patient-bundle → CDR")
        post_bundle(MEASURE_URL, patient_bundle, "patient-bundle → measure engine")
        entries = patient_bundle.get("entry", [])
        enc_entries = [e["resource"] for e in entries if e.get("resource", {}).get("resourceType") == "Encounter"]
        if enc_entries:
            probe_encounter_id = enc_entries[0].get("id")
            probe_patient_id = enc_entries[0].get("subject", {}).get("reference", "").removeprefix("Patient/")

    # -----------------------------------------------------------------------
    # Step 2: Load measure-bundle.json → measure engine
    # -----------------------------------------------------------------------
    measure_bundle_path = SEED_DIR / "measure-bundle.json"
    if measure_bundle_path.exists():
        log("Loading measure-bundle.json (CMS122 1.0.000 + libraries + value sets)...")
        with open(measure_bundle_path) as f:
            raw = json.load(f)
        resources = [e["resource"] for e in raw.get("entry", []) if "resource" in e]
        resources = fix_valueset_compose(resources)
        resources = fix_library_deps(resources)
        tx = make_put_bundle(resources)
        post_bundle(MEASURE_URL, tx, "measure-bundle → measure engine")

    # -----------------------------------------------------------------------
    # Step 3: Load connectathon bundles
    # -----------------------------------------------------------------------
    conn_probe_pid, conn_probe_eid = load_connectathon_bundles(CDR_URL, MEASURE_URL, BUNDLE_DIR)
    if not probe_patient_id:
        probe_patient_id = conn_probe_pid
        probe_encounter_id = conn_probe_eid

    # -----------------------------------------------------------------------
    # Step 4: Trigger $reindex
    # -----------------------------------------------------------------------
    if probe_patient_id and probe_encounter_id:
        log(f"Triggering $reindex (probe: Patient/{probe_patient_id})...")
        trigger_reindex(MEASURE_URL, probe_patient_id, probe_encounter_id)
        trigger_reindex(CDR_URL, probe_patient_id, probe_encounter_id)

    bundle_count = len(list(BUNDLE_DIR.glob("*.json"))) - 1 if MANIFEST_PATH.exists() else 0
    log("============================================")
    log("  Lenny seed data loaded successfully!")
    log("  CDR:     patient-bundle + connectathon clinical data + Groups")
    log(f"  Engine:  measure-bundle + {bundle_count} connectathon bundles")
    log("============================================")


if __name__ == "__main__":
    main()
