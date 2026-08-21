"""Integration coverage for the scoped wipe the validation pipeline now calls (#397 slice 3).

What this file actually covers: that a real FHIR server honours `DELETE {Type}?patient=<ids>`
as a genuinely scoped delete, exercised through the same `wipe_patients_by_id` the validation
path now calls instead of the unfiltered delete-every-patient sweep. Patient ids all carry the
`lenny-397-` prefix, so the test cannot disturb the session-scoped seed data other integration
tests rely on. The prefix is also the marginal coverage this file adds over its sibling.

What it does NOT cover: it never drives `run_validation`, `triage_test_bundle`, or any other
code path this branch changed. It calls `wipe_patients_by_id` directly. So it demonstrates a
property of the sweep, not a property of a validation run.

`tests/integration/test_scoped_wipe.py:104` asserts the same property on the same function
with a superset of assertions (it also covers `Encounter` and the `subject=`-scoped
`AdverseEvent`). That overlap is deliberate, not an oversight: this file keeps the
validation-path framing and the `lenny-397-` prefix alongside the job-path original.

Known gap — the spec's Testing → Integration section lists two items this branch does not
deliver, and they are follow-up work:
  1. "a validation run against a second MCS leaves a bystander patient intact"
  2. "a validation run's results are attributable to the snapshotted MCS"
Both need CDR-gather stubbing inside an integration context and are task-sized on their own.
"""

import httpx
import pytest

from app.services.fhir_client import wipe_patients_by_id

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_RUN_PATIENT = "lenny-397-run"
_BYSTANDER = "lenny-397-bystander"


def _patient(pid: str) -> dict:
    return {"resourceType": "Patient", "id": pid, "name": [{"family": pid}]}


def _condition(pid: str) -> dict:
    return {
        "resourceType": "Condition",
        "id": f"{pid}-cond",
        "subject": {"reference": f"Patient/{pid}"},
        "clinicalStatus": {"coding": [{"code": "active"}]},
    }


async def _seed(measure_url: str) -> None:
    entries = [
        {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}
        for r in (_patient(_RUN_PATIENT), _patient(_BYSTANDER), _condition(_RUN_PATIENT), _condition(_BYSTANDER))
    ]
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            measure_url,
            json={"resourceType": "Bundle", "type": "batch", "entry": entries},
            headers={"Content-Type": "application/fhir+json"},
        )
        resp.raise_for_status()


async def _exists(measure_url: str, rtype: str, rid: str) -> bool:
    """Direct read, not a search: per CLAUDE.md, a search can return a stale
    snapshot while a direct GET works regardless of index state."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(f"{measure_url}/{rtype}/{rid}")
    return resp.status_code == 200


async def test_validation_scoped_wipe_spares_other_patients(measure_url):
    """`wipe_patients_by_id` — the sweep validation now calls — spares a bystander.

    This exercises the sweep directly; it does not run a validation run. See the module
    docstring for what that does and does not establish.
    """
    await _seed(measure_url)
    assert await _exists(measure_url, "Patient", _RUN_PATIENT), "seed failed"
    assert await _exists(measure_url, "Patient", _BYSTANDER), "seed failed"

    try:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_RUN_PATIENT])

        assert not await _exists(measure_url, "Patient", _RUN_PATIENT)
        assert not await _exists(measure_url, "Condition", f"{_RUN_PATIENT}-cond")
        assert await _exists(measure_url, "Patient", _BYSTANDER), (
            "the scoped sweep deleted a patient it was not given — the #392 hazard, "
            "on the sweep the validation path now calls"
        )
        assert await _exists(measure_url, "Condition", f"{_BYSTANDER}-cond")
    finally:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_RUN_PATIENT, _BYSTANDER])
