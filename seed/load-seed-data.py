#!/usr/bin/env python3
"""MCT2 seed data loader.

Loads all seed data into HAPI FHIR in the correct order:
  1. patient-bundle.json  → CDR + measure engine (demo patients for CMS122 1.0.000)
  2. measure-bundle.json  → measure engine (CMS122 1.0.000 Measure + Libraries + ValueSets)
  3. connectathon-bundles → measure engine (measure defs) + CDR + measure engine (clinical)

Idempotent: uses PUT-based batch bundles — safe to re-run.

Applies the same patches as the integration test suite so HAPI can evaluate correctly:
  - ValueSet compose synthesised from expansion codes (HAPI re-expands via compose)
  - Library dependency URLs rewritten ecqi.healthit.gov → madie.cms.gov
  - ValueSet ID conflicts remapped to existing HAPI resources (prevents HAPI-0902)
  - Duplicate Claim IDs deduplicated (CMS71 MADiE v0.3.x export bug)

Environment variables:
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
from collections import Counter
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


def fix_duplicate_claims(resources: list[dict]) -> list[dict]:
    """Assign unique IDs to Claim resources that share a duplicate ID (CMS71)."""
    id_counts: Counter = Counter(
        r.get("id", "") for r in resources if r.get("resourceType") == "Claim"
    )
    duplicates = {id_ for id_, n in id_counts.items() if n > 1}
    if not duplicates:
        return resources
    result = []
    seen: dict[str, int] = {}
    for r in resources:
        if r.get("resourceType") != "Claim" or r.get("id") not in duplicates:
            result.append(r)
            continue
        r = copy.deepcopy(r)
        original_id = r["id"]
        enc_ref = ""
        for item in r.get("item", []):
            for enc in item.get("encounter", []):
                ref = enc.get("reference", "")
                if ref:
                    enc_ref = ref.split("/")[-1][:16]
                    break
            if enc_ref:
                break
        r["id"] = f"claim-{enc_ref}" if enc_ref else f"{original_id}-{seen.setdefault(original_id, 0) + 1}"
        seen[original_id] = seen.get(original_id, 0) + 1
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
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log("Starting MCT2 seed data loader...")
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
    if not MANIFEST_PATH.exists():
        log(f"No manifest found at {MANIFEST_PATH} — skipping connectathon bundles")
    else:
        with open(MANIFEST_PATH) as f:
            manifest = json.load(f)
        measures = manifest["measures"]
        # CMS1017 last — scoring type causes HAPI-0902 on some HAPI versions
        ordered = [m for m in measures if "CMS1017" not in m["id"]] + [
            m for m in measures if "CMS1017" in m["id"]
        ]
        log(f"Found {len(ordered)} connectathon bundles to load")

        # Pass 1: collect + deduplicate measure defs
        all_measure_defs: dict[str, dict] = {}
        clinical_per_bundle: list[tuple[str, list[dict]]] = []

        for entry in ordered:
            measure_id = entry["id"]
            bundle_path = BUNDLE_DIR / entry["bundle_file"]
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
                clinical_per_bundle.append((measure_id, clinical))

        deduped = list(all_measure_defs.values())
        log(f"Total unique measure def resources: {len(deduped)}")

        # Resolve ValueSet ID conflicts before loading
        log("Resolving ValueSet ID conflicts with existing HAPI resources...")
        deduped = resolve_valueset_id_conflicts(deduped, MEASURE_URL)

        # Pass 2: load measure defs
        log("Loading measure definitions to measure engine...")
        tx = make_put_bundle(deduped)
        post_bundle(MEASURE_URL, tx, "connectathon measure defs → measure engine", timeout=300)

        # Pass 3: load clinical data
        for measure_id, clinical in clinical_per_bundle:
            log(f"Loading clinical data: {measure_id} ({len(clinical)} resources)...")
            clinical = fix_duplicate_claims(clinical)
            tx = make_put_bundle(clinical)
            post_bundle(CDR_URL, tx, f"{measure_id} clinical → CDR")
            post_bundle(MEASURE_URL, tx, f"{measure_id} clinical → measure engine")
            if not probe_patient_id:
                enc_resources = [r for r in clinical if r.get("resourceType") == "Encounter"]
                if enc_resources:
                    probe_encounter_id = enc_resources[0].get("id")
                    probe_patient_id = (
                        enc_resources[0].get("subject", {}).get("reference", "").removeprefix("Patient/")
                    )

    # -----------------------------------------------------------------------
    # Step 4: Trigger $reindex
    # -----------------------------------------------------------------------
    if probe_patient_id and probe_encounter_id:
        log(f"Triggering $reindex (probe: Patient/{probe_patient_id})...")
        trigger_reindex(MEASURE_URL, probe_patient_id, probe_encounter_id)
        trigger_reindex(CDR_URL, probe_patient_id, probe_encounter_id)

    log("============================================")
    log("  MCT2 seed data loaded successfully!")
    log(f"  CDR:     patient-bundle + connectathon clinical data")
    log(f"  Engine:  measure-bundle + {len(list(BUNDLE_DIR.glob('*.json'))) - 1 if MANIFEST_PATH.exists() else 0} connectathon bundles")
    log("============================================")


if __name__ == "__main__":
    main()
