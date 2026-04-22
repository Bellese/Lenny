#!/usr/bin/env python3
"""Load all connectathon bundles to HAPI FHIR (measure engine + CDR).

Designed to run inside the backend Docker container, which has httpx installed
and the connectathon bundles mounted at /seed/connectathon-bundles.

Usage (from host):
    docker cp scripts/load_connectathon_bundles.py leonard-backend-1:/tmp/
    docker exec leonard-backend-1 python3 /tmp/load_connectathon_bundles.py

Applies the same patches as the integration test suite:
  - fix_valueset_compose: synthesise compose from expansion codes so HAPI can expand VS
  - fix_library_deps: rewrite ecqi.healthit.gov → madie.cms.gov Library URLs
  - fix_duplicate_claims: assign unique IDs to Claim resources (CMS71 workaround)
  - ValueSet ID conflict resolution: remap VS IDs to match existing HAPI resources

Loading strategy:
  - Measure defs (Measure, Library, ValueSet, CodeSystem) → measure engine only
  - Clinical data (Patient, Condition, Encounter, …) → BOTH CDR and measure engine
  - CMS1017 loaded last (scoring type causes HAPI-0902 for this bundle)
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import time
from collections import Counter
from typing import Any

import httpx

MEASURE_URL = "http://hapi-fhir-measure:8080/fhir"
CDR_URL = "http://hapi-fhir-cdr:8080/fhir"
BUNDLE_DIR = pathlib.Path("/seed/connectathon-bundles")
MANIFEST_PATH = BUNDLE_DIR / "manifest.json"

_MEASURE_DEF_TYPES = {"Measure", "Library", "ValueSet", "CodeSystem"}

HEADERS = {"Content-Type": "application/fhir+json"}


# ---------------------------------------------------------------------------
# Bundle classification
# ---------------------------------------------------------------------------

def classify_bundle(bundle: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Split bundle entries into (measure_def_resources, clinical_resources).

    MeasureReport resources are dropped (they are expected test case outputs,
    not data to load into HAPI).
    """
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


# ---------------------------------------------------------------------------
# Patches (mirrored from backend/tests/integration/_helpers.py)
# ---------------------------------------------------------------------------

def fix_valueset_compose(resources: list[dict]) -> list[dict]:
    """Synthesise compose from expansion codes for ValueSets HAPI can't re-expand.

    HAPI ignores pre-computed expansion elements and always re-expands via compose.
    ValueSets with only expansion (no compose) or with sub-ValueSet compose refs
    produce empty expansions. This fix derives a concrete compose from the expansion.
    """
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
    """Rewrite ecqi.healthit.gov Library dependency URLs to madie.cms.gov.

    MADiE bundles reference libraries via ecqi prefix but load them via madie prefix.
    HAPI resolves dependencies by canonical URL, so the mismatch silently breaks
    the library chain and produces IP=0.
    """
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

        if enc_ref:
            r["id"] = f"claim-{enc_ref}"
        else:
            seen[original_id] = seen.get(original_id, 0) + 1
            r["id"] = f"{original_id}-{seen[original_id]}"
        result.append(r)
    return result


# ---------------------------------------------------------------------------
# HAPI helpers
# ---------------------------------------------------------------------------

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
    """Remap ValueSet IDs to match existing HAPI resources (same url, different id).

    Without this, a PUT with a different ID creates a second resource with the same
    canonical URL, causing HAPI-0902 silent batch failures on subsequent loads.
    """
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
                        print(f"  [VS remap] {r['id']} → {hapi_id} (url=...{r['url'][-40:]})")
                        r["id"] = hapi_id
        except httpx.RequestError:
            pass
    return resources


def post_bundle_to(url: str, bundle: dict, label: str, timeout: int = 300) -> bool:
    resp = httpx.post(url, json=bundle, headers=HEADERS, timeout=timeout)
    if resp.status_code == 422 and "HAPI-0902" in resp.text:
        print(f"  {label}: already loaded (HAPI-0902)")
    elif resp.status_code >= 400:
        print(f"  ERROR {label}: HTTP {resp.status_code}")
        print(f"    {resp.text[:500]}")
        return False
    else:
        print(f"  {label}: HTTP {resp.status_code} OK")
    return True


def trigger_reindex(base_url: str, patient_id: str, encounter_id: str, timeout: int = 180) -> None:
    """POST $reindex then poll until Encounter?patient= returns results."""
    params = {
        "resourceType": "Parameters",
        "parameter": [{"name": "type", "valueString": "Encounter"}],
    }
    try:
        httpx.post(f"{base_url}/$reindex", json=params, headers=HEADERS, timeout=30)
        print(f"  $reindex triggered on {base_url}")
    except Exception as exc:
        print(f"  WARNING: $reindex request failed: {exc}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"{base_url}/Encounter",
                params={"patient": patient_id, "_count": "1"},
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("entry"):
                print(f"  $reindex complete on {base_url}")
                return
        except Exception:
            pass
        time.sleep(5)
    print(f"  WARNING: $reindex did not complete within {timeout}s on {base_url}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not MANIFEST_PATH.exists():
        print(f"ERROR: manifest not found at {MANIFEST_PATH}")
        sys.exit(1)

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    measures = manifest["measures"]
    # Put CMS1017 last — scoring type causes HAPI-0902 / HTTP 400 on some HAPI versions
    ordered = [m for m in measures if "CMS1017" not in m["id"]] + [
        m for m in measures if "CMS1017" in m["id"]
    ]

    print(f"Found {len(ordered)} bundles to load")
    print(f"Measure engine: {MEASURE_URL}")
    print(f"CDR:            {CDR_URL}")

    # Pass 1: parse + patch all bundles, deduplicate measure defs
    all_measure_defs: dict[str, dict] = {}
    clinical_per_bundle: list[tuple[str, list[dict]]] = []

    for entry in ordered:
        measure_id = entry["id"]
        bundle_path = BUNDLE_DIR / entry["bundle_file"]
        if not bundle_path.exists():
            print(f"\nSKIP: {measure_id} — bundle file not found at {bundle_path}")
            continue

        print(f"\n[{measure_id}] Parsing bundle ({bundle_path.name})...")
        with open(bundle_path) as f:
            bundle = json.load(f)

        measure_defs, clinical = classify_bundle(bundle)
        measure_defs = fix_valueset_compose(measure_defs)
        measure_defs = fix_library_deps(measure_defs)

        print(f"  {len(measure_defs)} measure def resources, {len(clinical)} clinical resources")

        for r in measure_defs:
            key = f"{r.get('resourceType')}/{r.get('id')}"
            all_measure_defs[key] = r

        if clinical:
            clinical_per_bundle.append((measure_id, clinical))

    deduped = list(all_measure_defs.values())
    print(f"\nTotal unique measure def resources (deduplicated): {len(deduped)}")

    # Pass 2: resolve ValueSet ID conflicts with existing HAPI state
    print("\nResolving ValueSet ID conflicts with existing HAPI resources...")
    deduped = resolve_valueset_id_conflicts(deduped, MEASURE_URL)

    # Pass 3: load measure defs to measure engine
    print("\nLoading measure definitions to measure engine (single deduplicated batch)...")
    tx = make_put_bundle(deduped)
    ok = post_bundle_to(MEASURE_URL, tx, "measure defs → measure engine")
    if not ok:
        print("WARNING: measure def load reported errors — continuing with clinical data")

    # Pass 4: load clinical data to both CDR and measure engine
    probe_patient_id: str | None = None
    probe_encounter_id: str | None = None

    for measure_id, clinical in clinical_per_bundle:
        print(f"\n[{measure_id}] Loading {len(clinical)} clinical resources...")
        clinical = fix_duplicate_claims(clinical)
        tx = make_put_bundle(clinical)

        post_bundle_to(CDR_URL, tx, f"{measure_id} clinical → CDR")
        post_bundle_to(MEASURE_URL, tx, f"{measure_id} clinical → measure engine")

        if not probe_patient_id:
            enc_resources = [r for r in clinical if r.get("resourceType") == "Encounter"]
            if enc_resources:
                first_enc = enc_resources[0]
                probe_encounter_id = first_enc.get("id")
                probe_patient_id = (
                    first_enc.get("subject", {}).get("reference", "").removeprefix("Patient/")
                )

    # Pass 5: trigger $reindex on both servers
    if probe_patient_id and probe_encounter_id:
        print(f"\nTriggering $reindex (probe: Patient/{probe_patient_id}, Encounter/{probe_encounter_id})...")
        trigger_reindex(MEASURE_URL, probe_patient_id, probe_encounter_id)
        trigger_reindex(CDR_URL, probe_patient_id, probe_encounter_id)
    else:
        print("\nNo probe patient found — skipping $reindex")

    print("\n=== All connectathon bundles loaded successfully! ===")
    print("Next: queue new jobs to verify CQL evaluation with the loaded data.")


if __name__ == "__main__":
    main()
