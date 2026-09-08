"""HAPI search-index readiness gates for integration setup (#425).

Why this lives outside conftest
-------------------------------
Same reason `_setup_budget.py` does: the logic that decides whether the suite may
proceed against a given HAPI is worth testing without standing up Docker. conftest
is imported by pytest as a plugin and cannot be imported cleanly from a unit test.
"""

from __future__ import annotations

import time

import httpx

from tests.integration._setup_budget import SetupBudget, record_gate

REINDEX_POLL_INTERVAL = 1  # seconds between probe checks
REINDEX_TIMEOUT = 300  # per-gate cap; the shared budget is the real bound

# The types CMS122/124/125 need present before any population can be trusted.
CDR_PROBE_TYPES = ("Encounter", "Observation", "Condition")


def wait_for_cdr_reference_index(cdr_url: str, probe_patient_id: str, budget: SetupBudget) -> None:
    """Verify CDR's patient-reference search works. Does NOT trigger a $reindex (#425).

    Why verifying beats rebuilding on the prebaked path
    ---------------------------------------------------
    This gate replaces a full CDR $reindex that cost ~292s in CI on every run. That
    rebuild was justified by the claim that "CDR has no persistent Lucene", which is
    true of the image but irrelevant to what the gate protects:

    * The baked images really do ship an *empty* Lucene index -- 4 files, a 69-byte
      `segments_1` commit point and a 0-byte `write.lock` per index, against 43MB of
      baked H2. (Probable cause: every `docker stop` in bake-hapi-image.yml is bare,
      so the JVM gets Docker's default 10s SIGTERM window. H2's MVStore is
      crash-durable and survives; Hibernate Search never commits and does not.)
    * But HAPI does not serve `Encounter?patient=` from Lucene. Reference, token and
      date params live in the relational HFJ_RES_LINK / HFJ_SPIDX_* tables, inside the
      H2 store that *is* baked. Lucene covers full-text and terminology only -- see
      docs/architecture.md's Hibernate Search row.

    Measured on the prebaked CDR image, standalone: patient-reference searches are
    correct the moment /fhir/metadata answers and stay correct (319 Patients,
    per-patient Encounter/Observation/Condition counts varying correctly, $everything
    summing exactly), and a full $reindex run to genuine completion afterwards changed
    *nothing* -- 0 differences across 6 resource-type totals, 10 patients x 4 types,
    and $everything.

    So the readiness property is still asserted here, at ~0s, instead of being rebuilt
    for ~292s. A never-ready index still fails as a named fixture error, not a job kill.
    """
    for resource_type in CDR_PROBE_TYPES:
        gate = f"cdr-{resource_type.lower()}-gate"
        allotted = budget.allot(gate, cap=REINDEX_TIMEOUT)
        started = time.monotonic()
        deadline = started + allotted
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{cdr_url}/{resource_type}?patient={probe_patient_id}&_count=1", timeout=10)
                if resp.status_code == 200 and resp.json().get("entry"):
                    break
            except Exception:
                pass
            time.sleep(REINDEX_POLL_INTERVAL)
        else:
            # Raise, do not warn (#425). An unusable CDR reference-param index makes
            # $everything return partial data, which surfaces as wrong populations
            # rather than an error -- the exact misdiagnosis CLAUDE.md's async-indexing
            # section exists to prevent.
            raise RuntimeError(
                f"CDR at {cdr_url} {resource_type} reference-param search was not ready within its "
                f"{allotted:.0f}s allotment (gate: {gate}, probe: "
                f"{resource_type}?patient={probe_patient_id}); {budget.spent():.0f}s of the "
                f"{budget.total:.0f}s setup budget spent. The baked image should serve this from "
                "H2 immediately; if this fires, the image is wrong rather than merely slow. "
                "INTEGRATION_FORCE_CDR_REINDEX=1 restores the old full-rebuild behaviour."
            )
        record_gate(gate, time.monotonic() - started)
