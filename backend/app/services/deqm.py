"""DEQM data-exchange payload builders.

Pure functions that assemble the DEQM Data Exchange MeasureReport and the two
$submit-data Parameters envelopes: the type-level `bundle` form Lenny targets
(1..* Bundles, one subject each) and the base-FHIR `measureReport`+`resource`
form it falls back to. No I/O here — HTTP delivery lives in
fhir_client.submit_data, orchestration in workflows.DeqmSubmitDataWorkflow.

The `bundle` form is the contract Lenny selected in #413; it is NOT the
published DEQM STU5 operation, whose own $deqm-submit-data was retired
upstream. Read the contract spec before changing either envelope.

Spec:     docs/superpowers/specs/2026-09-14-deqm-submit-data-contract-design.md
Original: docs/superpowers/specs/2026-08-21-deqm-submit-data-workflow-design.md
IG:       https://hl7.org/fhir/us/davinci-deqm/STU5/
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

DEQM_DATA_EXCHANGE_PROFILE = "http://hl7.org/fhir/us/davinci-deqm/StructureDefinition/datax-measurereport-deqm"
DEQM_UPDATE_TYPE_EXT = "http://hl7.org/fhir/us/davinci-deqm/StructureDefinition/extension-submitDataUpdateType"

# FHIR's `id` element is capped at 64 characters (spec: Resource.id).
_MAX_FHIR_ID_LENGTH = 64

# FHIR's `id` element is also restricted to this charset (spec: Resource.id).
# patient_id comes from a third-party CDR, so an underscore/colon/non-ASCII id
# would otherwise flow straight into the composed id below and yield an
# invalid MeasureReport id — a 400 for every patient with such an id.
_FHIR_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9\-.]+$")

# DEQM requires MeasureReport.reporter 1..1 (Organization). Lenny is the
# reporter; this fixed resource travels inside every submission so the
# reference resolves without the receiver chasing external references.
LENNY_REPORTER_ORG: dict[str, Any] = {
    "resourceType": "Organization",
    "id": "lenny-reporter",
    "name": "Lenny Measure Calculation Tool",
    "active": True,
}


def _measure_report_id(job_id: int, patient_id: str) -> str:
    """Build the `deqm-{job_id}-{patient_id}` id, falling back to a stable
    hash of patient_id when the composed id would overflow FHIR's 64-char
    `id` limit OR contain characters outside `[A-Za-z0-9\\-.]`.

    A long or illegally-charset patient_id can otherwise produce an invalid
    `id`; HAPI 400s the whole submission when it does. When that happens,
    keep the short `deqm-{job_id}-` prefix (useful for debugging) and replace
    patient_id with a stable hash of it, so the result is deterministic
    across calls for the same (job_id, patient_id) pair.
    """
    candidate = f"deqm-{job_id}-{patient_id}"
    if len(candidate) <= _MAX_FHIR_ID_LENGTH and _FHIR_ID_CHARSET_RE.match(candidate):
        return candidate
    prefix = f"deqm-{job_id}-"
    digest = hashlib.sha256(patient_id.encode("utf-8")).hexdigest()
    available = max(_MAX_FHIR_ID_LENGTH - len(prefix), 1)
    return f"{prefix}{digest[:available]}"[:_MAX_FHIR_ID_LENGTH]


def build_data_exchange_measure_report(
    *,
    job_id: int,
    patient_id: str,
    measure_canonical: str,
    period_start: str,
    period_end: str,
    resources: list[dict[str, Any]],
    timestamp: str,
) -> dict[str, Any]:
    """Build a DEQM Data Exchange MeasureReport for one patient's submission.

    `type` is `data-collection` — the R4 wire code; R5 renamed it to
    `data-exchange` but DEQM STU5 is R4-based. `submitDataUpdateType` is
    always `snapshot`: the job wipes the target's prior-run data first, and
    `incremental` would require stable ids + meta.source on every resource.
    `group` is intentionally absent — the profile prohibits measureScore and
    stratifier on data-exchange reports.
    """
    return {
        "resourceType": "MeasureReport",
        "id": _measure_report_id(job_id, patient_id),
        "meta": {"profile": [DEQM_DATA_EXCHANGE_PROFILE]},
        "extension": [{"url": DEQM_UPDATE_TYPE_EXT, "valueCode": "snapshot"}],
        "status": "complete",
        "type": "data-collection",
        "measure": measure_canonical,
        "subject": {"reference": f"Patient/{patient_id}"},
        "date": timestamp,
        "reporter": {"reference": f"Organization/{LENNY_REPORTER_ORG['id']}"},
        "period": {"start": period_start, "end": period_end},
        "evaluatedResource": [
            {"reference": f"{r['resourceType']}/{r['id']}"} for r in resources if r.get("resourceType") and r.get("id")
        ],
    }


def _canonical_json(resource: dict[str, Any]) -> str:
    """Stable serialisation for content comparison — key order is not meaning."""
    return json.dumps(resource, sort_keys=True, separators=(",", ":"), default=str)


def dedupe_by_identity(
    resources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """First-wins dedupe on `(resourceType, id)`.

    Returns the deduped list in first-seen order, and the identities whose
    later occurrences differed in content. Byte-identical duplicates are
    dropped silently — repetition alone is not worth an operator's attention;
    disagreement is.

    Deduplication is deliberately WITHIN one subject. Callers must not reuse
    one accumulator across subjects: a Practitioner shared by two subjects
    belongs in both their Bundles, and suppressing the second would leave a
    dangling reference in a Bundle the receiver may process on its own.

    Precondition: every resource carries `resourceType` and `id`. The caller
    filters first (see workflows.DeqmSubmitDataWorkflow.transfer_patient), and
    deriving the MeasureReport and the payload from that same filtered list is
    what keeps `evaluatedResource` aligned with the Bundle entries.
    """
    first_by_identity: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    conflicts: list[str] = []
    for resource in resources:
        identity = f"{resource['resourceType']}/{resource['id']}"
        if identity not in first_by_identity:
            first_by_identity[identity] = resource
            order.append(identity)
            continue
        if identity in conflicts:
            continue
        if _canonical_json(resource) != _canonical_json(first_by_identity[identity]):
            conflicts.append(identity)
    return [first_by_identity[identity] for identity in order], conflicts


@dataclass(frozen=True)
class SubjectBundle:
    """One subject's submission payload: its MeasureReport and its resources.

    `resources` must already be filtered and deduplicated — see
    `dedupe_by_identity`. Pairing them in one object is what stops a caller
    from accidentally deriving the MeasureReport from one list and the Bundle
    entries from another.
    """

    measure_report: dict[str, Any]
    resources: list[dict[str, Any]]


def build_stu5_parameters(subjects: list[SubjectBundle]) -> dict[str, Any]:
    """STU5 envelope: one `bundle` parameter per subject, 1..N.

    Each parameter carries a collection Bundle for exactly one subject, its
    MeasureReport first. Several subjects never share a Bundle: the receiver
    processes each Bundle as its own transaction, so one subject's bad resource
    does not roll back the others.

    HOW FAR THAT ISOLATION ACTUALLY GOES (measured 2026-09-15, #448/#449) -- it
    holds at the STORAGE layer only:
      - A resource that parses but fails on write (e.g. an illegal `id`) fails
        just its own Bundle. The other subjects stay committed.
      - A resource that fails to PARSE (e.g. an unknown enum code) rejects the
        whole request body before any transaction runs, so every subject in the
        submission is lost, including well-formed ones.
    Splitting subjects across Bundles therefore buys nothing against malformed
    content. What covers that case is the caller's isolation retry, not this
    function -- see workflows._apply_settlement's `isolate-stu5` branch.

    Note also that a partially-applied submission reports as a plain failure:
    the storage-layer case above answers 400 naming only the bad subject, with
    no indication that the others were committed. #449 has the evidence.

    With a single subject the output is byte-identical to the pre-#413 payload.
    """
    if not subjects:
        raise ValueError("build_stu5_parameters requires at least one subject (`bundle` is 1..*)")
    return {
        "resourceType": "Parameters",
        "parameter": [
            {
                "name": "bundle",
                "resource": {
                    "resourceType": "Bundle",
                    "type": "collection",
                    "entry": [{"resource": subject.measure_report}] + [{"resource": r} for r in subject.resources],
                },
            }
            for subject in subjects
        ],
    }


def build_base_parameters(measure_report: dict[str, Any], resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Base-FHIR $submit-data envelope (what HAPI clinical-reasoning accepts)."""
    return {
        "resourceType": "Parameters",
        "parameter": [{"name": "measureReport", "resource": measure_report}]
        + [{"name": "resource", "resource": r} for r in resources],
    }
