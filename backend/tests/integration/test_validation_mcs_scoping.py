"""Integration tests for validation-pipeline MCS scoping (issue #397 slice 3).

The unit tests assert which URLs the pipeline builds. Only a real FHIR server shows
that a validation run's scoped wipe actually spares another participant's patients —
the property the whole slice exists for.

Only ever wipes patients prefixed `lenny-397-`, so it cannot disturb the
session-scoped seed data other integration tests rely on.
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
    """The acceptance property for slice 3's wipe, against a real server."""
    await _seed(measure_url)
    assert await _exists(measure_url, "Patient", _RUN_PATIENT), "seed failed"
    assert await _exists(measure_url, "Patient", _BYSTANDER), "seed failed"

    try:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_RUN_PATIENT])

        assert not await _exists(measure_url, "Patient", _RUN_PATIENT)
        assert not await _exists(measure_url, "Condition", f"{_RUN_PATIENT}-cond")
        assert await _exists(measure_url, "Patient", _BYSTANDER), (
            "a validation run deleted a patient it was not evaluating — the #392 hazard on the validation path"
        )
        assert await _exists(measure_url, "Condition", f"{_BYSTANDER}-cond")
    finally:
        await wipe_patients_by_id(base_url=measure_url, patient_ids=[_RUN_PATIENT, _BYSTANDER])
