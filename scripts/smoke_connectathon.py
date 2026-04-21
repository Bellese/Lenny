#!/usr/bin/env python3
"""Quick smoke test for connectathon ValueSet expansion + $evaluate-measure.

Validates the deduplication + expansion-wait fix in ~5-10 minutes against
already-running test containers, instead of the full 25-minute test run.

Usage:
    docker compose -f docker-compose.test.yml up -d
    python scripts/smoke_connectathon.py

Tests CMS122 (Diabetes) with a known IP=1 patient. Passes if IP >= 1.
"""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

import httpx

from app.services.validation import _classify_bundle_entries
from tests.integration._helpers import (
    fix_library_deps_for_hapi,
    fix_valueset_compose_for_hapi,
    make_put_bundle,
)

MCS = "http://localhost:8181/fhir"
CDR = "http://localhost:8180/fhir"
HEADERS = {"Content-Type": "application/fhir+json"}

# Three bundles that all contain the 1797-code ValueSet — tests dedup fix
BUNDLE_NAMES = [
    "CMS122FHIRDiabetesAssessGreaterThan9Percent-bundle.json",
    "CMS125FHIRBreastCancerScreening-bundle.json",
    "CMS130FHIRColorectalCancerScreening-bundle.json",
]
BUNDLE_DIR = pathlib.Path(__file__).resolve().parents[1] / "seed" / "connectathon-bundles"

# Patient expected to have IP=1 in CMS122
PROBE_PATIENT = "9cba6cfa-9671-4850-803d-e286c7d59ee7"
PROBE_MEASURE_URL = "https://madie.cms.gov/Measure/CMS122FHIRDiabetesAssessGreaterThan9Percent"

LARGE_VS_THRESHOLD = 900
EXPANSION_TIMEOUT = 600
EXPANSION_POLL = 10


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_infra() -> bool:
    for url in (f"{CDR}/metadata", f"{MCS}/metadata"):
        try:
            r = httpx.get(url, timeout=10)
            r.raise_for_status()
        except Exception as e:
            print(f"ERROR: {url} unreachable: {e}")
            print("Start containers: docker compose -f docker-compose.test.yml up -d")
            return False
    return True


def load_bundles() -> tuple:
    """Load 3 bundles with deduplication.

    Returns (large_vs_ids, probe_patient_id, probe_encounter_id).
    probe_patient_id/encounter_id come from the bundle data directly (not HAPI
    search) so they are always available even before reindex settles.
    """
    all_measure_defs: dict[str, dict] = {}
    all_clinical: list[dict] = []
    probe_patient_id = None
    probe_encounter_id = None

    for name in BUNDLE_NAMES:
        path = BUNDLE_DIR / name
        with open(path) as f:
            bundle = json.load(f)
        measure_defs, clinical, _ = _classify_bundle_entries(bundle)
        measure_defs = fix_valueset_compose_for_hapi(measure_defs)
        measure_defs = fix_library_deps_for_hapi(measure_defs)
        for r in measure_defs:
            all_measure_defs[f"{r.get('resourceType')}/{r.get('id')}"] = r
        all_clinical.extend(clinical)
        # Capture probe patient+encounter from bundle data (not HAPI search)
        if not probe_patient_id:
            for r in clinical:
                if r.get("resourceType") == "Encounter" and r.get("id"):
                    probe_encounter_id = r["id"]
                    probe_patient_id = r.get("subject", {}).get("reference", "").removeprefix("Patient/")
                    break

    deduped = list(all_measure_defs.values())

    # Resolve ValueSet ID conflicts: seed data may have a VS under OID-YYYYMMDD
    # while connectathon bundles use bare OID. Rewrite IDs so PUT updates existing.
    for resource in deduped:
        if resource.get("resourceType") != "ValueSet" or not resource.get("url"):
            continue
        try:
            resp = httpx.get(f"{MCS}/ValueSet?url={resource['url']}&_count=1", timeout=10)
            if resp.status_code == 200:
                entries = resp.json().get("entry", [])
                if entries:
                    hapi_id = entries[0]["resource"]["id"]
                    if hapi_id != resource["id"]:
                        log(f"  VS remap: {resource['id']} → {hapi_id}")
                        resource["id"] = hapi_id
        except Exception:
            pass

    log(f"Loading {len(deduped)} deduplicated measure defs from {len(BUNDLE_NAMES)} bundles...")
    tx = make_put_bundle(deduped)
    r = httpx.post(MCS, json=tx, headers=HEADERS, timeout=300)
    r.raise_for_status()
    log(f"  Measure defs loaded: {r.status_code}")

    log(f"Loading {len(all_clinical)} clinical resources...")
    tx = make_put_bundle(all_clinical)
    for url, label in [(CDR, "CDR"), (MCS, "MCS")]:
        r = httpx.post(url, json=tx, headers=HEADERS, timeout=120)
        r.raise_for_status()
        log(f"  Clinical → {label}: {r.status_code}")

    large_ids = [
        r["id"]
        for r in deduped
        if r.get("resourceType") == "ValueSet"
        and r.get("id")
        and sum(len(inc.get("concept", [])) for inc in r.get("compose", {}).get("include", [])) > LARGE_VS_THRESHOLD
    ]
    log(f"Large ValueSets (>{LARGE_VS_THRESHOLD} codes): {large_ids}")
    return large_ids, probe_patient_id, probe_encounter_id


def wait_for_expansion(vs_ids: list[str]) -> bool:
    if not vs_ids:
        return True
    log(f"Waiting for HAPI background pre-expansion of {len(vs_ids)} large ValueSet(s)...")
    pending = set(vs_ids)
    deadline = time.monotonic() + EXPANSION_TIMEOUT
    while pending and time.monotonic() < deadline:
        done = set()
        for vs_id in list(pending):
            # count=1 short-circuits without full expansion; count=2 fails with
            # HAPI-0831 until background pre-expansion stores codes in the DB.
            r = httpx.get(f"{MCS}/ValueSet/{vs_id}/$expand?count=2", timeout=15)
            if r.status_code == 200:
                done.add(vs_id)
                log(f"  {vs_id}: pre-expanded OK")
        pending -= done
        if pending:
            elapsed = EXPANSION_TIMEOUT - (deadline - time.monotonic())
            log(f"  Still waiting for {len(pending)} VS ({elapsed:.0f}s elapsed)...")
            time.sleep(EXPANSION_POLL)
    if pending:
        log(f"TIMEOUT: {pending} not pre-expanded within {EXPANSION_TIMEOUT}s")
        return False
    return True


def reindex_and_wait(patient_id: str, encounter_id: str) -> None:
    for target in (CDR, MCS):
        params = {"resourceType": "Parameters"}  # full reindex, not just Encounter
        httpx.post(f"{target}/$reindex", json=params, headers=HEADERS, timeout=30)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            r = httpx.get(f"{target}/Encounter?patient={patient_id}&_count=1", timeout=10)
            if r.status_code == 200 and r.json().get("entry"):
                log(f"  Reindex complete on {target}")
                break
            time.sleep(5)


def evaluate(measure_hapi_id: str, patient: str) -> dict:
    url = (
        f"{MCS}/Measure/{measure_hapi_id}/$evaluate-measure"
        f"?subject=Patient/{patient}&periodStart=2026-01-01&periodEnd=2026-12-31"
    )
    r = httpx.get(url, timeout=60)
    r.raise_for_status()
    report = r.json()
    pops = {}
    for grp in report.get("group", []):
        for pop in grp.get("population", []):
            code = pop.get("code", {}).get("coding", [{}])[0].get("code", "?")
            pops[code] = pop.get("count", 0)
    return pops


def main() -> int:
    print("=" * 60)
    print("Connectathon smoke test — CMS122 Diabetes, 1 patient")
    print("=" * 60)

    if not check_infra():
        return 1

    large_ids, probe_patient, probe_encounter = load_bundles()

    if not wait_for_expansion(large_ids):
        print("\nFAIL: ValueSet expansion timed out")
        return 1

    # Always reindex: use probe IDs from bundle data, not from HAPI search.
    # HAPI's DEQM SearchParameter registration (~40s after startup) triggers an
    # async reindex that can leave encounters unindexed by reference search params.
    if probe_patient and probe_encounter:
        log(f"Reindexing with probe patient={probe_patient} encounter={probe_encounter}...")
        reindex_and_wait(probe_patient, probe_encounter)
    else:
        log("WARNING: no probe encounter found — skipping reindex")

    # Resolve measure HAPI ID
    r = httpx.get(f"{MCS}/Measure?url={PROBE_MEASURE_URL}&_count=1", timeout=15)
    entries = r.json().get("entry", [])
    if not entries:
        print("FAIL: CMS122 measure not found in HAPI")
        return 1
    measure_id = entries[0]["resource"]["id"]
    log(f"Evaluating CMS122 (HAPI ID: {measure_id}) for patient {PROBE_PATIENT}...")

    pops = evaluate(measure_id, PROBE_PATIENT)
    ip = pops.get("initial-population", 0)

    print()
    print("Population results:")
    for k, v in pops.items():
        print(f"  {k}: {v}")
    print()

    if ip >= 1:
        print(f"PASS: IP={ip} (expected >= 1)")
        return 0
    else:
        print(f"FAIL: IP={ip} (expected >= 1) — ValueSet expansion may not have completed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
