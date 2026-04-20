#!/usr/bin/env python3
"""Inventory script: audit resource types across all connectathon bundles.

Outputs a JSON report with:
  - bundle_resource_types: {resourceType: count} across all 12 bundles
  - missing_from_measure_def: types present in bundles but not in _MEASURE_DEF_TYPES
  - missing_from_wipe: types present in bundles but not in wipe_patient_data list
  - in_skip_types: types present in bundles that _SKIP_TYPES would filter out
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLES_DIR = REPO_ROOT / "seed" / "connectathon-bundles"

# Current sets from validation.py and fhir_client.py — keep in sync manually.
MEASURE_DEF_TYPES: set[str] = {"Measure", "Library", "ValueSet", "CodeSystem"}

# From fhir_client.py wipe_patient_data()
WIPE_TYPES: set[str] = {
    "MeasureReport",
    "Patient",
    "Condition",
    "Observation",
    "Encounter",
    "Procedure",
    "MedicationRequest",
    "Immunization",
    "DiagnosticReport",
    "AllergyIntolerance",
    "CarePlan",
    "CareTeam",
    "Goal",
    "ServiceRequest",
    "Coverage",
    "Claim",
    "DeviceRequest",
    "MedicationAdministration",
    "AdverseEvent",
    "Location",
    "Practitioner",
    "Organization",
}

# From fhir_client.py gather_patient_data() _SKIP_TYPES
SKIP_TYPES: set[str] = {"Group", "MeasureReport"}

# Types that are test case containers — always expected and not clinical data.
TEST_CASE_TYPES: set[str] = {"MeasureReport"}


def main() -> None:
    bundle_resource_types: dict[str, int] = {}

    bundle_files = sorted(BUNDLES_DIR.glob("*.json"))
    if not bundle_files:
        print(f"ERROR: No JSON files found in {BUNDLES_DIR}", file=sys.stderr)
        sys.exit(1)

    processed = 0
    for bundle_path in bundle_files:
        # Skip manifest.json — not a FHIR bundle
        if bundle_path.name == "manifest.json":
            continue
        try:
            data = json.loads(bundle_path.read_bytes())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: Could not parse {bundle_path.name}: {exc}", file=sys.stderr)
            continue

        for entry in data.get("entry", []):
            resource = entry.get("resource")
            if not resource or "resourceType" not in resource:
                continue
            rt = resource["resourceType"]
            bundle_resource_types[rt] = bundle_resource_types.get(rt, 0) + 1

        processed += 1

    print(f"Processed {processed} bundles from {BUNDLES_DIR}", file=sys.stderr)

    all_types = set(bundle_resource_types.keys())

    # Clinical candidates: not measure defs, not test-case containers
    clinical_types = all_types - MEASURE_DEF_TYPES - TEST_CASE_TYPES

    missing_from_measure_def = sorted(all_types - MEASURE_DEF_TYPES - SKIP_TYPES)
    missing_from_wipe = sorted(clinical_types - WIPE_TYPES - SKIP_TYPES)
    in_skip_types = sorted(all_types & SKIP_TYPES)

    report = {
        "bundle_resource_types": dict(sorted(bundle_resource_types.items())),
        "missing_from_measure_def": missing_from_measure_def,
        "missing_from_wipe": missing_from_wipe,
        "in_skip_types": in_skip_types,
        "totals": {
            "unique_types": len(all_types),
            "measure_def_types_found": sorted(all_types & MEASURE_DEF_TYPES),
            "skip_types_found": in_skip_types,
            "clinical_types_found": sorted(clinical_types),
        },
    }

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
