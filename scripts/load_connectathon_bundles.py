#!/usr/bin/env python3
"""Thin shim: load connectathon bundles via the shared seed module.

Used by the bake-hapi-image.yml Phase 2 step. All logic lives in
seed/load_seed_data.py; this script just wires env vars and triggers $reindex.

Environment variables (set by the bake workflow):
  CDR_FHIR_URL              (default http://hapi-fhir-cdr:8080/fhir)
  MEASURE_FHIR_URL          (default http://hapi-fhir-measure:8080/fhir)
  CONNECTATHON_BUNDLES_DIR  (default seed/connectathon-bundles)
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from seed.load_seed_data import load_connectathon_bundles, log, trigger_reindex

CDR_URL = os.environ.get("CDR_FHIR_URL", "http://hapi-fhir-cdr:8080/fhir")
MEASURE_URL = os.environ.get("MEASURE_FHIR_URL", "http://hapi-fhir-measure:8080/fhir")
BUNDLE_DIR = pathlib.Path(os.environ.get("CONNECTATHON_BUNDLES_DIR", "seed/connectathon-bundles"))

if __name__ == "__main__":
    probe_patient_id, probe_encounter_id = load_connectathon_bundles(
        CDR_URL, MEASURE_URL, BUNDLE_DIR
    )
    if probe_patient_id and probe_encounter_id:
        log(f"Triggering $reindex (probe: Patient/{probe_patient_id})...")
        trigger_reindex(MEASURE_URL, probe_patient_id, probe_encounter_id)
        trigger_reindex(CDR_URL, probe_patient_id, probe_encounter_id)
    log("=== All connectathon bundles loaded successfully! ===")
